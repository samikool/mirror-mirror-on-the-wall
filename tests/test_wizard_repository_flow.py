"""Adding a repository — both modes, driven headlessly with stubbed discovery.

Probing a URL and pasting a config are deliberately the *same* flow: both end
up at the same three dimension pickers. A pasted config only supplies a
pre-selection (and possibly a GPG key). These tests drive both through the same
helpers so the two cannot drift apart again.
"""
import asyncio

from textual import work
from textual.app import App, ComposeResult
from textual.widgets import Button, Input, Label, RadioButton, SelectionList, Switch, TextArea

from mmw.core import config, discover
from mmw.tui.custom_repo import StepCustom
from mmw.tui.init_wizard import AddMirrorScreen
from mmw.tui.wizard_steps import StepConfirm, WizardStep

URL = "http://archive.example.com/ubuntu"

MULTI = """\
Types: deb
URIs: http://archive.example.com/ubuntu/
Suites: noble noble-updates
Components: main

Types: deb
URIs: http://security.example.com/ubuntu/
Suites: noble-security
Components: main
"""

ARM_ONLY = """\
Types: deb
URIs: http://archive.example.com/ubuntu/
Suites: noble
Components: main
Architectures: arm64
"""

WITH_KEY = """\
Types: deb
URIs: http://archive.example.com/ubuntu/
Suites: noble
Components: main
Signed-By:
 -----BEGIN PGP PUBLIC KEY BLOCK-----
 .
 mQINBGXfTyoB
 -----END PGP PUBLIC KEY BLOCK-----
"""


class _Harness(App):
    def __init__(self):
        super().__init__()
        self.result = None

    def compose(self) -> ComposeResult:
        yield from ()

    def on_mount(self):
        self._pick()

    @work
    async def _pick(self):
        self.result = await self.push_screen_wait(AddMirrorScreen())


def _stub(monkeypatch, suites=("noble", "jammy"), components=("main", "universe"),
          arches=("amd64", "arm64", "i386")):
    monkeypatch.setattr(discover, "detect_repository", lambda url: discover.DetectResult(
        provider="apt", suites=list(suites), signal=f"APT  ({len(suites)} suites)",
    ))
    monkeypatch.setattr(discover, "list_suites", lambda url: list(suites))
    monkeypatch.setattr(discover, "list_components", lambda url, s: list(components))
    monkeypatch.setattr(discover, "list_arches", lambda url, s, c: list(arches))
    monkeypatch.setattr(discover, "release_dimensions", lambda url, s: ([], []))


def _stub_nothing(monkeypatch):
    """A repository that answers nothing — no listing, no Release file."""
    monkeypatch.setattr(discover, "list_suites", lambda url: [])
    monkeypatch.setattr(discover, "list_components", lambda url, s: [])
    monkeypatch.setattr(discover, "list_arches", lambda url, s, c: [])
    monkeypatch.setattr(discover, "release_dimensions", lambda url, s: ([], []))


async def _await_step(pilot, tries: int = 80):
    for _ in range(tries):
        if pilot.app.screen.query_one("#step-container").query(WizardStep):
            return
        await pilot.pause()
    raise AssertionError("step never finished loading")


async def _settle(pilot, tries: int = 30):
    for _ in range(tries):
        await pilot.pause()


async def _press(pilot, button_id: str):
    pilot.app.screen.query_one(button_id, Button).press()
    await pilot.pause()
    await pilot.pause()


async def _step(pilot) -> StepCustom:
    await _await_step(pilot)
    return pilot.app.screen.query_one(StepCustom)


async def _probe(pilot, url=URL) -> StepCustom:
    """Mode 1: type a URL and probe it."""
    step = await _step(pilot)
    step.query_one("#mode-url", RadioButton).value = True
    await pilot.pause()
    step.query_one("#custom-url", Input).value = url
    await _press(pilot, "#btn-detect")
    await _settle(pilot)
    return step


