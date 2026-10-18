"""PyPI in `mmw manage`: Add, Edit, and Check (resolve without saving)."""
import asyncio

from textual.widgets import Checkbox, Input, Label, TextArea

from mmw.core import config
from mmw.tui.provider_ui import add_entries
from pickers import choose_add_entry
from test_add_source import _Harness
from waiting import until

IDENT = config.normalize_url(config.PYPI_URL)


def _src(**kw):
    return config.PypiSource(provider="pypi", **{"pythons": ["3.12"], "platforms": ["linux-glibc"],
                                                 "arches": ["amd64"], **kw})


def _edit_page(src, **kw):
    from mmw.tui.pypi_source import EditPypiPage
    return EditPypiPage(src, existing_names=set(), existing_urls=set(), **kw)


def test_add_entry_offers_pypi():
    keys = [e.key for e in add_entries()]
    assert keys.index("pypi") == keys.index("vscode") + 1


def test_add_pypi_through_the_wizard_saves():
    from mmw.tui.add_source import AddSourcePage

    async def scenario():
        app = _Harness(lambda: AddSourcePage())
        async with app.run_test(size=(100, 32)) as pilot:
            await choose_add_entry(app, pilot, "pypi")
            page = app.screen
            await until(pilot, lambda: page.query("#vs-requirements"))
            page.query_one("#vs-all", Checkbox).value = True
            page.query_one("#vs-requirements", TextArea).text = "requests\nnumpy<2"
            await pilot.pause()
            page.query_one("#btn-wizard-next").press()
            await until(pilot, lambda: app.result != "unset")
        (src,) = app.result
        assert isinstance(src, config.PypiSource) and src.all and src.requirements == ["requests", "numpy<2"]
    asyncio.run(scenario())


def test_edit_pypi_keeps_untouched_fields():
    src = _src(requirements=["requests"], requirements_file="r.txt", glibc="2.31")

    async def scenario():
        app = _Harness(lambda: _edit_page(src))
        async with app.run_test(size=(100, 32)) as pilot:
            await until(pilot, lambda: app.screen.query("#vs-glibc"))
            app.screen.query_one("#vs-glibc", Input).value = "2.28"
            app.screen.query_one("#btn-save").press()
            await until(pilot, lambda: app.result != "unset")
        assert app.result == src.model_copy(update={"glibc": "2.28"})
    asyncio.run(scenario())


def test_add_replaces_the_existing_pypi_source():
    from mmw.tui.pypi_source import StepPypi

    async def scenario():
        app = _Harness(lambda: _StepHost(StepPypi({IDENT})))
        async with app.run_test(size=(100, 32)) as pilot:
            await until(pilot, lambda: app.screen.query("#vs-requirements"))
            labels = " ".join(str(w.render()) for w in app.screen.query(Label))
            assert "replaces the existing PyPI source" in labels
    asyncio.run(scenario())
    merged, _, _ = config.merge_sources([_src(all=True)], [_src(requirements=["six"])])
    assert [s.requirements for s in merged if s.provider == "pypi"] == [["six"]]


def test_bad_requirement_line_blocks_save_and_focuses_the_field():
    async def scenario():
        app = _Harness(lambda: _edit_page(_src()))
        async with app.run_test(size=(100, 32)) as pilot:
            await until(pilot, lambda: app.screen.query("#vs-requirements"))
            app.screen.query_one("#vs-requirements", TextArea).text = "-r base.txt"
            app.screen.query_one("#btn-save").press()
            await until(pilot, lambda: "-r base.txt" in str(app.screen.query_one("#page-status").render()))
            assert app.result == "unset"
            assert app.focused is not None and app.focused.id == "vs-requirements"
    asyncio.run(scenario())


def test_summary_follows_the_form():
    async def scenario():
        app = _Harness(lambda: _edit_page(_src()))
        async with app.run_test(size=(100, 32)) as pilot:
            await until(pilot, lambda: app.screen.query("#pypi-summary"))
            app.screen.query_one("#vs-all", Checkbox).value = True
            await until(pilot, lambda: "all of PyPI" in str(app.screen.query_one("#pypi-summary").render()))
    asyncio.run(scenario())


from mmw.tui.ui.page import Page  # noqa: E402


class _StepHost(Page):
    """A wizard step on its own page, for tests that only look at the step."""

    def __init__(self, step):
        super().__init__()
        self.step_widget = step

    def compose_body(self):
        yield self.step_widget


# ── Check (Task 2) ───────────────────────────────────────────────────────────

import threading  # noqa: E402

import pytest  # noqa: E402

from mmw.providers.pypi.uv import Compiled  # noqa: E402

REAL_CONFLICT = ("  × No solution found when resolving dependencies:\n"
                 "  ╰─▶ Because you require a==1 and a>=2, we can conclude that your\n"
                 "      requirements are unsatisfiable.\n")


@pytest.fixture
def fake_uv(monkeypatch):
    from mmw.tui import pypi_source
    calls, gate = [], {"event": None}

    def compile_for(lines, target, floors, **kw):
        calls.append((list(lines), target.key))
        if gate["event"] is not None:
            gate["event"].wait(10)
        if target.platform == "windows":
            return Compiled([], REAL_CONFLICT, False)
        return Compiled([(f"p{i}", "1.0") for i in range(14)], None, False)
    monkeypatch.setattr(pypi_source.uvmod, "compile_for", compile_for)
    return calls, gate


