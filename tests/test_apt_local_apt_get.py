"""Real apt-get against a published folder repository, flat and structured.

Runs wherever apt-get, gpg and gpgv are installed (CI's ubuntu-latest), as
the current user, in an isolated apt root — no system apt state is touched.
"""
import functools
import hashlib
import http.server
import shutil
import subprocess
import threading

import pytest

from debs import make_deb
from mmw.core import config
from mmw.providers import apt_local
from test_apt_local import _sync_local

pytestmark = pytest.mark.skipif(
    any(shutil.which(t) is None for t in ("apt-get", "gpg", "gpgv")),
    reason="needs apt-get, gpg and gpgv")


@pytest.fixture
def served(tmp_path):
    """An HTTP server on 127.0.0.1; yields (port, set_root)."""
    state = {"root": tmp_path}

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(state["root"]), **kw)

        def log_message(self, *_a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], functools.partial(state.__setitem__, "root")
    server.shutdown()


def _apt(root, *args, cwd=None):
    opts = [f"-oDir={root}", f"-oDir::State::status={root}/status", "-oDebug::NoLocking=1",
            f"-oDir::Etc::sourcelist={root}/none.list", f"-oDir::Etc::sourceparts={root}/sources.list.d",
            f"-oDir::Etc::trustedparts={root}/trusted.gpg.d", "-oAPT::Architecture=amd64",
            "-oAPT::Architectures=amd64", "-oAcquire::Languages=none"]
    return subprocess.run(["apt-get", *opts, *args], capture_output=True, text=True, cwd=cwd, timeout=120)


def _client(tmp_path, src, unit, pub, fmt):
    root = tmp_path / "client"
    for d in ("sources.list.d", "trusted.gpg.d", "var/lib/apt/lists/partial",
              "var/cache/apt/archives/partial", "etc/apt/apt.conf.d", "etc/apt/preferences.d"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "status").write_text("")
    (root / "none.list").write_text("")
    keyring = root / "tools.gpg"
    shutil.copy(pub / f"keys/{unit.label}/tools.gpg", keyring)
    text = apt_local.sources_file(src, fmt).replace("/etc/apt/keyrings/tools.gpg", str(keyring))
    (root / "sources.list.d" / ("tools.sources" if fmt == "deb822" else "tools.list")).write_text(text)
    return root


@pytest.mark.parametrize("structured,fmt", [(False, "deb822"), (True, "list")])
def test_apt_get_update_and_download(tmp_path, served, structured, fmt):
    port, set_root = served
    deb = make_deb(tmp_path / "debs", "tool", "1.0")
    make_deb(tmp_path / "debs", "doc", "1.0", "all")
    src = config.LocalAptSource(provider="apt-local", url=f"http://127.0.0.1:{port}/apt",
                                folder=str(tmp_path / "debs"), key="tools", structured=structured)
    unit, summary, pub = _sync_local(tmp_path, src)
    assert summary.ok
    set_root(pub / f"127.0.0.1_{port}")
    root = _client(tmp_path, src, unit, pub, fmt)

    update = _apt(root, "update")
    assert update.returncode == 0, update.stdout + update.stderr
    assert not [ln for ln in (update.stdout + update.stderr).splitlines()
                if ln.startswith(("W:", "E:"))], update.stdout + update.stderr

    out = tmp_path / "got"
    out.mkdir()
    got = _apt(root, "download", "tool", "doc", cwd=out)
    assert got.returncode == 0, got.stdout + got.stderr
    fetched = out / "tool_1.0_amd64.deb"
    assert hashlib.sha256(fetched.read_bytes()).digest() == hashlib.sha256(deb.read_bytes()).digest()
    assert (out / "doc_1.0_all.deb").is_file()


def test_a_tampered_index_is_refused(tmp_path, served):
    port, set_root = served
    make_deb(tmp_path / "debs", "tool", "1.0")
    src = config.LocalAptSource(provider="apt-local", url=f"http://127.0.0.1:{port}/apt",
                                folder=str(tmp_path / "debs"), key="tools")
    unit, _, pub = _sync_local(tmp_path, src)
    base = pub / f"127.0.0.1_{port}"
    set_root(base)
    for name in ("Packages", "Packages.gz", "Packages.xz"):
        target = base / "apt" / name
        data = target.read_bytes()
        target.unlink()                       # a hardlink into the pool: never write through it
        target.write_bytes(data + b"\n" if name == "Packages" else b"x" + data[1:])
    root = _client(tmp_path, src, unit, pub, "deb822")
    update = _apt(root, "update")
    text = update.stdout + update.stderr
    assert update.returncode != 0 and "Hashes of expected file" in text, text
