"""A served tree's own rules, shared by every way mmw writes one (publish,
`mmw site apply`, adoption): which paths are safe, where TLS material goes
and what HTTPS needs, nginx's root placeholder, derived pages
(`render_derived`), and pruning the directories a removal empties."""
import os
import posixpath
from pathlib import Path

from mmw.core import disk_io

try:
    import ssl          # checks a certificate fits its key; optional
except ImportError:     # a Python built without OpenSSL
    ssl = None

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


def _prune_empty_parents(root, start) -> None:
    """Remove directories left empty by a deletion, upward to `root`.

    A retired suite or component otherwise leaves its whole directory skeleton
    behind, and the generated nginx config sets `autoindex on` — so clients
    browse a tree of empty directories that accumulates with every suite
    rotation and never goes away. Stops at `root`, stops at the first
    non-empty directory, and never touches `.mmw/` (or any dot directory).
    """
    root, current = Path(os.path.abspath(root)), Path(os.path.abspath(start))
    while root in current.parents:
        if current.name.startswith("."):
            return
        try:
            if any(disk_io.ops.iterdir(current)):
                return
            disk_io.ops.rmdir(current)
        except OSError:
            return
        current = current.parent


def _nginx_root_config(rel: str) -> bool:
    # nginx/ also holds a potentially large Docker image. Never buffer or
    # rewrite binary payloads while substituting generated config paths.
    return rel.startswith("nginx/") and rel.endswith(".conf")


def _install_tls(root, name: str, src) -> None:
    """Write one TLS file to {root}/.mmw/tls/{name}, staged and renamed.

    The private key is created 0600 from its first byte — never written
    0644 and tightened afterwards — and the directory is 0700, so the key is
    not readable by anyone but the applying user (and root, i.e. nginx's
    master process) at any moment.
    """
    d = Path(root) / ".mmw" / "tls"
    if disk_io.ops.is_symlink(d):
        raise OSError(f"{d} is a symlink; refusing to write a private key through it")
    disk_io.ops.mkdir(d, parents=True, exist_ok=True)
    disk_io.ops.chmod(d, 0o700)
    mode = 0o600 if name.endswith(".key") else 0o644
    dest = d / name
    tmp = d / (name + ".tmp")
    disk_io.ops.unlink(tmp, missing_ok=True)
    fd = disk_io.ops.open_fd(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with disk_io.ops.open_file(fd, "wb") as out:
        while chunk := src.read(1 << 20):
            out.write(chunk)
    disk_io.ops.chmod(tmp, mode)
    disk_io.ops.replace(tmp, dest)


def _cnf_hosts(path) -> set[str]:
    """The hostnames a request's OpenSSL config asks for (`DNS.n = host`)."""
    hosts = set()
    for line in disk_io.ops.read_text(Path(path)).splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip().startswith("DNS."):
            hosts.add(value.strip())
    return hosts


def _cert_hosts(crt) -> set[str] | None:
    """The certificate's DNS names, or None if this Python cannot read them."""
    try:
        decoded = ssl._ssl._test_decode_cert(str(crt))
    except Exception:  # noqa: BLE001 — a private helper; absent means "can't tell"
        return None
    return {v for k, v in decoded.get("subjectAltName", ()) if k == "DNS"}


def https_state(root) -> list[str]:
    """Turn HTTPS on or off in the tree's nginx config, from what is in
    {root}/.mmw/tls/: on when a certificate fits the key, off otherwise.
    Returns the lines to tell the operator (empty for a network without TLS)."""
    d = Path(root) / ".mmw" / "tls"
    if not disk_io.ops.is_dir(d):
        return []
    crt, key, conf = d / "mirror.crt", d / "mirror.key", d / HTTPS_CONF
    sign = "mmw site certs (a publish tree: mmw certs on the connected side)"
    want, notes = None, []
    if disk_io.ops.is_file(crt) and disk_io.ops.is_file(key):
        problem = None
        if ssl is not None:
            try:
                ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(str(crt), str(key))
            except (ssl.SSLError, OSError) as e:
                problem = f"mirror.crt does not fit mirror.key ({e})"
        if problem:
            notes.append(f"HTTPS off: {problem} — re-sign: {sign}")
        else:
            want = ("# Written by mmw: mirror.crt here fits mirror.key.\n"
                    "listen 443 ssl;\nssl_certificate /srv/mmw/.mmw/tls/mirror.crt;\n"
                    "ssl_certificate_key /srv/mmw/.mmw/tls/mirror.key;\n")
            cnf = d / "mirror.cnf"
            have = _cert_hosts(crt)
            if have is not None and disk_io.ops.is_file(cnf):
                missing = sorted(_cnf_hosts(cnf) - have)
                if missing:
                    notes.append(f"the certificate does not cover {', '.join(missing)} — re-sign: {sign}")
    elif disk_io.ops.is_file(d / "mirror.csr"):
        notes.append(f"HTTPS waits for a signed certificate: {sign}")
    current = disk_io.ops.read_text(conf) if disk_io.ops.is_file(conf) else None
    if want != current:
        if want is None:
            disk_io.ops.unlink(conf)
            notes.append("HTTPS off — recreate the nginx container")
        else:
            disk_io.ops.atomic_write(conf, want.encode())
            notes.append("HTTPS on — recreate the nginx container")
    return notes


# Derived trees: (renderer shipped in the tree, the directory it owns). The
# one rule every side runs — live publish, snapshot replay, and the far side
# after a bundle — so a far side's pages are the near side's, byte for byte.
DERIVED = (("pypi-data/render.py", "pypi.org", ("PYPI.md",)),)
RENDERED_MARK = ".mmw-rendered"


def render_derived(root, say=None, **io) -> None:
    """Run each shipped renderer; with none left, remove the directory it
    owned (only if it carries the renderer's mark — never someone else's).
    `io` (`write_pages`, `stop`) is mmw's own disk layer, passed through."""
    import runpy
    root = Path(root)
    for script, owned, companions in DERIVED:
        renderer = root / script
        if disk_io.ops.is_file(renderer):
            runpy.run_path(str(renderer), run_name="mmw_render")["render"](str(root), say=say, **io)
        elif disk_io.ops.is_dir(root / owned / RENDERED_MARK):
            disk_io.ops.rmtree(root / owned)
            for name in companions:          # its far-side notes go with it
                disk_io.ops.unlink(root / name, missing_ok=True)
