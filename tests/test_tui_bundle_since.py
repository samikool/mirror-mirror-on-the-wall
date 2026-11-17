"""Date transfer selection, preview and recovery decision through the pilot."""
from textual.widgets import Input, Select

from mmw.core import points
from mmw.core.publisher.bundle import bundle_network

from pickers import confirm_picker
from test_bundle_format3 import mmw_net  # noqa: F401
from test_bundle_since import clock
from test_dry_run_totals import two_mirrors  # noqa: F401
from test_site_apply_timeline import set_a
from test_snapshot_tui import _finished, _run
from waiting import shows, until


def configured(env, tmp_path):
    cfg, net, md = env
    net = net.model_copy(update={"bundle_dir": tmp_path / "usb"})
    return cfg.model_copy(update={"networks": [net]}), net, md


async def choose_date(app, pilot, date):
    await pilot.press("t")
    await until(pilot, lambda: type(app.screen).__name__ == "BundlePickPage" and app.screen.query("#pick-from"))
    app.screen.query_one("#pick-from", Select).value = "date"
    await until(pilot, lambda: app.screen.query_one("#field-pick-since").display)
    app.screen.query_one("#pick-since", Input).value = date
    app.screen.query_one("#pick-installer", Select).value = "no"


def test_tui_date_previews_and_runs_the_shared_command(mmw_net, tmp_path, monkeypatch):  # noqa: F811
    from mmw.commands import bundle as command
    cfg, net, md = configured(mmw_net, tmp_path)
    clock(monkeypatch, 1)
    first = bundle_network(net, cfg, md)
    set_a(md, 2)
    clock(monkeypatch, 2)
    seen = []
    real = command.transfer

    def spy(*a, **kw):
        seen.append(kw.get("since"))
        return real(*a, **kw)
    monkeypatch.setattr(command, "transfer", spy)

    async def check(app, pilot):
        await choose_date(app, pilot, "2026-11-01")
        assert app.screen.query_one("#pick-kept", Input).value == str(net.bundle_dir)
        await shows(pilot, lambda: app.screen.query_one("#pick-estimate").render(),
                    f"since {first.out_path.name} (its manifest)")
        await confirm_picker(app, pilot, "BundlePickPage")
        await until(pilot, lambda: _finished(app))
        assert app.active_transfer.outcome == "ok", app.notes
        assert "since" in app.active_transfer.summary()
    _run(cfg, md.parent, check)
    assert seen == ["2026-11-01"]


def test_tui_missing_baseline_offers_everything(mmw_net, tmp_path):  # noqa: F811
    cfg, net, md = configured(mmw_net, tmp_path)

    async def check(app, pilot):
        await choose_date(app, pilot, "2000-01-01")
        await confirm_picker(app, pilot, "BundlePickPage")
        await until(pilot, lambda: type(app.screen).__name__ == "ConfirmDialog" and app.screen.query("#confirm"))
        await shows(pilot, lambda: app.screen.query_one("#confirm-message").render(), "Send everything instead?")
        app.screen.query_one("#confirm").press()
        await until(pilot, lambda: _finished(app))
        assert app.active_transfer.outcome == "ok", app.notes
    _run(cfg, md.parent, check)


def test_tui_points_preview(mmw_net, tmp_path):  # noqa: F811
    cfg, net, md = configured(mmw_net, tmp_path)
    points.record_network_points(net, cfg, md, stamp="20261101T120000Z")

    async def check(app, pilot):
        await choose_date(app, pilot, "2026-11-01")
        await shows(pilot, lambda: app.screen.query_one("#pick-estimate").render(), "since the points of 2026-11-01")
    _run(cfg, md.parent, check)
