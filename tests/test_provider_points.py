"""PyPI and VS Code history belongs to the repository: every sync records one
repository point (track "", every unit's refs, provider metadata kept but
never served), and a network's slice of it is the provider's own publish run
over the point, narrowed as of the point's day."""
# ruff: noqa: F811
import hashlib
from datetime import date, timedelta

import pytest
from typer.testing import CliRunner

from mmw.cli import app
from mmw.core import config, downloader, points
from mmw.core.pool import Pool
from mmw.core.publisher import publish_network
from mmw.core.publisher.bundle import bundle_network
from mmw.core.repository_points import current_repository_points, files_for_settings, network_slice
from mmw.core.snapshots import walk_tree
from mmw.providers.pypi import provider as pypi_provider
from mmw.providers.vscode import marketplace as mp
from mmw.providers.vscode import provider as vs_provider
from test_deployment_parity import _apply
from test_pypi_publish import _bump_pysocks, _src, _sync
from test_vscode_publish import docker  # noqa: F401  (fixture)
from test_vscode_review_fixes import _vs
from test_vscode_sync_e2e import _sync as _vs_sync
from vscode_fakes import FakeUpstream, fake_asset_bytes

OLD = "PySocks-1.7.1-py3-none-any.whl"


def _net(name, tmp_path, **kw):
    return config.NetworkConfig(name=name, nginx=False, certs=False, root=tmp_path / f"pub-{name}",
                                bundle_dir=tmp_path / f"usb-{name}", **kw)


def _cfg(tmp_path, sources, networks):
    return config.Config(settings=config.Settings(mirror_dir=tmp_path / "m"), sources=sources, networks=networks)


def _record(cfg, sources=None):
    said = []
    sources = sources or {s.identity for s in cfg.sources}
    points.record_points_after_sync(cfg, cfg.settings.mirror_dir, sources, say=said.append)
    return said


def _pypi_point(md):
    with Pool(md, read_only=True) as pool:
        (pid,) = current_repository_points(pool, "pypi")
        return pid


def _settings(md, network):
    with Pool(md, read_only=True) as pool:
        return pool.settings_at(network, "99999999T999999Z")


class _Day(date):
    shift = 0

    @classmethod
    def today(cls):
        return date.fromordinal(date.today().toordinal() + cls.shift)


def _left_30_days_ago(cfg, up, monkeypatch):
    """pysocks 1.7.1 synced, then replaced by 9.0.0 on a sync that ran 30
    days before today (the clock moves, never the point's `as_of`)."""
    _sync(cfg)
    _bump_pysocks(up)
    monkeypatch.setattr(_Day, "shift", -30)
    monkeypatch.setattr(pypi_provider, "date", _Day)
    _sync(cfg)
    monkeypatch.setattr(pypi_provider, "date", date)


# ── recording ───────────────────────────────────────────────────────────────

def test_pypi_sync_records_one_repository_point(tmp_path, up):
    cfg = _cfg(tmp_path, [_src(requirements=["requests"])], [_net("lab", tmp_path)])
    units = _sync(cfg)
    said = _record(cfg)
    md = cfg.settings.mirror_dir
    assert any("New repository point" in s for s in said), said
    with Pool(md, read_only=True) as pool:
        (pid,) = current_repository_points(pool, "pypi")
        point = pool.points_of([pid])[pid]
        assert not point.scoped and point.ident.startswith("state:")
        assert pool.track_of_point(pid).name == ""
        assert pid in {p.id for p in pool.repository_points("pypi")}
        rows = pool._conn.execute(
            "SELECT u.dims, p.path FROM point_unit_files f JOIN units u ON u.id = f.unit "
            "JOIN snapshot_paths p ON p.id = f.path WHERE f.point = ?", (pid,)).fetchall()
        flows = {__import__("json").loads(d)["flow"] for d, _ in rows}
        assert flows == {"all", "req"}
        assert len({d for d, _ in rows}) == len(units)
        assert any(path.startswith(".pypi-meta/") for _, path in rows)
        served = pool.point_files(pid)
        assert served and not any(p.startswith(".pypi-meta/") for p in served)
        assert all(p.startswith("files.pythonhosted.org/") for p in served)
        settings = _settings(md, "lab")
        mine = files_for_settings(pool, pid, settings)
        assert mine and not any(p.startswith(".pypi-meta/") for p in mine)
        assert pid in {p.id for p in pool.network_points("lab")}


