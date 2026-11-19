"""Publishing as of a date from the command line and the TUI."""
import pytest
from textual.widgets import Input, Select

from mmw.core import config, points
from mmw.core.pool import Pool

from pickers import confirm_picker
from test_dry_run_totals import two_mirrors  # noqa: F401
from test_snapshot_publish import _mmw as run
from test_snapshot_tui import _finished, _move_on, _run
from waiting import until


def _cfg_dir(cfg, tmp_path):
    d = tmp_path / "cfg"
    d.mkdir()
    config.save(cfg, d)
    return d


def test_cli_as_of_reaches_publish(two_mirrors, tmp_path, monkeypatch):  # noqa: F811
    cfg, net, md = two_mirrors
    points.record_network_points(net, cfg, md, stamp="20300101T000000Z")
    seen = []
    from mmw.commands import publish as pub
    real = pub.publish_as_of
    monkeypatch.setattr(pub, "publish_as_of", lambda *a, **k: seen.append(a[3]) or real(*a, **k))
    r = run(monkeypatch, _cfg_dir(cfg, tmp_path), "publish", "-n", net.name, "--as-of", "2030-01-01")
    assert r.exit_code == 0, r.output
    assert seen == ["2030-01-01"] and "as of 2030-01-01" in r.output
    assert (net.root / "example.com" / "a" / "a.deb").exists()


@pytest.mark.parametrize("extra,message", [
    (["--dry-run"], "--as-of")])
def test_as_of_combinations(two_mirrors, tmp_path, monkeypatch, extra, message):  # noqa: F811
    cfg, net, md = two_mirrors
    r = run(monkeypatch, _cfg_dir(cfg, tmp_path), "publish", "-n", net.name, "--as-of", "2030-01-01", *extra)
    assert r.exit_code == 1 and message in r.output


def test_as_of_needs_a_network(two_mirrors, tmp_path, monkeypatch):  # noqa: F811
    cfg, net, md = two_mirrors
    r = run(monkeypatch, _cfg_dir(cfg, tmp_path), "publish", "--as-of", "2030-01-01")
    assert r.exit_code == 1 and "-n" in r.output


def test_the_publish_page_takes_a_date(two_mirrors):  # noqa: F811
    cfg, net, md = two_mirrors
    points.record_network_points(net, cfg, md, stamp="20300101T000000Z")
    _move_on(md)
    points.record_network_points(net, cfg, md, stamp="20300102T000000Z")

    async def check(app, pilot):
        await pilot.press("p")
        await until(pilot, lambda: type(app.screen).__name__ == "PublishPickPage"
                    and app.screen.query("#pick-target"))
        app.screen.query_one("#pick-target", Select).value = "date"
        await until(pilot, lambda: app.screen.query_one("#field-pick-when").display)
        app.screen.query_one("#pick-when", Input).value = "2030-01-01"
        await confirm_picker(app, pilot, "PublishPickPage")
        await until(pilot, lambda: _finished(app))
        assert app.active_transfer.outcome == "ok", app.notes
    _run(cfg, md.parent, check)
    assert (net.root / "example.com" / "a" / "a.deb").exists()
    assert not (net.root / "example.com" / "a" / "a2.deb").exists()
    with Pool(md, read_only=True) as pool:
        assert [p.target for p in pool.published("oak")] == ["as-of:20300101T235959Z"]
