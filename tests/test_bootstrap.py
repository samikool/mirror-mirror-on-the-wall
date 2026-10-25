"""bootstrap.sh: installs mmw offline from a bundle's runtime, with only /bin/sh."""
import io
import json
import os
import platform
import shutil
import subprocess
import tarfile

import pytest

from mmw.resources import BOOTSTRAP

ARCH = {"arm64": "aarch64"}.get(platform.machine(), platform.machine())
OTHER = "aarch64" if ARCH == "x86_64" else "x86_64"

# Stands in for uv: logs each call, makes what the real one would.
STUB_UV = r"""#!/bin/sh
echo "$* UV_CACHE_DIR=$UV_CACHE_DIR" >> "$STUB_UV_LOG"
case "$1" in
venv)
    for a; do last=$a; done
    mkdir -p "$last/bin" && ln -s "$3" "$last/bin/python" ;;
pip)
    [ "${STUB_UV_FAIL:-}" = pip ] && { echo "disk full" >&2; exit 1; }
    while [ $# -gt 0 ]; do [ "$1" = --python ] && py=$2; shift; done
    venv=${py%/bin/python}
    printf '#!/bin/sh\necho mmw-stub "$MMW_SITE_HOME" "$@"\n' > "$venv/bin/mmw" && chmod +x "$venv/bin/mmw" ;;
esac
"""


def _runtime(tmp_path, arch=ARCH, version="1.2.0"):
    """A bundle's .mmw/runtime/, with a stub uv and a stub interpreter."""
    rt = tmp_path / "runtime"
    a = rt / arch
    (a / "wheels").mkdir(parents=True)
    (a / "uv").write_text(STUB_UV)
    (a / "uv").chmod(0o755)
    (a / "wheels" / f"mirror_mirror_on_the_wall-{version}-py3-none-any.whl").write_bytes(b"wheel")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"#!/bin/sh\necho python\n"
        info = tarfile.TarInfo("python/bin/python3")
        info.size, info.mode = len(data), 0o755
        tar.addfile(info, io.BytesIO(data))
    (a / f"cpython-3.12.9-{arch}-unknown-linux-gnu-install_only_stripped.tar.gz").write_bytes(buf.getvalue())
    (a / "RUNTIME.json").write_text(json.dumps({"mmw": version, "arch": arch}))
    (rt / "RUNTIME.json").write_text(json.dumps({"mmw": version, "arches": [arch]}))
    shutil.copy(BOOTSTRAP, rt / "bootstrap.sh")
    return rt


def _run(rt, home, **env):
    log = rt.parent / "uv.log"
    e = {**os.environ, "STUB_UV_LOG": str(log), **env}
    e.pop("MMW_SITE_HOME", None)
    r = subprocess.run(["sh", str(rt / "bootstrap.sh"), "--home", str(home)],
                       capture_output=True, text=True, env=e)
    return r, (log.read_text() if log.exists() else "")


def test_bootstrap_installs_with_stub_uv(tmp_path):
    rt, home = _runtime(tmp_path), tmp_path / "site"
    r, log = _run(rt, home)
    assert r.returncode == 0, r.stderr
    assert f"mmw 1.2.0 installed — {home}/bin/mmw" in r.stdout
    assert "--offline" in log and "--no-index" in log and "mirror-mirror-on-the-wall==1.2.0" in log
    assert f"UV_CACHE_DIR={home}/" in log                          # nothing written outside --home
    version, python, name = (home / "runtime/CURRENT").read_text().split()
    assert (version, python) == ("1.2.0", "3.12") and name.startswith("1.2.0-py3.12-")
    assert (home / f"runtime/{name}-{ARCH}/python/bin/python3").exists()
    out = subprocess.run([str(home / "bin/mmw"), "site", "show"], capture_output=True, text=True).stdout
    assert out.split() == ["mmw-stub", str(home), "site", "show"]   # the launcher knows its site
    assert not list((home / "runtime").glob("*.tmp")) and not (home / "runtime/.uv-cache").exists()


