"""Serving with a lag: each suite serves its newest point at least that
old — a site setting, or a suite's own. Nothing old enough keeps what is
served (or, serving nothing, the oldest); pins and holds win; time alone
moves it on, at the next apply or status refresh."""
import asyncio

import pytest
from textual.widgets import DataTable
from typer.testing import CliRunner

from mmw import cli
from mmw.site import home as H
from mmw.site import ops, recents, timeline
from mmw.site.db import SiteDB

from test_dry_run_totals import two_mirrors  # noqa: F401
from test_site_apply_timeline import _apply, _bundle, _served_day, _track, env, set_a  # noqa: F401
from test_snapshot_repo_versions import _key
from waiting import until


@pytest.fixture(autouse=True)
def _own_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


@pytest.fixture
def clock(monkeypatch):
    now = {"at": "20261108T120000Z"}
    monkeypatch.setattr(timeline, "_now", lambda: now["at"])
    return now


def _days(env, days, tmp_path):  # noqa: F811
    cfg, net, md, home = env
    for d in days:
        set_a(md, d)
        _apply(home, _bundle(cfg, net, md, tmp_path / f"b{d}"))
    return cfg, home


def _served(home, cfg):
    with SiteDB(home) as db:
        return _served_day(db, _key(cfg, "a"))


def test_lag_reads_and_saves(env):  # noqa: F811
    _cfg, _net, _md, home = env
    assert H.load_site(home).lag is None
    ops.keep_here(home, None, lag="7D")
    assert H.load_site(home).lag == "7d"
    ops.keep_here(home, None, lag="none")
    assert H.load_site(home).lag is None and "\nlag = " not in (home / H.SETTINGS).read_text()
    with pytest.raises(ValueError):
        H.parse_lag("7")
    assert H.lag_words("1d") == "1 day behind" and H.lag_words(None) == "no lag"


def test_lag_serves_the_newest_point_old_enough(env, tmp_path, clock):  # noqa: F811
    ops.keep_here(env[3], None, lag="5d")                 # 8 Nov noon − 5 days: 3 Nov noon
    cfg, home = _days(env, range(1, 6), tmp_path)
    assert _served(home, cfg) == "20261103"


def test_lag_with_nothing_old_enough_keeps_what_it_serves(env, tmp_path, clock):  # noqa: F811
    cfg, home = _days(env, range(1, 4), tmp_path)
    assert _served(home, cfg) == "20261103"
    clock["at"] = "20261104T000000Z"
    ops.keep_here(home, None, lag="7d")                   # nothing is a week old: day 3 stays
    assert _served(home, cfg) == "20261103"
    _days(env, range(4, 6), tmp_path)
    assert _served(home, cfg) == "20261103"


def test_lag_on_an_empty_track_serves_the_oldest(env, tmp_path, clock):  # noqa: F811
    ops.keep_here(env[3], None, lag="30d")
    cfg, net, md, home = env
    set_a(md, 3)
    b3 = _bundle(cfg, net, md, tmp_path / "3", from_ref=None)
    set_a(md, 1)
    _apply(home, _bundle(cfg, net, md, tmp_path / "1", from_ref=None))
    assert _served(home, cfg) == "20261101"
    _apply(home, b3)
    assert _served(home, cfg) == "20261101"                # kept: day 3 isn't 30 days old either


def test_pin_beats_lag(env, tmp_path, clock):  # noqa: F811
    cfg, home = _days(env, range(1, 6), tmp_path)
    ops.serve(home, "a", "2026-11-05")                     # pinned to the newest
    ops.keep_here(home, None, lag="5d")
    ops.status(home)
    assert _served(home, cfg) == "20261105"


def test_hold_beats_lag(env, tmp_path, clock):  # noqa: F811
    cfg, home = _days(env, range(1, 6), tmp_path)
    ops.hold(home, "a")
    ops.keep_here(home, None, lag="5d")
    ops.status(home)
    assert _served(home, cfg) == "20261105"


