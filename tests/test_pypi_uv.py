"""The uv runner: per-target compile, real uv binary, local index only."""
import pytest

from mmw.providers.pypi import uv as uvmod
from mmw.providers.pypi.targets import Floors, Target
from pypi_fakes import FakePyPI, file_entry, metadata_text, real_sdist
from pypi_server import serve

T = Target("3.12", "linux-glibc", "amd64")


def whl(name, version, requires=()):
    return file_entry(f"{name}-{version}-py3-none-any.whl", metadata=metadata_text(name, version, requires))


@pytest.fixture
def index():
    fake = FakePyPI()
    fake.add("a", [whl("a", "1.0", ["b<2"])])
    fake.add("b", [whl("b", "1.0"), whl("b", "2.0")])
    fake.add("c", [whl("c", "1.0"), real_sdist("c", "2.0")])
    with serve(fake) as base:
        yield f"{base}/simple"


def _compile(lines, index, tmp_path, target=T):
    return uvmod.compile_for(lines, target, Floors(), index_url=index, cache_dir=tmp_path / "uv-cache")


def test_platform_triples():
    f = Floors()
    assert uvmod.platform_triple(T, f) == "x86_64-manylinux_2_31"
    assert uvmod.platform_triple(T, Floors(glibc="2.30")) == "x86_64-manylinux_2_28"
    assert uvmod.platform_triple(T, Floors(glibc="2.5")) == "x86_64-manylinux_2_17"
    assert uvmod.platform_triple(Target("3.12", "linux-musl", "arm64"), f) == "aarch64-unknown-linux-musl"
    assert uvmod.platform_triple(Target("3.12", "windows", "amd64"), f) == "x86_64-pc-windows-msvc"
    assert uvmod.platform_triple(Target("3.12", "macos", "arm64"), f) == "aarch64-apple-darwin"


def test_compile_resolves_together(index, tmp_path):
    r = _compile(["a", "b"], index, tmp_path)
    assert r.error is None and not r.built
    assert r.pins == [("a", "1.0"), ("b", "1.0")]


def test_unknown_project_is_a_target_error(index, tmp_path):
    r = _compile(["no-such-pkg"], index, tmp_path)
    assert r.pins == [] and "no-such-pkg" in r.error


def test_wheels_preferred_then_source(index, tmp_path):
    r = _compile(["c"], index, tmp_path)
    assert r.pins == [("c", "1.0")] and not r.built
    r = _compile(["c==2.0"], index, tmp_path)
    assert r.pins == [("c", "2.0")] and r.built and r.error is None


def test_user_uv_settings_do_not_leak(index, tmp_path, monkeypatch):
    bogus = "http://127.0.0.1:9/nope"
    for var in ("UV_INDEX_URL", "UV_DEFAULT_INDEX", "UV_INDEX", "PIP_INDEX_URL", "UV_EXTRA_INDEX_URL"):
        monkeypatch.setenv(var, bogus)
    cfg = tmp_path / "xdg" / "uv"
    cfg.mkdir(parents=True)
    (cfg / "uv.toml").write_text(f'index-url = "{bogus}"\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert _compile(["a"], index, tmp_path).pins == [("a", "1.0"), ("b", "1.0")]


def test_macos_floor_is_passed_as_deployment_target(tmp_path, monkeypatch):
    seen = []

    class Done:
        returncode, stdout, stderr = 0, "", ""
    monkeypatch.setattr(uvmod, "_run_uv", lambda cmd, text, env, timeout, cancelled: seen.append(env) or Done())
    uvmod.compile_for(["x"], Target("3.12", "macos", "arm64"), Floors(), index_url="http://x", cache_dir=tmp_path)
    uvmod.compile_for(["x"], T, Floors(), index_url="http://x", cache_dir=tmp_path)
    assert seen[0]["MACOSX_DEPLOYMENT_TARGET"] == "11.0" and "MACOSX_DEPLOYMENT_TARGET" not in seen[1]


@pytest.mark.parametrize("raised", [__import__("subprocess").TimeoutExpired(["uv"], 5), OSError("no uv")])
def test_uv_failures_are_target_errors(tmp_path, monkeypatch, raised):
    def boom(*a, **kw):
        raise raised
    monkeypatch.setattr(uvmod, "_run_uv", boom)
    r = uvmod.compile_for(["x"], T, Floors(), index_url="http://x", cache_dir=tmp_path)
    assert r.pins == [] and r.error



def test_error_tail_keeps_uvs_reason_head():
    long = "  × No solution found when resolving dependencies:\n  ╰─▶ Because a needs b\n" + \
        "".join(f"      and because x{i} needs y{i},\n" for i in range(30)) + "      we conclude.\n"
    kept = uvmod._tail(long)
    assert kept.startswith("× No solution found") and "╰─▶ Because a needs b" in kept


def test_a_cancelled_run_is_killed_promptly():
    import time
    started = time.monotonic()
    with pytest.raises(uvmod.Cancelled):
        uvmod._run_uv(["sleep", "30"], "", {}, timeout=60, cancelled=lambda: time.monotonic() - started > 0.3)
    assert time.monotonic() - started < 5


def test_cancel_reaches_compile_for(tmp_path):
    r = uvmod.compile_for(["x"], T, Floors(), index_url="http://127.0.0.1:9", cache_dir=tmp_path,
                          cancelled=lambda: True)
    assert r.pins == [] and r.error == "cancelled"


def test_cache_dir_helper_and_a_single_upstream(tmp_path):
    from mmw.providers.pypi import index
    assert uvmod.cache_dir(tmp_path) == tmp_path / "pypi-cache" / "uv-cache"
    assert uvmod.INDEX_URL == index.SIMPLE_URL.rstrip("/")


def test_transport_settings_pass_through_index_settings_do_not(monkeypatch):
    monkeypatch.setenv("UV_NATIVE_TLS", "1")
    monkeypatch.setenv("UV_HTTP_TIMEOUT", "99")
    monkeypatch.setenv("UV_INDEX_URL", "http://127.0.0.1:9/nope")
    monkeypatch.setenv("UV_CONSTRAINT", "/tmp/c.txt")
    monkeypatch.setenv("PIP_INDEX_URL", "http://127.0.0.1:9/nope")
    env = uvmod._env(T, Floors())
    assert env["UV_NATIVE_TLS"] == "1" and env["UV_HTTP_TIMEOUT"] == "99"
    assert not any(k in env for k in ("UV_INDEX_URL", "UV_CONSTRAINT", "PIP_INDEX_URL"))
