#!/usr/bin/env python3
"""Apply an mmw bundle to a mirror root.

Shipped inside every bundle and written beside it (`.mmw/apply.py` in the
tar, and a plain copy next to the bundle on our side so a first transfer has
something to run at all). Also included as top-level apply.py in published
trees and bundle payloads, so directly copied trees carry the same tools.
This is the ONLY piece of mmw that runs on the far
side of the airgap: no mmw install, often no pip, sometimes Python 3.6
(RHEL 8). Every other component in this feature fails loudly on our side of
the wire; this one fails on someone else's, in a facility no one here can
reach, so every check below favours a clear stderr message and a non-zero
exit over a guess — and that includes exceptions this script's own logic
raises, not just the conditions it checks for explicitly. A raw Python
traceback on a machine nobody can attach a debugger to is exactly the
"someone else's, months later" failure mode this file exists to avoid.

    python3 apply.py lab-20260906T143000Z.tar --root /srv/www

Needs nothing but Python — see test_apply_script_imports_only_stdlib for the
enforced import allowlist. mmw writes plain tars; one compressed in transit
(`zstd lab-….tar`) is refused with a message: decompress it first.
"""
import argparse
import hashlib
import io
import json
import os
import posixpath
import shutil
import sys
import tarfile

try:
    import ssl          # checks a certificate fits its key; optional
except ImportError:     # a Python built without OpenSSL
    ssl = None

STATE_FILE = ".mmw-state"
MMW_DIR = ".mmw"
MANIFEST_MEMBER = ".mmw/manifest.jsonl"
# Bundles from before plain manifests carry it zstd-compressed — each came with
# its own apply.py, which reads it.
OLD_MANIFEST_MEMBER = ".mmw/manifest.jsonl.zst"
DELETE_MEMBER = ".mmw/delete.list"
# A `certs` network's certificate and private key. Installed under
# {root}/.mmw/tls/ — never a served path — and referenced from there by the
# bundle's own nginx/mirror.conf.
TLS_PREFIX = ".mmw/tls/"
# Written in {root}/.mmw/tls/ once a signed certificate fits the key; every
# server block of the bundle's nginx config includes `https*.conf` there, a
# wildcard nginx ignores while nothing matches it.
HTTPS_CONF = "https.conf"
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


_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def _is_zstd(path):
    """Sniff the actual bytes rather than trust the filename: a bundle
    someone compressed for transfer must be decompressed before applying."""
    with open(path, "rb") as fh:
        return fh.read(4) == _ZSTD_MAGIC


def read_manifest_bytes(blob):
    """(header dict, {path: checksum}) from the JSONL manifest blob, read a
    line at a time (no second full copy of a two-million-line manifest).

    A corrupt or hand-truncated manifest surfaces as json.JSONDecodeError or
    a KeyError for a missing header field; a truncation landing on a line
    boundary parses, and is caught by the file_count check instead. The
    caller wraps this call; it does not catch its own errors.
    """
    header = None
    files = {}
    for raw in io.BytesIO(blob):
        line = raw.decode("utf-8").strip()
        if not line:
            continue
        if header is None:
            header = json.loads(line)
            # Require the header to carry the fields the script relies on, not
            # just valid JSON. A partially-truncated manifest might have a
            # header line but lack required fields.
            for field in ("network", "created", "previous", "full"):
                if field not in header:
                    raise KeyError(field)
            continue
        row = json.loads(line)
        files[row["p"]] = row["c"]
    if header is None:
        raise json.JSONDecodeError("empty manifest", "", 0)
    return header, files


def _prune_empty_parents(root, start):
    """Remove directories left empty by a deletion, upward to `root`.

    A retired suite or component otherwise leaves its whole directory skeleton
    behind, and the generated nginx config sets `autoindex on` — so clients
    browse a tree of empty directories that accumulates with every suite
    rotation and never goes away. Stops at `root`, stops at the first
    non-empty directory, and never touches `.mmw/`.
    """
    root = os.path.abspath(root)
    current = os.path.abspath(start)
    while current.startswith(root + os.sep) and current != root:
        base = os.path.basename(current)
        if base == MMW_DIR or base.startswith("."):
            return
        try:
            if os.listdir(current):
                return
            os.rmdir(current)
        except OSError:
            return
        current = os.path.dirname(current)


