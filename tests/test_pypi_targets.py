"""PyPI targets: which wheels a (python, platform, arch) installs at its OS floor."""
from packaging.utils import parse_wheel_filename

from mmw.providers.pypi.targets import (Floors, Target, TagMatcher, is_universal, marker_env,
                                        requires_python_ok, targets)


def tags(fn):
    return parse_wheel_filename(fn)[3]


T = Target("3.12", "linux-glibc", "amd64")
M = TagMatcher(T, Floors())


def test_empty_means_all_and_keys():
    ts = targets([], [], [])
    assert len(ts) == 6 * 4 * 2
    assert ts[0].key == "3.9-linux-glibc-amd64"
    assert targets(["3.12"], ["windows"], ["arm64"]) == [Target("3.12", "windows", "arm64")]


def test_glibc_floor_decides_installability_not_keeping():
    old = tags("x-1-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl")
    new = tags("x-1-cp312-cp312-manylinux_2_34_x86_64.whl")
    assert M.priority(old) is not None and M.keeps(old)
    assert M.priority(new) is None and M.keeps(new)


def test_legacy_manylinux_aliases():
    assert M.priority(tags("x-1-cp312-cp312-manylinux1_x86_64.whl")) is not None
    assert M.priority(tags("x-1-cp312-cp312-manylinux2010_x86_64.whl")) is not None
    assert not M.keeps(tags("x-1-cp312-cp312-manylinux2014_aarch64.whl"))


def test_abi3_and_pure_and_wrong_python():
    assert M.priority(tags("x-1-cp39-abi3-manylinux_2_28_x86_64.whl")) is not None
    assert M.priority(tags("x-1-py3-none-any.whl")) is not None
    assert not M.keeps(tags("x-1-cp313-cp313-manylinux_2_17_x86_64.whl"))
    assert not M.keeps(tags("x-1-cp313-cp313t-manylinux_2_17_x86_64.whl"))
    assert not M.keeps(tags("x-1-pp310-pypy310_pp73-manylinux_2_17_x86_64.whl"))
    assert not M.keeps(tags("x-1-cp312-cp312-manylinux_2_17_i686.whl"))


def test_specific_beats_pure_in_priority():
    assert M.priority(tags("x-1-cp312-cp312-manylinux_2_17_x86_64.whl")) < \
        M.priority(tags("x-1-py3-none-any.whl"))


def test_musl_glibc_are_separate_and_macos_floor():
    musl = TagMatcher(Target("3.12", "linux-musl", "amd64"), Floors())
    assert musl.keeps(tags("x-1-cp312-cp312-musllinux_1_2_x86_64.whl"))
    assert not musl.keeps(tags("x-1-cp312-cp312-manylinux_2_17_x86_64.whl"))
    mac = TagMatcher(Target("3.12", "macos", "arm64"), Floors())
    assert mac.priority(tags("x-1-cp312-cp312-macosx_11_0_arm64.whl")) is not None
    assert mac.priority(tags("x-1-cp312-cp312-macosx_14_0_arm64.whl")) is None
    assert mac.keeps(tags("x-1-cp312-cp312-macosx_14_0_arm64.whl"))
    assert mac.priority(tags("x-1-cp312-cp312-macosx_10_9_universal2.whl")) is not None


def test_windows_arches():
    win = TagMatcher(Target("3.12", "windows", "amd64"), Floors())
    assert win.priority(tags("x-1-cp312-cp312-win_amd64.whl")) is not None
    assert not win.keeps(tags("x-1-cp312-cp312-win_arm64.whl"))
    assert not win.keeps(tags("x-1-cp312-cp312-win32.whl"))


def test_universal_and_requires_python_and_markers():
    assert is_universal(tags("x-1-py2.py3-none-any.whl"))
    assert not is_universal(tags("x-1-py3-none-manylinux_2_17_x86_64.whl"))
    assert requires_python_ok(">=3.11", T) and not requires_python_ok(">=3.13", T)
    assert requires_python_ok("not a spec", T) and requires_python_ok(None, T)
    env = marker_env(Target("3.10", "windows", "arm64"))
    assert env["sys_platform"] == "win32" and env["platform_machine"] == "ARM64"
    assert env["python_full_version"] == "3.10.0" and env["os_name"] == "nt"
    assert marker_env(T)["platform_machine"] == "x86_64"
    assert marker_env(Target("3.12", "linux-musl", "arm64"))["platform_machine"] == "aarch64"
