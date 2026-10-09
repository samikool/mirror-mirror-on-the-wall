"""`mmw prune` — the only destructive CLI verb.

CRITICAL: with zero configured sources, `pool.prune_orphans([])` takes the
`DELETE FROM refs WHERE source != ''` branch, after which *every* blob is an
orphan and gets unlinked. There was no guard, no preview and no confirmation
— just "Pruning pool against 0 configured repositor(y/ies)..." and a wiped
multi-terabyte pool. These tests build a real (tiny) pool, so the set
difference is exercised for real rather than against a stub.
"""
from pathlib import Path

import pytest

from mmw.commands import prune as prune_cmd
from mmw.core.pool import KIND_PKG, Pool

SRC = "https://example.invalid/repo"
OTHER = "https://other.invalid/repo"


def _blob(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / "incoming" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


@pytest.fixture
def pooled(tmp_path):
    """A pool holding two packages: one referenced by SRC, one by OTHER."""
    import hashlib

    mirror_dir = tmp_path / "mirror"
    entries = {}
    with Pool(mirror_dir) as pool:
        for name, source in (("a.deb", SRC), ("b.deb", OTHER)):
            data = name.encode() * 50
            checksum = hashlib.sha256(data).hexdigest()
            pool.add(_blob(tmp_path, name, data), checksum, "sha256", name, len(data))
            pool.flush_writes()
            entries.setdefault(source, []).append((checksum, f"pool/{name}"))
        for i, (source, rows) in enumerate(entries.items()):
            pool.reconcile_refs(
                f"m{i}", [(c, p, KIND_PKG) for c, p in rows], source=source)
    return mirror_dir


def _cfg_dir(tmp_path: Path, mirror_dir: Path, sources: list[str]) -> Path:
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(exist_ok=True)
    text = f'[settings]\nmirror_dir = "{mirror_dir}"\n'
    for url in sources:
        text += f'\n[[sources]]\nurl = "{url}"\nprovider = "apt"\n'
    (cfg_dir / "mmw.toml").write_text(text)
    return cfg_dir


def _blob_count(mirror_dir: Path) -> int:
    return len(list((mirror_dir / ".pool" / "packages").rglob("*.deb")))


def test_zero_configured_sources_refuses_and_deletes_nothing(pooled, tmp_path, capsys):
    """THE bug: an empty source list made the whole pool look unwanted."""
    cfg_dir = _cfg_dir(tmp_path, pooled, [])
    before = _blob_count(pooled)
    assert before == 2

    code = prune_cmd.run(cfg_dir)

    assert code == 1
    assert _blob_count(pooled) == before, "prune deleted blobs with nothing configured"
    err = capsys.readouterr().err
    assert "Refusing to prune" in err


def test_dry_run_reports_but_deletes_nothing(pooled, tmp_path, capsys):
    cfg_dir = _cfg_dir(tmp_path, pooled, [SRC])

    code = prune_cmd.run(cfg_dir, dry_run=True)

    assert code == 0
    assert _blob_count(pooled) == 2
    out = capsys.readouterr().out
    assert "Would remove 1" in out
    assert "nothing deleted" in out


def test_yes_prunes_the_unconfigured_repository(pooled, tmp_path, capsys):
    cfg_dir = _cfg_dir(tmp_path, pooled, [SRC])

    code = prune_cmd.run(cfg_dir, yes=True)

    assert code == 0
    assert _blob_count(pooled) == 1
    assert "Removed 1" in capsys.readouterr().out


def test_tty_declining_the_confirmation_deletes_nothing(pooled, tmp_path, monkeypatch, capsys):
    cfg_dir = _cfg_dir(tmp_path, pooled, [SRC])
    monkeypatch.setattr(prune_cmd.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda *a: "n")

    code = prune_cmd.run(cfg_dir)

    assert code == 1
    assert _blob_count(pooled) == 2
    assert "Aborted" in capsys.readouterr().out


def test_tty_accepting_the_confirmation_prunes(pooled, tmp_path, monkeypatch):
    cfg_dir = _cfg_dir(tmp_path, pooled, [SRC])
    monkeypatch.setattr(prune_cmd.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda *a: "y")

    assert prune_cmd.run(cfg_dir) == 0
    assert _blob_count(pooled) == 1


def test_non_interactive_run_does_not_wait_for_input(pooled, tmp_path, monkeypatch):
    """cron has no TTY: a prompt nobody can answer is a hang, not a safeguard."""
    cfg_dir = _cfg_dir(tmp_path, pooled, [SRC])
    monkeypatch.setattr(prune_cmd.sys.stdin, "isatty", lambda: False, raising=False)

    def no_input(*a):
        raise AssertionError("prompted on a non-interactive run")

    monkeypatch.setattr("builtins.input", no_input)
    assert prune_cmd.run(cfg_dir) == 0


def test_clean_pool_exits_0(pooled, tmp_path, capsys):
    cfg_dir = _cfg_dir(tmp_path, pooled, [SRC, OTHER])
    assert prune_cmd.run(cfg_dir, yes=True) == 0
    assert _blob_count(pooled) == 2
    assert "clean" in capsys.readouterr().out


def test_invalid_config_is_one_readable_line(tmp_path, capsys):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "mmw.toml").write_text('[settings]\nmirror_dir = "/tmp/x"\nnope = 1\n')
    assert prune_cmd.run(cfg_dir) == 1
    err = capsys.readouterr().err
    assert "Invalid config" in err
    assert "Traceback" not in err


def test_unreadable_pool_is_one_line_not_a_traceback(tmp_path, monkeypatch, capsys):
    cfg_dir = _cfg_dir(tmp_path, tmp_path / "nope", [SRC])

    def boom(*a, **k):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(prune_cmd, "_preview", boom)

    assert prune_cmd.run(cfg_dir) == 1
    err = capsys.readouterr().err
    assert "Permission denied" in err
    assert len(err.strip().splitlines()) <= 3
