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

    monkeypatch.setattr(prune_cmd, "_plan", boom)

    assert prune_cmd.run(cfg_dir) == 1
    err = capsys.readouterr().err
    assert "Permission denied" in err
    assert len(err.strip().splitlines()) <= 3


def test_core_refuses_an_empty_source_list(pooled):
    """The guard lives in `Pool.prune_orphans` itself: the TUI's Prune button
    had none of the CLI's, and with every repository removed it deleted every
    blob no snapshot held."""
    with Pool(pooled) as pool:
        with pytest.raises(ValueError, match="no configured repositories"):
            pool.prune_orphans([])
    assert _blob_count(pooled) == 2


def test_preview_does_not_count_snapshot_held_blobs(pooled, tmp_path, capsys):
    """Snapshots are GC roots. The preview was a second copy of the orphan
    SQL that never learned that, so the dry run and the confirmation prompt
    promised to delete files the prune then kept."""
    import hashlib
    with Pool(pooled) as pool:
        held = hashlib.sha256(b"b.deb" * 50).hexdigest()
        pool.create_snapshot("lab", "20260101T000000Z", kind="manual",
                             rows=[("other.invalid/repo/pool/b.deb", held)])
        assert pool.prune_preview([SRC]) == (0, 0)
        assert pool.prune_orphans([SRC]) == (0, 0)

    assert prune_cmd.run(_cfg_dir(tmp_path, pooled, [SRC]), dry_run=True) == 0
    assert "Pool is clean" in capsys.readouterr().out


def test_a_suite_dropped_from_the_config_is_pruned(tmp_path, capsys):
    """Narrowing `suites` on a still-configured repository: the dropped
    suite's refs are proven unwanted by the config, so prune frees them."""
    import hashlib

    from mmw.core.config import MirrorConfig, normalize_url

    mirror_dir = tmp_path / "mirror"
    src = normalize_url(SRC)
    with Pool(mirror_dir) as pool:
        for suite in ("jammy", "noble"):
            data = suite.encode() * 10
            cs = hashlib.sha256(data).hexdigest()
            pool.add(data, cs, "sha256", f"{suite}.deb", len(data))
            m = MirrorConfig(name=f"r-{suite}-main-amd64", provider="apt", source_url=SRC,
                             source=src, arch="amd64", suite=suite, component="main")
            pool.record_mirror(m)
            pool.reconcile_refs(m.name, [(cs, f"pool/{suite}.deb", KIND_PKG)], source=src)
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "mmw.toml").write_text(
        f'[settings]\nmirror_dir = "{mirror_dir}"\n\n[[sources]]\nurl = "{SRC}"\n'
        'suites = ["noble"]\ncomponents = ["main"]\narches = ["amd64"]\n')

    assert prune_cmd.run(cfg_dir, dry_run=True) == 0
    out = capsys.readouterr().out
    assert "r-jammy-main-amd64" in out and "Would remove 1 orphaned" in out
    assert prune_cmd.run(cfg_dir, yes=True) == 0
    with Pool(mirror_dir, read_only=True) as pool:
        assert [m.name for m in pool.mirrors([src])] == ["r-noble-main-amd64"]
