"""Edges of publishing as of a date: a pool from before recordings, partial
stamps, a blank date, renaming onto a name with leftover markers."""
import pytest

from mmw.core import points
from mmw.core import snapshots as S
from mmw.core.pool import Pool
from mmw.core.publisher import publish_as_of

from test_dry_run_totals import two_mirrors  # noqa: F401
from test_publish_as_of_cli import _cfg_dir
from test_snapshot_publish import _mmw as run


def test_a_snapshot_before_any_recording_can_be_published_by_date(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = two_mirrors
    snap = S.take_snapshot(net, cfg, md, kind="manual")
    with Pool(md) as pool:                              # as a pool from before recordings
        for t in ("network_recordings", "network_settings", "recordings_seeded"):
            pool._conn.execute(f"DROP TABLE {t}")
        pool._conn.commit()
    root = tmp_path / "x"
    publish_as_of(net.model_copy(update={"root": root}), cfg, md, snap.stamp)
    assert (root / "example.com" / "a" / "a.deb").exists()


@pytest.mark.parametrize("text,stamp", [("20261103T12", "20261103T125959Z"),
                                        ("20261103T", "20261103T235959Z"),
                                        ("20261103T1230", "20261103T123059Z"),
                                        ("20261103T123000Z", "20261103T123000Z"),
                                        ("2026-11-03", "20261103T235959Z")])
def test_partial_stamps_mean_the_end_of_what_they_name(text, stamp):
    assert points.resolve_when(text) == stamp


def test_a_blank_date_is_refused(two_mirrors, tmp_path, monkeypatch):  # noqa: F811
    cfg, net, md = two_mirrors
    r = run(monkeypatch, _cfg_dir(cfg, tmp_path), "publish", "-n", net.name, "--as-of", " ")
    assert r.exit_code == 1 and "--as-of" in r.output


def test_a_date_when_no_repository_was_there_says_so(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = two_mirrors
    points.record_network_points(net, cfg, md, stamp="20300101T000000Z")
    with Pool(md) as pool:
        for repo in pool.points_at(net.name, "20300101T000000Z"):
            pool.record_recording(net.name, repo, "20300102T000000Z", [])
    with pytest.raises(ValueError, match="no repository was in oak on 2030-01-02"):
        publish_as_of(net.model_copy(update={"root": tmp_path / "x"}), cfg, md, "2030-01-02")


def test_renaming_onto_a_name_with_only_markers(two_mirrors):  # noqa: F811
    cfg, net, md = two_mirrors
    points.record_network_points(net, cfg, md, stamp="20300101T000000Z")
    with Pool(md) as pool:
        for t in ("points_converted", "recordings_seeded", "sent_migrated"):
            pool._conn.execute(f"INSERT INTO {t} (network) VALUES ('elm')")
        pool._conn.commit()
        assert pool.rename_network(net.name, "elm") is None
        assert pool.points_at("elm", "20300101T000000Z")
