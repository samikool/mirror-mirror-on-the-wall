"""A version named the way a person names it: v3, a bundle's date, its label."""
import pytest

from mmw.site.db import VersionInfo
from mmw.site.versionref import as_of, bundle_stamp, resolve


def _v(number, stamp, label=None, repo="a"):
    return VersionInfo(number, repo, number, repo, "d", stamp, label)


# newest first, as history gives them; v4 and v5 share a day
VERSIONS = [_v(5, "20261028T150000Z"), _v(4, "20261028T090000Z", "patch-tuesday"),
            _v(3, "20261021T120000Z", "q4-baseline"), _v(2, "20251021T120000Z"), _v(1, "20251001T120000Z")]


def test_number_forms():
    for text in ("3", "v3", "V3", " v3 "):
        assert resolve(VERSIONS, text).number == 3


def test_stamp_prefix():
    assert resolve(VERSIONS, "20261028T15").number == 5
    assert resolve(VERSIONS, "20261028T090000Z").number == 4
    assert resolve(VERSIONS, "20261021").number == 3


def test_iso_and_day_month_dates():
    assert resolve(VERSIONS, "2026-10-21").number == 3
    assert resolve(VERSIONS, "2025-10-21").number == 2
    for text in ("21oct", "21 oct", "oct21", "21-Oct", "21/10", "Oct 21"):
        assert resolve(VERSIONS, text).number == 3, text   # the newest year with that day
    assert resolve(VERSIONS, "1oct").number == 1


def test_two_on_one_day_names_both():
    with pytest.raises(ValueError) as e:
        resolve(VERSIONS, "28oct")
    assert "v4" in str(e.value) and "v5" in str(e.value) and "09:00" in str(e.value)


def test_label_only_when_nothing_else():
    assert resolve(VERSIONS, "q4-baseline").number == 3
    labelled = [_v(2, "20261002T120000Z", "1"), _v(1, "20261001T120000Z")]
    assert resolve(labelled, "1").number == 1          # a number is always a number


def test_unknown_lists_kept_versions():
    with pytest.raises(ValueError) as e:
        resolve(VERSIONS, "nope")
    assert "v5" in str(e.value) and "v1" in str(e.value)
    with pytest.raises(ValueError, match="v9"):
        resolve(VERSIONS, "v9")


def test_as_of_picks_newest_at_or_before():
    assert as_of(VERSIONS, "20261025T000000Z").number == 3
    assert as_of(VERSIONS, "20261028T090000Z").number == 4
    assert as_of(VERSIONS, "20240101T000000Z") is None


def test_bundle_stamp_across_repos():
    b = [_v(2, "20261021T120000Z", "q4-baseline", repo="b"), _v(1, "20261028T090000Z", repo="b")]
    everything = VERSIONS + b
    assert bundle_stamp(everything, "q4-baseline") == "20261021T120000Z"
    assert bundle_stamp(everything, "2026-10-21") == "20261021T120000Z"
    with pytest.raises(ValueError, match="09:00"):
        bundle_stamp(everything, "28oct")
    with pytest.raises(ValueError):
        bundle_stamp(everything, "v3")                 # a number is one repository's, not a bundle


def test_a_label_that_looks_like_a_date():
    labelled = [_v(2, "20261002T120000Z", "1jan"), _v(1, "20261001T120000Z")]
    assert resolve(labelled, "1jan").number == 2
    assert bundle_stamp(labelled, "1jan") == "20261002T120000Z"
