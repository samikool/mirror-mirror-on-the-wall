"""Transfer — one path (Task 21): every bundle, preview and dated publish
reads recorded points, never live refs. A live bundle builds its layers'
views first; only the network's own track (keys, PyPI's pages, the gallery)
comes from a walk."""
# ruff: noqa: F811
import json
import tarfile

import pytest
from apt_upstream import sync_upstream
from debs import make_deb

from mmw.core import config, downloader, points
from mmw.core.layers import rebuild_current_views
from mmw.core.pool import Pool
from mmw.core.publisher import publish_as_of
from mmw.core.publisher.bundle import bundle_network
from mmw.core.transfer_ranges import preview_transfer
from test_layer_views import _make
from test_provider_points import _sync_vscode, _vs_source, vs  # noqa: F401  (fixture)
from test_pypi_publish import _bump_pysocks, _src, _sync

LATEST = "Tue, 03 Nov 2026 06:00:00 UTC"
_NETWORK = downloader._thread_client          # the PyPI and VS Code fakes replace it; APT syncs need it


def _apt_sync(lab, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(downloader, "_thread_client", _NETWORK)
        sync_upstream(lab.cfg, lab.src)


def _read(path):
    """(header, repository rows {path: checksum}, network rows, blob names)."""
    with tarfile.open(path) as tar:
        lines = tar.extractfile(".mmw/manifest.jsonl").read().decode().splitlines()
        blobs = {n.rsplit("/", 1)[1] for n in tar.getnames() if n.startswith("blobs/")}
    header = json.loads(lines[0])
    repo_rows, net_rows = {}, {}
    for row in map(json.loads, lines[1:]):
        (repo_rows if "r" in row else net_rows)[row["p"]] = row["c"]
    return header, repo_rows, net_rows, blobs


def _idents(header):
    return {repo: sorted((p["track"], p["ident"]) for p in entry["points"])
            for repo, entry in header["points"].items()}


@pytest.fixture
def lab(tmp_path, monkeypatch):
    plain = config.NetworkConfig(name="plain", nginx=False, certs=False)
    lab = _make(tmp_path, monkeypatch, networks=[plain])
    lab.plain = plain
    yield lab
    lab.up.close()


def test_bundle_never_walks_live_refs(lab, tmp_path):
    """Refs a sync changed without recording a point (no recording ran)
    don't reach a bundle, layered or not: it carries the recorded points."""
    before = {net.name: _read(bundle_network(net, lab.cfg, lab.md, out_path=tmp_path / f"{net.name}-1.tar",
                                             full=True).out_path)
              for net in (lab.net, lab.plain)}
    with Pool(lab.md, read_only=True) as pool:
        recordings = {n: pool.latest_recordings(n) for n in ("lab", "plain")}
    hello = make_deb(tmp_path / "u", "hello", "2.10-9")
    lab.up.republish({("jammy", "main"): [hello, make_deb(tmp_path / "u", "unrecorded", "1.0")]}, date=LATEST)
    sync_upstream(lab.cfg, lab.src)                       # refs move; no point is recorded
    for net in (lab.net, lab.plain):
        header, rows, net_rows, _ = _read(bundle_network(net, lab.cfg, lab.md, out_path=tmp_path / f"{net.name}-2.tar",
                                                         full=True).out_path)
        old_header, old_rows, old_net_rows, _ = before[net.name]
        assert not any("unrecorded" in p or "2.10-9" in p for p in rows), net.name
        assert rows == old_rows and _idents(header) == _idents(old_header), net.name
        assert set(net_rows) == set(old_net_rows), net.name
        preview = preview_transfer(net, lab.md, cfg=lab.cfg, full=True)
        assert all(row.to_date.startswith("20261102") for row in preview.rows), net.name
    with Pool(lab.md, read_only=True) as pool:
        assert {n: pool.latest_recordings(n) for n in ("lab", "plain")} == recordings


# ── a network with APT (layered), PyPI and VS Code ──────────────────────────

def _record(cfg, sources, stamp, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr("mmw.core.publisher.manifest.now_stamp", lambda: stamp)
        points.record_points_after_sync(cfg, cfg.settings.mirror_dir, sources, say=lambda *_: None)


def _pypi_sync(lab, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(downloader, "_thread_client", lab.pypi_up.client)
        _sync(lab.cfg.model_copy(update={"sources": [lab.pypi]}))


@pytest.fixture
def mixed(tmp_path, monkeypatch, up, vs):
    """`lab` (APT with a layer, synced 1 and 2 Nov) plus PyPI and VS Code,
    both recorded on 2 Nov."""
    with monkeypatch.context() as m:
        m.setattr(downloader, "_thread_client", _NETWORK)
        lab = _make(tmp_path, monkeypatch)
    pypi, code = _src(), _vs_source()
    lab.cfg = lab.cfg.model_copy(update={"sources": [lab.src, pypi, code]})
    lab.pypi, lab.code, lab.pypi_up = pypi, code, up
    _pypi_sync(lab, monkeypatch)
    with monkeypatch.context() as m:
        _sync_vscode(lab.md, code, vs, m)
    _record(lab.cfg, {pypi.identity, code.identity}, "20261102T130000Z", monkeypatch)
    yield lab
    lab.up.close()


def test_network_track_still_travels(mixed, tmp_path):
    """The network's own files — the upstream key, PyPI's pages and PYPI.md,
    the VS Code gallery — go in every bundle, live or dated, as a publish
    lays them out."""
    for name, kw in (("live", {}), ("dated", {"until": "2026-11-02"})):
        header, rows, net_rows, blobs = _read(bundle_network(mixed.net, mixed.cfg, mixed.md,
                                                             out_path=tmp_path / f"{name}.tar", full=True,
                                                             **kw).out_path)
        assert any(p.startswith("keys/") for p in net_rows), (name, sorted(net_rows))
        assert "PYPI.md" in net_rows, name
        assert any(p.startswith("pypi-data/") for p in net_rows), name
        assert any(p.startswith("vscode-gallery/") for p in net_rows), name
        assert set(net_rows.values()) <= blobs, name                 # full: every one of them carried
        assert {"pypi", "vscode", mixed.src.identity} <= set(header["points"]), name
    published = publish_as_of(mixed.net, mixed.cfg, mixed.md, "2026-11-02", root=tmp_path / "as-of")
    assert not published.errors
    for top in ("pypi-data", "vscode-gallery"):
        assert (tmp_path / "as-of" / top).is_dir(), top
    assert (tmp_path / "as-of" / "PYPI.md").is_file()


def test_since_with_mixed_providers(mixed, tmp_path, monkeypatch):
    """`--since` over APT, PyPI, VS Code and a layer: each repository's
    points as of that day are what the far side has; only what changed
    after goes — an upstream sync, a PyPI release, a layer edit."""
    lab = mixed
    first = bundle_network(lab.net, lab.cfg, lab.md, out_path=tmp_path / "first.tar", full=True)
    _, first_rows, _, _ = _read(first.out_path)
    # After 2 Nov: APT syncs, PyPI gains a release, the layer gains a package.
    lab.up.republish({("jammy", "main"): [make_deb(tmp_path / "u", "hello", "2.10-3"),
                                          make_deb(tmp_path / "u", "cowsay", "3.03")]}, date=LATEST)
    _apt_sync(lab, monkeypatch)
    _bump_pysocks(lab.pypi_up)
    _pypi_sync(lab, monkeypatch)
    _record(lab.cfg, {lab.src.identity, lab.pypi.identity}, "20261103T120000Z", monkeypatch)
    make_deb(lab.folder, "new-tool", "1.0")
    assert not rebuild_current_views(lab.cfg, lab.md, "lab").errors

    preview = preview_transfer(lab.net, lab.md, cfg=lab.cfg, since="2026-11-02")
    result = bundle_network(lab.net, lab.cfg, lab.md, out_path=tmp_path / "since.tar", since="2026-11-02")
    assert not result.errors
    assert result.since_source == preview.source == "repository points as of 2026-11-02"
    header, rows, _, blobs = _read(result.out_path)
    assert {"pypi", "vscode", lab.src.identity} <= set(header["points"])

    def carried(fragment):
        return [p for p, cs in rows.items() if fragment in p and cs in blobs]
    assert carried("hello_2.10-3"), "the new upstream package"
    assert carried("new-tool_1.0"), "the layer edit"
    assert carried("pysocks-9.0.0"), "the new PyPI release"
    # As of 2 Nov the far side has these: they don't go again.
    for path, cs in rows.items():
        if first_rows.get(path) == cs and ("/pool/" in path and "cowsay" in path or "vscode" in path
                                           or path.startswith("files.pythonhosted.org/")):
            assert cs not in blobs, path
    assert result.sent == preview.blobs


def test_converted_pool_after_a_first_sync_still_transfers(tmp_path, up):
    """A converted (scoped) point can stay a network's recorded point after
    the first sync (the reuse rule). With no legacy path left, it is served
    through its network's slice: publish as of a date, a dated bundle and
    --since all work, PyPI's pages included."""
    from test_one_model_conversion import old_pool
    cfg, md, a, layered = old_pool.__wrapped__(tmp_path, up)
    apt, pypi = cfg.sources
    assert points.prepare_history(md)
    points.record_points_after_sync(cfg, md, {apt.identity, pypi.identity}, say=lambda *_: None)
    with Pool(md, read_only=True) as pool:
        recorded = {n.name: pool.latest_recordings(n.name) for n in (a, layered)}
        scoped = {n: [pid for ids in rec.values() for pid in ids if pool.point_scopes(pid)]
                  for n, rec in recorded.items()}
    assert any(scoped.values()), recorded                      # the reuse rule kept a converted point
    from mmw.site.versionref import day
    today = day(__import__("mmw.core.publisher.manifest", fromlist=["now_stamp"]).now_stamp())
    for net in (a, layered):
        out = tmp_path / f"as-of-{net.name}"
        published = publish_as_of(net, cfg, md, today, root=out)
        assert not published.errors, (net.name, published.errors)
        dated = bundle_network(net, cfg, md, out_path=tmp_path / f"{net.name}-dated.tar", until=today, full=True)
        assert not dated.errors
        header, rows, net_rows, _ = _read(dated.out_path)
        assert rows
        tree = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
        assert set(rows) <= tree, sorted(set(rows) - tree)[:5]
        # As of today the far side has all of it (the dated bundle carried it).
        assert preview_transfer(net, md, cfg=cfg, since=today).blobs == 0, net.name
        with pytest.raises(ValueError, match="nothing to send"):
            bundle_network(net, cfg, md, out_path=tmp_path / f"{net.name}-since.tar", since=today)
    assert any(p.startswith("pypi-data/") for p in (p.relative_to(tmp_path / "as-of-a").as_posix()
                                                    for p in (tmp_path / "as-of-a").rglob("*")))
    assert any(p.startswith("pypi-data/") for p in _read(tmp_path / "a-dated.tar")[2])