def _read_state_lines(root):
    path = os.path.join(root, STATE_FILE)
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return [line.strip() for line in fh.read().splitlines()]


def read_state(root):
    """The snapshot this mirror is at: the state file's first line (an older
    apply.py wrote only that line)."""
    lines = _read_state_lines(root)
    return (lines[0] or None) if lines else None


def read_state_label(root):
    lines = _read_state_lines(root)
    return (lines[1] or None) if len(lines) > 1 else None


def _named(stamp, label):
    if not stamp:
        return "(empty)"
    return '%s "%s"' % (stamp, label) if label else stamp


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


def _nginx_root_config(rel):
    # nginx/ also holds a potentially large Docker image. Never buffer or
    # rewrite binary payloads while substituting generated config paths.
    return rel.startswith("nginx/") and rel.endswith(".conf")


def _link_or_copy(target, dest, tmp):
    """Recreate a by-hash duplicate. os.link is nearly free (the pool key IS
    the sha256, so the two paths are byte-identical); fall back to a copy
    only when the destination filesystem can't hardlink at all — a USB drive
    or NFS mount formatted differently from what mmw was built against.

    Staged at `tmp` and renamed, like every regular file: unlinking `dest`
    first left a window in which a live-served by-hash path 404'd.
    """
    if os.path.lexists(tmp):
        os.unlink(tmp)
    try:
        os.link(target, tmp)
    except OSError:
        shutil.copy2(target, tmp)
    os.rename(tmp, dest)


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


def _install_tls(root, name, src):
    """Write one TLS file to {root}/.mmw/tls/{name}, staged and renamed.

    The private key is created 0600 from its first byte — never written
    0644 and tightened afterwards — and the directory is 0700, so the key is
    not readable by anyone but the applying user (and root, i.e. nginx's
    master process) at any moment.
    """
    d = os.path.join(root, ".mmw", "tls")
    if os.path.islink(d):
        raise OSError("%s is a symlink; refusing to write a private key through it" % d)
    if not os.path.isdir(d):
        os.makedirs(d)
    os.chmod(d, 0o700)
    mode = 0o600 if name.endswith(".key") else 0o644
    dest = os.path.join(d, name)
    tmp = dest + ".tmp"
    if os.path.lexists(tmp):
        os.unlink(tmp)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as out:
        shutil.copyfileobj(src, out)
    os.chmod(tmp, mode)
    os.rename(tmp, dest)


def _cnf_hosts(path):
    """The hostnames a request's OpenSSL config asks for (`DNS.n = host`)."""
    hosts = set()
    with open(path) as fh:
        for line in fh:
            key, sep, value = line.partition("=")
            if sep and key.strip().startswith("DNS."):
                hosts.add(value.strip())
    return hosts


def _cert_hosts(crt):
    """The certificate's DNS names, or None if this Python cannot read them."""
    try:
        decoded = ssl._ssl._test_decode_cert(crt)
    except Exception:  # noqa: BLE001 — a private helper; absent means "can't tell"
        return None
    return {v for k, v in decoded.get("subjectAltName", ()) if k == "DNS"}


