"""`?` shows the keys of the page in front of you — derived from its bindings,
so it can never list a key that does nothing — plus that page's notes."""
import asyncio

from mmw.core import config
from mmw.tui.app import MmwApp
from mmw.tui.ui.dialog import HelpDialog

SRC = "https://deb.example.com/debian"


def _run(tmp_path, section, check):
    cfg = config.Config(
        settings=config.Settings(mirror_dir=tmp_path / "pool"),
        sources=[config.SourceConfig(url=SRC, suites=["bookworm"], components=["main"],
                                     arches=["amd64"])],
    )
    config.save(cfg, tmp_path)

    async def go():
        app = MmwApp(cfg, tmp_path, initial_section=section)
        async with app.run_test(size=(100, 30)) as pilot:
            for _ in range(6):
                await pilot.pause()
            await check(app, pilot)
    asyncio.run(go())


def _text(app) -> str:
    return "\n".join(str(w.render()) for w in app.screen.query("#help-body Label"))


def test_question_mark_lists_the_repositories_keys_and_notes(tmp_path):
    async def check(app, pilot):
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpDialog)
        text = _text(app)
        for key, what in (("a", "Add"), ("s", "Sync"), ("p", "Prune pool"),
                          ("^w", "Wipe pool"), ("1", "Repositories"), ("q", "Quit")):
            assert any(line.split()[:1] == [key] and what in line
                       for line in text.splitlines()), (key, what, text)
        assert "narrow" in text                     # the page's own notes
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, HelpDialog)
    _run(tmp_path, "repositories", check)


def test_question_mark_in_networks_lists_its_verbs_not_repositories(tmp_path):
    async def check(app, pilot):
        await pilot.press("question_mark")
        await pilot.pause()
        text = _text(app)
        assert "Publish" in text and "Bundle" in text
        assert "Prune pool" not in text
        await pilot.press("question_mark")           # ? closes it too
        await pilot.pause()
        assert not isinstance(app.screen, HelpDialog)
    _run(tmp_path, "networks", check)
