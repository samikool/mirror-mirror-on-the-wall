"""mmw's installer: `mmw-<version>-<arch>.tar`, mmw's runtime on its own."""
import hashlib
import tarfile

import pytest

from mmw.core import installer as I
from mmw.core import runtime as R

from test_runtime import FakeFetcher


def _write(tmp_path, **kw):
    kw.setdefault("arches", ["x86_64"])
    return I.write_installers(tmp_path / "out", python="3.12", cache=tmp_path / "cache",
                              fetcher=kw.pop("fetcher", FakeFetcher()), **kw)


def test_installer_name():
    assert I.installer_name("1.2.0", "x86_64") == "mmw-1.2.0-x86_64.tar"


def test_installer_layout(tmp_path):
    (path,) = _write(tmp_path)
    assert path == tmp_path / "out" / "mmw-1.2.0-x86_64.tar"
    with tarfile.open(path) as t:
        members = {m.name: m for m in t.getmembers()}
    assert all(n.startswith("mmw-1.2.0/") for n in members)
    assert members["mmw-1.2.0/install.sh"].mode & 0o777 == 0o755
    for name in ("uv", "RUNTIME.json", "cpython-3.12.9-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz",
                 "wheels/mirror_mirror_on_the_wall-1.2.0-py3-none-any.whl"):
        assert f"mmw-1.2.0/x86_64/{name}" in members


def test_one_tar_per_arch(tmp_path):
    paths = _write(tmp_path, arches=["x86_64", "aarch64"])
    assert [p.name for p in paths] == ["mmw-1.2.0-x86_64.tar", "mmw-1.2.0-aarch64.tar"]


def test_installer_written_atomically(tmp_path):
    class Broken(FakeFetcher):
        def wheels(self, *a):
            raise R.FetchError("no network")
    with pytest.raises(R.FetchError):
        _write(tmp_path, fetcher=Broken())
    assert not list((tmp_path / "out").glob("mmw-*")) if (tmp_path / "out").exists() else True


def test_installer_carries_the_given_wheel(tmp_path):
    wheel = tmp_path / "mirror_mirror_on_the_wall-1.3.0.dev1-py3-none-any.whl"
    wheel.write_bytes(b"a dev build")
    (path,) = _write(tmp_path, wheel=wheel)
    assert path.name == "mmw-1.3.0.dev1-x86_64.tar"
    with tarfile.open(path) as t:
        data = t.extractfile(f"mmw-1.3.0.dev1/x86_64/wheels/{wheel.name}").read()
    assert hashlib.sha256(data).digest() == hashlib.sha256(b"a dev build").digest()
