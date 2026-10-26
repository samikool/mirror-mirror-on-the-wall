"""A version named the way a person names it: v3, a bundle's date, its label."""
import pytest

from datetime import date

from mmw.site.db import VersionInfo
from mmw.site.versionref import as_of, bundle_stamp, resolve


def _v(number, stamp, label=None, repo="a"):
    return VersionInfo(number, repo, number, repo, "d", stamp, label)


TODAY = date(2026, 10, 30)

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
    for text in ("21oct", "21 oct", "oct21", "21-Oct", "21/10", "Oct 21", "21 October", "oct. 21"):
        assert resolve(VERSIONS, text, today=TODAY).number == 3, text


def test_a_day_and_month_means_the_most_recent_one():
    # 1 Oct 2026 has no version here; 1 Oct 2025 does, but it is never picked by surprise.
    with pytest.raises(ValueError, match="2025-10-01"):
        resolve(VERSIONS, "1oct", today=TODAY)
    assert resolve(VERSIONS, "2025-10-01").number == 1
    # Before 21 Oct comes round again, 21oct is last year's.
    assert resolve(VERSIONS, "21oct", today=date(2026, 3, 1)).number == 2


def test_not_a_date():
    for text in ("31feb", "1/13", "oct2026", "2026-02-30", "20261340"):
        with pytest.raises(ValueError, match="isn't a date"):
            resolve(VERSIONS, text, today=TODAY)


def test_two_on_one_day_names_both():
    with pytest.raises(ValueError) as e:
        resolve(VERSIONS, "28oct", today=TODAY)
    assert "v4" in str(e.value) and "v5" in str(e.value) and "09:00" in str(e.value)


def test_labels():
    assert resolve(VERSIONS, "q4-baseline").number == 3
    assert resolve(VERSIONS, "Q4-Baseline").number == 3               # any case
    looks_dated = [_v(2, "20261002T120000Z", "28 octopus"), _v(1, "20261001T120000Z", "1 Oct")]
    assert resolve(looks_dated, "28 octopus", today=TODAY).number == 2
    assert resolve(looks_dated, "1 oct", today=TODAY).number == 1     # a label before a date
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
        bundle_stamp(everything, "28oct", today=TODAY)
    with pytest.raises(ValueError):
        bundle_stamp(everything, "v3")                 # a number is one repository's, not a bundle


def test_a_label_that_looks_like_a_date():
    labelled = [_v(2, "20261002T120000Z", "1jan"), _v(1, "20261001T120000Z")]
    assert resolve(labelled, "1jan").number == 2
    assert bundle_stamp(labelled, "1jan") == "20261002T120000Z"