def test_pypi_slice_narrows_by_retention_as_of_the_point_day(tmp_path, up, monkeypatch):
    week, quarter = _net("week", tmp_path, retention="7d"), _net("quarter", tmp_path, retention="90d")
    cfg = _cfg(tmp_path, [_src()], [week, quarter])
    _left_30_days_ago(cfg, up, monkeypatch)
    _record(cfg)
    md = cfg.settings.mirror_dir
    pid = _pypi_point(md)
    with Pool(md, read_only=True) as pool:
        slices = {n: network_slice(pool, n, pid, _settings(md, n)) for n in ("week", "quarter")}
    assert any(p.endswith(OLD) for p in slices["quarter"])
    assert not any(p.endswith(OLD) for p in slices["week"])
    assert any(p.endswith("pysocks-9.0.0-py3-none-any.whl") for p in slices["week"])
    # Days later (no sync), the point still slices as of its own day.
    monkeypatch.setattr(_Day, "shift", 80)
    from mmw.providers.pypi import publish as pypub
    monkeypatch.setattr(pypub, "date", _Day, raising=False)
    monkeypatch.setattr(pypi_provider, "date", _Day)
    with Pool(md, read_only=True) as pool:
        assert network_slice(pool, "quarter", pid, _settings(md, "quarter")) == slices["quarter"]


def test_pypi_slice_equals_the_walk(tmp_path, up, monkeypatch):
    week, quarter = _net("week", tmp_path, retention="7d"), _net("quarter", tmp_path, retention="90d")
    cfg = _cfg(tmp_path, [_src(requirements=["requests"])], [week, quarter])
    _left_30_days_ago(cfg, up, monkeypatch)
    _record(cfg)
    md = cfg.settings.mirror_dir
    pid = _pypi_point(md)
    for net in (week, quarter):
        with Pool(md) as pool:
            # The oracle: today's live walk (no points), the point's day being today.
            rows, _, _ = walk_tree(net, cfg, md, pool)
            walk = {p: cs for p, cs in rows.items() if p.startswith("files.pythonhosted.org/")}
            assert walk
            assert network_slice(pool, net.name, pid, _settings(md, net.name)) == walk


@pytest.fixture
def vs(monkeypatch):
    fake = FakeUpstream()
    monkeypatch.setattr(vs_provider, "_client", fake.client)
    monkeypatch.setattr(mp, "BACKOFF", 0.0)
    monkeypatch.setattr(mp, "RATE_LIMIT_WAIT", 0.0)
    return fake


def _vs_source():
    return _vs(kinds=["extensions", "server"], platforms=["linux-x64"], qualities=["stable"])


def _sync_vscode(md, src, vs, monkeypatch):
    monkeypatch.setattr(downloader, "_thread_client", vs.client)
    units, summaries = _vs_sync(md, src)
    assert all(s.ok for s in summaries)
    return units


def test_publish_and_bundle_lay_out_one_tree(tmp_path, up, vs, docker, monkeypatch):
    lab = _net("lab", tmp_path, retention="7d")
    pypi_src, vs_src = _src(), _vs_source()
    cfg = _cfg(tmp_path, [pypi_src, vs_src], [lab])
    md = cfg.settings.mirror_dir
    _left_30_days_ago(cfg.model_copy(update={"sources": [pypi_src]}), up, monkeypatch)
    _sync_vscode(md, vs_src, vs, monkeypatch)
    _record(cfg)
    with Pool(md, read_only=True) as pool:
        assert current_repository_points(pool, "vscode")
    published = publish_network(lab, cfg, md)
    assert not published.errors
    bundled = bundle_network(lab, cfg, md, from_ref=None)
    assert not bundled.errors
    applied = _apply(lab, bundled.out_path, tmp_path / "applied")

    def tree(root, top):
        # Not the far side's dated trees (`@YYYY-MM-DD/`: its history).
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in (root / top).rglob("*")
                if p.is_file() and "/@" not in p.relative_to(root).as_posix()}
    for top in ("files.pythonhosted.org", "pypi-data", "vscode-gallery"):
        assert tree(lab.root, top), top
        assert tree(lab.root, top) == tree(applied, top), top
    assert not list(lab.root.rglob(OLD))                        # left 30 days ago; 7d network


