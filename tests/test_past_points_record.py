"""Recording a repository's past state from an archive, as points — never as
what the network is on now."""
import threading

import httpx
import pytest

from apt_upstream import UpstreamRepo, sync_upstream
from debs import make_deb
from mmw.core import config, downloader, points
from mmw.core.past_points import ARCHIVES, PastPointsFailed, add_past_points
from mmw.core.pool import Pool
from mmw.core.repos import repository_key

UBUNTU = ARCHIVES["ubuntu"]
OLD_DATE = "Fri, 01 Apr 2022 10:00:00 UTC"


@pytest.fixture
def lab(tmp_path):
    hello = make_deb(tmp_path / "u", "hello", "2.10-2")
    up = UpstreamRepo(tmp_path, {("jammy", "main"): [hello]})
    src = up.source(["jammy"])

    class Lab:
        pass
    lab = Lab()
    lab.tmp, lab.up, lab.src, lab.mirror_dir = tmp_path, up, src, tmp_path / "m"
    lab.hello = hello

    def cfg(*networks):
        nets = list(networks) or [config.NetworkConfig(name="lab", root=tmp_path / "pub")]
        return config.Config(settings=config.Settings(mirror_dir=lab.mirror_dir), sources=[src], networks=nets)
    lab.cfg = cfg
    lab.units = sync_upstream(cfg(), src)
    lab.repo = repository_key(lab.units[0])
    lab.recorded = points.record_points_after_sync(cfg(), lab.mirror_dir, {lab.repo}, say=lambda _: None)[0]

    def older(*debs, valid_until=None):
        """Upstream swapped for an older tree (the archive as it was); a deb
        is (name, version) or an existing file."""
        up.valid_until = valid_until
        files = [d if not isinstance(d, tuple) else make_deb(tmp_path / "old", *d) for d in debs]
        up.republish({("jammy", "main"): files}, date=OLD_DATE)
    lab.older = older

    def past(c=None, **kw):
        c = c or cfg()
        return add_past_points(c, lab.mirror_dir, lab.repo, UBUNTU, "ubuntu", "20220401T235959Z",
                               base=up.url, **kw)
    lab.past = past
    yield lab
    up.close()


def _state(lab, network="lab"):
    with Pool(lab.mirror_dir, read_only=True) as pool:
        return (points.current_points(pool, network), pool.latest_recordings(network),
                pool.ref_map(lab.units[0].name))


def test_as_of_now_equals_a_sync(lab):
    report = lab.past()
    assert report.added == [] and report.skipped == [] and report.warnings == []
    assert len(lab.recorded.new) == 1 and report.already == lab.recorded.new
    assert report.fetched == 0 and report.in_pool == 1


def test_a_past_state_is_a_new_point_not_the_current_one(lab):
    before = _state(lab)
    lab.older(("hello", "2.9-1"))
    report = lab.past()
    assert len(report.added) == 1 and "jammy 2022-04-01" in report.added[0]
    assert report.already == [] and report.fetched >= 1
    assert _state(lab) == before                     # refs, current points, recordings: untouched
    with Pool(lab.mirror_dir, read_only=True) as pool:
        tracks = pool.network_tracks("lab")
        dates = sorted(p.date[:8] for t in tracks for p in pool.network_points("lab", t.id)
                       if not t.repo.startswith("network:"))
    assert dates[0] == "20220401" and len(dates) == 2


def test_same_past_state_twice_is_already_had(lab):
    lab.older(("hello", "2.9-1"))
    first = lab.past()
    second = lab.past()
    assert len(first.added) == 1
    assert second.added == [] and second.already == first.added
    assert second.fetched == 0


def test_layered_network_is_skipped_and_named(lab):
    plain = config.NetworkConfig(name="lab", root=lab.tmp / "pub")
    layered = config.NetworkConfig(name="tools", root=lab.tmp / "pub2",
                                   layers=[config.NetworkLayer(source=lab.src.url, signing="unsigned")])
    c = lab.cfg(plain, layered)
    lab.older(("hello", "2.9-1"))
    report = lab.past(c)
    assert len(report.added) == 1
    assert len(report.skipped) == 1 and report.skipped[0].startswith("tools adds its own debs to")
    assert "layered" in report.skipped[0]
    with Pool(lab.mirror_dir, read_only=True) as pool:
        assert pool.network_point_stamps("tools") == {}


def test_files_already_in_the_pool_are_not_fetched(lab):
    lab.older(lab.hello, ("cowsay", "3.03"))               # hello's deb unchanged since the sync
    report = lab.past()
    assert report.in_pool == 1 and report.fetched == 1


def test_archive_failure_records_nothing_and_a_rerun_fetches_only_the_rest(lab, monkeypatch):
    lab.older(("hello", "2.9-1"), ("cowsay", "3.03"))
    real = downloader._thread_client
    failing = {"on": True}

    class Flaky(httpx.BaseTransport):
        def __init__(self):
            self.inner = httpx.HTTPTransport()

        def handle_request(self, request):
            if failing["on"] and request.url.path.endswith(".deb") and "cowsay" in request.url.path:
                return httpx.Response(429, request=request)
            return self.inner.handle_request(request)

    local = threading.local()

    def client():
        if not hasattr(local, "c"):
            local.c = httpx.Client(transport=Flaky(), follow_redirects=True)
        return local.c
    monkeypatch.setattr(downloader, "_thread_client", client)

    before = _state(lab)
    with pytest.raises(PastPointsFailed) as e:
        lab.past()
    assert "cowsay" in str(e.value) and lab.up.url.split("//")[1].split("/")[0] in str(e.value)
    assert _state(lab) == before
    with Pool(lab.mirror_dir, read_only=True) as pool:
        assert all(p.date[:8] != "20220401" for t in pool.network_tracks("lab")
                   for p in pool.network_points("lab", t.id))

    failing["on"] = False
    report = lab.past()
    assert report.fetched == 1 and len(report.added) == 1
    monkeypatch.setattr(downloader, "_thread_client", real)


