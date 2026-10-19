"""`mmw sync --quiet` — the headless sync's exit-code gate.

No network, no pool, no downloads: `resolve_sources`, `Pool` and `sync_mirror`
are all monkeypatched out, so this exercises only the command's own reporting
and exit-code logic.

**Why this file exists.** A per-mirror exception used to print `ERROR: ...`
and `continue` *without appending to `summaries`* — so with every mirror
failing, the gate saw `total_failed == 0`, `metadata_failed == []` and
`resolve_errors == []`, and the command returned 0. A nightly cron sync that
downloaded nothing reported success, which is the exact failure mode the
project documents itself against: "a cron sync that silently did nothing is
worse than one that fails". Nothing covered `commands/mirror.py`'s exit
logic at all, which is why it stayed alive.
"""
import concurrent.futures
from pathlib import Path

import pytest

from mmw.commands import mirror as mirror_cmd
from mmw.core import config
from mmw.core.downloader import MirrorSummary


def _mirror(name: str = "m1") -> config.MirrorConfig:
    return config.MirrorConfig(
        name=name, provider="apt", source_url="https://example.invalid/repo",
        source="https://example.invalid/repo", suite="stable", component="main",
    )


class _FakePool:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A loaded config, a stub pool, and hooks for resolution and syncing."""
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / "pool", max_connections=2))
    (tmp_path / "mmw.toml").write_text('[settings]\nmirror_dir = "%s"\n' % (tmp_path / "pool"))
    monkeypatch.setattr(config, "load", lambda directory=Path("."): cfg)
    monkeypatch.setattr(mirror_cmd, "Pool", lambda *a, **k: _FakePool())

    state = {"mirrors": [_mirror()], "errors": [], "summaries": {}, "raise": None}

    def fake_resolve(sources, progress=None, context=None):
        return state["mirrors"], [], state["errors"]

    async def fake_sync(mirror, **kw):
        if state["raise"] is not None:
            raise state["raise"]
        return state["summaries"].get(mirror.name, MirrorSummary(mirror=mirror.name))

    monkeypatch.setattr(mirror_cmd, "resolve_sources", fake_resolve)
    monkeypatch.setattr(mirror_cmd, "sync_mirror", fake_sync)
    state["cfg"] = cfg
    state["cfg_dir"] = tmp_path
    return state


# ── the exit-code matrix ──────────────────────────────────────────────────────

def test_clean_sync_exits_0(env):
    assert mirror_cmd.run(env["cfg_dir"]) == 0


def test_every_mirror_raising_exits_1(env, capsys):
    """THE bug: all mirrors blew up, nothing was appended to `summaries`, and
    the gate had nothing to look at."""
    env["mirrors"] = [_mirror("m1"), _mirror("m2")]
    env["raise"] = RuntimeError("connection refused")

    code = mirror_cmd.run(env["cfg_dir"])

    assert code == 1
    err = capsys.readouterr().err
    assert "connection refused" in err
    assert "2" in err  # both mirrors named in the failure count


def test_one_mirror_raising_exits_1_even_when_the_other_is_clean(env):
    env["mirrors"] = [_mirror("m1"), _mirror("m2")]
    calls = []

    async def fake_sync(mirror, **kw):
        calls.append(mirror.name)
        if mirror.name == "m1":
            raise RuntimeError("boom")
        return MirrorSummary(mirror=mirror.name)

    import mmw.commands.mirror as m
    m.sync_mirror = fake_sync
    try:
        code = mirror_cmd.run(env["cfg_dir"])
    finally:
        pass
    assert calls == ["m1", "m2"]      # a failure never aborts the remaining mirrors
    assert code == 1


def test_package_failure_exits_1(env):
    env["summaries"] = {"m1": MirrorSummary(mirror="m1", failed=3)}
    assert mirror_cmd.run(env["cfg_dir"]) == 1


def test_metadata_failure_exits_1(env):
    env["summaries"] = {"m1": MirrorSummary(mirror="m1", metadata_ok=False)}
    assert mirror_cmd.run(env["cfg_dir"]) == 1


def test_resolve_error_exits_1(env):
    env["mirrors"] = []
    env["errors"] = ["https://example.invalid/repo: could not list dists/"]
    assert mirror_cmd.run(env["cfg_dir"]) == 1


# ── failure presentation ──────────────────────────────────────────────────────

def test_keyboard_interrupt_exits_130(env, capsys):
    env["raise"] = KeyboardInterrupt()
    code = mirror_cmd.run(env["cfg_dir"])
    assert code == 130
    assert "nterrupt" in capsys.readouterr().err


def test_unwritable_mirror_dir_is_one_line_not_a_traceback(tmp_path, monkeypatch, capsys):
    """The single most likely real-world misconfiguration (NFS not mounted,
    wrong uid) used to produce a 121-line rich traceback."""
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / "ro" / "pool"))
    monkeypatch.setattr(config, "load", lambda directory=Path("."): cfg)
    monkeypatch.setattr(mirror_cmd, "resolve_sources", lambda *a, **k: ([], [], []))

    def boom(*a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "mkdir", boom)

    code = mirror_cmd.run(tmp_path)
    assert code == 1
    err = capsys.readouterr().err
    assert "Permission denied" in err
    assert len(err.strip().splitlines()) <= 3


def test_executor_is_shut_down_even_when_the_pool_cannot_open(tmp_path, monkeypatch):
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / "pool"))
    monkeypatch.setattr(config, "load", lambda directory=Path("."): cfg)
    monkeypatch.setattr(mirror_cmd, "resolve_sources", lambda *a, **k: ([_mirror()], [], []))

    shut = []
    real = concurrent.futures.ThreadPoolExecutor

    class Tracking(real):
        def shutdown(self, *a, **k):
            shut.append(True)
            return super().shutdown(*a, **k)

    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", Tracking)

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(mirror_cmd, "Pool", boom)

    code = mirror_cmd.run(tmp_path)
    assert code == 1
    assert shut, "the executor must be shut down on the error path too"


def test_invalid_config_is_one_readable_line(tmp_path, capsys):
    (tmp_path / "mmw.toml").write_text('[settings]\nmirror_dir = "/tmp/x"\nnope = 1\n')
    code = mirror_cmd.run(tmp_path)
    assert code == 1
    err = capsys.readouterr().err
    assert "Invalid config" in err
    assert "Traceback" not in err


def test_missing_config_is_one_readable_line(tmp_path, capsys):
    code = mirror_cmd.run(tmp_path)
    assert code == 1
    assert "mmw init" in capsys.readouterr().err


def test_cfg_dir_is_honoured(tmp_path, monkeypatch):
    """`run()` took no cfg_dir at all, so `--config` could never reach it."""
    seen = []
    monkeypatch.setattr(config, "load",
                        lambda directory=Path("."): (seen.append(Path(directory)), _raise_missing())[1])

    def _raise_missing():
        raise FileNotFoundError("no config")

    mirror_cmd.run(tmp_path / "elsewhere")
    assert seen == [tmp_path / "elsewhere"]


def test_ctrl_c_during_resolve_stops_it(monkeypatch, tmp_path):
    import os
    import signal

    import pytest

    from mmw.commands import mirror as mirror_cmd
    seen = {}

    def fake_resolve(sources, progress=None, context=None):
        os.kill(os.getpid(), signal.SIGINT)
        for _ in range(100):
            if context.stop_event.is_set():
                break
        seen["stopped"] = context.stop_event.is_set()
        return [], [], []
    monkeypatch.setattr(mirror_cmd, "resolve_sources", fake_resolve)
    before = signal.getsignal(signal.SIGINT)
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / "m"))
    with pytest.raises(KeyboardInterrupt):
        mirror_cmd.resolve_and_report(cfg, echo=lambda *_: None)
    assert seen["stopped"] and signal.getsignal(signal.SIGINT) is before


def test_resolve_progress_is_printed_but_throttled(monkeypatch, tmp_path):
    from mmw.commands import mirror as mirror_cmd
    lines = []

    def fake_resolve(sources, progress=None, context=None):
        progress("pypi: reading the PyPI index…")
        for i in range(500):
            progress(f"PyPI index: {i}/500 changed projects")
        progress("pypi: resolving latest + dependencies for 1 target(s)…")
        return [], [], []
    monkeypatch.setattr(mirror_cmd, "resolve_sources", fake_resolve)
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / "m"))
    mirror_cmd.resolve_and_report(cfg, echo=lines.append)
    assert any("reading the PyPI index" in line for line in lines)
    assert any("resolving latest" in line for line in lines)
    assert sum("PyPI index:" in line for line in lines) <= 3
