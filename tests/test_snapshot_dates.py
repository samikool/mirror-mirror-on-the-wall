"""The connected side says dates, not version numbers: a snapshot's suites
by the date upstream signed them, and which changed since the one before;
the far side's apply review likewise."""
from pathlib import Path

from mmw.core import snapshots as S
from mmw.core.pool import Pool

from test_bundle_mmw import _mmw
from test_dry_run_totals import two_mirrors  # noqa: F401
from test_site_apply_timeline import set_a
from test_snapshot_repo_versions import _key


def test_suite_dates_and_what_changed(two_mirrors):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    set_a(md, 1)
    s1 = S.take_snapshot(net, cfg, md, kind="manual")
    set_a(md, 2)
    s2 = S.take_snapshot(net, cfg, md, kind="manual")
    with Pool(md, read_only=True) as pool:
        assert (_key(cfg, "a"), "stable", "2026-11-01") in S.suite_dates(pool, s1)
        assert S.changed_suites(pool, s2, s1) == [(_key(cfg, "a"), "stable", "2026-11-02")]
        assert {r for r, _s, _d in S.changed_suites(pool, s1, None)} == {_key(cfg, "a"), _key(cfg, "b")}




def test_apply_review_says_dates():
    from mmw.site.apply import PlanRow
    from mmw.tui.site.apply_flow import _row_text
    row = PlanRow("r", "Debian", 1, 2, 10, 1000, 1, False, False, Path("/srv"),
                  to_date="20261103T000000Z", serving_date="20261101T000000Z")
    text = _row_text(row, "/srv")
    assert "2026-11-01 → 2026-11-03" in text and "v1" not in text and "v2" not in text
    new = PlanRow("r", "Debian", None, 1, 10, 1000, None, False, False, Path("/srv"), to_date="20261103T000000Z")
    assert "new → 2026-11-03" in _row_text(new, "/srv")
