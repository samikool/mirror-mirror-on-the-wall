"""A point named the way a person names it: a date (2026-11-03, 3nov, a
stamp), a label, or its digest."""
import pytest

from datetime import date

# ── timeline points: named by date, stamp, label or digest ───────────────────

from mmw.site.db import PointInfo  # noqa: E402
from mmw.site.versionref import as_of_points, resolve_point  # noqa: E402


def _pt(pid, date, ident, stamp="20261120T000000Z", label=None):
    return PointInfo(pid, 1, ident, date, stamp, label, True)


PTS = [_pt(3, "20261110T100000Z", "release:a1b2c3d4e5", label="before-upgrade"),
       _pt(2, "20261103T090000Z", "release:9f8e7d6c5b", stamp="20261105T000000Z"),
       _pt(1, "20261103T180000Z", "release:77aa0011bb")]
TODAY = date(2026, 11, 14)


def test_point_by_date_label_digest_and_stamp():
    assert resolve_point(PTS, "2026-11-10", TODAY).id == 3
    assert resolve_point(PTS, "before-upgrade", TODAY).id == 3
    assert resolve_point(PTS, "9f8e7d", TODAY).id == 2
    assert resolve_point(PTS, "20261103T09", TODAY).id == 2
    assert resolve_point(PTS, "20261105", TODAY).id == 2          # no point dated then: the arrival stamp


def test_point_ambiguous_date_lists_both():
    with pytest.raises(ValueError, match="matches") as e:
        resolve_point(PTS, "3 Nov", TODAY)
    assert "9f8e7d" in str(e.value) and "77aa00" in str(e.value)


def test_point_v_number_says_dates_now():
    with pytest.raises(ValueError, match="named by date now") as e:
        resolve_point(PTS, "v3", TODAY)
    assert "10 Nov 2026" in str(e.value)


def test_point_unknown():
    with pytest.raises(ValueError, match="no point"):
        resolve_point(PTS, "2026-10-01", TODAY)


def test_as_of_points_counts_that_whole_day():
    assert as_of_points(PTS, "2026-11-03").id == 1               # 18:00 that day counts
    assert as_of_points(PTS, "2026-11-09").id == 1
    assert as_of_points(PTS, "2026-11-10").id == 3
    assert as_of_points(PTS, "2026-11-01") is None


def test_a_day_and_month_means_the_most_recent_one():
    pts = [_pt(2, "20261103T090000Z", "release:bbbbbbbbbb"), _pt(1, "20251103T090000Z", "release:aaaaaaaaaa")]
    assert resolve_point(pts, "3nov", today=date(2026, 11, 14)).id == 2
    assert resolve_point(pts, "3nov", today=date(2026, 3, 1)).id == 1      # this year's hasn't come yet
    assert resolve_point(pts, "2025-11-03").id == 1


def test_not_a_date_nor_a_label():
    with pytest.raises(ValueError):
        resolve_point(PTS, "32nov", today=TODAY)


# ── bundles: named by date, stamp or label across repositories ──────────────

from mmw.site.db import VersionInfo  # noqa: E402
from mmw.site.versionref import bundle_stamp  # noqa: E402


def _v(number, stamp, label=None, repo="a"):
    return VersionInfo(number, repo, number, repo, "d", stamp, label)


def test_bundle_stamp_across_repos():
    vs = [_v(2, "20261028T150000Z"), _v(1, "20261021T120000Z", "q4-baseline"),
          _v(2, "20261021T120000Z", "q4-baseline", repo="b"), _v(1, "20261028T090000Z", repo="b")]
    assert bundle_stamp(vs, "q4-baseline") == "20261021T120000Z"
    assert bundle_stamp(vs, "2026-10-21") == "20261021T120000Z"
    with pytest.raises(ValueError, match="09:00"):
        bundle_stamp(vs, "28oct", today=date(2026, 10, 30))
    with pytest.raises(ValueError, match="isn't a bundle"):
        bundle_stamp(vs, "v3")


def test_a_numeric_bundle_label_is_a_label_not_a_version():
    vs = [_v(2, "20261002T120000Z", "2026"), _v(1, "20261001T120000Z")]
    assert bundle_stamp(vs, "2026") == "20261002T120000Z"


def test_a_bundle_label_that_looks_like_a_date():
    vs = [_v(2, "20261002T120000Z", "1jan"), _v(1, "20261001T120000Z")]
    assert bundle_stamp(vs, "1jan") == "20261002T120000Z"