def _server_build(commit, ts):
    url = (f"https://vscode.download.prss.microsoft.com/dbazure/download/stable/"
           f"{commit}/vscode-server-linux-x64.tar.gz")
    return {"url": url, "name": "1", "version": commit, "productVersion": "1", "timestamp": ts,
            "sha256hash": hashlib.sha256(fake_asset_bytes(url)).hexdigest()}


def test_vscode_point_keeps_the_previous_two_server_builds(tmp_path, vs, monkeypatch):
    src = _vs(kinds=["server"], platforms=["linux-x64"], qualities=["stable"])
    lab = _net("lab", tmp_path)
    cfg = _cfg(tmp_path, [src], [lab])
    md = cfg.settings.mirror_dir
    vs.update_overrides["cli-alpine-x64"] = 204
    ids = []
    for i, commit in enumerate(("a" * 40, "b" * 40, "c" * 40, "d" * 40), start=1):
        vs.update_overrides["server-linux-x64"] = _server_build(commit, i)
        _sync_vscode(md, src, vs, monkeypatch)
        _record(cfg)
        with Pool(md, read_only=True) as pool:
            ids += current_repository_points(pool, "vscode")
    assert len(set(ids)) == 4
    with Pool(md, read_only=True) as pool:
        settings = _settings(md, "lab")

        def commits(pid):
            return {p.split("/stable/", 1)[1].split("/", 1)[0] for p in network_slice(pool, "lab", pid, settings)
                    if p.endswith("vscode-server-linux-x64.tar.gz")}
        assert commits(ids[1]) == {"a" * 40, "b" * 40}
        assert commits(ids[3]) == {"b" * 40, "c" * 40, "d" * 40}


def _cfg_dir(cfg, tmp_path):
    d = tmp_path / "cfg"
    d.mkdir(exist_ok=True)
    config.save(cfg, d)
    return d


def test_pypi_history_as_of_an_earlier_point(tmp_path, up, monkeypatch):
    lab = _net("lab", tmp_path, retention="current")
    cfg = _cfg(tmp_path, [_src()], [lab])
    md = cfg.settings.mirror_dir
    _sync(cfg)
    _record(cfg)
    first = _pypi_point(md)
    with Pool(md, read_only=True) as pool:
        stamp = pool.points_of([first])[first].stamp
    from mmw.core.publisher import manifest
    monkeypatch.setattr(manifest, "now_stamp", lambda: "20991231T000000Z")
    _bump_pysocks(up)
    _sync(cfg)
    _record(cfg)
    assert _pypi_point(md) != first
    out = tmp_path / "as-of"
    r = CliRunner().invoke(app, ["--config", str(_cfg_dir(cfg, tmp_path)), "publish", "-n", "lab",
                                 "--as-of", stamp, "--root", str(out)])
    assert r.exit_code == 0, r.output
    wheels = {p.name.lower() for p in (out / "files.pythonhosted.org").rglob("*.whl") if "pysocks" in p.name.lower()}
    assert wheels == {OLD.lower()}
    page = (out / "pypi.org" / "simple" / "pysocks" / "index.html").read_text()
    assert "1.7.1" in page and "9.0.0" not in page


