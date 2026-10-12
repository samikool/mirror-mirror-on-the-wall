"""Until stages 4–5: a VS Code source in the config is safe everywhere."""
import asyncio

from mmw.core import config


def _cfg(tmp_path):
    return config.Config(settings=config.Settings(mirror_dir=tmp_path / "m"),
                         sources=[config.VscodeSource(provider="vscode", kinds=["server"],
                                                      platforms=["linux-x64"])])


def test_publishing_a_vscode_unit_is_refused_clearly(tmp_path):
    from mmw.core.pool import KIND_PKG, Pool
    from mmw.core.publisher import publish_network
    from mmw.providers import get_provider
    cfg = _cfg(tmp_path)
    (unit, *_) = get_provider("vscode").expand(cfg.sources[0])
    import hashlib
    cs = hashlib.sha256(b"x").hexdigest()
    with Pool(cfg.settings.mirror_dir) as pool:
        pool.add(b"x", cs, "sha256", "x", 1)
        pool.reconcile_refs(unit.name, [(cs, "h.example/x", KIND_PKG)], source=unit.source)
        pool.record_mirror(unit)
    # Named explicitly: refused loudly. (An implicit "everything" network
    # skips VS Code units quietly — see test_vscode_review_fixes.py.)
    net = config.NetworkConfig(name="lab", root=tmp_path / "pub",
                               select=[config.NetworkSelect(source=config.MARKETPLACE_URL)])
    result = publish_network(net, cfg, cfg.settings.mirror_dir)
    errors = [e for r in result.mirrors for e in r.errors]
    assert any("stage 4" in e for e in errors)
    assert not (tmp_path / "pub" / "marketplace.visualstudio.com").exists()


def test_edit_on_a_vscode_source_explains_instead_of_crashing(tmp_path):
    from mmw.tui import provider_ui
    assert provider_ui.edit_page(_cfg(tmp_path).sources[0], set(), set()) is None


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
            await until(pilot, lambda: any("mmw.toml" in n for n in notes))
    asyncio.run(go())
