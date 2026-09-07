#!/usr/bin/env python3
"""Apply an mmw bundle to a mirror root.

Shipped inside every bundle and written beside it (`.mmw/apply.py` in the
tar, and a plain copy next to the bundle on our side so a first transfer has
something to run at all). This is the ONLY piece of mmw that runs on the far
side of the airgap: no mmw install, often no pip, sometimes Python 3.6
(RHEL 8). Every other component in this feature fails loudly on our side of
the wire; this one fails on someone else's, in a facility no one here can
reach, so every check below favours a clear stderr message and a non-zero
exit over a guess.

    python3 apply.py lab-20260906T143000Z.tar.zst --root /srv/www

Requires the `zstd` binary on PATH. Nothing else — see
test_apply_script_imports_only_stdlib for the enforced import allowlist.
"""
import argparse
import hashlib
import json
import os
import posixpath
import shutil
import subprocess
import sys
import tarfile

STATE_FILE = ".mmw-state"
MANIFEST_MEMBER = ".mmw/manifest.jsonl.zst"
DELETE_MEMBER = ".mmw/delete.list"
ROOT_PLACEHOLDER = "__MMW_ROOT__"

# Exit codes. Not load-bearing for any caller today, but a script that always
# exits 1 makes "what went wrong" undiscoverable from a cron/Ansible log that
# only kept the exit status.
EXIT_UNSAFE = 2
EXIT_OUT_OF_ORDER = 3
EXIT_VERIFY_FAILED = 4
EXIT_TOOLING = 5


def is_safe_path(name):
    """Reject anything that could escape --root.

    tarfile's filter="data" does not exist before Python 3.12, so an
    unguarded extractall on RHEL 8's 3.6 is a directory-traversal hole. These
    are the same rules mmw applies to upstream metadata in
    core/downloader.is_safe_repo_path (absolute paths, `..` traversal,
    backslashes, NULs), plus two things that check is specifically about
    *upstream metadata paths* and this one is not:

    - a bare "." or a "." path segment (`os.path.normpath` alone treats "."
      as already-normalized, so `name == "."` slips past a plain
      normpath-equality check; joined with root that means the destination
      IS root, and the regular-file write path below would then try to
      rename a temp file on top of the whole mirror directory).
    - `os.path.isabs`, which also catches path forms `startswith("/")`
      misses on some platforms; kept even though this script only ever runs
      on POSIX far sides, because a defensive check here costs nothing and
      the consequence of getting it wrong is a machine mmw can't reach.
    """
    if not name:
        return False
    if name.startswith("/") or name.startswith("~"):
        return False
    if "\\" in name or "\x00" in name:
        return False
    if os.path.isabs(name):
        return False
    parts = name.split("/")
    if any(p in ("..", ".") for p in parts):
        return False
    return posixpath.normpath(name) == name.rstrip("/")


def open_stream(path):
    """zstd -dc <path> as a readable pipe. Raises FileNotFoundError if the
    zstd binary itself is missing — main() turns that into one clear line
    instead of a raw traceback, since "zstd assumed present" is an
    assumption, not a guarantee, on a machine we've never seen."""
    return subprocess.Popen(["zstd", "-dc", path], stdout=subprocess.PIPE)


