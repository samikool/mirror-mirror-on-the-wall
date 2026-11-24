"""Far side — parity over every provider (Task 22): one model serves APT,
PyPI, VS Code and a layer the same on both sides. A `--history` bundle
applied far away, served as of each date, lays out what `publish --as-of`
lays out here; old snapshot-format bundles still replay; lag and pins work
on PyPI's one "" track."""
# ruff: noqa: F811
import pytest
from apt_upstream import sync_upstream
from debs import make_deb

from mmw.core import downloader
from mmw.core.publisher import publish_as_of, publish_network, publish_snapshot
from mmw.core.publisher.bundle import bundle_network
from mmw.core.snapshots import take_snapshot
from mmw.site import ops, timeline
from mmw.site.db import SiteDB
from mmw.site.home import root_home
from test_bundle import _sign_lab_cert
from test_deployment_parity import _apply, _files
from test_layer_views import _make
from test_one_path_transfer import _NETWORK, _pypi_sync, _record
from test_provider_points import _server_build, _sync_vscode, _vs_source, vs  # noqa: F401  (fixture)
from test_pypi_publish import _bump_pysocks, _src
from test_vscode_publish import docker  # noqa: F401  (fixture)

DATES = ("2026-11-02", "2026-11-03", "2026-11-04")
RELEASE = {"2026-11-03": "Tue, 03 Nov 2026 06:00:00 UTC", "2026-11-04": "Wed, 04 Nov 2026 06:00:00 UTC"}


def _apt(lab, date, debs, monkeypatch):
    lab.up.republish({("jammy", "main"): debs}, date=RELEASE[date])
    with monkeypatch.context() as m:
        m.setattr(downloader, "_thread_client", _NETWORK)
        sync_upstream(lab.cfg, lab.src)


def _vscode(lab, n, monkeypatch):
    lab.vs.update_overrides["server-linux-x64"] = _server_build(str(n) * 40, n)
    with monkeypatch.context() as m:
        _sync_vscode(lab.md, lab.code, lab.vs, m)


def _sync_all(lab, monkeypatch, stamp):
    _pypi_sync(lab, monkeypatch)
    _vscode(lab, int(stamp[6:8]), monkeypatch)
    _record(lab.cfg, {lab.src.identity, lab.pypi.identity, lab.code.identity}, stamp, monkeypatch)


@pytest.fixture
def second(tmp_path, monkeypatch, up, vs, docker):
    """Network `lab` (TLS on, signed) with APT and a layer, PyPI and VS Code,
    as of 2 Nov: APT synced 1 and 2 Nov (each Release dated its day), PyPI
    and VS Code recorded 2 Nov, after the Release."""
    with monkeypatch.context() as m:
        m.setattr(downloader, "_thread_client", _NETWORK)
        lab = _make(tmp_path, monkeypatch)
    lab.net = lab.net.model_copy(update={"certs": True})
    _sign_lab_cert(tmp_path, lab.md, ["127.0.0.1"])
    lab.pypi, lab.code, lab.pypi_up, lab.vs = _src(), _vs_source(), up, vs
    lab.cfg = lab.cfg.model_copy(update={"sources": [lab.src, lab.pypi, lab.code], "networks": [lab.net]})
    _sync_all(lab, monkeypatch, "20261102T130000Z")
    yield lab
    lab.up.close()


def _third(lab, monkeypatch):
    """3 Nov: every provider moves and the layer gains a package."""
    u = lab.tmp / "u"
    _apt(lab, "2026-11-03", [make_deb(u, "hello", "2.10-3"), make_deb(u, "cowsay", "3.03")], monkeypatch)
    _bump_pysocks(lab.pypi_up)
    make_deb(lab.folder, "new-tool", "1.0")
    _sync_all(lab, monkeypatch, "20261103T070000Z")


def _fourth(lab, monkeypatch):
    """4 Nov: APT and VS Code move; PyPI syncs again."""
    u = lab.tmp / "u"
    _apt(lab, "2026-11-04", [make_deb(u, "hello", "2.10-3"), make_deb(u, "sl", "5.02")], monkeypatch)
    _sync_all(lab, monkeypatch, "20261104T070000Z")


@pytest.fixture
def three(second, monkeypatch):
    _third(second, monkeypatch)
    _fourth(second, monkeypatch)
    return second


def _history_far(lab, tmp_path):
    bundle = bundle_network(lab.net, lab.cfg, lab.md, out_path=tmp_path / "history.tar", full=True, history=True)
    assert not bundle.errors, bundle.errors
    far = _apply(lab.net, bundle.out_path, tmp_path / "far")
    return far, root_home(far)


def _diff(a, b):
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def test_history_bundle_applies_to_publish_at_each_point(three, tmp_path):
    lab = three
    far, home = _history_far(lab, tmp_path)
    with SiteDB(home) as db:
        repos = [s.repo for s in db.repo_states()]
    assert {"pypi", "vscode", lab.src.identity} <= set(repos), repos
    seen = []
    for date in DATES:
        here = tmp_path / f"as-of-{date}"
        published = publish_as_of(lab.net, lab.cfg, lab.md, date, root=here)
        assert not published.errors, (date, published.errors)
        for repo in repos:
            ops.serve(home, repo, date)
        there = _files(far)
        assert _files(here) == there, (date, _diff(_files(here), there))
        seen.append(there)
    assert seen[0] != seen[1] != seen[2]


