"""`mmw dry-run` / `mmw sync --dry-run --quiet` — the headless check's exit gate.

Same shape of bug as `commands/mirror.py` had (see `test_mirror_command.py`):
a per-mirror `ERROR fetching metadata:` was printed and `continue`d, only
`resolve_errors` reached the exit gate, and the totals then printed a
confident `0 packages / 0.0 B` while the command returned 0. Nothing
exercised this file's exit logic, which is why it stayed alive.
"""
from pathlib import Path

import pytest

from mmw.commands import dry_run as dry_run_cmd
from mmw.core import config
from mmw.providers.base import PackageInfo


def _mirror(name: str = "m1", provider: str = "apt") -> config.MirrorConfig:
    return config.MirrorConfig(
        name=name, provider=provider, source_url="https://example.invalid/repo",
        source="https://example.invalid/repo", suite="stable", component="main",
    )


class _FakePool:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def batch_contains(self, checksums):
        return set()

    def stats(self):
        return {"packages": 0, "total_size": 0}


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / "pool", max_connections=2))
    monkeypatch.setattr(config, "load", lambda directory=Path("."): cfg)
    monkeypatch.setattr(dry_run_cmd, "Pool", lambda *a, **k: _FakePool())

    state = {"mirrors": [_mirror()], "errors": [], "raise": None, "packages": [],
             "cfg_dir": tmp_path}

    monkeypatch.setattr(dry_run_cmd, "resolve_and_report",
                        lambda cfg, echo=None: (state["mirrors"], state["errors"]))

    def fake_fetch(provider, mirror):
        if state["raise"] is not None:
            raise state["raise"]
        return state["packages"]

    monkeypatch.setattr(dry_run_cmd, "fetch_packages_threaded", fake_fetch)
    return state


def test_clean_dry_run_exits_0(env):
    env["packages"] = [PackageInfo(name="a", url="https://example.invalid/repo/pool/a.deb",
                                   checksum="a" * 64, checksum_type="sha256", size=10,
                                   repo_path="pool/a.deb", filename="a.deb")]
    assert dry_run_cmd.run(env["cfg_dir"]) == 0


def test_every_metadata_fetch_failing_exits_1(env, capsys):
    """THE bug: 'ERROR fetching metadata' on every mirror, then exit 0."""
    env["mirrors"] = [_mirror("m1"), _mirror("m2")]
    env["raise"] = RuntimeError("503 from upstream")

    code = dry_run_cmd.run(env["cfg_dir"])

    assert code == 1
    err = capsys.readouterr().err
    assert "503 from upstream" in err


def test_unknown_provider_exits_1(env, monkeypatch):
    """The other `continue` in the loop: a provider that cannot be constructed
    left the same hole in the exit gate."""
    def boom(name):
        raise ValueError(f"unknown provider {name!r}")

    monkeypatch.setattr(dry_run_cmd, "get_provider", boom)
    assert dry_run_cmd.run(env["cfg_dir"]) == 1


def test_resolve_error_exits_1(env):
    env["mirrors"] = []
    env["errors"] = ["https://example.invalid/repo: could not list dists/"]
    assert dry_run_cmd.run(env["cfg_dir"]) == 1


def test_keyboard_interrupt_exits_130(env, capsys):
    env["raise"] = KeyboardInterrupt()
    assert dry_run_cmd.run(env["cfg_dir"]) == 130
    assert "nterrupt" in capsys.readouterr().err


def test_invalid_config_is_one_readable_line(tmp_path, capsys):
    (tmp_path / "mmw.toml").write_text('[settings]\nmirror_dir = "/tmp/x"\nnope = 1\n')
    code = dry_run_cmd.run(tmp_path)
    assert code == 1
    err = capsys.readouterr().err
    assert "Invalid config" in err
    assert "Traceback" not in err


def test_cfg_dir_is_honoured(tmp_path, monkeypatch):
    seen = []

    def fake_load(directory=Path(".")):
        seen.append(Path(directory))
        raise FileNotFoundError("no config")

    monkeypatch.setattr(config, "load", fake_load)
    dry_run_cmd.run(tmp_path / "elsewhere")
    assert seen == [tmp_path / "elsewhere"]
