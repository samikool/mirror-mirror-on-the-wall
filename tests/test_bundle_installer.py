"""mmw's installer: written beside an mmw network's bundle when asked; bundles never carry it."""
import tarfile
from pathlib import Path

import pytest

from mmw.core import runtime as R
from mmw.core.publisher.bundle import bundle_network, stage_network

from test_bundle_mmw import _mmw
from test_dry_run_totals import two_mirrors  # noqa: F401 — the shared pool fixture
from test_runtime import FakeFetcher


@pytest.fixture(autouse=True)
def _a_release(monkeypatch):
    """As an installed release would: PyPI's wheel, no checkout build."""
    monkeypatch.setattr(R, "source_checkout", lambda: None)


def test_bundle_never_carries_runtime(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    r = bundle_network(net, cfg, md, out_path=tmp_path, from_ref=None, fetcher=FakeFetcher())
    with tarfile.open(r.out_path) as tar:
        assert not [n for n in tar.getnames() if n.startswith(".mmw/runtime")]
    assert r.installers == [] and not list(tmp_path.glob("mmw-*.tar"))


def test_installer_written_beside_bundle(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    r = bundle_network(net, cfg, md, out_path=tmp_path, from_ref=None, installer=True, fetcher=FakeFetcher())
    want = r.out_path.parent / f"mmw-{R.mmw_version()}-x86_64.tar"
    assert r.installers == [want] and want.exists()


def test_installer_beside_stage(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    r = stage_network(net, cfg, md, tmp_path / "s", from_ref=None, installer=True, fetcher=FakeFetcher())
    assert r.installers == [r.out_path.parent / f"mmw-{R.mmw_version()}-x86_64.tar"]
    assert not list(r.out_path.rglob("mmw-*.tar"))                    # beside the stage, not in it


def test_installer_with_stdout_is_refused_up_front(two_mirrors, tmp_path):  # noqa: F811
    import io

    from mmw.core.pool import Pool
    cfg, net, md = _mmw(two_mirrors)
    with Pool(md) as p:
        before = len(p.snapshots(net.name))
    with pytest.raises(ValueError, match="mmw installer --out"):
        bundle_network(net, cfg, md, out_path=Path("-"), stdout=io.BytesIO(), from_ref=None, installer=True,
                       fetcher=FakeFetcher())
    with Pool(md) as p:
        assert len(p.snapshots(net.name)) == before


def test_installer_failure_never_costs_the_bundle(two_mirrors, tmp_path):  # noqa: F811
    class Offline(FakeFetcher):
        def uv_binary(self, version, arch):
            raise R.FetchError("no route to host")
    cfg, net, md = _mmw(two_mirrors)
    r = bundle_network(net, cfg, md, out_path=tmp_path, from_ref=None, installer=True, fetcher=Offline())
    assert r.out_path.exists() and r.sent > 0
    assert "mmw installer not written (no route to host)" in r.warnings
    assert r.installers == [] and not list(tmp_path.glob("mmw-*"))


def test_cli_installer_flag(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from mmw.cli import app
    from mmw.commands import bundle as bundle_cmd
    from mmw.core import config
    from mmw.core.publisher.results import BundleResult
    (tmp_path / "mmw.toml").write_text('[settings]\nmirror_dir = "/tmp/pool"\n')
    net = config.NetworkConfig(name="lab", bundle_dir=Path("/tmp/out"), far_side="mmw")
    monkeypatch.setattr(config, "load", lambda d: config.Config(
        settings=config.Settings(mirror_dir=Path("/tmp/pool")), sources=[], networks=[net]))
    seen = []

    def fake(net, cfg, mirror_dir, out_path=None, from_ref=None, to_ref="now", **kw):
        seen.append({k: v for k, v in kw.items() if k != "progress"})
        return BundleResult(network_name="lab", out_path=Path("/tmp/out/x.tar"),
                            installers=[Path("/tmp/out/mmw-1.2.0-x86_64.tar")])
    monkeypatch.setattr(bundle_cmd, "bundle_network", fake)
    r = CliRunner().invoke(app, ["--config", str(tmp_path), "bundle", "-n", "lab", "--installer"])
    assert r.exit_code == 0, r.output
    assert seen == [{"installer": True}]
    assert "mmw installer: /tmp/out/mmw-1.2.0-x86_64.tar" in r.output
    assert "--no-runtime" not in CliRunner().invoke(app, ["bundle", "--help"]).output


def test_cli_installer_command(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from mmw.cli import app
    from mmw.commands import installer as inst_cmd
    monkeypatch.setattr(inst_cmd, "default_fetcher", lambda stop_event=None: FakeFetcher())
    (tmp_path / "mmw.toml").write_text(f'[settings]\nmirror_dir = "{tmp_path / "pool"}"\n')
    r = CliRunner().invoke(app, ["--config", str(tmp_path), "installer", "--out", str(tmp_path / "o")])
    assert r.exit_code == 0, r.output
    (made,) = (tmp_path / "o").glob("mmw-*.tar")
    assert str(made) in r.output


def test_runtime_settings_round_trip(tmp_path):
    from mmw.core import config
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path), sources=[], networks=[
        config.NetworkConfig(name="plain"),
        config.NetworkConfig(name="both", runtime_arches=["aarch64", "x86_64"], runtime_python="3.13")])
    config.save(cfg, tmp_path)
    text = (tmp_path / "mmw.toml").read_text()
    assert [line.split(" =")[0] for line in text.splitlines() if line.startswith("runtime_")] == [
        "runtime_arches", "runtime_python"]                      # only the non-defaults
    plain, both = config.load(tmp_path).networks
    assert (plain.runtime_arches, plain.runtime_python) == (["x86_64"], "3.12")
    assert (both.runtime_arches, both.runtime_python) == (["aarch64", "x86_64"], "3.13")
    with pytest.raises(ValueError):
        config.NetworkConfig(name="x", runtime_python="python3")


def test_tui_edits_runtime_settings(tmp_path, monkeypatch):
    from textual.widgets import Button, Input, ListView

    from mmw.core import config
    from mmw.tui.network_screens import EditNetworkPage
    from test_pypi_retention import _cfg, _names, _run, _settle
    from waiting import until

    async def edit(app, pilot, arches, python):
        app.screen.query_one("#net-list", ListView).index = _names(app).index("lab")
        await _settle(pilot)
        await pilot.press("e")
        await until(pilot, lambda: isinstance(app.screen, EditNetworkPage) and app.screen.query("#btn-save"))
        app.screen.query_one("#inp-runtime-arches", Input).value = arches
        app.screen.query_one("#inp-runtime-python", Input).value = python
        app.screen.query_one("#btn-save", Button).press()

    async def check(app, pilot):
        await edit(app, pilot, "x86_64 aarch64", "3.13")
        await until(pilot, lambda: config.load(tmp_path).networks[0].runtime_arches == ["aarch64", "x86_64"])
        assert config.load(tmp_path).networks[0].runtime_python == "3.13"
        await until(pilot, lambda: not isinstance(app.screen, EditNetworkPage))
        await edit(app, pilot, "sparc", "3.13")
        await until(pilot, lambda: any("runtime arches" in str(n) for n in app.notes))
        assert isinstance(app.screen, EditNetworkPage)
        app.screen.query_one("#inp-runtime-arches", Input).value = "x86_64"
        app.screen.query_one("#inp-runtime-python", Input).value = "3.9"
        app.screen.query_one("#btn-save", Button).press()
        await until(pilot, lambda: any("mmw needs Python" in str(n) for n in app.notes))
        assert isinstance(app.screen, EditNetworkPage)
    _run(tmp_path, check, cfg=_cfg(tmp_path, "lab"), monkeypatch=monkeypatch)


# ── the Transfer page ────────────────────────────────────────────────────────

def _pick_page(tmp_path, status, check, far_side="mmw"):
    import asyncio

    from mmw.core import config
    from mmw.tui.snapshot_pages import BundlePickPage
    from test_tui_snapshots import _app

    net = config.NetworkConfig(name="lab", select=[], far_side=far_side)

    async def run():
        app = _app(lambda: BundlePickPage(net, status, tmp_path,
                                          repos={"pypi": "PyPI"} if far_side == "mmw" else None))
        async with app.run_test() as pilot:
            await pilot.pause()
            return await check(app, pilot)
    return asyncio.run(run())


def _never_sent():
    from dataclasses import replace

    from test_tui_sweep import snapshot_status_demo
    return replace(snapshot_status_demo(), last_bundle=None)


def test_transfer_page_offers_installer(tmp_path, monkeypatch):
    from mmw.tui.ui.form import Form
    from test_tui_sweep import snapshot_status_demo

    async def value(app, pilot):
        return app.screen.query_one(Form).values().get("pick-installer")
    assert _pick_page(tmp_path, _never_sent(), value) == "yes"          # first transfer: on (no pool, never sent)
    monkeypatch.setattr(R, "installer_due", lambda network, mirror_dir: False)
    assert _pick_page(tmp_path, snapshot_status_demo(), value) == "no"  # this mmw already went across: off

    async def absent(app, pilot):
        return app.screen.query("#pick-installer")
    assert not _pick_page(tmp_path, snapshot_status_demo(), absent, far_side="apply")


def test_transfer_with_installer_writes_it(tmp_path, monkeypatch):
    from mmw.core.publisher.results import BundleResult
    from mmw.tui.publish_flows import BundleChoice
    from test_bundle_progress_notifications import _run_bundle_scenario
    seen = []

    def fake(network, cfg, mirror_dir, progress=None, stop_event=None, mirror_start_callback=None, **kw):
        seen.append(kw.get("installer"))
        return BundleResult(network_name=network.name, out_path=tmp_path / "x.tar", sent=1)
    _run_bundle_scenario(tmp_path, fake, monkeypatch, choice=BundleChoice(installer=True))
    assert seen == [True]


def test_estimate_counts_the_installer(tmp_path):
    from textual.widgets import Label

    from waiting import until

    async def check(app, pilot):
        await until(pilot, lambda: "+ mmw installer (about 100 MB per architecture)"
                    in str(app.screen.query_one("#pick-estimate", Label).render()))
    _pick_page(tmp_path, _never_sent(), check)


def test_a_bad_runtime_wheel_is_refused_before_bundling(two_mirrors, tmp_path):  # noqa: F811
    from mmw.core.pool import Pool
    cfg, net, md = _mmw(two_mirrors)
    with Pool(md) as p:
        before = len(p.snapshots(net.name))
    bad_name = tmp_path / "mmw.whl"
    bad_name.write_bytes(b"x")
    for wheel, match in ((bad_name, "isn't a wheel file name"), (tmp_path / "missing-1.0-py3-none-any.whl",
                                                                 "no such wheel")):
        with pytest.raises(ValueError, match=match):
            bundle_network(net, cfg, md, out_path=tmp_path / "o", from_ref=None, installer=True,
                           runtime_wheel=wheel, fetcher=FakeFetcher())
    with Pool(md) as p:
        assert len(p.snapshots(net.name)) == before


def test_transfer_summary_names_the_installer(tmp_path):
    from mmw.core.publisher.results import BundleResult
    from mmw.tui.transfer import BUNDLE, TransferView
    view = TransferView(BUNDLE, "lab")
    view.mark_done(BundleResult(network_name="lab", out_path=tmp_path / "lab-x.tar", sent=1,
                                installers=[tmp_path / "mmw-1.2.0-x86_64.tar"]), stopped=False)
    assert f"mmw installer → {tmp_path / 'mmw-1.2.0-x86_64.tar'}" in view.summary()


def test_installer_for_every_network_skips_apply_ones_and_writes_each_once(monkeypatch, tmp_path):
    """`mmw bundle --installer` with no -n: an apply network gets its bundle
    without one; networks sharing a directory and runtime get one installer."""
    from mmw.commands import bundle as bundle_cmd
    from mmw.core import config
    from mmw.core.publisher.results import BundleResult
    out = Path("/tmp/out")
    nets = [config.NetworkConfig(name="apt", bundle_dir=out),
            config.NetworkConfig(name="lab", bundle_dir=out, far_side="mmw"),
            config.NetworkConfig(name="dev", bundle_dir=out, far_side="mmw")]
    monkeypatch.setattr(config, "load", lambda d: config.Config(
        settings=config.Settings(mirror_dir=Path("/tmp/pool")), sources=[], networks=nets))
    monkeypatch.setattr(bundle_cmd, "preflight_ok", lambda *a, **k: True)
    seen = {}

    def fake(net, cfg, mirror_dir, out_path=None, from_ref=None, to_ref="now", **kw):
        seen[net.name] = kw.get("installer", False)
        return BundleResult(network_name=net.name, out_path=out / f"{net.name}.tar")
    monkeypatch.setattr(bundle_cmd, "bundle_network", fake)
    (tmp_path / "mmw.toml").write_text("")
    assert bundle_cmd.run(tmp_path, installer=True) == 0
    assert seen == {"apt": False, "lab": True, "dev": False}


def test_installer_command_uses_the_default_python(monkeypatch, tmp_path):
    from mmw.commands import installer as inst_cmd
    from mmw.core import config
    got = {}
    monkeypatch.setattr(config, "DEFAULT_RUNTIME_PYTHON", "3.13")
    monkeypatch.setattr(inst_cmd, "write_installers", lambda out, **kw: got.update(kw) or [])
    (tmp_path / "mmw.toml").write_text(f'[settings]\nmirror_dir = "{tmp_path / "pool"}"\n')
    assert inst_cmd.run(tmp_path, out=tmp_path / "o") == 0
    assert got["python"] == "3.13"


def test_cli_installer_defaults_to_the_current_folder(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from mmw.cli import app
    from mmw.commands import installer as inst_cmd
    monkeypatch.setattr(inst_cmd, "default_fetcher", lambda stop_event=None: FakeFetcher())
    (tmp_path / "mmw.toml").write_text(f'[settings]\nmirror_dir = "{tmp_path / "pool"}"\n')
    work = tmp_path / "here"
    work.mkdir()
    monkeypatch.chdir(work)
    r = CliRunner().invoke(app, ["--config", str(tmp_path), "installer"])
    assert r.exit_code == 0, r.output
    assert list(work.glob("mmw-*.tar"))


# ── the newest mmw rides along by default ────────────────────────────────────

def test_installer_due_first_time_and_after_an_upgrade(two_mirrors, tmp_path, monkeypatch):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    assert R.installer_due(net, md)                                   # never sent
    bundle_network(net, cfg, md, out_path=tmp_path / "1", from_ref=None, installer=True, fetcher=FakeFetcher())
    assert not R.installer_due(net, md)                               # this mmw went with it
    monkeypatch.setattr(R, "mmw_version", lambda: "99.0.0")
    assert R.installer_due(net, md)                                   # a newer mmw here now


def test_transfer_page_defaults_installer_from_the_ledger(tmp_path, monkeypatch):
    from mmw.tui.ui.form import Form
    from test_tui_sweep import snapshot_status_demo
    monkeypatch.setattr(R, "installer_due", lambda network, mirror_dir: True)

    async def value(app, pilot):
        return app.screen.query_one(Form).values().get("pick-installer")
    assert _pick_page(tmp_path, snapshot_status_demo(), value) == "yes"   # sent before, but this mmw is newer
