"""Real apt-get against an upstream mirror a network added custom debs to."""
import functools
import hashlib
import http.server
import shutil
import threading

import pytest

from debs import make_deb
from mmw.core.signing import Keyring
from test_apt_local_apt_get import _apt
from test_layers import _layer, _publish, lab  # noqa: F401 — the fixture

pytestmark = pytest.mark.skipif(any(shutil.which(t) is None for t in ("apt-get", "gpg", "gpgv")),
                                reason="needs apt-get, gpg and gpgv")


def _serve(directory):
    handler = functools.partial(_Quiet, directory=str(directory))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_a):
        pass


def _client(tmp_path, entry):
    root = tmp_path / "client"
    for d in ("sources.list.d", "trusted.gpg.d", "var/lib/apt/lists/partial",
              "var/cache/apt/archives/partial", "etc/apt/apt.conf.d", "etc/apt/preferences.d"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "status").write_text("")
    (root / "none.list").write_text("")
    (root / "sources.list.d" / "ubuntu.list").write_text(entry)
    return root


@pytest.mark.parametrize("signing", ["resign", "unsigned"])
def test_apt_get_installs_a_custom_deb_from_the_layered_mirror(lab, signing):  # noqa: F811
    ours = make_deb(lab.folder / "jammy/main", "tools-ours", "1.0")
    layer = _layer(lab) if signing == "resign" else _layer(lab, signing="unsigned", key=None)
    _publish(lab, layer)
    server = _serve(lab.tmp / "pub" / lab.base.split("/")[0])
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/ubuntu"
        (lab.tmp / "kh").mkdir(exist_ok=True, mode=0o700)
        if signing == "resign":
            keyring = lab.tmp / "mmw-key.gpg"
            import subprocess
            subprocess.run(["gpg", "--batch", "--yes", "--homedir", str(lab.tmp / "kh"), "--dearmor",
                            "-o", str(keyring)], input=Keyring(lab.mirror_dir).public_armor("tools").encode(),
                           check=True, capture_output=True)
            entry = f"deb [signed-by={keyring}] {url} jammy main universe\n"
        else:
            entry = f"deb [trusted=yes] {url} jammy main universe\n"
        root = _client(lab.tmp, entry)
        update = _apt(root, "update")
        text = update.stdout + update.stderr
        assert update.returncode == 0, text
        assert not [ln for ln in text.splitlines() if ln.startswith("E:")], text
        out = lab.tmp / "got"
        out.mkdir()
        got = _apt(root, "download", "tools-ours", "hello", cwd=out)
        assert got.returncode == 0, got.stdout + got.stderr
        assert hashlib.sha256((out / "tools-ours_1.0_amd64.deb").read_bytes()).digest() == \
            hashlib.sha256(ours.read_bytes()).digest()
    finally:
        server.shutdown()