def read_manifest_bytes(blob):
    """(header dict, {path: checksum}) from a zstd-compressed JSONL blob.

    The manifest travels inside the tar already zstd-compressed
    (manifest.py writes it that way before BundleSink ever adds it as a tar
    member), so this is a second, independent zstd invocation from the one
    unpacking the outer tar.zst stream.
    """
    proc = subprocess.Popen(["zstd", "-dc"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE)
    out, _ = proc.communicate(blob)
    lines = out.decode("utf-8").splitlines()
    header = json.loads(lines[0])
    files = {}
    for line in lines[1:]:
        if line:
            row = json.loads(line)
            files[row["p"]] = row["c"]
    return header, files


def read_state(root):
    path = os.path.join(root, STATE_FILE)
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return fh.read().strip() or None


def substitute_root(root, path):
    """Replace the nginx root placeholder once the destination is known.

    A bundle is built before anyone knows where it will land, so nginx.py
    writes ROOT_PLACEHOLDER instead of a real path (see its own docstring).
    This is the one place that placeholder ever gets resolved.
    """
    with open(path, "rb") as fh:
        data = fh.read()
    if ROOT_PLACEHOLDER.encode() not in data:
        return
    with open(path, "wb") as fh:
        fh.write(data.replace(ROOT_PLACEHOLDER.encode(), root.encode()))


def _link_or_copy(target, dest):
    """Recreate a by-hash duplicate. os.link is nearly free (the pool key IS
    the sha256, so the two paths are byte-identical); fall back to a copy
    only when the destination filesystem can't hardlink at all — a USB drive
    or NFS mount formatted differently from what mmw was built against.
    """
    if os.path.exists(dest):
        os.unlink(dest)
    try:
        os.link(target, dest)
    except OSError:
        shutil.copy2(target, dest)


def apply(bundle, root, dry_run=False, verify=False):
    proc = open_stream(bundle)
    header = None
    manifest = {}
    deletes = []
    written = 0

    tar = tarfile.open(fileobj=proc.stdout, mode="r|")
    for member in tar:
        name = member.name
        if not is_safe_path(name):
            sys.stderr.write("refusing unsafe path in bundle: %r\n" % (name,))
            return EXIT_UNSAFE

        # Everything mmw ever puts in a bundle is a regular file, a
        # directory, or a hardlink (by-hash duplicates — see sinks.py's
        # BundleSink.close). Anything else — a symlink above all, but also
        # device/fifo entries — is refused outright: tarfile has no
        # filter="data" on this Python to reject it for us, and a symlink
        # whose *name* passes is_safe_path but whose target does not would
        # otherwise get created, then have a later "safe" write follow it
        # straight out of --root.
        if not (member.isreg() or member.isdir() or member.islnk()):
            sys.stderr.write(
                "refusing unsupported tar entry type for %r: %s\n"
                % (name, member.type))
            return EXIT_UNSAFE

        if member.islnk() and not is_safe_path(member.linkname):
            # linkname is attacker/producer data exactly like name is.
            # os.path.join(root, linkname) ignores root entirely if linkname
            # is absolute — os.path.join("/srv/www", "/etc/shadow") ==
            # "/etc/shadow" — which would hardlink an arbitrary file on this
            # machine into the served tree under a name that passed the
            # check above. Same rules, same reason, applied to the other
            # half of a hardlink entry.
            sys.stderr.write(
                "refusing hardlink with unsafe target: %r -> %r\n"
                % (name, member.linkname))
            return EXIT_UNSAFE

        if name == MANIFEST_MEMBER:
            header, manifest = read_manifest_bytes(tar.extractfile(member).read())
            have = read_state(root)
            want = header.get("previous")
            if not header.get("full") and have != want:
                sys.stderr.write(
                    "this bundle expects the mirror to be at %s but it is at %s\n"
                    "applying out of order would leave the tree silently "
                    "incomplete. Re-bundle with --full, or apply the missing "
                    "bundle first.\n" % (want or "(empty)", have or "(empty)")
                )
                return EXIT_OUT_OF_ORDER
            sys.stdout.write("%s -> %s\n" % (want or "(new)", header["created"]))
            continue

        if name == DELETE_MEMBER:
            deletes = tar.extractfile(member).read().decode().split("\n")
            deletes = [d for d in deletes if d]
            continue

        if header is None:
            sys.stderr.write("bundle is malformed: no manifest before payload\n")
            return EXIT_UNSAFE

        if name.startswith(".mmw/"):
            continue
        if member.isdir():
            continue
        if dry_run:
            written += 1
            continue

        dest = os.path.join(root, name)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if member.islnk():
            target = os.path.join(root, member.linkname)
            if not os.path.exists(target):
                # Our own sinks always emit a hardlink's first occurrence
                # before any duplicate (BundleSink.close only registers
                # `by_checksum[checksum]` after a successful write), so this
                # should be unreachable against a bundle mmw produced. A
                # clear message beats an unhandled FileNotFoundError from
                # deep inside os.link either way.
                sys.stderr.write(
                    "hardlink target missing, bundle is malformed: "
                    "%r -> %r\n" % (name, member.linkname))
                return EXIT_UNSAFE
            _link_or_copy(target, dest)
        else:
            tmp = dest + ".mmw-tmp"
            with open(tmp, "wb") as out:
                src = tar.extractfile(member)
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
            os.rename(tmp, dest)
        written += 1

    tar.close()
    proc.stdout.close()
    rc = proc.wait()
    if rc != 0:
        # tarfile only raises if the truncated stream lands mid-member; a
        # zstd failure that happens to leave a byte-complete tar up to that
        # point would otherwise look like a clean, if short, apply. Checked
        # before any deletion or state write below, so a corrupt transfer
        # never gets to claim success.
        sys.stderr.write(
            "zstd exited with status %d decompressing %r — the bundle is "
            "truncated or corrupt; nothing after the last good entry was "
            "applied\n" % (rc, bundle))
        return EXIT_TOOLING

    if header is None:
        # A bundle with no manifest member at all — never one mmw produced
        # (BundleSink always writes it first), but this script's whole job
        # is treating "never produced by mmw" as the normal case. Without
        # this, a delete.list with no manifest would fall straight through
        # to the deletion loop below and remove files on nothing but an
        # unauthenticated bundle's say-so.
        sys.stderr.write("bundle is malformed: no manifest found\n")
        return EXIT_UNSAFE

    # Deletions are applied LAST, after every file above has been written —
    # a client mid-`apt update` on a live-served far side must never see a
    # release file whose referenced indexes have already been removed.
    removed = 0
    for rel in deletes:
        if not is_safe_path(rel):
            continue
        target = os.path.join(root, rel)
        if dry_run:
            removed += 1
            continue
        try:
            os.unlink(target)
            removed += 1
        except OSError:
            pass

    sys.stdout.write("%d applied  %d removed\n" % (written, removed))

    if not dry_run:
        for rel in manifest:
            if rel.startswith("nginx/"):
                target = os.path.join(root, rel)
                if os.path.exists(target):
                    substitute_root(root, target)
        with open(os.path.join(root, STATE_FILE), "w") as fh:
            fh.write(header["created"] + "\n")

    if verify:
        return verify_tree(root, manifest)
    return 0


def verify_tree(root, manifest):
    """Hash every file the manifest names. The manifest describes the
    COMPLETE tree, so this catches a bundle that was never applied at all,
    not just a corrupt one."""
    missing = []
    wrong = []
    for rel, want in manifest.items():
        if not want:
            continue
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            missing.append(rel)
            continue
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
        if h.hexdigest() != want:
            wrong.append(rel)
    if missing or wrong:
        sys.stderr.write("verify FAILED: %d missing, %d wrong\n"
                         % (len(missing), len(wrong)))
        for rel in (missing + wrong)[:20]:
            sys.stderr.write("  %s\n" % rel)
        return EXIT_VERIFY_FAILED
    sys.stdout.write("verified %d files OK\n" % len(manifest))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Apply an mmw bundle.")
    ap.add_argument("bundle")
    ap.add_argument("--root", required=True,
                    help="directory the mirror is served from")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true",
                    help="hash the whole tree against the manifest afterwards")
    args = ap.parse_args(argv)
    if not args.dry_run:
        os.makedirs(args.root, exist_ok=True)
    try:
        return apply(args.bundle, args.root, args.dry_run, args.verify)
    except FileNotFoundError as e:
        # Almost always the zstd binary itself missing from PATH — the one
        # external dependency this script has, and the one thing about a far
        # side we cannot verify ahead of time. A raw traceback here reads
        # like a bug in apply.py; this reads like what it is, a missing tool.
        sys.stderr.write("required external tool not found: %s\n" % (e,))
        return EXIT_TOOLING


if __name__ == "__main__":
    sys.exit(main())
