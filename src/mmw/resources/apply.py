#!/usr/bin/env python3
"""Apply an mmw bundle to a mirror root.

Shipped inside every bundle and written beside it (`.mmw/apply.py` in the
tar, and a plain copy next to the bundle on our side so a first transfer has
something to run at all). This is the ONLY piece of mmw that runs on the far
side of the airgap: no mmw install, often no pip, sometimes Python 3.6
(RHEL 8). Every other component in this feature fails loudly on our side of
the wire; this one fails on someone else's, in a facility no one here can
reach, so every check below favours a clear stderr message and a non-zero
exit over a guess — and that includes exceptions this script's own logic
raises, not just the conditions it checks for explicitly. A raw Python
traceback on a machine nobody can attach a debugger to is exactly the
"someone else's, months later" failure mode this file exists to avoid.

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
# only kept the exit status. EXIT_UNSAFE is deliberately reused for several
# distinct failure shapes (unsafe path, unsupported entry type, corrupt tar,
# corrupt manifest, a filesystem error mid-write) rather than minted fresh
# per case — none of today's callers parse these beyond zero/non-zero, so
# finer granularity is not worth the extra surface until one does.
EXIT_UNSAFE = 2
EXIT_OUT_OF_ORDER = 3
EXIT_VERIFY_FAILED = 4
EXIT_TOOLING = 5

# Human names for the tar type byte, used only so "refusing unsupported tar
# entry type" names something a person holding the drive can recognise —
# member.type is a single byte (b'2' for a symlink) and prints unreadably on
# its own.
_TAR_TYPE_NAMES = {
    tarfile.REGTYPE: "regular file",
    tarfile.AREGTYPE: "regular file",
    tarfile.LNKTYPE: "hardlink",
    tarfile.SYMTYPE: "symlink",
    tarfile.CHRTYPE: "character device",
    tarfile.BLKTYPE: "block device",
    tarfile.DIRTYPE: "directory",
    tarfile.FIFOTYPE: "fifo",
    tarfile.CONTTYPE: "contiguous file",
}


def _type_name(member):
    return _TAR_TYPE_NAMES.get(member.type, repr(member.type))


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
    unpacking the outer tar.zst stream. A corrupt or hand-truncated manifest
    blob surfaces here as json.JSONDecodeError, or via subprocess return
    code check — a truncation on a line boundary otherwise produces valid
    JSON lines that are merely incomplete. The caller wraps this call; it
    does not catch its own errors.
    """
    proc = subprocess.Popen(["zstd", "-dc"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE)
    out, _ = proc.communicate(blob)
    if proc.returncode != 0:
        # zstd exited 1 on both truncated frames and garbage. A return-code
        # check is necessary and sufficient to detect both.
        raise json.JSONDecodeError(
            "zstd decompression failed (exit %d)" % proc.returncode,
            "", 0)
    lines = out.decode("utf-8").splitlines()
    if not lines:
        raise json.JSONDecodeError("empty manifest", "", 0)
    header = json.loads(lines[0])
    # Require the header to carry the fields the script relies on, not just
    # valid JSON. A partially-truncated manifest might have a header line but
    # lack required fields.
    required_fields = ["network", "created", "previous", "full"]
    for field in required_fields:
        if field not in header:
            raise KeyError(field)
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


def _tmp_dir(root):
    return os.path.join(root, ".mmw", "tmp")


def _tmp_path(root, name):
    """A flat, collision-free staging path for one tar member's content.

    Content is written here in full, then renamed into its final place —
    never assembled in place at the destination. That is what makes a
    killed process or a mid-write exception leave nothing partial where a
    client could fetch it: the destination path only ever comes into
    existence via one atomic same-filesystem rename, all-or-nothing. This
    used to be `{dest}.mmw-tmp`, a sibling of the real file — see
    _sweep_tmp for why that was a live disclosure risk, not just untidy.

    Hashed rather than derived from `name` by substitution: `name` can be
    arbitrarily long or deep
    (`archive.ubuntu.com/ubuntu/dists/.../by-hash/SHA256/<64 hex>`), and
    flattening a deep tree into one directory by replacing "/" risks
    exceeding a filesystem's max filename length on some far side we've
    never seen. A fixed-width digest has no such ceiling and needs no
    escaping — collisions are not a concern here since a run is
    single-threaded and sequential, so at most one member ever holds a
    given digest's tmp file at a time.
    """
    digest = hashlib.sha256(name.encode("utf-8", "surrogateescape")).hexdigest()
    return os.path.join(_tmp_dir(root), digest)


def _sweep_tmp(root):
    """Clear whatever the last apply — crashed, killed, or interrupted —
    left behind in the staging directory.

    Before staging moved here, an in-progress write used `{dest}.mmw-tmp`
    right beside its real destination — inside the served tree. nginx's
    generated config turns on `autoindex`, so a partial file left there by a
    killed process was not just present but *listed and fetchable* by any
    client hitting that directory, indefinitely: a `--full` recovery bundle
    never removes a path its own manifest doesn't mention, and a file whose
    content is genuinely unchanged is never resent, so nothing would ever
    overwrite or clean up the stray on its own.

    Staging now lives under `{root}/.mmw/`, which is never part of any
    mirror_base and is never extracted into — every `.mmw/`-prefixed tar
    member is skipped on the way in (see the payload loop in `apply()`) —
    so nothing here is ever reachable through a served URL, and cleanup is
    one directory listing rather than a walk over a multi-million-file tree.
    Called at the start of every non-dry-run apply, before anything is
    extracted, so a bundle applied on top of debris from a previous failed
    run starts from a clean slate rather than accumulating it.

    A cleanup routine that can delete outside its own directory is
    indefensible. Refuse a symlink, regardless of who created it.
    """
    d = _tmp_dir(root)
    if os.path.islink(d):
        sys.stderr.write(
            "refusing to sweep tmp directory: %r is a symlink; this would "
            "delete outside --root\n" % (d,))
        raise OSError("tmp directory is a symlink")
    try:
        entries = os.listdir(d)
    except OSError:
        return
    for entry in entries:
        try:
            os.unlink(os.path.join(d, entry))
        except OSError:
            pass


def apply(bundle, root, dry_run=False, verify=False):
    header = None
    manifest = {}
    deletes = []
    written = 0
    # Name of whatever this apply is working on when something goes wrong —
    # threaded through the try/except below purely so a corrupt-tar or
    # filesystem error names *what* failed, not just that something did.
    current = None

    try:
        if not dry_run:
            mmw_dir = os.path.join(root, ".mmw")
            if os.path.islink(mmw_dir):
                sys.stderr.write(
                    "refusing to create tmp directory: %r is a symlink; this "
                    "would write outside --root\n" % (mmw_dir,))
                return EXIT_UNSAFE
            os.makedirs(_tmp_dir(root), exist_ok=True)
            _sweep_tmp(root)
        proc = open_stream(bundle)
        tar = tarfile.open(fileobj=proc.stdout, mode="r|")
        for member in tar:
            name = member.name
            current = name
            if not is_safe_path(name):
                sys.stderr.write("refusing unsafe path in bundle: %r\n" % (name,))
                return EXIT_UNSAFE

            # Everything mmw ever puts in a bundle is a regular file, a
            # directory, or a hardlink (by-hash duplicates — see sinks.py's
            # BundleSink.close). Anything else — a symlink above all, but
            # also device/fifo entries — is refused outright: tarfile has no
            # filter="data" on this Python to reject it for us, and a
            # symlink whose *name* passes is_safe_path but whose target does
            # not would otherwise get created, then have a later "safe"
            # write follow it straight out of --root.
            if not (member.isreg() or member.isdir() or member.islnk()):
                sys.stderr.write(
                    "refusing unsupported tar entry type for %r: %s\n"
                    % (name, _type_name(member)))
                return EXIT_UNSAFE

            if member.islnk() and not is_safe_path(member.linkname):
                # linkname is attacker/producer data exactly like name is.
                # os.path.join(root, linkname) ignores root entirely if
                # linkname is absolute — os.path.join("/srv/www",
                # "/etc/shadow") == "/etc/shadow" — which would hardlink an
                # arbitrary file on this machine into the served tree under
                # a name that passed the check above. Same rules, same
                # reason, applied to the other half of a hardlink entry.
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
                    # Our own sinks always emit a hardlink's first
                    # occurrence before any duplicate (BundleSink.close only
                    # registers `by_checksum[checksum]` after a successful
                    # write), so this should be unreachable against a bundle
                    # mmw produced. A clear message beats an unhandled
                    # FileNotFoundError from deep inside os.link either way.
                    sys.stderr.write(
                        "hardlink target missing, bundle is malformed: "
                        "%r -> %r\n" % (name, member.linkname))
                    return EXIT_UNSAFE
                _link_or_copy(target, dest)
            else:
                # Staged under {root}/.mmw/tmp/, never beside `dest` — see
                # _tmp_path/_sweep_tmp. `dest` only ever comes into being via
                # the os.rename below, so a crash or exception anywhere in
                # this block leaves nothing at `dest` for nginx to serve.
                tmp = _tmp_path(root, name)
                with open(tmp, "wb") as out:
                    src = tar.extractfile(member)
                    while True:
                        chunk = src.read(1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
                os.rename(tmp, dest)
            written += 1

        current = None
        tar.close()
        proc.stdout.close()
        rc = proc.wait()
        if rc != 0:
            # tarfile only raises if the truncated stream lands mid-member;
            # a zstd failure that happens to leave a byte-complete tar up to
            # that point would otherwise look like a clean, if short, apply.
            # Checked before any deletion or state write below, so a
            # corrupt transfer never gets to claim success.
            sys.stderr.write(
                "zstd exited with status %d decompressing %r — the bundle is "
                "truncated or corrupt; nothing after the last good entry was "
                "applied\n" % (rc, bundle))
            return EXIT_TOOLING

        if header is None:
            # A bundle with no manifest member at all — never one mmw
            # produced (BundleSink always writes it first), but this
            # script's whole job is treating "never produced by mmw" as the
            # normal case. Without this, a delete.list with no manifest
            # would fall straight through to the deletion loop below and
            # remove files on nothing but an unauthenticated bundle's
            # say-so.
            sys.stderr.write("bundle is malformed: no manifest found\n")
            return EXIT_UNSAFE

        # Deletions are applied LAST, after every file above has been
        # written — a client mid-`apt update` on a live-served far side must
        # never see a release file whose referenced indexes have already
        # been removed.
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
                    current = rel
                    target = os.path.join(root, rel)
                    if os.path.exists(target):
                        substitute_root(root, target)
            current = STATE_FILE
            with open(os.path.join(root, STATE_FILE), "w") as fh:
                fh.write(header["created"] + "\n")

        if verify:
            return verify_tree(root, manifest)
        return 0

    except tarfile.TarError as e:
        # A truncated or otherwise corrupt tar stream — raised either by
        # tarfile.open() itself (garbage from the start) or while iterating
        # (EOF mid-member). Whichever bundle byte range didn't make it
        # across, this is the "silently incomplete" case the out-of-order
        # check exists for turned into a loud one instead: nothing after the
        # break point was applied, and now that's stated rather than guessed.
        sys.stderr.write(
            "bundle is corrupt or truncated%s: %s\n"
            % (" (at %r)" % current if current else "", e))
        return EXIT_UNSAFE
    except json.JSONDecodeError as e:
        sys.stderr.write("manifest is corrupt (invalid JSON): %s\n" % (e,))
        return EXIT_UNSAFE
    except KeyError as e:
        # header["created"] is the only bracket access on parsed manifest
        # JSON — a manifest missing that field entirely (never one mmw
        # writes; manifest.py always includes it) lands here instead of an
        # unhandled traceback two frames deep.
        sys.stderr.write(
            "bundle is malformed, missing field %s%s\n"
            % (e, " (while processing %r)" % current if current else ""))
        return EXIT_UNSAFE
    except OSError as e:
        if isinstance(e, FileNotFoundError):
            # Not a bundle problem — almost always the zstd binary itself
            # missing from PATH. Re-raise so main()'s own handler reports
            # that specifically, rather than folding it into the generic
            # "filesystem error" message below.
            raise
        sys.stderr.write(
            "filesystem error%s: %s\n"
            % (" while writing %r" % current if current else "", e))
        return EXIT_UNSAFE


def verify_tree(root, manifest):
    """Hash every file the manifest names. The manifest describes the
    COMPLETE tree, so this catches a bundle that was never applied at all,
    not just a corrupt one.

    Coverage note: this only checks files the manifest *lists* — it proves
    every listed file is present with the right content, not that nothing
    extra or orphaned exists under root. It cannot detect a stray file a
    manifest never mentioned (including, before the tmp-directory change
    above, a `.mmw-tmp` left by a killed process). A future reader should
    not read "verified N files OK" as "the tree contains nothing else."
    """
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
