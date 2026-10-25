"""mmw's runtime rides an mmw network's bundles when the far side needs it."""
import json
import tarfile
from pathlib import Path

import pytest

from mmw.core import runtime as R
from mmw.core.pool import Pool
from mmw.core.publisher.bundle import bundle_network

from test_bundle_mmw import _change_b, _mmw
from test_dry_run_totals import two_mirrors  # noqa: F401 — the shared pool fixture
from test_runtime import FakeFetcher
from test_snapshot_repo_versions import _change_a


def _members(bundle):
    with tarfile.open(bundle) as tar:
        names = tar.getnames()
        rows = [json.loads(line) for line in tar.extractfile(".mmw/manifest.jsonl").read().decode().splitlines()]
    return names, rows


def _runtime_names(names):
    return sorted(n for n in names if n.startswith(".mmw/runtime/"))


def test_mmw_bundle_carries_runtime(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    r = bundle_network(net, cfg, md, out_path=tmp_path, from_ref=None, fetcher=FakeFetcher())
    names, rows = _members(r.out_path)
    got = _runtime_names(names)
    assert ".mmw/runtime/bootstrap.sh" in got and ".mmw/runtime/RUNTIME.json" in got
    assert ".mmw/runtime/x86_64/uv" in got and ".mmw/runtime/x86_64/RUNTIME.json" in got
    assert any(n.startswith(".mmw/runtime/x86_64/wheels/mirror_mirror_on_the_wall-") for n in got)
    assert not [row for row in rows[1:] if row["p"].startswith(".mmw/")]     # control members, not rows
    with tarfile.open(r.out_path) as tar:
        top = json.loads(tar.extractfile(".mmw/runtime/RUNTIME.json").read())
    assert top == {"mmw": R.mmw_version(), "python": "3.12", "arches": ["x86_64"]}


def test_runtime_only_when_changed(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    bundle_network(net, cfg, md, out_path=tmp_path / "1", from_ref=None, fetcher=FakeFetcher())
    _change_a(md)
    again = FakeFetcher()
    r = bundle_network(net, cfg, md, out_path=tmp_path / "2", fetcher=again)
    assert _runtime_names(_members(r.out_path)[0]) == [] and again.calls == []
    both = net.model_copy(update={"runtime_arches": ["x86_64", "aarch64"]})
    cfg = cfg.model_copy(update={"networks": [both]})
    _change_b(md)
    r = bundle_network(both, cfg, md, out_path=tmp_path / "3", fetcher=FakeFetcher())
    got = _runtime_names(_members(r.out_path)[0])
    assert ".mmw/runtime/aarch64/uv" in got and ".mmw/runtime/x86_64/uv" in got


class Offline(FakeFetcher):
    def uv_binary(self, version, arch):
        raise R.FetchError(f"couldn't fetch https://pypi.org/pypi/uv/{version}/json: no route to host")


def test_offline_build_refuses_cleanly(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    out = tmp_path / "out"
    with Pool(md, read_only=True) as pool:
        snaps = len(pool.snapshots(net.name))
    with pytest.raises(R.FetchError, match="couldn't fetch https://pypi.org"):
        bundle_network(net, cfg, md, out_path=out, from_ref=None, fetcher=Offline())
    assert not out.exists() or not list(out.iterdir())
    with Pool(md, read_only=True) as pool:
        assert pool.bundles(net.name) == []
        assert len(pool.snapshots(net.name)) == snaps             # no snapshot left behind either
    r = bundle_network(net, cfg, md, out_path=out, from_ref=None, fetcher=Offline(), runtime=False)
    assert _runtime_names(_members(r.out_path)[0]) == []
    # Not sent, so the next bundle still carries it.
    _change_a(md)
    r = bundle_network(net, cfg, md, out_path=tmp_path / "2", fetcher=FakeFetcher())
    assert ".mmw/runtime/bootstrap.sh" in _members(r.out_path)[0]


def test_apply_network_never_carries_runtime(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = two_mirrors
    f = FakeFetcher()
    r = bundle_network(net, cfg, md, out_path=tmp_path, from_ref=None, fetcher=f)
    assert _runtime_names(_members(r.out_path)[0]) == [] and f.calls == []


def test_no_runtime_flag(monkeypatch, tmp_path):
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
        return BundleResult(network_name="lab", out_path=Path("/tmp/out/x.tar"))
    monkeypatch.setattr(bundle_cmd, "bundle_network", fake)
    wheel = tmp_path / "mirror_mirror_on_the_wall-1.3.0.dev0-py3-none-any.whl"
    assert bundle_cmd.run(tmp_path, network="lab", runtime=False) == 0
    assert bundle_cmd.run(tmp_path, network="lab", runtime_wheel=wheel) == 0
    assert seen == [{"runtime": False}, {"runtime_wheel": wheel}]
    from typer.testing import CliRunner

    from mmw.cli import app
    seen.clear()
    assert CliRunner().invoke(app, ["--config", str(tmp_path), "bundle", "-n", "lab", "--no-runtime"]).exit_code == 0
    assert seen == [{"runtime": False}]


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


# ── review findings ──────────────────────────────────────────────────────────

def test_stage_takes_the_runtime_options(two_mirrors, tmp_path, monkeypatch):  # noqa: F811
    from mmw.core.publisher.bundle import stage_network
    cfg, net, md = _mmw(two_mirrors)
    r = stage_network(net, cfg, md, tmp_path / "s", from_ref=None, runtime=False, fetcher=Offline())
    assert not (r.out_path / ".mmw/runtime").exists()
    from mmw.commands import stage as stage_cmd
    seen = []
    monkeypatch.setattr("mmw.commands.bundle.stage_network", lambda *a, **kw: seen.append(kw) or r)
    monkeypatch.setattr("mmw.commands.bundle.load_config_or_none", lambda d: cfg)
    monkeypatch.setattr("mmw.commands.bundle.preflight_ok", lambda *a, **k: True)
    stage_cmd.run(tmp_path, network=net.name, out=tmp_path / "s2", runtime=False)
    assert seen and seen[0]["runtime"] is False
    from typer.testing import CliRunner

    from mmw.cli import app
    seen.clear()
    CliRunner().invoke(app, ["--config", str(tmp_path), "stage", "-n", net.name, "--out", str(tmp_path / "s3"),
                             "--no-runtime"])
    assert seen and seen[0]["runtime"] is False


def test_tui_bundles_without_a_runtime_it_cannot_build(tmp_path, monkeypatch):
    from mmw.core.publisher.results import BundleResult
    from test_bundle_progress_notifications import _run_bundle_scenario
    calls = []

    def fake(network, cfg, mirror_dir, progress=None, stop_event=None, mirror_start_callback=None, **kw):
        calls.append(kw.get("runtime", True))
        if kw.get("runtime", True):
            raise R.FetchError("couldn't fetch https://pypi.org/pypi/uv/0.11.33/json: no route to host")
        return BundleResult(network_name=network.name, out_path=tmp_path / "x.tar", sent=1)

    [(message, severity)] = _run_bundle_scenario(tmp_path, fake, monkeypatch)
    assert calls == [True, False]
    assert "1 warning" in message


def test_a_rebuilt_wheel_is_sent_again(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    wheel = tmp_path / "mirror_mirror_on_the_wall-1.3.0.dev0-py3-none-any.whl"
    wheel.write_bytes(b"first build")
    bundle_network(net, cfg, md, out_path=tmp_path / "1", from_ref=None, runtime_wheel=wheel)
    wheel.write_bytes(b"second build")
    _change_a(md)
    r = bundle_network(net, cfg, md, out_path=tmp_path / "2", runtime_wheel=wheel)
    assert ".mmw/runtime/bootstrap.sh" in _members(r.out_path)[0]
    _change_b(md)
    r = bundle_network(net, cfg, md, out_path=tmp_path / "3", runtime_wheel=wheel)
    assert ".mmw/runtime/bootstrap.sh" not in _members(r.out_path)[0]


def test_cli_says_what_the_runtime_build_is_doing(monkeypatch, tmp_path, capsys):
    from mmw.commands import bundle as bundle_cmd
    from mmw.core import config
    from mmw.core.publisher.results import BundleResult
    (tmp_path / "mmw.toml").write_text('[settings]\nmirror_dir = "/tmp/pool"\n')
    net = config.NetworkConfig(name="lab", bundle_dir=Path("/tmp/out"), far_side="mmw")
    monkeypatch.setattr(config, "load", lambda d: config.Config(
        settings=config.Settings(mirror_dir=Path("/tmp/pool")), sources=[], networks=[net]))

    def fake(net, cfg, mirror_dir, out_path=None, from_ref=None, to_ref="now", progress=None, **kw):
        progress("", "runtime:uv 0.11.33 for x86_64…")
        progress("", "runtime:done")
        return BundleResult(network_name="lab", out_path=Path("/tmp/out/x.tar"))
    monkeypatch.setattr(bundle_cmd, "bundle_network", fake)
    assert bundle_cmd.run(tmp_path, network="lab") == 0
    err = capsys.readouterr().err
    assert "mmw runtime: uv 0.11.33 for x86_64…" in err and "runtime:done" not in err


def test_a_stage_never_links_into_the_installation(two_mirrors, tmp_path, monkeypatch):  # noqa: F811
    import shutil

    import mmw.resources
    from mmw.core.publisher.bundle import stage_network
    BOOTSTRAP = tmp_path / "site-packages" / "bootstrap.sh"      # mmw installed on the stage's filesystem
    BOOTSTRAP.parent.mkdir()
    shutil.copy(mmw.resources.BOOTSTRAP, BOOTSTRAP)
    monkeypatch.setattr(mmw.resources, "BOOTSTRAP", BOOTSTRAP)
    cfg, net, md = _mmw(two_mirrors)
    r = stage_network(net, cfg, md, tmp_path / "s", from_ref=None)
    staged = r.out_path / ".mmw/runtime/bootstrap.sh"
    assert staged.read_bytes() == BOOTSTRAP.read_bytes()
    assert staged.stat().st_ino != BOOTSTRAP.stat().st_ino
