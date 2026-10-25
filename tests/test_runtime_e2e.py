"""The real thing: build mmw's runtime from PyPI and python-build-standalone,
then bootstrap it in a RHEL 8 container with no Python and no network.

MMW_RUNTIME_E2E=1 enables it; ordinary pytest never downloads or starts
Docker. MMW_RUNTIME_E2E_IMAGE overrides the image (default rockylinux:8).
"""
import os
import platform
import shutil
import subprocess
from pathlib import Path

import pytest

from mmw.core import config
from mmw.core import runtime as R

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(os.environ.get("MMW_RUNTIME_E2E") != "1",
                       reason="opt-in real downloads and Docker: MMW_RUNTIME_E2E=1"),
]
IMAGE = os.environ.get("MMW_RUNTIME_E2E_IMAGE", "rockylinux:8")
ARCH = {"arm64": "aarch64"}.get(platform.machine(), platform.machine())


def test_bootstrap_in_rocky8(tmp_path):
    repo = Path(__file__).resolve().parent.parent
    subprocess.run(["uv", "build", "--wheel", "-o", str(tmp_path / "dist")], cwd=repo, check=True,
                   capture_output=True)
    (wheel,) = (tmp_path / "dist").glob("mirror_mirror_on_the_wall-*.whl")
    net = config.NetworkConfig(name="e2e", far_side="mmw", runtime_arches=[ARCH])
    members = R.runtime_files(net, None, tmp_path / "pool", fetcher=R.RealFetcher(), wheel=wheel)
    rt = tmp_path / "bundle"
    for name, data in members.items():
        dest = rt / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            dest.write_bytes(data)
        else:
            shutil.copy(data, dest)
    script = ("set -e; ! command -v python3; sh /b/.mmw/runtime/bootstrap.sh --home /site; "
              "/site/bin/mmw --help >/dev/null; /site/bin/mmw site init --root /srv/oak; "
              "/site/bin/mmw site status")
    r = subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", f"{rt}:/b:ro", IMAGE,
                        "sh", "-c", script], capture_output=True, text=True, timeout=900)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"mmw {R.wanted(net, wheel)['mmw']} installed — /site/bin/mmw" in r.stdout