def test_history_lists_pypi_and_vscode_points(tmp_path, up, vs, monkeypatch):
    lab = _net("lab", tmp_path)
    pypi_src, vs_src = _src(), _vs(kinds=["server"], platforms=["linux-x64"], qualities=["stable"])
    cfg = _cfg(tmp_path, [pypi_src, vs_src], [lab])
    md = cfg.settings.mirror_dir
    _sync(cfg.model_copy(update={"sources": [pypi_src]}))
    _sync_vscode(md, vs_src, vs, monkeypatch)
    _record(cfg)
    r = CliRunner().invoke(app, ["--config", str(_cfg_dir(cfg, tmp_path)), "history"])
    assert r.exit_code == 0, r.output
    assert "PyPI" in r.output and "VS Code" in r.output, r.output
    assert "legacy" not in r.output.lower()

    from test_snapshot_tui import _run
    from waiting import until

    async def check(tui, pilot):
        await pilot.press("h")
        await until(pilot, lambda: type(tui.screen).__name__ == "HistoryPage"
                    and tui.screen.query("#history-table")
                    and tui.screen.query_one("#history-table").row_count >= 2)
        table = tui.screen.query_one("#history-table")
        text = " ".join(str(c) for row in range(table.row_count) for c in table.get_row_at(row))
        assert "PyPI" in text and "VS Code" in text, text
    (tmp_path / "tui").mkdir()
    _run(cfg, tmp_path / "tui", check)


# ── upgrading ───────────────────────────────────────────────────────────────

def test_first_sync_after_conversion_reuses_the_converted_point(tmp_path, up):
    """A pool whose PyPI history is a network's walk (converted, scoped):
    the first sync records a repository point; the network's slice of it
    equals what it already had, so the old point is linked again and
    nothing is sent twice."""
    lab = _net("lab", tmp_path)
    cfg = _cfg(tmp_path, [_src()], [lab])
    md = cfg.settings.mirror_dir
    _sync(cfg)
    points.record_network_points(lab, cfg, md)            # the old walk: a scoped point
    with Pool(md, read_only=True) as pool:
        (old,) = [p.id for p in pool.network_points("lab") if pool.track_of_point(p.id).repo == "pypi"]
        assert pool.point_scopes(old) == {"lab"}
    first = bundle_network(lab, cfg, md, from_ref=None)
    assert not first.errors
    _record(cfg)                                         # the first sync after upgrading
    with Pool(md, read_only=True) as pool:
        new = _pypi_point(md)
        assert new != old and not pool.points_of([new])[new].scoped
        linked = {p.id for p in pool.network_points("lab") if pool.track_of_point(p.id).repo == "pypi"}
        assert linked == {old}
    from mmw.core.transfer_ranges import preview_transfer
    assert not preview_transfer(lab, md, cfg=cfg).blobs


def test_older_recording_without_a_network_still_reads(tmp_path, up):
    """Settings recorded before networks were (no `network`, mirrors without
    dims): a provider point still slices — every unit it holds, forever."""
    import json
    lab = _net("lab", tmp_path)
    cfg = _cfg(tmp_path, [_src()], [lab])
    md = cfg.settings.mirror_dir
    _sync(cfg)
    _record(cfg)
    pid = _pypi_point(md)
    data = json.loads(_settings(md, "lab"))
    older = json.dumps({"mirrors": data["mirrors"], "repos": data["repos"]})
    with Pool(md, read_only=True) as pool:
        assert files_for_settings(pool, pid, older) == pool.point_files(pid)


def test_slice_of_a_day_old_point_keeps_its_day(tmp_path, up):
    """`as_of` is the point's own day: a point recorded yesterday is narrowed
    from yesterday."""
    from mmw.core.repository_points import provider_view
    lab = _net("lab", tmp_path)
    cfg = _cfg(tmp_path, [_src()], [lab])
    _sync(cfg)
    _record(cfg)
    md = cfg.settings.mirror_dir
    pid = _pypi_point(md)
    with Pool(md, read_only=True) as pool:
        view = provider_view(pool, "pypi", [pid], settings=_settings(md, "lab"))
        stamp = pool.points_of([pid])[pid].stamp
    assert view.as_of == date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8]))
    assert view.as_of <= date.today() + timedelta(days=1)