async def _paste(pilot, text) -> StepCustom:
    """Mode 2: paste a config. Same destination as probing."""
    step = await _step(pilot)
    step.query_one("#mode-paste", RadioButton).value = True
    await pilot.pause()
    step.query_one("#paste-area", TextArea).text = text
    await _settle(pilot)
    return step


async def _pick(pilot, step, dim, values):
    """Open a dimension's modal, tick exactly `values`, confirm."""
    step.query_one(f"#btn-{dim}", Button).press()
    await _settle(pilot)
    modal = pilot.app.screen
    lst = modal.query_one("#pick-list", SelectionList)
    lst.deselect_all()
    for v in values:
        lst.select(v)
    await pilot.pause()
    modal.query_one("#btn-done", Button).press()
    await _settle(pilot)


def _run(scenario):
    asyncio.run(scenario())


# ── mode 1: probe a URL ──────────────────────────────────────────────────────

def test_probing_offers_every_suite_none_preselected(monkeypatch):
    """archive.ubuntu.com publishes 55 suites; preselecting them would turn one
    Enter into a multi-terabyte sync."""
    _stub(monkeypatch, suites=("noble", "jammy", "focal"))

    async def scenario():
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            step = await _probe(pilot)
            assert step._draft.available["suites"] == ["noble", "jammy", "focal"]
            assert step._draft.chosen["suites"] == []

    _run(scenario)


def test_choosing_a_suite_fills_components_and_arches(monkeypatch):
    _stub(monkeypatch)

    async def scenario():
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            step = await _probe(pilot)
            await _pick(pilot, step, "suites", ["noble"])
            assert step._draft.available["components"] == ["main", "universe"]
            assert step._draft.available["arches"] == ["amd64", "arm64", "i386"]
            # they are what the repository serves, so all are ticked
            assert step._draft.chosen["arches"] == ["amd64", "arm64", "i386"]

    _run(scenario)


def test_everything_selected_is_stored_as_all(monkeypatch):
    """A frozen full list would silently stop following upstream — the bug
    `NetworkConfig.mirrors` had. Everything ticked means "all", stored empty."""
    _stub(monkeypatch)

    async def scenario():
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            step = await _probe(pilot)
            await _pick(pilot, step, "suites", ["noble", "jammy"])
            src = step.get_sources()[0]

        assert src.suites == []          # both suites offered and both picked
        assert src.components == []
        assert src.arches == []

    _run(scenario)


def test_narrowing_is_stored_explicitly(monkeypatch):
    _stub(monkeypatch)

    async def scenario():
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            step = await _probe(pilot)
            await _pick(pilot, step, "suites", ["noble"])
            await _pick(pilot, step, "arches", ["arm64"])
            src = step.get_sources()[0]

        assert src.suites == ["noble"]
        assert src.arches == ["arm64"]

    _run(scenario)


def test_a_host_root_offers_its_directories(monkeypatch):
    """http://archive.ubuntu.com/ has no dists/ — it holds ubuntu/."""
    from textual.widgets import OptionList

    monkeypatch.setattr(discover, "detect_repository", lambda url: discover.DetectResult(
        provider=None, candidates=["ubuntu"], error="not an apt repository",
    ))

    async def scenario():
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            step = await _probe(pilot, "http://archive.example.com/")
            browse = step.query_one("#browse-list", OptionList)
            assert browse.display is True
            assert [str(o.prompt) for o in browse._options] == ["ubuntu"]
            assert step.query_one("#fields-container").display is False

    _run(scenario)


def test_picking_a_directory_descends_and_probes_again(monkeypatch):
    from textual.widgets import OptionList

    def fake_detect(url):
        if url.rstrip("/").endswith("/ubuntu"):
            return discover.DetectResult(provider="apt", suites=["noble"], signal="APT")
        return discover.DetectResult(provider=None, candidates=["ubuntu"])

    monkeypatch.setattr(discover, "detect_repository", fake_detect)
    monkeypatch.setattr(discover, "list_suites", lambda url: ["noble"])
    monkeypatch.setattr(discover, "list_components", lambda url, s: ["main"])
    monkeypatch.setattr(discover, "list_arches", lambda url, s, c: ["amd64"])

    async def scenario():
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            step = await _probe(pilot, "http://archive.example.com/")
            browse = step.query_one("#browse-list", OptionList)
            browse.focus()
            browse.highlighted = 0
            await pilot.press("enter")
            await _settle(pilot)

            assert step.query_one("#custom-url", Input).value.rstrip("/").endswith("/ubuntu")
            assert browse.display is False
            assert step._draft.available["suites"] == ["noble"]

    _run(scenario)