def _status(app):
    return str(app.screen.query_one("#pypi-check-status").render())


def _check(src, body):
    async def scenario():
        app = _Harness(lambda: _edit_page(src))
        async with app.run_test(size=(100, 32)) as pilot:
            await until(pilot, lambda: app.screen.query("#btn-pypi-check"))
            await body(app, pilot)
    asyncio.run(scenario())


def test_check_lists_each_target(fake_uv):
    async def body(app, pilot):
        app.screen.query_one("#btn-pypi-check").press()
        await until(pilot, lambda: "✗" in _status(app))
        assert "3.12-linux-glibc-amd64: ✓ 14 packages" in _status(app)
        assert ("3.12-windows-amd64: ✗ Because you require a==1 and a>=2, we can conclude that your "
                "requirements are unsatisfiable.") in _status(app)
        assert "No solution found" not in _status(app)
    _check(_src(platforms=["linux-glibc", "windows"], requirements=["a"]), body)


def test_check_with_no_requirements_says_so(fake_uv):
    async def body(app, pilot):
        app.screen.query_one("#btn-pypi-check").press()
        await until(pilot, lambda: "No requirements to check." in _status(app))
    _check(_src(), body)


def test_check_reports_an_unreadable_file(fake_uv, tmp_path):
    async def body(app, pilot):
        app.screen.query_one("#btn-pypi-check").press()
        await until(pilot, lambda: "Requirements not read" in _status(app))
        assert "nope.txt" in _status(app)
    _check(_src(requirements_file=str(tmp_path / "nope.txt")), body)


def test_check_uses_the_unsaved_form(fake_uv):
    calls, _ = fake_uv

    async def body(app, pilot):
        app.screen.query_one("#vs-requirements", TextArea).text = "flask>=3"
        await pilot.pause()
        app.screen.query_one("#btn-pypi-check").press()
        await until(pilot, lambda: "✓" in _status(app))
        assert calls[0][0] == ["flask>=3"] and app.result == "unset"
    _check(_src(), body)


def test_check_result_is_discarded_after_an_edit(fake_uv):
    _, gate = fake_uv
    gate["event"] = threading.Event()

    async def body(app, pilot):
        app.screen.query_one("#btn-pypi-check").press()
        await until(pilot, lambda: "Checking" in _status(app))
        app.screen.query_one("#vs-requirements", TextArea).text = "b"
        await until(pilot, lambda: _status(app) == "")
        gate["event"].set()
        await pilot.pause(0.3)
        assert _status(app) == ""
    _check(_src(requirements=["a"]), body)


def test_leaving_during_check_is_quiet(fake_uv):
    _, gate = fake_uv
    gate["event"] = threading.Event()

    async def body(app, pilot):
        app.screen.query_one("#btn-pypi-check").press()
        await until(pilot, lambda: "Checking" in _status(app))
        app.screen.query_one("#btn-cancel").press()
        await until(pilot, lambda: app.result is None)
        gate["event"].set()
        await pilot.pause(0.3)
    _check(_src(requirements=["a"]), body)


def test_superseded_check_stops_between_targets(fake_uv):
    calls, gate = fake_uv
    gate["event"] = threading.Event()

    async def body(app, pilot):
        app.screen.query_one("#btn-pypi-check").press()
        await until(pilot, lambda: calls)
        app.screen.query_one("#vs-requirements", TextArea).text = "b"
        await until(pilot, lambda: _status(app) == "")
        gate["event"].set()
        await pilot.pause(0.5)
        assert len(calls) == 1          # the second target never ran for the abandoned list
    _check(_src(platforms=["linux-glibc", "windows"], requirements=["a"]), body)


def test_pypi_edit_saves_in_page_through_sources(tmp_path, monkeypatch):
    from mmw.tui.app import MmwApp
    from mmw.tui.pypi_source import EditPypiPage

    async def go():
        cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / "m"), sources=[_src(all=True)])
        config.save(cfg, tmp_path)
        app = MmwApp(cfg, tmp_path, initial_section="repositories")
        async with app.run_test(size=(100, 32)) as pilot:
            await until(pilot, lambda: app.screen.query("#repo-list"))
            await until(pilot, lambda: "pypi" in str(app.screen.query_one("#repo-detail").content))
            await until(pilot, lambda: not any(w.group == "pool" and not w.is_finished
                                               for w in app.screen.workers))
            await pilot.press("e")
            await until(pilot, lambda: isinstance(app.screen, EditPypiPage))
            page = app.screen

            async def refuse(*a, **kw):
                raise OSError("disk full")
            monkeypatch.setattr(app, "save_config", refuse)
            page.query_one("#vs-glibc", Input).value = "2.28"
            page.query_one("#btn-save").press()
            await until(pilot, lambda: "Not saved: disk full" in str(page.query_one("#page-status").render()))
            assert app.screen is page                     # the edits are still on screen
    asyncio.run(go())
