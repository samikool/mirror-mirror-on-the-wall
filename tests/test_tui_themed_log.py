"""RichLog parses with Rich markup, which knows nothing of Textual's theme
variables: `[$mmw-warn]…[/]` was left as literal text and the `[/]` then
raised MarkupError — the whole TUI died on the first sync warning."""
import asyncio
import re
from pathlib import Path

from mmw.tui.ui.base import ThemedApp
from mmw.tui.ui.widgets import ThemedLog

LINE = "  [$mmw-warn]⚠ index not updated — 8 file(s) failed: [b $mmw-error]x[/][/] [dim]y[/]"


def test_theme_variables_in_log_markup_render():
    async def go():
        class _App(ThemedApp):
            def compose(self):
                yield ThemedLog(id="log", markup=True)
        app = _App()
        async with app.run_test(size=(100, 20)) as pilot:
            log = app.query_one(ThemedLog)
            log.write(LINE)                     # before and after the size is known
            await pilot.pause()
            log.write(LINE)
            await pilot.pause()
            text = "\n".join(strip.text for strip in log.lines)
            assert "$mmw" not in text and "[/]" not in text
            assert text.count("⚠ index not updated") == 2
    asyncio.run(go())


def test_no_plain_richlog_in_the_tui():
    tui = Path(__file__).resolve().parent.parent / "src" / "mmw" / "tui"
    offenders = [str(p.relative_to(tui)) for p in tui.rglob("*.py")
                 if "__pycache__" not in p.parts and re.search(r"\bRichLog\(", p.read_text())]
    assert offenders == [], "use ui.widgets.ThemedLog: RichLog can't read $theme colours"
