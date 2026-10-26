"""mmw keeps itself up to date: the core — check, versions, install kind, run."""
import json
import sys
import time
from pathlib import Path

import pytest

from mmw.core import update as U


def _fetch(releases):
    return lambda url, timeout: json.dumps({"info": {"version": max(releases)},
                                            "releases": {v: [{}] for v in releases}}).encode()


def test_newest_skips_prereleases():
    assert U.newest_release(fetch=_fetch(["1.2.0", "1.3.0", "2.0.0rc1"]), current="1.2.0") == "1.3.0"
    assert U.newest_release(fetch=_fetch(["1.3.0", "2.0.0rc1"]), current="2.0.0b1") == "2.0.0rc1"


def test_newest_is_none_when_unreachable():
    def down(url, timeout):
        raise OSError("no route")
    assert U.newest_release(fetch=down, current="1.2.0") is None


def test_offer_major_and_minor():
    minor = U.offer("1.3.0", current="1.2.0")
    assert not minor.major and minor.default_yes() and "1.3.0" in minor.prompt_text()
    major = U.offer("2.0.0", current="1.9.0")
    assert major.major and not major.default_yes() and "major" in major.prompt_text()
    assert U.offer("1.2.0", current="1.2.0") is None and U.offer(None, current="1.2.0") is None


def test_checker_falls_back_to_cache():
    U.remember("9.9.9")
    slow = lambda url, timeout: time.sleep(5) or b"{}"          # noqa: E731
    c = U.Checker(current="1.2.0")
    c.start(fetch=slow)
    o = c.result(wait=0.1)
    assert o is not None and o.newest == "9.9.9"


def test_checker_remembers_what_it_found():
    c = U.Checker(current="1.2.0")
    c.start(fetch=_fetch(["1.4.0"]))
    assert c.result(wait=5).newest == "1.4.0" and U.cached()["newest"] == "1.4.0"


def test_checks_disabled_by_env_and_setting(monkeypatch):
    from mmw.core import config
    monkeypatch.delenv("MMW_NO_UPDATE_CHECK", raising=False)
    assert not U.checks_disabled()
    monkeypatch.setenv("MMW_NO_UPDATE_CHECK", "1")
    assert U.checks_disabled()
    monkeypatch.delenv("MMW_NO_UPDATE_CHECK")
    cfg = config.Config(settings=config.Settings(mirror_dir=Path("/x"), update_check=False), sources=[], networks=[])
    assert U.checks_disabled(cfg)


def test_update_check_setting_round_trips(tmp_path):
    from mmw.core import config
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path, update_check=False), sources=[], networks=[])
    config.save(cfg, tmp_path)
    assert "update_check = false" in (tmp_path / "mmw.toml").read_text()
    assert config.load(tmp_path).settings.update_check is False


@pytest.mark.parametrize("kind", ["uv-tool", "pipx", "venv", "installer", "checkout"])
def test_detect_install_kinds(kind, monkeypatch, tmp_path):
    prefix = {"uv-tool": tmp_path / "uv" / "tools" / "mirror-mirror-on-the-wall",
              "pipx": tmp_path / "pipx" / "venvs" / "mirror-mirror-on-the-wall",
              "venv": tmp_path / "venv",
              "installer": tmp_path / "pre" / "share" / "mmw" / "1.2.0-py3.12-1-x86_64" / "venv",
              "checkout": tmp_path / "co" / ".venv"}[kind]
    prefix.mkdir(parents=True)
    if kind == "installer":
        (tmp_path / "pre" / "share" / "mmw" / "CURRENT").write_text("1.2.0 3.12 1.2.0-py3.12-1\n")
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    monkeypatch.setattr(U, "_uv_tool_dir", lambda: tmp_path / "uv" / "tools")
    monkeypatch.setattr(U, "_pipx_venvs", lambda: tmp_path / "pipx" / "venvs")
    monkeypatch.setattr(U, "_checkout", lambda: tmp_path / "co" if kind == "checkout" else None)
    k = U.detect_install()
    assert k.name == kind
    if kind == "uv-tool":
        assert k.argv == ["uv", "tool", "upgrade", "mirror-mirror-on-the-wall"]
    if kind == "venv":
        assert k.argv[-4:] == ["pip", "install", "-U", "mirror-mirror-on-the-wall"]
    if kind == "checkout":
        assert k.argv is None and "git pull" in k.note


def test_externally_managed_shows_the_command(monkeypatch, tmp_path):
    import sysconfig
    stdlib = tmp_path / "lib" / "python3"
    stdlib.mkdir(parents=True)
    (stdlib / "EXTERNALLY-MANAGED").write_text("[externally-managed]\n")
    monkeypatch.setattr(sys, "prefix", "/usr")
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    monkeypatch.setattr(sysconfig, "get_path", lambda name: str(stdlib))
    monkeypatch.setattr(U, "_uv_tool_dir", lambda: tmp_path / "nope")
    monkeypatch.setattr(U, "_pipx_venvs", lambda: tmp_path / "nope2")
    monkeypatch.setattr(U, "_checkout", lambda: None)
    k = U.detect_install()
    assert k.name == "system" and k.argv is None and "pip install -U mirror-mirror-on-the-wall" in k.note


def test_run_update_reports_failure():
    lines = []
    ok, last = U.run_update(U.InstallKind("venv", ["sh", "-c", "echo working; echo boom >&2; exit 3"], ""),
                            progress=lines.append)
    assert not ok and last == "boom" and "working" in lines