def test_a_dead_url_is_reported_not_crawled(monkeypatch):
    from textual.widgets import OptionList

    monkeypatch.setattr(discover, "detect_repository", lambda url: discover.DetectResult(
        provider=None, candidates=[], error="connection refused",
    ))

    async def scenario():
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            step = await _probe(pilot, "http://nowhere.invalid/repo")
            assert step.query_one("#browse-list", OptionList).display is False
            assert step.query_one("#fields-container").display is False

    _run(scenario)


def test_probing_an_arch_placeholder_url_resolves_into_one_repo_per_arch(monkeypatch):
    """Typing `.../deb/$(ARCH)` straight into the URL field (NVIDIA's own docs
    call this the "repository root") never goes through `parse_apt_sources` at
    all, so it needs its own $(ARCH) check — separate from the config-paste
    path's, and from `probers.probe_url`'s ordinary single-result dispatch."""
    def fake_detect(url):
        if url.endswith("/amd64"):
            return discover.DetectResult(provider="apt-flat", suites=["stable"])
        if url.endswith("/arm64"):
            return discover.DetectResult(provider="apt-flat", suites=["stable"])
        return discover.DetectResult(provider=None, error="404")

    monkeypatch.setattr(discover, "detect_repository", fake_detect)

    async def scenario():
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            step = await _probe(pilot, "https://nvidia.example/deb/$(ARCH)")
            assert len(step._bundle) == 2
            assert step.query_one("#bulk-row").display is True
            sources = step.get_sources()

        assert sorted(s.url for s in sources) == [
            "https://nvidia.example/deb/amd64",
            "https://nvidia.example/deb/arm64",
        ]

    _run(scenario)


def test_probing_an_arch_placeholder_url_that_resolves_to_nothing_is_reported(monkeypatch):
    monkeypatch.setattr(discover, "detect_repository",
                         lambda url: discover.DetectResult(provider=None, error="404"))

    async def scenario():
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            step = await _probe(pilot, "https://nvidia.example/deb/$(ARCH)")
            assert step._bundle == []
            assert step._draft is None
            assert step.query_one("#fields-container").display is False
            assert step.query_one("#bulk-row").display is False
            assert "no architecture" in str(step.query_one("#detect-status", Label).content)

    _run(scenario)


# ── mode 2: paste a config — the same flow, pre-filtered ─────────────────────

def test_a_pasted_filter_is_preselected_and_the_rest_still_offered(monkeypatch):
    """The case that motivated this: paste an arm64-only config and every
    architecture the repository serves is shown, with only arm64 ticked."""
    _stub(monkeypatch, arches=("amd64", "arm64", "i386"))

    async def scenario():
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            step = await _paste(pilot, ARM_ONLY)
            assert step._draft.available["arches"] == ["amd64", "arm64", "i386"]
            assert step._draft.chosen["arches"] == ["arm64"]
            src = step.get_sources()[0]

        assert src.arches == ["arm64"], "a stated filter must survive"

    _run(scenario)


def test_a_pasted_config_without_arches_gets_them_probed(monkeypatch):
    """A config naming no architectures means "all" — but the user should see
    and be able to narrow them, not stare at a blank field."""
    _stub(monkeypatch, arches=("arm64",))

    async def scenario():
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            step = await _paste(pilot, WITH_KEY)
            assert step._draft.available["arches"] == ["arm64"]
            assert step._draft.chosen["arches"] == ["arm64"]

    _run(scenario)


