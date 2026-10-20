"""PyPI pages are derived: publish ships shards and a stdlib renderer, and one
rule renders them — after a live publish, a snapshot replay, a bundle apply."""
import runpy
import shutil

from mmw.core.pool import Pool
from mmw.core.publisher import publish_snapshot
from mmw.core.publisher.bundle import bundle_network
from mmw.core.snapshots import take_snapshot
from mmw.resources import apply as apply_mod
from test_pypi_publish import _bump_pysocks, _cfg, _publish, _src, _sync


def pages(root):
    base = root / "pypi.org"
    return {str(p.relative_to(base)): p.read_bytes() for p in base.rglob("*")
            if p.is_file() and ".mmw-rendered" not in p.parts}


def test_snapshot_records_shards_not_pages(tmp_path, up):
    cfg = _cfg(tmp_path, _src())
    _sync(cfg)
    snap = take_snapshot(cfg.networks[0], cfg, cfg.settings.mirror_dir)
    with Pool(cfg.settings.mirror_dir, read_only=True) as pool:
        rels = [rel for rel, _ in pool.snapshot_files(snap.id)]
    assert any(r.startswith("pypi-data/shards/") for r in rels) and "pypi-data/render.py" in rels
    assert not any(r.startswith("pypi.org/") for r in rels)


def test_snapshot_replay_renders_the_same_pages(tmp_path, up):
    cfg = _cfg(tmp_path, _src())
    _sync(cfg)
    live = pages(_publish(cfg))
    snap = take_snapshot(cfg.networks[0], cfg, cfg.settings.mirror_dir)
    replay = tmp_path / "replay"
    publish_snapshot(cfg.networks[0], cfg, cfg.settings.mirror_dir, str(snap.id), root=replay)
    assert live and pages(replay) == live


def test_shipped_renderer_alone_makes_the_same_pages(tmp_path, up):
    cfg = _cfg(tmp_path, _src())
    _sync(cfg)
    root = _publish(cfg)
    far = tmp_path / "far"
    shutil.copytree(root / "pypi-data", far / "pypi-data")
    runpy.run_path(str(far / "pypi-data" / "render.py"))["render"](far)
    assert pages(far) == pages(root)


def test_bundle_apply_renders_the_pages(tmp_path, up):
    cfg = _cfg(tmp_path, _src())
    _sync(cfg)
    live = pages(_publish(cfg))
    result = bundle_network(cfg.networks[0], cfg, cfg.settings.mirror_dir, out_path=tmp_path / "b.tar",
                            from_ref=None)
    far = tmp_path / "far"
    assert apply_mod.apply(str(result.out_path), str(far)) == 0
    assert pages(far) == live


def test_changed_shard_rerenders_only_its_projects(tmp_path, up):
    cfg = _cfg(tmp_path, _src(), retention="current")
    _sync(cfg)
    root = _publish(cfg)
    before = {p: p.stat().st_mtime_ns for p in (root / "pypi.org").rglob("*") if p.is_file()}
    _bump_pysocks(up)
    up.projects["six"]["files"] = []
    up.projects["six"]["serial"] += 1
    _sync(cfg)
    _publish(cfg)
    after = {p: p.stat().st_mtime_ns for p in (root / "pypi.org").rglob("*") if p.is_file()}
    changed = {str(p.relative_to(root / "pypi.org")) for p in after if before.get(p) != after[p]}
    assert any("pysocks" in c for c in changed)
    assert all("pysocks" in c or c.startswith(("simple/index", ".mmw-rendered")) for c in changed), changed
    assert not (root / "pypi.org" / "simple" / "six").exists()


def test_render_reports_progress(tmp_path, up):
    cfg = _cfg(tmp_path, _src())
    _sync(cfg)
    root = _publish(cfg)
    shutil.rmtree(root / "pypi.org")
    said = []
    written, removed = runpy.run_path(str(root / "pypi-data" / "render.py"))["render"](root, say=said.append)
    assert written > 0 and any("shards" in s for s in said)


def test_without_a_renderer_the_rendered_tree_goes(tmp_path):
    root = tmp_path / "t"
    (root / "pypi.org" / ".mmw-rendered").mkdir(parents=True)
    (root / "pypi.org" / "simple").mkdir()
    apply_mod.render_derived(str(root))
    assert not (root / "pypi.org").exists()
    (root / "pypi.org").mkdir()                     # not ours: no marker, left alone
    apply_mod.render_derived(str(root))
    assert (root / "pypi.org").exists()


def test_without_a_renderer_pypi_md_goes_too(tmp_path):
    root = tmp_path / "t"
    (root / "pypi.org" / ".mmw-rendered").mkdir(parents=True)
    (root / "PYPI.md").write_text("old")
    apply_mod.render_derived(str(root))
    assert not (root / "PYPI.md").exists()


def test_live_publish_writes_pages_on_the_disk_layer(tmp_path, up, disk_limits, monkeypatch):
    """The near side renders through `disk_io` (the write lane), the far side
    with the stdlib loop — and the two make the same bytes."""
    import threading
    from mmw.core import disk_io
    disk_limits(write_threads=4, read_threads=1, readahead=0)
    threads, real = set(), disk_io.ops.write_if_changed

    def spy(dst, data):
        threads.add(threading.current_thread().name)
        return real(dst, data)
    monkeypatch.setattr(disk_io.ops, "write_if_changed", spy)
    cfg = _cfg(tmp_path, _src())
    _sync(cfg)
    root = _publish(cfg)
    assert threads and all(t.startswith("mmw-disk-render-write") for t in threads), threads
    far = tmp_path / "far"
    shutil.copytree(root / "pypi-data", far / "pypi-data")
    runpy.run_path(str(far / "pypi-data" / "render.py"))["render"](far)
    assert pages(far) == pages(root)


def test_stopped_render_redoes_what_it_did_not_finish(tmp_path, up):
    """A stop lands between shards; the shard it interrupted and every one
    after it render on the next run, and the pages come out whole."""
    import json
    cfg = _cfg(tmp_path, _src())
    _sync(cfg)
    root = _publish(cfg)
    live = pages(root)
    shutil.rmtree(root / "pypi.org")
    render = runpy.run_path(str(root / "pypi-data" / "render.py"))["render"]
    calls = []

    def write_pages(batch):
        calls.append(len(batch))
        return 0                                   # wrote nothing: stopped mid-shard
    render(root, write_pages=write_pages, stop=lambda: bool(calls))
    assert len(calls) == 1
    state = json.loads((root / "pypi.org" / ".mmw-rendered" / "shards.json").read_text())
    assert [entry[0] for entry in state.values()] == [""]     # marked unfinished
    render(root)
    assert pages(root) == live


def test_a_failed_page_write_fails_the_render(tmp_path, up, disk_limits, monkeypatch):
    import pytest
    from mmw.core import disk_io
    from mmw.core.publisher import _render_derived
    disk_limits(write_threads=4, read_threads=1, readahead=0)
    cfg = _cfg(tmp_path, _src())
    _sync(cfg)
    root = _publish(cfg)
    shutil.rmtree(root / "pypi.org")

    def broken(dst, data):
        raise PermissionError(13, "denied", str(dst))
    monkeypatch.setattr(disk_io.ops, "write_if_changed", broken)
    with pytest.raises(PermissionError):
        _render_derived(root)
