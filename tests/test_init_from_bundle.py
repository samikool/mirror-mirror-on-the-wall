"""CLI and Setup recover via one preparation command, without clobbering config."""
import pytest
from textual.widgets import Button, Input, TextArea
from typer.testing import CliRunner

from mmw import cli
from mmw.core import config
from mmw.core.publisher.bundle import bundle_network

from test_dry_run_totals import two_mirrors  # noqa: F401
from test_tui_setup import _run
from waiting import at_step, settle, until


@pytest.fixture
def kept(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = two_mirrors
    bundle = bundle_network(net, cfg, md, out_path=tmp_path / "kept")
    target = tmp_path / "restore"
    target.mkdir()
    return cfg, bundle.out_path, target


def invoke(kept, *flags):
    _, path, target = kept
    return CliRunner().invoke(cli.app, ["--config", str(target), "init", "--from-bundle", str(path), *flags])


def test_cli_recovers_sources_and_networks(kept):
    cfg, _, target = kept
    result = invoke(kept, "--mirror-dir", str(target / "pool"))
    assert result.exit_code == 0, result.output
    recovered = config.load(target)
    assert recovered.mirrors == cfg.mirrors
    assert recovered.networks == cfg.networks
    assert recovered.settings.mirror_dir == target / "pool"
    assert "global" in result.output and "never bundled" in result.output


@pytest.mark.parametrize("force", [[], ["--force"]])
def test_cli_refuses_existing_config_even_with_force(kept, force):
    _, _, target = kept
    path = config.config_path(target)
    path.write_text("keep these bytes")
    result = invoke(kept, *force)
    assert result.exit_code == 1
    assert "mmw.toml exists" in result.output and "--dry-run" in result.output
    assert path.read_text() == "keep these bytes"


def test_dry_run_prints_toml_without_writing_and_allows_an_existing_config(kept):
    _, _, target = kept
    path = config.config_path(target)
    path.write_text("existing")
    result = invoke(kept, "--dry-run")
    assert result.exit_code == 0, result.output
    recovered = config.Config.model_validate(config.tomllib.loads(result.stdout))
    assert recovered.sources and recovered.networks
    assert recovered.settings.mirror_dir == config.Settings(mirror_dir="./mirror").mirror_dir
    assert "not recorded" in result.stderr
    assert path.read_text() == "existing"


def test_missing_bundle_is_a_readable_failure(kept):
    _, _, target = kept
    r = CliRunner().invoke(cli.app, ["--config", str(target), "init", "--from-bundle", str(target / "missing.tar")])
    assert r.exit_code == 1
    assert "cannot read" in r.output
    assert not config.config_path(target).exists()


def test_exclusive_save_refuses_a_racing_writer(kept, monkeypatch):
    from mmw.core import disk_io
    _, _, target = kept
    path = config.config_path(target)
    real = disk_io.ops.link_into

    def race(src, dest):
        path.write_text("another writer")
        return real(src, dest)
    monkeypatch.setattr(disk_io.ops, "link_into", race)
    result = invoke(kept)
    assert result.exit_code == 1
    assert path.read_text() == "another writer"
    assert not list(target.glob("*.tmp"))


async def pick_bundle(app, pilot, path):
    await until(pilot, lambda: app.screen.query("#btn-setup-from-bundle"))
    app.screen.query_one("#btn-setup-from-bundle", Button).press()
    await until(pilot, lambda: type(app.screen).__name__ == "PathPickPage" and app.screen.query("#path-input"))
    # Initial tree highlights can still replace the input while the picker
    # loads. Type only after that work settles, as a person would.
    await settle(pilot)
    box = app.screen.query_one("#path-input", Input)
    box.focus()
    box.value = str(path)
    await pilot.pause()
    app.screen.query_one("#btn-use", Button).press()
    await until(pilot, lambda: type(app.screen).__name__ == "SetupPage"
                and path in app.screen._bundle_paths)


def test_setup_prefills_review_edits_and_writes_via_shared_command(kept, monkeypatch):
    from mmw.commands import init_from_bundle as command
    cfg, path, target = kept
    seen = []
    real = command.prepare

    def spy(*a, **kw):
        seen.append(a[0])
        return real(*a, **kw)
    monkeypatch.setattr(command, "prepare", spy)

    async def check(app, pilot):
        page = app.screen
        page.query_one("#mirror-dir", Input).value = str(target / "pool")
        await pick_bundle(app, pilot, path)
        assert not config.config_path(target).exists()
        assert page._planned == cfg.sources
        page.query_one("#btn-wizard-next", Button).press()
        await at_step(pilot, page, 1)
        assert "example.com/a" in str(page.query_one("#setup-repo-list").render())
        page.query_one("#btn-setup-review", Button).press()
        await until(pilot, lambda: type(app.screen).__name__ == "BundleConfigReviewPage" and app.screen.query("#recovered-config"))
        editor = app.screen.query_one("#recovered-config", TextArea)
        editor.load_text(editor.text.replace('name = "oak"', 'name = "recovered"'))
        app.screen.query_one("#btn-recovery-review-save", Button).press()
        await until(pilot, lambda: app.screen is page)
        assert not config.config_path(target).exists()
        page.query_one("#btn-wizard-next", Button).press()
        await at_step(pilot, page, 2)
        assert "recovered" in str(page.query_one("#setup-recovered-networks").render())
        page.query_one("#btn-wizard-next", Button).press()
        await until(pilot, lambda: app.cfg is not None and app.current_mode == "repositories")
    _run(target, check)
    recovered = config.load(target)
    assert recovered.networks[0].name == "recovered"
    assert recovered.sources == cfg.sources
    assert recovered.settings.mirror_dir == (target / "pool").resolve()
    assert seen == [[path]]


def test_setup_can_add_a_second_bundle_and_cancel_review(kept):
    cfg, path, target = kept
    net = cfg.networks[0].model_copy(update={"name": "prod", "root": None})
    other = bundle_network(net, cfg, cfg.settings.mirror_dir, out_path=target / "prod")

    async def check(app, pilot):
        await pick_bundle(app, pilot, path)
        await pick_bundle(app, pilot, other.out_path)
        page = app.screen
        assert {n.name for n in page._recovered.networks} == {"oak", "prod"}
        page.query_one("#btn-wizard-next", Button).press()
        await at_step(pilot, page, 1)
        page.query_one("#btn-setup-review", Button).press()
        await until(pilot, lambda: type(app.screen).__name__ == "BundleConfigReviewPage")
        await pilot.press("escape")
        await until(pilot, lambda: app.screen is page)
        assert not config.config_path(target).exists()
    _run(target, check)


def test_cli_accepts_several_network_bundles(kept):
    cfg, path, target = kept
    net = cfg.networks[0].model_copy(update={"name": "prod", "root": None})
    other = bundle_network(net, cfg, cfg.settings.mirror_dir, out_path=target / "kept-prod")
    result = invoke(kept, "--from-bundle", str(other.out_path), "--dry-run")
    assert result.exit_code == 0, result.output
    recovered = config.Config.model_validate(config.tomllib.loads(result.stdout))
    assert {n.name for n in recovered.networks} == {"oak", "prod"}
    assert recovered.sources == cfg.sources
    assert not config.config_path(target).exists()


def test_cli_refuses_a_dangling_config_symlink(kept):
    _, _, target = kept
    path = config.config_path(target)
    path.symlink_to(target / "absent.toml")
    result = invoke(kept)
    assert result.exit_code == 1 and "mmw.toml exists" in result.output
    assert path.is_symlink() and not (target / "absent.toml").exists()


def test_setup_invalid_review_stays_editable_and_writes_nothing(kept):
    _, path, target = kept

    async def check(app, pilot):
        await pick_bundle(app, pilot, path)
        page = app.screen
        page.query_one("#btn-wizard-next", Button).press()
        await until(pilot, lambda: page.step == 1 and page.query_one("#btn-setup-review").display)
        page.query_one("#btn-setup-review", Button).press()
        await until(pilot, lambda: type(app.screen).__name__ == "BundleConfigReviewPage" and app.screen.query("#recovered-config"))
        app.screen.query_one("#recovered-config", TextArea).load_text("invalid = [")
        app.screen.query_one("#btn-recovery-review-save", Button).press()
        await until(pilot, lambda: "Invalid" in str(app.screen.query_one("#page-status").render()))
        assert not config.config_path(target).exists()
        await pilot.press("escape")
        await until(pilot, lambda: app.screen is page)
    _run(target, check)


@pytest.mark.parametrize("frontend", ["cli", "setup"])
def test_network_only_recovery_can_finish_without_sources(tmp_path, frontend):
    import json
    from mmw.core.publisher.manifest import ManifestHeader, write_manifest
    from mmw.providers import get_provider
    from test_tui_setup import _settle

    source = config.VscodeSource(provider="vscode", kinds=["desktop"],
                                 platforms=["linux-x64"], qualities=["stable"])
    net = config.NetworkConfig(name="lab")
    settings = {"network": net.model_dump(mode="json"),
                "mirrors": [u.legacy_dict() for u in get_provider("vscode").expand(source)]}
    path = tmp_path / "lab.manifest.jsonl"
    write_manifest(path, ManifestHeader("lab", "20261101T000000Z", None, True, format=3,
                                       settings=json.dumps(settings)), [])
    target = tmp_path / frontend
    target.mkdir()
    if frontend == "cli":
        result = CliRunner().invoke(cli.app, ["--config", str(target), "init", "--from-bundle", str(path),
                                                "--mirror-dir", str(target / "pool")])
        assert result.exit_code == 0, result.output
        assert "vscode" in result.stderr and "not recorded" in result.stderr
    else:
        async def check(app, pilot):
            app.screen.query_one("#mirror-dir", Input).value = str(target / "pool")
            await pick_bundle(app, pilot, path)
            page = app.screen
            assert page._planned == []
            page.query_one("#btn-wizard-next", Button).press()
            await until(pilot, lambda: page.step == 1)
            page.query_one("#btn-wizard-next", Button).press()
            await _settle(pilot)
            assert page.step == 2, str(page.query_one("#page-status").render())
            page.query_one("#btn-wizard-next", Button).press()
            await until(pilot, lambda: app.cfg is not None and app.current_mode == "repositories")
        _run(target, check)
    saved = config.load(target)
    assert saved.sources == []
    assert saved.networks == [net]


def test_normal_init_cli_can_write_an_empty_config(tmp_path, monkeypatch):
    import asyncio
    from mmw.tui.add_source import AddSourcePage
    from test_tui_setup import _settle

    def drive(app):
        async def go():
            async with app.run_test(size=(120, 40)) as pilot:
                await until(pilot, lambda: type(app.screen).__name__ == "SetupPage" and app.screen.query("#mirror-dir"))
                page = app.screen
                page.query_one("#btn-wizard-next", Button).press()
                await until(pilot, lambda: isinstance(app.screen, AddSourcePage) and app.screen.query("#add-entry-list"))
                await pilot.press("escape")
                await _settle(pilot)
                assert app.screen is page
                page.query_one("#btn-wizard-next", Button).press()
                await _settle(pilot)
                assert page.step == 2
                page.query_one("#btn-wizard-next", Button).press()
                await until(pilot, lambda: app.cfg is not None and app.current_mode == "repositories")
        asyncio.run(go())
    monkeypatch.setattr(cli, "_run_tui", drive)
    result = CliRunner().invoke(cli.app, ["--config", str(tmp_path), "init", "--mirror-dir", str(tmp_path / "pool")])
    assert result.exit_code == 0, result.exception
    assert config.load(tmp_path).sources == []
