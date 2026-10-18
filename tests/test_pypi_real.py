"""Real wheels and sdists for the client tests: they must really install and build."""
import base64
import hashlib
import io
import subprocess
import zipfile
from pathlib import Path

from uv import find_uv_bin

from pypi_real import FLIT_CORE_SHA256, committed_wheel, real_flit_sdist, real_wheel

REAL = Path(__file__).parent / "fixtures" / "pypi" / "real"


def _uv(*args, cwd=None):
    return subprocess.run([find_uv_bin(), *args], capture_output=True, text=True, cwd=cwd, timeout=300)


def test_real_wheel_installs(tmp_path):
    name, data, meta = real_wheel("mmw-b", "1.0")
    (tmp_path / name).write_bytes(data)
    r = _uv("pip", "install", "--no-index", "--target", str(tmp_path / "t"), str(tmp_path / name),
            "--python", "python3")
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "t" / "mmw_b.py").read_text().strip() == '__version__ = "1.0"'
    assert b"Name: mmw-b" in meta and b"Version: 1.0" in meta


def test_real_wheel_record_hashes_are_correct():
    _, data, _ = real_wheel("mmw-a", "1.0", requires=["mmw-b<2"])
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        record = z.read("mmw_a-1.0.dist-info/RECORD").decode().splitlines()
        for line in record:
            path, digest, size = line.rsplit(",", 2)
            if not digest:
                continue
            body = z.read(path)
            want = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(body).digest()).rstrip(b"=").decode()
            assert digest == want and int(size) == len(body)
        assert "Requires-Dist: mmw-b<2" in z.read("mmw_a-1.0.dist-info/METADATA").decode()


def test_flit_sdist_builds_offline(tmp_path):
    name, data = real_flit_sdist("mmw-c", "1.0")
    (tmp_path / name).write_bytes(data)
    r = _uv("build", "--wheel", "--no-index", "--find-links", str(REAL), "--out-dir", str(tmp_path / "out"),
            str(tmp_path / name))
    assert r.returncode == 0, r.stderr
    assert [p.name for p in (tmp_path / "out").glob("*.whl")] == ["mmw_c-1.0-py3-none-any.whl"]


def test_committed_flit_core_is_the_recorded_wheel():
    name, data, meta = committed_wheel("flit_core-3.9.0-py3-none-any.whl")
    assert hashlib.sha256(data).hexdigest() == FLIT_CORE_SHA256
    assert b"Name: flit_core" in meta