def https_state(root):
    """Turn HTTPS on or off in the bundle's nginx config, from what is in
    {root}/.mmw/tls/: on when a certificate fits the key, off otherwise.
    Returns the lines to tell the operator (empty for a network without TLS)."""
    d = os.path.join(root, ".mmw", "tls")
    if not os.path.isdir(d):
        return []
    crt, key = os.path.join(d, "mirror.crt"), os.path.join(d, "mirror.key")
    conf = os.path.join(d, HTTPS_CONF)
    sign = "python3 sign.py --ca CA.crt --ca-key CA.key %s" % root
    want, notes = None, []
    if os.path.isfile(crt) and os.path.isfile(key):
        problem = None
        if ssl is not None:
            try:
                ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(crt, key)
            except (ssl.SSLError, OSError) as e:
                problem = "mirror.crt does not fit mirror.key (%s)" % (e,)
        if problem:
            notes.append("HTTPS off: %s — re-sign: %s" % (problem, sign))
        else:
            want = ("# Written by apply.py: mirror.crt here fits mirror.key.\n"
                    "listen 443 ssl;\nssl_certificate %s;\nssl_certificate_key %s;\n"
                    % (crt, key))
            cnf = os.path.join(d, "mirror.cnf")
            have = _cert_hosts(crt)
            if have is not None and os.path.isfile(cnf):
                missing = sorted(_cnf_hosts(cnf) - have)
                if missing:
                    notes.append("the certificate does not cover %s — re-sign: %s"
                                 % (", ".join(missing), sign))
    elif os.path.isfile(os.path.join(d, "mirror.csr")):
        notes.append("HTTPS waits for a signed certificate: %s" % sign)
    if want is not None:
        want = want.replace(crt, "/srv/mmw/.mmw/tls/mirror.crt").replace(key, "/srv/mmw/.mmw/tls/mirror.key")
    current = None
    if os.path.isfile(conf):
        with open(conf) as fh:
            current = fh.read()
    if want != current:
        if want is None:
            os.unlink(conf)
            notes.append("HTTPS off — recreate the nginx container")
        else:
            tmp = conf + ".tmp"
            with open(tmp, "w") as fh:
                fh.write(want)
            os.rename(tmp, conf)
            notes.append("HTTPS on — recreate the nginx container")
    return notes


# Derived trees: (renderer shipped in the tree, the directory it owns). The
# one rule every side runs — live publish, snapshot replay, and here after a
# bundle — so a far side's pages are the near side's, byte for byte.
DERIVED = (("pypi-data/render.py", "pypi.org"),)
RENDERED_MARK = ".mmw-rendered"


def render_derived(root, say=None):
    """Run each shipped renderer; with none left, remove the directory it
    owned (only if it carries the renderer's mark — never someone else's)."""
    import runpy
    for script, owned in DERIVED:
        renderer = os.path.join(root, script)
        if os.path.isfile(renderer):
            runpy.run_path(renderer, run_name="mmw_render")["render"](root, say=say)
        elif os.path.isdir(os.path.join(root, owned, RENDERED_MARK)):
            shutil.rmtree(os.path.join(root, owned))