def test_nothing_probeable_leaves_dimensions_empty(monkeypatch):
    """Empty means "every arch upstream serves", resolved at sync. An
    arm64-only repository was once silently turned into an amd64 one — with
    `prune = true` that publishes an empty tree over a correct one."""
    _stub_nothing(monkeypatch)

    async def scenario():
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            step = await _paste(pilot, WITH_KEY)
            src = step.get_sources()[0]

        assert src.arches == [], f"guessed {src.arches}"
        assert src.suites == ["noble"]

    _run(scenario)


def test_an_inline_gpg_key_reaches_the_source(monkeypatch):
    _stub(monkeypatch)

    async def scenario():
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            step = await _paste(pilot, WITH_KEY)
            src = step.get_sources()[0]

        assert src.gpg_key and "BEGIN PGP PUBLIC KEY BLOCK" in src.gpg_key

    _run(scenario)


def test_a_paste_naming_several_repositories_imports_all_of_them(monkeypatch):
    _stub(monkeypatch)

    async def scenario():
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            step = await _paste(pilot, MULTI)
            # Three entries, two repositories — one draft each, and the pickers
            # step aside for the review pane.
            assert len(step._bundle) == 2
            assert step.query_one("#bulk-row").display is True
            assert step.query_one("#fields-container").display is False
            sources = step.get_sources()

        assert len(sources) == 2, "one per repository"
        assert sorted(s.url for s in sources) == [
            "http://archive.example.com/ubuntu",
            "http://security.example.com/ubuntu",
        ]

    _run(scenario)


ARCH_PLACEHOLDER = "deb https://nvidia.example/deb/$(ARCH) /\n"


def test_a_placeholder_paste_resolves_off_thread_into_one_repo_per_arch(monkeypatch):
    """`$(ARCH)` (NVIDIA's libnvidia-container ships exactly this) can't be
    resolved on the UI thread — resolving it means probing each known
    architecture over the network. This drives the real paste → worker →
    call_from_thread path, not just the pure functions underneath it."""
    monkeypatch.setattr(discover, "expand_arch_placeholder",
                         lambda url, token: ["amd64", "arm64", "ppc64el"])

    async def scenario():
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            step = await _paste(pilot, ARCH_PLACEHOLDER)
            assert len(step._bundle) == 3
            assert step.query_one("#bulk-row").display is True
            sources = step.get_sources()

        assert sorted(s.url for s in sources) == [
            "https://nvidia.example/deb/amd64",
            "https://nvidia.example/deb/arm64",
            "https://nvidia.example/deb/ppc64el",
        ]
        assert all(s.provider == "apt-flat" for s in sources)

    _run(scenario)


def test_a_placeholder_that_resolves_to_nothing_reports_and_does_not_advance(monkeypatch):
    monkeypatch.setattr(discover, "expand_arch_placeholder", lambda url, token: [])

    async def scenario():
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            step = await _paste(pilot, ARCH_PLACEHOLDER)
            assert step._bundle == []
            assert step._draft is None
            assert step.query_one("#fields-container").display is False
            assert step.query_one("#bulk-row").display is False
            assert "no architecture" in str(step.query_one("#detect-status", Label).content)

    _run(scenario)


def test_an_unrecognized_paste_does_not_advance(monkeypatch):
    _stub(monkeypatch)

    async def scenario():
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            step = await _paste(pilot, "not a repo config\nsecond line")
            assert step.query_one("#fields-container").display is False
            assert step.get_sources() == []

    _run(scenario)


# ── both modes reach the same place ──────────────────────────────────────────

def test_the_tui_names_a_repository_the_same_way_mmw_add_does(monkeypatch):
    """These had drifted: the step derived `archive.example.com-ubuntu` while
    core derives `example-archive-ubuntu`."""
    _stub(monkeypatch)

    async def scenario():
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            step = await _probe(pilot)
            await _pick(pilot, step, "suites", ["noble"])
            src = step.get_sources()[0]

        assert src.name == config.derive_name(URL)
        assert src.url == URL, "no trailing slash — must match what `mmw add` writes"

    _run(scenario)