def test_valid_until_warns_only_when_passed(lab):
    lab.older(("hello", "2.9-1"), valid_until="Fri, 08 Apr 2022 10:00:00 UTC")
    report = lab.past()
    assert len(report.warnings) == 1
    assert "jammy 2022-04-01: Valid-Until passed" in report.warnings[0]
    assert "Check-Valid-Until=false" in report.warnings[0]


def test_valid_until_in_the_future_is_quiet(lab):
    lab.older(("hello", "2.9-1"), valid_until="Fri, 08 Apr 2099 10:00:00 UTC")
    assert lab.past().warnings == []


def test_prune_keeps_the_past_points_files(lab):
    lab.older(("hello", "2.9-1"))
    lab.past()
    with Pool(lab.mirror_dir, read_only=True) as pool:
        tid = next(t.id for t in pool.network_tracks("lab") if not t.repo.startswith("network:"))
        old = next(p for p in pool.network_points("lab", tid) if p.date.startswith("20220401"))
        files = pool.point_files(old.id)
    with Pool(lab.mirror_dir) as pool:
        pool.prune_orphans([lab.repo])
        assert all(pool.contains(cs) for cs in files.values())


def test_bundle_history_carries_the_past_point(lab, tmp_path):
    from mmw.core.publisher.bundle import bundle_network
    import json
    import tarfile
    c = lab.cfg()
    net = c.networks[0]
    bundle_network(net, c, lab.mirror_dir, out_path=tmp_path / "1.tar")
    lab.older(("hello", "2.9-1"))
    lab.past(now="20991101T000000Z")                 # after the first bundle, whatever the clock says
    r = bundle_network(net, c, lab.mirror_dir, out_path=tmp_path / "2.tar", history=True)
    with tarfile.open(r.out_path) as tar:
        header = json.loads(tar.extractfile(".mmw/manifest.jsonl").readline())
    dates = sorted(p["date"][:8] for p in header["points"][lab.repo]["points"])
    assert "20220401" in dates


@pytest.fixture
def two_suites(tmp_path):
    """jammy and jammy-backports synced; the archive's past has only jammy."""
    up = UpstreamRepo(tmp_path, {("jammy", "main"): [make_deb(tmp_path / "u", "hello", "2.10-2")],
                                 ("jammy-backports", "main"): [make_deb(tmp_path / "b", "hello", "2.11-1")]})
    src = up.source(["jammy", "jammy-backports"])
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / "m"), sources=[src],
                        networks=[config.NetworkConfig(name="lab", root=tmp_path / "pub")])
    units = sync_upstream(cfg, src)
    points.record_network_points(cfg.networks[0], cfg, tmp_path / "m")
    up.republish({("jammy", "main"): [make_deb(tmp_path / "old", "hello", "2.9-1")]}, date=OLD_DATE)
    yield cfg, up, repository_key(units[0])
    up.close()


def test_a_suite_not_in_the_archive_then_is_skipped_with_a_warning(two_suites):
    cfg, up, repo = two_suites
    report = add_past_points(cfg, cfg.settings.mirror_dir, repo, UBUNTU, "ubuntu", "20220401T235959Z",
                             base=up.url)
    assert len(report.added) == 1 and "jammy 2022-04-01" in report.added[0]
    assert report.warnings == ["jammy-backports: not in the archive on 2022-04-01 — skipped"]


def test_no_suite_in_the_archive_then_records_nothing_and_says_so(two_suites, tmp_path):
    cfg, up, repo = two_suites
    up.republish({("noble", "main"): [make_deb(tmp_path / "n", "hello", "3.0-1")]}, date=OLD_DATE)
    report = add_past_points(cfg, cfg.settings.mirror_dir, repo, UBUNTU, "ubuntu", "20220401T235959Z",
                             base=up.url)
    assert report.added == [] and report.already == []
    assert len(report.warnings) == 2 and all("not in the archive" in w for w in report.warnings)


def test_only_a_404_means_not_there(two_suites):
    from mmw.core.past_points import plan_past_points, skip_missing_suites
    cfg, up, repo = two_suites
    plan = plan_past_points(cfg, cfg.settings.mirror_dir, repo)
    with pytest.raises(PastPointsFailed, match="HTTP 429"):
        skip_missing_suites(plan, up.url, "20220401T235959Z", status=lambda url: 429)
    kept = skip_missing_suites(plan, up.url, "20220401T235959Z",
                               status=lambda url: 404 if "backports" in url else 200)
    assert {u.suite for u in kept.units} == {"jammy"}
    assert {u.suite for _n, us in kept.networks for u in us} == {"jammy"}
    assert skip_missing_suites(plan, up.url, "x", status=lambda url: 200) is plan


def test_past_points_record_repository_without_a_network(lab):
    cfg = lab.cfg().model_copy(update={'networks': []})
    lab.older(('hello', '2.9-1'))
    report = lab.past(cfg)
    assert len(report.added) == 1
    with Pool(lab.mirror_dir) as pool:
        assert len(pool.repository_points(lab.repo)) == 2