def apply(bundle, root, dry_run=False, verify=False):
    header = None
    manifest = {}
    manifest_blob = None
    deletes = []
    written = 0
    tls_installed = []
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
        if _is_zstd(bundle):
            sys.stderr.write(
                "%s is zstd-compressed — mmw bundles are plain tars. Decompress it "
                "first (zstd -d %s) and apply the .tar.\n" % (bundle, bundle))
            return EXIT_TOOLING
        stream = open(bundle, "rb")
        tar = tarfile.open(fileobj=stream, mode="r|")
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

            if name == OLD_MANIFEST_MEMBER:
                sys.stderr.write(
                    "this bundle was made by an older mmw (compressed manifest) — "
                    "apply it with the apply.py that came with it\n")
                return EXIT_TOOLING
            if name == MANIFEST_MEMBER:
                manifest_blob = tar.extractfile(member).read()
                header, manifest = read_manifest_bytes(manifest_blob)
                have = read_state(root)
                want = header.get("previous")
                if not header.get("full") and have != want:
                    sys.stderr.write(
                        "out of order: this bundle goes %s -> %s, but this mirror "
                        "is at %s.\n"
                        "Applying it would leave the tree silently wrong. Ask for a "
                        "bundle from where this mirror is:\n"
                        "    mmw bundle -n %s --from %s --to %s\n"
                        "(or a full bundle: --from none). A failed apply never "
                        "advances %s.\n" % (
                            _named(want, header.get("previous_label")),
                            _named(header["created"], header.get("created_label")),
                            _named(have, read_state_label(root)),
                            header.get("network", "NETWORK"), have or "none",
                            header["created"], STATE_FILE)
                    )
                    return EXIT_OUT_OF_ORDER
                if not dry_run:
                    # The tree is about to change: its record says what it
                    # held before. Gone until this apply finishes, so an
                    # interrupted one never leaves it describing a half-new tree.
                    try:
                        os.unlink(os.path.join(root, MANIFEST_MEMBER))
                    except OSError:
                        pass
                sys.stdout.write("%s -> %s\n" % (
                    _named(want, header.get("previous_label")) if want else "(new)",
                    _named(header["created"], header.get("created_label"))))
                continue

            if name == DELETE_MEMBER:
                deletes = tar.extractfile(member).read().decode().split("\n")
                deletes = [d for d in deletes if d]
                continue

            if header is None:
                sys.stderr.write("bundle is malformed: no manifest before payload\n")
                return EXIT_UNSAFE

            if name.startswith(TLS_PREFIX):
                tls_name = name[len(TLS_PREFIX):]
                if not member.isreg() or not tls_name or "/" in tls_name:
                    sys.stderr.write("refusing unexpected TLS entry: %r\n" % (name,))
                    return EXIT_UNSAFE
                if not dry_run:
                    _install_tls(root, tls_name, tar.extractfile(member))
                tls_installed.append(tls_name)
                continue
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
                _link_or_copy(target, dest, _tmp_path(root, name))
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
        stream.close()

        # A first attempt at this compared `manifest` (the network's COMPLETE
        # expected tree — that's what makes --verify meaningful) against
        # `applied_paths` directly, and broke every delta bundle: a delta's
        # tar deliberately contains only what changed, so every legitimately
        # unchanged file (listed in the manifest, never a member of THIS tar)
        # looked "missing". `file_count` sidesteps that: BundleSink.close()
        # declares it from its own send/generate lists — which ARE the exact
        # scope of the bundle that produced this manifest — before writing a
        # single tar byte, so a delta bundle simply declares a smaller count
        # than the manifest's full file list, correctly. Absent on an older
        # manifest written before this field existed: skip rather than guess.
        declared = header.get("file_count") if header is not None else None
        if declared is not None and written != declared:
            sys.stderr.write(
                "bundle is incomplete: expected %d file(s) from this bundle, "
                "applied %d — the archive is truncated or corrupt\n"
                % (declared, written))
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
                # Refused, not skipped. Every *extraction* entry that fails
                # this check aborts with EXIT_UNSAFE; the delete list quietly
                # ignored its failures, so a bundle trying to unlink something
                # outside --root was indistinguishable from a clean one. It is
                # harmless only because the check is here — which is exactly
                # the thing worth saying out loud.
                sys.stderr.write("unsafe path in delete list: %r\n" % (rel,))
                return EXIT_UNSAFE
            target = os.path.join(root, rel)
            if dry_run:
                removed += 1
                continue
            try:
                os.unlink(target)
                removed += 1
            except OSError:
                pass
            else:
                _prune_empty_parents(root, os.path.dirname(target))

        sys.stdout.write("%d applied  %d removed\n" % (written, removed))
        if tls_installed:
            sys.stdout.write("TLS: %s -> %s\n" % (
                ", ".join(sorted(tls_installed)), os.path.join(root, ".mmw", "tls")))

        if not dry_run:
            for rel in manifest:
                if _nginx_root_config(rel):
                    current = rel
                    target = os.path.join(root, rel)
                    if os.path.exists(target):
                        substitute_root(root, target)
                        # The spec says an unsubstituted placeholder must
                        # never reach disk (nginx.py's own docstring: "An
                        # unsubstituted placeholder reaching disk on either
                        # side is a bug"). substitute_root() is a no-op when
                        # the placeholder isn't present at all, so this can
                        # only fire on a real bug — warn rather than silently
                        # serve a config nginx will refuse to load.
                        with open(target, "rb") as fh:
                            if ROOT_PLACEHOLDER.encode() in fh.read():
                                sys.stderr.write(
                                    "warning: %r still contains %s after "
                                    "substitution\n" % (rel, ROOT_PLACEHOLDER))
            current = "rendering derived pages"
            render_derived(root, say=lambda message: sys.stdout.write(message + "\n"))
            current = STATE_FILE
            # Staged and renamed: a state file cut short by a full disk or a
            # kill reads as "at nothing", and every later delta is refused as
            # out of order until someone carries a full bundle across.
            state = os.path.join(root, STATE_FILE)
            with open(state + ".tmp", "w") as fh:
                fh.write(header["created"] + "\n" + (header.get("created_label") or "") + "\n")
            os.rename(state + ".tmp", state)
            # The tree's record — the same file a publish keeps in its own
            # tree, so a copied publish and an applied bundle agree on it.
            if manifest_blob is not None:
                current = MANIFEST_MEMBER
                record = os.path.join(root, MANIFEST_MEMBER)
                os.makedirs(os.path.dirname(record), exist_ok=True)
                with open(record + ".tmp", "wb") as fh:
                    fh.write(manifest_blob)
                os.rename(record + ".tmp", record)

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

    nginx/ note: the manifest's checksum for an `nginx/*.conf` file was
    computed by BundleSink over the bytes it actually wrote — the
    ROOT_PLACEHOLDER form, since a bundle is built before anyone knows where
    it will land (nginx.py). `apply()` rewrites that file in place with the
    real `--root` once the archive lands (`substitute_root`). Hashing the
    on-disk bytes directly against that manifest checksum would therefore
    fail on every healthy bundle — reverse the one substitution before
    hashing so this checks the same bytes the manifest describes.
    """
    missing = []
    wrong = []
    skipped = 0
    checked = 0
    root_bytes = root.encode()
    placeholder_bytes = ROOT_PLACEHOLDER.encode()
    for rel, want in manifest.items():
        if not want:
            # Nothing to check this against. Keys and the changelog blobs used
            # to land here, which meant --verify could not detect a missing or
            # corrupted *key* — the one artefact that makes every package
            # beside it verifiable. They now carry a real checksum, so this
            # branch should be empty; it is counted rather than hidden so that
            # if anything ever lands here again, the summary says so instead of
            # quietly inflating the "verified" total.
            skipped += 1
            continue
        checked += 1
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            missing.append(rel)
            continue
        h = hashlib.sha256()
        if _nginx_root_config(rel):
            # These files are tiny generated configs — reading the whole
            # thing to reverse one substitution is fine; nothing else here
            # gets that treatment, so the streaming read below still bounds
            # memory for every real payload file.
            with open(path, "rb") as fh:
                data = fh.read()
            h.update(data)
            if h.hexdigest() != want:
                # Older bundles embedded the host root; current configs use
                # the fixed container path and already equal the recorded bytes.
                h = hashlib.sha256(data.replace(root_bytes, placeholder_bytes))
        else:
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
    note = "" if not skipped else "  (%d entries carried no checksum)" % skipped
    sys.stdout.write("verified %d files OK%s\n" % (checked, note))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Apply an mmw bundle.")
    ap.add_argument("bundle", nargs="?")
    ap.add_argument("--root", required=True,
                    help="directory the mirror is served from")
    ap.add_argument("--status", action="store_true",
                    help="print the snapshot this mirror is at, and exit")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true",
                    help="hash the whole tree against the manifest afterwards")
    ap.add_argument("--tls", action="store_true",
                    help="turn HTTPS on (or off) from the certificate in ROOT/.mmw/tls/, "
                         "and exit — after signing with sign.py")
    args = ap.parse_args(argv)
    if args.tls:
        notes = https_state(args.root) if os.path.isdir(args.root) else []
        if not notes:
            if os.path.isfile(os.path.join(args.root, ".mmw", "tls", HTTPS_CONF)):
                notes = ["HTTPS already enabled — TLS unchanged"]
            else:
                notes = ["no TLS changes in %s/.mmw/tls/" % args.root]
        for line in notes:
            sys.stdout.write("%s\n" % line)
        return 0
    if args.status:
        at = read_state(args.root)
        if at is None:
            sys.stdout.write("no bundle applied yet at %s\n" % args.root)
        else:
            sys.stdout.write("at %s\n" % _named(at, read_state_label(args.root)))
        return 0
    if not args.bundle:
        ap.error("a bundle to apply is required (or --status)")
    if not args.dry_run:
        os.makedirs(args.root, exist_ok=True)
    rc = apply(args.bundle, args.root, args.dry_run, args.verify)
    if rc == 0 and not args.dry_run:
        for line in https_state(args.root):
            sys.stdout.write("%s\n" % line)
    return rc


if __name__ == "__main__":
    sys.exit(main())
