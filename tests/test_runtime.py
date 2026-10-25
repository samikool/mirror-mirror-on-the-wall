"""mmw's own runtime for the far side: uv, an interpreter and wheels per target."""
import json
from pathlib import Path

import pytest

from mmw.core import runtime as R


class FakeFetcher:
    """Every download, offline. Records what was asked for."""

    def __init__(self, on_index=True):
        self.on_index = on_index
        self.calls = []

    def uv_binary(self, version, arch):
        self.calls.append(("uv", version, arch))
        return b"#!/bin/sh\necho fake uv\n"

    def python_archive(self, python, arch):
        self.calls.append(("python", python, arch))
        return f"cpython-{python}.9-{arch}-unknown-linux-gnu-install_only_stripped.tar.gz", b"tarball"

    def wheels(self, requirement, python, arch, dest):
        self.calls.append(("wheels", requirement, python, arch))
        if not self.on_index and requirement.startswith(R.DIST):
            raise R.NotOnIndex(requirement)
        dest.mkdir(parents=True, exist_ok=True)
        out = [dest / f"cryptography-45.0-cp311-abi3-manylinux_2_28_{arch}.whl"]
        if not requirement.startswith(R.DIST):                     # a local wheel: pip copies it too
            out.append(dest / Path(requirement).name)
        else:
            out.append(dest / "mirror_mirror_on_the_wall-1.2.0-py3-none-any.whl")
        for p in out:
            p.write_bytes(p.name.encode())
        return out


def test_build_runtime_with_fakes(tmp_path):
    f = FakeFetcher()
    d = R.build_runtime(tmp_path, arch="aarch64", python="3.12", mmw_version="1.2.0", fetcher=f)
    assert d == tmp_path / "1.2.0-py3.12-aarch64"
    assert (d / "uv").read_bytes().startswith(b"#!") and (d / "uv").stat().st_mode & 0o111
    assert (d / "cpython-3.12.9-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz").exists()
    assert sorted(p.name for p in (d / "wheels").iterdir()) == [
        "cryptography-45.0-cp311-abi3-manylinux_2_28_aarch64.whl",
        "mirror_mirror_on_the_wall-1.2.0-py3-none-any.whl"]
    meta = json.loads((d / "RUNTIME.json").read_text())
    assert meta["mmw"] == "1.2.0" and meta["python"] == "3.12" and meta["arch"] == "aarch64"
    assert set(meta["files"]) == {"uv", "cpython-3.12.9-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz",
                                  "wheels/cryptography-45.0-cp311-abi3-manylinux_2_28_aarch64.whl",
                                  "wheels/mirror_mirror_on_the_wall-1.2.0-py3-none-any.whl"}
    assert ("wheels", f"{R.DIST}==1.2.0", "3.12", "aarch64") in f.calls


def test_build_is_cached(tmp_path):
    R.build_runtime(tmp_path, arch="x86_64", python="3.12", mmw_version="1.2.0", fetcher=FakeFetcher())
    again = FakeFetcher()
    d = R.build_runtime(tmp_path, arch="x86_64", python="3.12", mmw_version="1.2.0", fetcher=again)
    assert again.calls == []
    (d / "uv").write_bytes(b"tampered")                            # a cache that doesn't verify is rebuilt
    third = FakeFetcher()
    R.build_runtime(tmp_path, arch="x86_64", python="3.12", mmw_version="1.2.0", fetcher=third)
    assert third.calls and (d / "uv").read_bytes().startswith(b"#!")


def test_dev_version_needs_a_wheel(tmp_path):
    with pytest.raises(ValueError, match=r"isn't on PyPI — pass --runtime-wheel PATH"):
        R.build_runtime(tmp_path, arch="x86_64", python="3.12", mmw_version="1.3.0.dev0",
                        fetcher=FakeFetcher(on_index=False))
    assert not list(tmp_path.iterdir())                            # nothing half-built left behind
    wheel = tmp_path.parent / "mirror_mirror_on_the_wall-1.3.0.dev0-py3-none-any.whl"
    wheel.write_bytes(b"local")
    f = FakeFetcher(on_index=False)
    d = R.build_runtime(tmp_path, arch="x86_64", python="3.12", mmw_version="1.3.0.dev0", fetcher=f, wheel=wheel)
    assert (d / "wheels" / wheel.name).exists()
    assert ("wheels", str(wheel), "3.12", "x86_64") in f.calls


# ── review minors ────────────────────────────────────────────────────────────

def test_runtime_python_must_run_mmw():
    from mmw.core import config
    with pytest.raises(ValueError, match="mmw needs Python >=3.10"):
        config.NetworkConfig(name="x", runtime_python="3.9")
    assert config.NetworkConfig(name="x", runtime_python="3.13").runtime_python == "3.13"


def test_pip_failures_are_told_apart():
    req = f"{R.DIST}==1.2.0"
    miss = f"ERROR: No matching distribution found for {req}\n"
    offline = ("WARNING: Retrying (Retry(total=4)) after connection broken by 'NewConnectionError(...: "
               "Failed to establish a new connection: [Errno -3] Temporary failure in name resolution')\n" + miss)
    too_old = ("ERROR: Ignored the following versions that require a different python version: "
               "1.2.0 Requires-Python >=3.10\n" + miss)
    assert isinstance(R.pip_failure(miss, req, "3.12", "x86_64"), R.NotOnIndex)
    e = R.pip_failure(offline, req, "3.12", "x86_64")
    assert isinstance(e, R.FetchError) and "couldn't reach the index" in str(e)
    e = R.pip_failure(too_old, req, "3.9", "x86_64")
    assert isinstance(e, R.RuntimeUnavailable) and "doesn't run on Python 3.9" in str(e)
    e = R.pip_failure("ERROR: something else\n", req, "3.12", "x86_64")
    assert isinstance(e, R.FetchError) and "something else" in str(e)
