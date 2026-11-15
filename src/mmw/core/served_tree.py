"""A served tree's own rules, shared by every way mmw writes one (publish,
`mmw site apply`, adoption): which paths are safe, where TLS material goes
and what HTTPS needs, nginx's root placeholder, derived pages
(`render_derived`), and pruning the directories a removal empties."""
import hashlib
import os
import posixpath
import shutil
import sys

try:
    import ssl          # checks a certificate fits its key; optional
except ImportError:     # a Python built without OpenSSL
    ssl = None

MMW_DIR = ".mmw"
MANIFEST_MEMBER = ".mmw/manifest.jsonl"
# A `certs` network's certificate and private key. Installed under
# {root}/.mmw/tls/ — never a served path — and referenced from there by the
# bundle's own nginx/mirror.conf.
TLS_PREFIX = ".mmw/tls/"
# Written in {root}/.mmw/tls/ once a signed certificate fits the key; every
# server block of the bundle's nginx config includes `https*.conf` there, a
# wildcard nginx ignores while nothing matches it.
HTTPS_CONF = "https.conf"
ROOT_PLACEHOLDER = "__MMW_ROOT__"

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


def _nginx_root_config(rel):
    # nginx/ also holds a potentially large Docker image. Never buffer or
    # rewrite binary payloads while substituting generated config paths.
    return rel.startswith("nginx/") and rel.endswith(".conf")


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
    sign = "mmw site certs (a publish tree: mmw certs on the connected side)"
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
            want = ("# Written by mmw: mirror.crt here fits mirror.key.\n"
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
DERIVED = (("pypi-data/render.py", "pypi.org", ("PYPI.md",)),)
RENDERED_MARK = ".mmw-rendered"


def render_derived(root, say=None, **io):
    """Run each shipped renderer; with none left, remove the directory it
    owned (only if it carries the renderer's mark — never someone else's).
    `io` (`write_pages`, `stop`) is mmw's own disk layer on the near side;
    the far side runs the renderer's stdlib defaults."""
    import runpy
    for script, owned, companions in DERIVED:
        renderer = os.path.join(root, script)
        if os.path.isfile(renderer):
            runpy.run_path(renderer, run_name="mmw_render")["render"](root, say=say, **io)
        elif os.path.isdir(os.path.join(root, owned, RENDERED_MARK)):
            shutil.rmtree(os.path.join(root, owned))
            for name in companions:          # its far-side notes go with it
                path = os.path.join(root, name)
                if os.path.isfile(path):
                    os.remove(path)