def test_lag_moves_forward_on_the_next_apply_or_status_refresh(env, tmp_path, clock):  # noqa: F811
    ops.keep_here(env[3], None, lag="5d")
    cfg, home = _days(env, range(1, 6), tmp_path)
    assert _served(home, cfg) == "20261103"
    clock["at"] = "20261109T120000Z"
    ops.status(home)                                       # a status refresh: day 4 is old enough now
    assert _served(home, cfg) == "20261104"
    clock["at"] = "20261110T120000Z"
    _days(env, [6], tmp_path)                              # the next apply: day 5 is old enough, 6 not yet
    assert _served(home, cfg) == "20261105"


def test_a_suites_own_lag(env, tmp_path, clock):  # noqa: F811
    ops.keep_here(env[3], None, lag="5d")
    cfg, home = _days(env, range(1, 6), tmp_path)
    ops.set_track_lag(home, "a", "stable", "none")         # this suite: no lag, whatever the site's
    assert _served(home, cfg) == "20261105"
    ops.set_track_lag(home, "a", "stable", "6d")
    assert _served(home, cfg) == "20261102"
    ops.set_track_lag(home, "a", "stable", None)           # back to the site's
    assert _served(home, cfg) == "20261103"


def test_cli_settings_lag_and_suite_lag(env, tmp_path, clock):  # noqa: F811
    cfg, home = _days(env, range(1, 6), tmp_path)
    root = str(home.parent.parent)
    r = CliRunner().invoke(cli.app, ["site", "settings", "--root", root, "--lag", "5d"])
    assert r.exit_code == 0 and "5 days behind" in r.output, r.output
    assert _served(home, cfg) == "20261103"
    r = CliRunner().invoke(cli.app, ["site", "status", "--root", root])
    assert r.exit_code == 0 and "5 days behind" in r.output, r.output
    r = CliRunner().invoke(cli.app, ["site", "settings", "--root", root, "--lag", "7x"])
    assert r.exit_code == 1 and "7d, or none" in r.output, r.output
    r = CliRunner().invoke(cli.app, ["site", "lag", "a", "--track", "stable", "none", "--root", root])
    assert r.exit_code == 0 and "no lag" in r.output, r.output
    assert _served(home, cfg) == "20261105"
    r = CliRunner().invoke(cli.app, ["site", "lag", "a", "--track", "stable", "default", "--root", root])
    assert r.exit_code == 0 and "site setting" in r.output, r.output
    assert _served(home, cfg) == "20261103"


def _run(check):
    async def go():
        from mmw.site.mset import open_set
        from mmw.tui.site.app import SiteApp
        app = SiteApp(open_set())
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: type(app.screen).__name__ == "SiteHomePage")
            await check(app, pilot)
    asyncio.run(go())


def _status(app) -> str:
    return str(app.screen.query_one("#page-status").render())


def test_pilot_settings_lag_and_suite_settings(env, tmp_path, clock):  # noqa: F811
    cfg, home = _days(env, range(1, 6), tmp_path)
    recents.remember(str(home.parent.parent))
    a = _key(cfg, "a")

    async def check(app, pilot):
        await pilot.press("s")
        await until(pilot, lambda: type(app.screen).__name__ == "SiteSettingsPage")
        app.screen.query_one("#set-lag").value = "5d"
        app.screen.query_one("#btn-set-save").press()
        await until(pilot, lambda: type(app.screen).__name__ == "SiteHomePage" and "5 days behind" in _status(app))
        page = app.screen
        t = page.query_one("#site-history", DataTable)
        await until(pilot, lambda: t.row_count >= 2)
        t.move_cursor(row=next(i for i, (tr, p, *_x) in enumerate(page._history) if p is not None and tr.name))
        await pilot.press("e")
        await until(pilot, lambda: type(app.screen).__name__ == "SuiteSettingsPage")
        app.screen.query_one("#suite-lag").value = "none"
        app.screen.query_one("#btn-suite-save").press()
        await until(pilot, lambda: type(app.screen).__name__ == "SiteHomePage" and "no lag" in _status(app))
    _run(check)
    assert H.load_site(home).lag == "5d"
    with SiteDB(home) as db:
        assert _track(db, a).lag == "none"
    assert _served(home, cfg) == "20261105"