def test_a_probed_repository_reaches_confirm_and_dismisses(monkeypatch):
    _stub(monkeypatch)

    async def scenario():
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            step = await _probe(pilot)
            await _pick(pilot, step, "suites", ["noble"])
            await _press(pilot, "#btn-next")
            assert screen._step_idx == screen._CONFIRM
            await _press(pilot, "#btn-next")
            for _ in range(40):
                if app.result is not None:
                    break
                await pilot.pause()

        assert app.result and app.result[0].suites == ["noble"]

    _run(scenario)


def test_confirm_hosts_the_deb_src_switch(monkeypatch):
    """StepConfirm owns include_sources; the repository step must route through
    it rather than dismissing directly."""
    _stub(monkeypatch)

    async def scenario():
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            step = await _probe(pilot)
            await _pick(pilot, step, "suites", ["noble"])
            await _press(pilot, "#btn-next")

            app.screen.query_one(StepConfirm).query_one("#src-switch", Switch).value = True
            await pilot.pause()
            await _press(pilot, "#btn-next")
            for _ in range(40):
                if app.result is not None:
                    break
                await pilot.pause()

        assert app.result and all(s.include_sources for s in app.result)

    _run(scenario)


def test_back_from_confirm_returns_to_the_repository_step(monkeypatch):
    _stub(monkeypatch)

    async def scenario():
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            step = await _probe(pilot)
            await _pick(pilot, step, "suites", ["noble"])
            await _press(pilot, "#btn-next")
            assert screen._step_idx == screen._CONFIRM

            await _press(pilot, "#btn-back")
            await _await_step(pilot)
            assert screen._step_idx == screen._REPO
            assert screen.query(StepCustom)
            assert screen._sources == []

    _run(scenario)


# ── merging a repository we already have ─────────────────────────────────────

def test_pasting_a_config_for_a_known_host_merges_rather_than_duplicating():
    """Identity is the URI, so a second config for the same repository extends
    it — suites and components unioned, never a duplicate entry."""
    from mmw.core.apt_sources import entries_to_sources, parse_apt_sources

    existing = entries_to_sources(parse_apt_sources(
        "deb http://archive.example.com/ubuntu noble main universe\n"))
    existing[0].arches = ["amd64", "arm64"]

    merged, added, _ = config.merge_sources(
        existing, entries_to_sources(parse_apt_sources(ARM_ONLY)))

    assert added == 0, "same URI must merge, not append"
    assert len(merged) == 1
    src = merged[0]
    assert src.suites == ["noble"]
    assert "universe" in src.components and "main" in src.components
    assert src.arches == ["amd64", "arm64"], "a stated subset must not narrow it"


def test_merging_an_all_arches_paste_keeps_all():
    from mmw.core.apt_sources import entries_to_sources, parse_apt_sources

    existing = entries_to_sources(parse_apt_sources(
        "deb [arch=arm64] http://archive.example.com/ubuntu noble main\n"))
    incoming = entries_to_sources(parse_apt_sources(
        "deb http://archive.example.com/ubuntu noble main\n"))   # no arch = all

    merged, _, _ = config.merge_sources(existing, incoming)
    assert merged[0].arches == [], '"all" must absorb a narrower list'


def test_an_unprobeable_config_keeps_its_stated_selection(monkeypatch):
    """`RepositoryDraft.stored` may only collapse a *probed* list to "all". When nothing could
    be enumerated, `available` is merely what the config named — collapsing it
    would turn an explicit `Suites: noble` into every suite the repo has."""
    _stub_nothing(monkeypatch)

    async def scenario():
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            step = await _paste(pilot, ARM_ONLY)
            assert step._draft.enumerated["suites"] is False
            src = step.get_sources()[0]

        assert src.suites == ["noble"], "an unprobed selection must stay explicit"
        assert src.arches == ["arm64"]

    _run(scenario)


# ── a bundle: the pane, reached from the same paste box ──────────────────────