def test_snapshot_replay_still_matches(second, tmp_path, monkeypatch):
    """A snapshot (the old format) of a network with every provider, taken
    on 2 Nov: after a later sync its replay, and the bundle made from it
    applied far away, lay out what was published live that day."""
    lab = second
    live = publish_network(lab.net, lab.cfg, lab.md)
    assert not live.errors, live.errors
    then = _files(lab.net.root)
    snap = take_snapshot(lab.net, lab.cfg, lab.md, label="2 Nov")
    _third(lab, monkeypatch)
    replay = tmp_path / "replay"
    assert not publish_snapshot(lab.net, lab.cfg, lab.md, snap.stamp, root=replay).errors
    assert _files(replay) == then, _diff(_files(replay), then)
    bundle = bundle_network(lab.net, lab.cfg, lab.md, to_ref=snap.stamp, from_ref=None,
                            out_path=tmp_path / "snapshot.tar")
    assert not bundle.errors, bundle.errors
    applied = _apply(lab.net, bundle.out_path, tmp_path / "applied")
    assert _files(applied) == then, _diff(_files(applied), then)
    for top in ("files.pythonhosted.org", "pypi-data", "vscode-gallery", "PYPI.md"):
        assert any(p.startswith(top) for p in then), top


def _pypi_day(home) -> str:
    with SiteDB(home) as db:
        (t,) = db.tracks("pypi")
        (pid,) = [p for p in db.state_points(db.repo_state("pypi").serving.id) if db.point(p).track == t.id]
        return db.point(pid).date[:8]


def test_lag_and_pin_on_the_pypi_track(second, tmp_path, monkeypatch):
    """PyPI's one "" track lags, takes its own lag, pins and moves forward
    like any suite."""
    lab = second
    _third(lab, monkeypatch)                                  # PyPI points of 2 and 3 Nov
    now = {"at": "20261105T060000Z"}
    monkeypatch.setattr(timeline, "_now", lambda: now["at"])
    far, home = _history_far(lab, tmp_path)
    with SiteDB(home) as db:
        assert [t.name for t in db.tracks("pypi")] == [""]
    assert _pypi_day(home) == "20261103"
    ops.keep_here(home, None, lag="2d")                       # 5 Nov 06:00 − 2 days: 3 Nov 06:00
    assert _pypi_day(home) == "20261102"
    ops.set_track_lag(home, "pypi", "files", "none")          # its own ("files"): no lag
    assert _pypi_day(home) == "20261103"
    ops.set_track_lag(home, "pypi", "files", None)            # back to the site's
    assert _pypi_day(home) == "20261102"
    now["at"] = "20261105T120000Z"
    ops.status(home)                                          # time alone: 3 Nov 07:00 is old enough now
    assert _pypi_day(home) == "20261103"
    ops.serve(home, "pypi", "2026-11-02")                     # a pin beats the lag
    assert _pypi_day(home) == "20261102"
    ops.status(home)
    assert _pypi_day(home) == "20261102"
    ops.forward(home, "pypi")
    assert _pypi_day(home) == "20261103"


def test_pages_move_from_the_network_to_their_point(second, tmp_path, monkeypatch):
    """A far side whose PyPI pages and gallery came as network files (a
    bundle from before pages travelled with their point) takes them over
    from the next bundle's points: the tree equals a publish, and so does
    serving any date whose points brought their pages."""
    from mmw.core.publisher import bundle as B
    lab = second
    with monkeypatch.context() as m:
        m.setattr(B, "_pages_of", lambda *a: (lambda pid: {}))
        old = bundle_network(lab.net, lab.cfg, lab.md, out_path=tmp_path / "old.tar", full=True)
    assert not old.errors
    far = _apply(lab.net, old.out_path, tmp_path / "far")
    home = root_home(far)
    with SiteDB(home) as db:
        assert any(p.startswith("pypi-data/") for p in db.network_files())
    _third(lab, monkeypatch)
    new = bundle_network(lab.net, lab.cfg, lab.md, out_path=tmp_path / "new.tar")
    assert not new.errors, new.errors
    _apply(lab.net, new.out_path, far)
    here = tmp_path / "3"
    assert not publish_as_of(lab.net, lab.cfg, lab.md, "2026-11-03", root=here).errors
    assert _files(far) == _files(here), _diff(_files(far), _files(here))
    _fourth(lab, monkeypatch)
    _apply(lab.net, bundle_network(lab.net, lab.cfg, lab.md, out_path=tmp_path / "next.tar").out_path, far)
    for date in ("2026-11-03", "2026-11-04"):
        for repo in ("pypi", "vscode", lab.src.identity):
            ops.serve(home, repo, date)
        here = tmp_path / f"as-of-{date}"
        assert not publish_as_of(lab.net, lab.cfg, lab.md, date, root=here).errors
        assert _files(far) == _files(here), (date, _diff(_files(far), _files(here)))