def test_bootstrap_wrong_arch(tmp_path):
    rt, home = _runtime(tmp_path, arch=OTHER), tmp_path / "site"
    r, _log = _run(rt, home)
    assert r.returncode == 2
    assert f"this bundle has runtimes for: {OTHER} — this machine is {ARCH}" in r.stderr
    assert not home.exists()


def test_bootstrap_is_posix_sh():
    for shell in ("sh", "dash"):
        if shutil.which(shell):
            subprocess.run([shell, "-n", str(BOOTSTRAP)], check=True)
    if shutil.which("checkbashisms"):
        subprocess.run(["checkbashisms", str(BOOTSTRAP)], check=True)


@pytest.mark.skipif(not shutil.which("dash"), reason="no dash to run it under")
def test_bootstrap_runs_under_dash(tmp_path):
    rt, home = _runtime(tmp_path), tmp_path / "site"
    r = subprocess.run(["dash", str(rt / "bootstrap.sh"), "--home", str(home)], capture_output=True, text=True,
                       env={**os.environ, "STUB_UV_LOG": str(tmp_path / "uv.log")})
    assert r.returncode == 0, r.stderr


def test_bootstrap_failure_leaves_no_partial(tmp_path):
    rt, home = _runtime(tmp_path, version="1.3.0"), tmp_path / "site"
    (home / "bin").mkdir(parents=True)
    (home / "bin/mmw").write_text("#!/bin/sh\necho old\n")
    r, _log = _run(rt, home, STUB_UV_FAIL="pip")
    assert r.returncode != 0 and "disk full" in r.stderr
    assert (home / "bin/mmw").read_text() == "#!/bin/sh\necho old\n"
    assert sorted(p.name for p in (home / "runtime").iterdir()) == []      # no install, no CURRENT


def test_bootstrap_again_is_quick(tmp_path):
    rt, home = _runtime(tmp_path), tmp_path / "site"
    _run(rt, home)
    r, log = _run(rt, home)
    assert r.returncode == 0 and log.count("pip install") == 1    # already installed: only the launcher


def test_launcher_refuses_another_arch(tmp_path):
    """A home on a share, mounted by machines of two architectures: each
    machine's launcher runs only what was installed for it."""
    rt, home = _runtime(tmp_path), tmp_path / "site"
    _run(rt, home)
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "uname").write_text(f"#!/bin/sh\necho {OTHER}\n")
    (fake / "uname").chmod(0o755)
    r = subprocess.run([str(home / "bin/mmw"), "site", "show"], capture_output=True, text=True,
                       env={**os.environ, "PATH": f"{fake}:{os.environ['PATH']}"})
    assert r.returncode == 2
    assert f"mmw 1.2.0 isn't installed here for {OTHER}" in r.stderr and "bootstrap.sh" in r.stderr


def test_a_rebuilt_wheel_installs_beside(tmp_path):
    """The same version, rebuilt: a new install beside the old one, which
    keeps working until the launcher switches."""
    rt, home = _runtime(tmp_path), tmp_path / "site"
    _run(rt, home)
    first = (home / "runtime/CURRENT").read_text().split()[2]
    (rt / ARCH / "wheels/mirror_mirror_on_the_wall-1.2.0-py3-none-any.whl").write_bytes(b"rebuilt")
    r, log = _run(rt, home)
    second = (home / "runtime/CURRENT").read_text().split()[2]
    assert r.returncode == 0 and log.count("pip install") == 2 and first != second
    assert (home / f"runtime/{first}-{ARCH}/.complete").exists()
    assert (home / f"runtime/{second}-{ARCH}/mmw.whl").read_bytes() == b"rebuilt"


@pytest.mark.parametrize("bad", ['si"te', "si$te", "si`te`", "si\\te", "si\nte"])
def test_a_home_the_launcher_cannot_hold_is_refused(tmp_path, bad):
    rt, home = _runtime(tmp_path), tmp_path / bad
    r, log = _run(rt, home)
    assert r.returncode == 64 and "can't hold" in r.stderr
    assert not home.exists() and log == ""