def test_a_bundle_paste_opens_the_review_pane_and_keeps_what_it_returns(monkeypatch):
    """The DGX case: one paste, several repositories, each configured in the
    pane rather than imported wholesale."""
    from textual.widgets import Checkbox

    from mmw.tui.bulk_review import RepoRow
    _stub(monkeypatch)

    async def scenario():
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            step = await _paste(pilot, MULTI)
            assert len(step._bundle) == 2

            step.query_one("#btn-bulk", Button).press()
            await _settle(pilot)
            pane = pilot.app.screen
            assert len(list(pane.query(RepoRow))) == 2

            pane.query_one("#repo-1-on", Checkbox).value = False
            await pilot.pause()
            pane.query_one("#btn-done", Button).press()
            await _settle(pilot)

            sources = step.get_sources()

        assert [s.url for s in sources] == ["http://archive.example.com/ubuntu"]

    _run(scenario)


def test_cancelling_the_review_pane_leaves_the_bundle_alone(monkeypatch):
    _stub(monkeypatch)

    async def scenario():
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            step = await _paste(pilot, MULTI)
            step.query_one("#btn-bulk", Button).press()
            await _settle(pilot)
            pilot.app.screen.query_one("#btn-cancel", Button).press()
            await _settle(pilot)

            assert len(step._bundle) == 2
            assert len(step.get_sources()) == 2

    _run(scenario)


def test_a_pasted_deb_src_survives_the_confirm_step(monkeypatch):
    """The switch used to default to off and overwrite what the config said."""
    _stub(monkeypatch)

    async def scenario():
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            await _paste(pilot, "deb-src http://archive.example.com/ubuntu noble main\n"
                                "deb http://archive.example.com/ubuntu noble main\n")
            await _press(pilot, "#btn-next")
            await _settle(pilot)
            assert pilot.app.screen.query_one("#src-switch", Switch).value is True
            await _press(pilot, "#btn-next")
            await _settle(pilot)

        assert app.result[0].include_sources is True

    _run(scenario)


def test_loading_a_directory_of_configs_lands_in_the_paste_box(monkeypatch, tmp_path):
    """The DGX path without a shell: point at a sources.list.d and mmw reads and
    joins the files itself, so records can never run together the way a
    hand-rolled `cat` lets them."""
    from mmw.tui.path_pick import PathPickModal
    _stub(monkeypatch)

    d = tmp_path / "sources.list.d"
    d.mkdir()
    # No trailing blank line — exactly how these files ship.
    for host in ("archive", "security"):
        (d / f"{host}.sources").write_text(
            f"Types: deb\nURIs: http://{host}.example.com/ubuntu\n"
            "Suites: noble\nComponents: main\n"
        )

    async def scenario():
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            step = await _step(pilot)
            step.query_one("#mode-paste", RadioButton).value = True
            await pilot.pause()
            step.query_one("#btn-load", Button).press()
            await _settle(pilot)

            picker = pilot.app.screen
            assert isinstance(picker, PathPickModal)
            picker.query_one("#path-input", Input).value = str(d)
            picker.query_one("#btn-use", Button).press()
            await _settle(pilot)

            assert "archive.example.com" in step.query_one("#paste-area", TextArea).text
            assert len(step._bundle) == 2, "both files parsed as separate records"

    _run(scenario)


def test_cancelling_the_file_browser_leaves_the_paste_box_alone(monkeypatch):
    _stub(monkeypatch)

    async def scenario():
        app = _Harness()
        async with app.run_test() as pilot:
            await pilot.pause()
            step = await _step(pilot)
            step.query_one("#mode-paste", RadioButton).value = True
            await pilot.pause()
            step.query_one("#paste-area", TextArea).text = "deb http://a.example.com/ubuntu noble main\n"
            await _settle(pilot)

            step.query_one("#btn-load", Button).press()
            await _settle(pilot)
            pilot.app.screen.query_one("#btn-cancel", Button).press()
            await _settle(pilot)

            assert "a.example.com" in step.query_one("#paste-area", TextArea).text

    _run(scenario)
