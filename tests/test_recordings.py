"""Each network's recordings over time: which points each repository had at
every recording that changed them, and the walk's settings — what
publishing as of a date reads."""
from mmw.core import legacy_points
from mmw.core import points
from mmw.core import snapshots as S
from mmw.core.pool import Pool

from test_bundle_mmw import _mmw
from test_dry_run_totals import two_mirrors  # noqa: F401
from test_site_apply_timeline import set_a
from test_snapshot_repo_versions import _key


def _rec(cfg, net, md, stamp):
    return points.record_network_points(net, cfg, md, stamp=stamp)


def test_a_recording_is_kept_when_points_change(two_mirrors):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    a = _key(cfg, "a")
    set_a(md, 1)
    _rec(cfg, net, md, "20300101T000000Z")
    set_a(md, 2)
    _rec(cfg, net, md, "20300102T000000Z")
    with Pool(md, read_only=True) as pool:
        first, second = pool.points_at(net.name, "20300101T120000Z"), pool.points_at(net.name, "20300103T000000Z")
        assert first[a] != second[a] and first[_key(cfg, "b")] == second[_key(cfg, "b")]
        assert pool.points_at(net.name, "20291231T000000Z") == {}
        assert pool.first_recording(net.name) == "20300101T000000Z"


def test_unchanged_repo_adds_no_recording(two_mirrors):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    set_a(md, 1)
    _rec(cfg, net, md, "20300101T000000Z")
    _rec(cfg, net, md, "20300102T000000Z")
    with Pool(md, read_only=True) as pool:
        n = pool._conn.execute("SELECT count(*) FROM network_recordings WHERE network = ?",
                               (net.name,)).fetchone()[0]
        assert n == len(pool.points_at(net.name, "99999999T999999Z"))


def test_recordings_track_a_repository_leaving(two_mirrors):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    set_a(md, 1)
    _rec(cfg, net, md, "20300101T000000Z")
    cfg2 = cfg.model_copy(update={"sources": [s for s in cfg.sources if s.name != "b"]})
    _rec(cfg2, net, md, "20300102T000000Z")
    with Pool(md, read_only=True) as pool:
        assert _key(cfg, "b") in pool.points_at(net.name, "20300101T000000Z")
        assert _key(cfg, "b") not in pool.points_at(net.name, "20300102T000000Z")
        assert _key(cfg, "b") not in pool.points_at(net.name, "99999999T999999Z")


def test_settings_at(two_mirrors):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    set_a(md, 1)
    report = _rec(cfg, net, md, "20300101T000000Z")
    with Pool(md, read_only=True) as pool:
        assert pool.settings_at(net.name, "20300101T000000Z") == report.settings
        assert pool.settings_at(net.name, "20291231T000000Z") is None


def test_seeded_from_snapshots_once(two_mirrors):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    set_a(md, 1)
    snap = S.take_snapshot(net, cfg, md, kind="manual")
    with Pool(md) as pool:
        pool._conn.execute("DELETE FROM network_recordings")
        pool._conn.execute("DELETE FROM recordings_seeded")
        pool._conn.commit()
        assert legacy_points.seed_recordings(pool, net.name) == 1
        assert legacy_points.seed_recordings(pool, net.name) == 0
        assert set(pool.points_at(net.name, snap.stamp)) >= {_key(cfg, "a"), _key(cfg, "b")}
        assert pool.settings_at(net.name, snap.stamp) == snap.settings


def test_rename_and_forget_carry_points_and_sent_records(two_mirrors, tmp_path):  # noqa: F811
    from mmw.core.publisher.bundle import bundle_network
    cfg, net, md = _mmw(two_mirrors)
    set_a(md, 1)
    bundle_network(net, cfg, md, out_path=tmp_path / "1.tar")
    with Pool(md) as pool:
        before = (pool.points_at(net.name, "99999999T999999Z"), pool.sent_point_ids(net.name),
                  pool.points_at(net.name, "29991231T000000Z"))
        assert all(before)
        pool.rename_network(net.name, "elm")
        assert (pool.points_at("elm", "99999999T999999Z"), pool.sent_point_ids("elm"),
                pool.points_at("elm", "29991231T000000Z")) == before
        assert pool.points_at(net.name, "99999999T999999Z") == {} and pool.sent_point_ids(net.name) == set()
        pool.forget_network("elm")
        assert pool.points_at("elm", "99999999T999999Z") == {} and pool.sent_point_ids("elm") == set()
        assert pool.points_at("elm", "29991231T000000Z") == {} and not pool.sent_migrated("elm")


def test_a_date_before_the_first_sync_says_it_comes_from_a_snapshot(two_mirrors):  # noqa: F811
    """Before mmw recorded every sync, a date reads the snapshot before it:
    publishing it says so, with when the first sync was recorded."""
    cfg, net, md = _mmw(two_mirrors)
    set_a(md, 1)
    snap = S.take_snapshot(net, cfg, md, kind="manual")
    with Pool(md) as pool:
        pool._conn.execute("DELETE FROM network_recordings")
        pool._conn.execute("DELETE FROM recordings_seeded")
        pool._conn.commit()
        legacy_points.seed_recordings(pool, net.name, now="20990101T000000Z")      # the upgrade
    with Pool(md, read_only=True) as pool:
        note = points.seeded_note(pool, net.name, "20500101T000000Z")
        assert note and f"snapshot of {snap.stamp[:4]}-" in note and "since 2099-01-01" in note
        assert points.seeded_note(pool, net.name, "20990102T000000Z") is None
        assert points.seeded_note(pool, net.name, "20000101T000000Z") is None    # before any snapshot


def test_a_network_recorded_from_its_first_sync_has_no_seeded_note(two_mirrors):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    set_a(md, 1)
    _rec(cfg, net, md, "20300101T000000Z")
    _rec(cfg, net, md, "20300201T000000Z")
    with Pool(md, read_only=True) as pool:
        assert points.seeded_note(pool, net.name, "20300115T000000Z") is None


def test_a_pool_seeded_before_its_moment_was_kept_uses_its_first_sync(two_mirrors):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    set_a(md, 1)
    S.take_snapshot(net, cfg, md, kind="manual")
    with Pool(md) as pool:
        pool._conn.execute("DELETE FROM network_recordings")
        pool._conn.execute("DELETE FROM recordings_seeded")
        pool._conn.commit()
        legacy_points.seed_recordings(pool, net.name)
        pool._conn.execute("DELETE FROM recordings_seeded_at")       # as a build before it seeded
        pool._conn.commit()
    set_a(md, 2)
    _rec(cfg, net, md, "20990101T000000Z")
    with Pool(md, read_only=True) as pool:
        assert "since 2099-01-01" in points.seeded_note(pool, net.name, "20500101T000000Z")
        assert points.seeded_note(pool, net.name, "20990102T000000Z") is None


def test_listed_suites_follow_a_renamed_network(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    with Pool(md) as pool:
        pool.set_listed_tracks(net.name, {"r": {1, 2}})
        pool.rename_network(net.name, "elm")
        assert pool.listed_tracks("elm") == {"r": {1, 2}} and pool.listed_tracks(net.name) == {}
