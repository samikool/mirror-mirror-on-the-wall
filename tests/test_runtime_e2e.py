"""The real thing: build mmw's installer from this checkout, PyPI and
python-build-standalone, then install it in a RHEL 8 container with no
Python and no network.

MMW_RUNTIME_E2E=1 enables it; ordinary pytest never downloads or starts
Docker. MMW_RUNTIME_E2E_IMAGE overrides the image (default rockylinux:8).
"""
import os
import platform
import subprocess

import pytest

from mmw.core import runtime as R
from mmw.core.installer import write_installers

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(os.environ.get("MMW_RUNTIME_E2E") != "1",
                       reason="opt-in real downloads and Docker: MMW_RUNTIME_E2E=1"),
]
IMAGE = os.environ.get("MMW_RUNTIME_E2E_IMAGE", "rockylinux:8")
ARCH = {"arm64": "aarch64"}.get(platform.machine(), platform.machine())


def test_installer_in_rocky8(tmp_path):
    cache = tmp_path / "cache"
    # A checkout ships its own build: `site` exists in it, not in PyPI's 1.2.0.
    (path,) = write_installers(tmp_path / "out", arches=[ARCH], python="3.12", cache=cache,
                               fetcher=R.RealFetcher(), wheel=R.resolve_wheel(None, cache / "build"))
    script = ("set -e; ! command -v python3; tar -xf /b/" + path.name + " -C /tmp; "
              "sh /tmp/mmw-*/install.sh; ~/.local/bin/mmw site --help")
    r = subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", f"{path.parent}:/b:ro", IMAGE,
                        "sh", "-c", script], capture_output=True, text=True, timeout=900)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "installed — /root/.local/bin/mmw" in r.stdout
    assert "apply" in r.stdout and "serve" in r.stdout          # `mmw site --help`: the dev build's commands
