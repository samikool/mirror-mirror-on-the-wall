"""install.sh: installs mmw offline onto the path from its installer tar, with only /bin/sh."""
import io
import json
import os
import platform
import shutil
import subprocess
import tarfile

import pytest

from mmw.resources import INSTALL

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
    """An unpacked installer: mmw-<version>/install.sh and <arch>/, with a stub uv and interpreter."""
    rt = tmp_path / f"mmw-{version}"
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
    shutil.copy(INSTALL, rt / "install.sh")
    return rt


def _run(rt, prefix=None, path=None, home=None, shell="sh", **env):
    log = rt.parent / "uv.log"
    e = {**os.environ, "STUB_UV_LOG": str(log), **env}
    e.pop("MMW_SITE_HOME", None)
    if path is not None:
        e["PATH"] = path
    if home is not None:
        e["HOME"] = str(home)
    args = [shell, str(rt / "install.sh")] + (["--prefix", str(prefix)] if prefix is not None else [])
    r = subprocess.run(args, capture_output=True, text=True, env=e)
    return r, (log.read_text() if log.exists() else "")


def _current(prefix):
    return (prefix / "share/mmw/CURRENT").read_text().split()


def test_install_into_prefix(tmp_path):
    rt, prefix = _runtime(tmp_path), tmp_path / "pre"
    r, log = _run(rt, prefix)
    assert r.returncode == 0, r.stderr
    assert f"mmw 1.2.0 installed — {prefix}/bin/mmw" in r.stdout
    assert "--offline" in log and "--no-index" in log and "mirror-mirror-on-the-wall==1.2.0" in log
    version, python, name = _current(prefix)
    assert (version, python) == ("1.2.0", "3.12") and name.startswith("1.2.0-py3.12-")
    assert (prefix / f"share/mmw/{name}-{ARCH}/python/bin/python3").exists()
    out = subprocess.run([str(prefix / "bin/mmw"), "site", "show"], capture_output=True, text=True).stdout
    assert out.split()[0] == "mmw-stub" and out.split()[-2:] == ["site", "show"]
    assert not (prefix / "share/mmw/.uv-cache").exists()


def test_launcher_sets_no_site_home(tmp_path):
    rt, prefix = _runtime(tmp_path), tmp_path / "pre"
    _run(rt, prefix)
    env = {k: v for k, v in os.environ.items() if k != "MMW_SITE_HOME"}
    out = subprocess.run([str(prefix / "bin/mmw"), "x"], capture_output=True, text=True, env=env).stdout
    assert out.split() == ["mmw-stub", "x"]                        # $MMW_SITE_HOME empty
    assert "MMW_SITE_HOME" not in (prefix / "bin/mmw").read_text()


def test_default_prefix_is_home_local(tmp_path):
    rt = _runtime(tmp_path)
    r, _log = _run(rt, home=tmp_path / "h")
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "h/.local/bin/mmw").exists() and (tmp_path / "h/.local/share/mmw/CURRENT").exists()


def test_install_says_when_bin_is_not_on_path(tmp_path):
    rt, prefix = _runtime(tmp_path), tmp_path / "pre"
    r, _log = _run(rt, prefix, path="/usr/bin:/bin")
    assert f"add it to your PATH: export PATH={prefix}/bin:$PATH" in r.stdout
    r, _log = _run(rt, prefix, path=f"{prefix}/bin:/usr/bin:/bin")
    assert "add it to your PATH" not in r.stdout


def test_reinstall_and_upgrade(tmp_path):
    prefix = tmp_path / "pre"
    old = _runtime(tmp_path / "a")
    _run(old, prefix)
    first = _current(prefix)[2]
    r, log = _run(old, prefix)
    assert r.returncode == 0 and log.count("pip install") == 1    # the same file again: quick
    new = _runtime(tmp_path / "b", version="1.3.0")
    r, _log = _run(new, prefix)
    assert r.returncode == 0 and _current(prefix)[0] == "1.3.0"
    assert (prefix / f"share/mmw/{first}-{ARCH}/.complete").exists()  # the old one stays
    assert "1.3.0" in (prefix / "bin/mmw").read_text()


def test_install_wrong_arch(tmp_path):
    rt, prefix = _runtime(tmp_path, arch=OTHER), tmp_path / "pre"
    r, _log = _run(rt, prefix)
    assert r.returncode == 2
    assert f"this installer is for: {OTHER} — this machine is {ARCH}" in r.stderr
    assert not prefix.exists()


def test_install_is_posix_sh():
    for shell in ("sh", "dash"):
        if shutil.which(shell):
            subprocess.run([shell, "-n", str(INSTALL)], check=True)
    if shutil.which("checkbashisms"):
        subprocess.run(["checkbashisms", str(INSTALL)], check=True)


@pytest.mark.skipif(not shutil.which("dash"), reason="no dash to run it under")
def test_install_runs_under_dash(tmp_path):
    rt, prefix = _runtime(tmp_path), tmp_path / "pre"
    r, _log = _run(rt, prefix, shell="dash")
    assert r.returncode == 0, r.stderr


def test_install_failure_leaves_no_partial(tmp_path):
    rt, prefix = _runtime(tmp_path, version="1.3.0"), tmp_path / "pre"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin/mmw").write_text("#!/bin/sh\necho old\n")
    r, _log = _run(rt, prefix, STUB_UV_FAIL="pip")
    assert r.returncode != 0 and "disk full" in r.stderr
    assert (prefix / "bin/mmw").read_text() == "#!/bin/sh\necho old\n"
    assert sorted(p.name for p in (prefix / "share/mmw").iterdir()) == []


def test_launcher_refuses_another_arch(tmp_path):
    rt, prefix = _runtime(tmp_path), tmp_path / "pre"
    _run(rt, prefix)
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "uname").write_text(f"#!/bin/sh\necho {OTHER}\n")
    (fake / "uname").chmod(0o755)
    r = subprocess.run([str(prefix / "bin/mmw"), "x"], capture_output=True, text=True,
                       env={**os.environ, "PATH": f"{fake}:{os.environ['PATH']}"})
    assert r.returncode == 2
    assert f"mmw 1.2.0 isn't installed here for {OTHER}" in r.stderr
    assert f"run install.sh from mmw-1.2.0-{OTHER}.tar" in r.stderr


@pytest.mark.parametrize("bad", ['pr"e', "pr$e", "pr`e`", "pr\\e", "pr\ne"])
def test_a_prefix_the_launcher_cannot_hold_is_refused(tmp_path, bad):
    rt, prefix = _runtime(tmp_path), tmp_path / bad
    r, log = _run(rt, prefix)
    assert r.returncode == 64 and "can't hold" in r.stderr
    assert not prefix.exists() and log == ""
