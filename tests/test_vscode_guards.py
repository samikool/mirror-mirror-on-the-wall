"""Until stages 4–5: a VS Code source in the config is safe everywhere."""
import asyncio

from mmw.core import config


def _cfg(tmp_path):
    return config.Config(settings=config.Settings(mirror_dir=tmp_path / "m"),
                         sources=[config.VscodeSource(provider="vscode", kinds=["server"],
                                                      platforms=["linux-x64"])])


def test_an_explicitly_selected_vscode_source_is_published(tmp_path, monkeypatch):
    """Stage 4: what stage 3 refused is published — at the payload's own host."""
    import hashlib
    from mmw.core.pool import KIND_PKG, Pool
    from mmw.core.publisher import publish_network
    from mmw.providers import get_provider
    from mmw.providers.vscode import image
    from test_gallery_image import _fake_docker
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setattr(image, "DOCKER", _fake_docker(bin_dir)[0])
    cfg = _cfg(tmp_path)
    (unit, *_) = get_provider("vscode").expand(cfg.sources[0])
    cs = hashlib.sha256(b"x").hexdigest()
    with Pool(cfg.settings.mirror_dir) as pool:
        pool.add(b"x", cs, "sha256", "x", 1)
        pool.reconcile_refs(unit.name, [(cs, "h.example/x", KIND_PKG)], source=unit.source)
        pool.record_mirror(unit)
    net = config.NetworkConfig(name="lab", root=tmp_path / "pub",
                               select=[config.NetworkSelect(source=config.MARKETPLACE_URL)])
    result = publish_network(net, cfg, cfg.settings.mirror_dir)
    assert not [e for r in result.mirrors for e in r.errors]
    assert (tmp_path / "pub" / "h.example" / "x").read_bytes() == b"x"
    assert (tmp_path / "pub" / "vscode-gallery" / "catalog.json").exists()


def test_edit_on_a_vscode_source_opens_its_form(tmp_path):
    from mmw.tui import provider_ui
    from mmw.tui.vscode_source import EditVscodePage
    assert isinstance(provider_ui.edit_page(_cfg(tmp_path).sources[0], set(), set()), EditVscodePage)


def test_the_sources_page_lists_a_vscode_source(tmp_path):
    from mmw.tui.app import MmwApp
    from waiting import until

    async def go():
        cfg = _cfg(tmp_path)
        config.save(cfg, tmp_path)
        app = MmwApp(cfg, tmp_path, initial_section="repositories")
        async with app.run_test(size=(100, 32)) as pilot:
            await until(pilot, lambda: app.screen.query("#repo-list"))
            await until(pilot, lambda: "vscode" in str(app.screen.query_one("#repo-detail").content))
            # The pool read rebuilds the list when it lands; Edit pressed
            # mid-rebuild finds nothing highlighted. Wait for it to finish.
            await until(pilot, lambda: not any(w.group == "pool" and not w.is_finished
                                               for w in app.screen.workers))
            notes = []
            app.notify = lambda message, **kw: notes.append(str(message))
            await pilot.press("e")
            from mmw.tui.vscode_source import EditVscodePage
            await until(pilot, lambda: isinstance(app.screen, EditVscodePage))
    asyncio.run(go())
