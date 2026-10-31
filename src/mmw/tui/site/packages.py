"""The far side's local packages: debs this site adds to a repository it
serves, and how it is then signed. Every change serves the repository again
(`ops.relayer`); the functions are the ones `mmw site packages` uses."""
import asyncio
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.markup import escape
from textual.widgets import Checkbox, Label, ListItem, ListView, Select

from mmw.site import layers as site_layers
from mmw.site.db import SiteDB
from mmw.tui.packages import _RESIGN_NOTE, _SIGNING, _UNSIGNED_WARNING, ask_clashes
from mmw.tui.ui.form import ChoiceField, Form, ToggleField
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button

_NO_KEY = ""


def _keys(home: Path) -> list[tuple[str, str]]:
    from mmw.core.signing import Keyring, SigningError
    try:
        names = [k.name for k in Keyring(home).list()]
    except SigningError:
        names = []
    return [("no key", _NO_KEY), *[(n, n) for n in names]]


class SitePackagesPage(Page):
    TITLE = "Local packages"

    def __init__(self, home: Path, store, repo: str):
        super().__init__()
        self.home, self.store, self.repo = Path(home), store, repo
        self.subtitle = repo
        self._dims: dict = {}
        self._rows: list[tuple[str, str]] = []

    def compose_body(self) -> ComposeResult:
        with SiteDB(self.home) as db:
            layer = db.layer(self.repo)
        keys = _keys(self.home)
        key = layer.key if layer and layer.key in dict((v, k) for k, v in keys) else _NO_KEY
        yield Form(
            ChoiceField("Signing", _SIGNING, layer.signing if layer else "upstream", id="spkg-signing",
                        hint="Local packages change the index, so upstream's signature cannot stay."),
            ChoiceField("Key", keys, key, id="spkg-key",
                        hint="This site's signing key, when re-signing (K: signing keys)."),
            ToggleField("apt-file support", bool(layer and layer.contents), id="spkg-contents",
                        hint="Do you want apt-file to pick up these packages? Rewrites the Contents "
                             "files, which can make signing take significantly longer."),
            ToggleField("Drop Valid-Until", bool(layer and layer.drop_valid_until), id="spkg-drop-vu",
                        hint="Re-signing only: this copy never expires — but a client can no longer tell "
                             "a stale mirror from a fresh one."),
            ChoiceField("Add into suite", [("—", "")], "", id="spkg-suite"),
            ChoiceField("Component", [("main", "main")], "main", id="spkg-component"),
        )
        yield Label("", id="spkg-warning")
        yield ListView(id="spkg-debs")

    def compose_actions(self) -> ComposeResult:
        yield Button("Add…", id="btn-spkg-add", compact=True)
        yield Button("Remove", id="btn-spkg-remove", compact=True)
        yield Button("Sources file", id="btn-spkg-sources", compact=True)
        yield Button("Save", id="btn-spkg-save", variant="primary", compact=True)

    def on_mount(self) -> None:
        self._signing_changed()
        self.run_worker(self._load(), group="spkg-load", exclusive=True, exit_on_error=False)

    def _dims_now(self) -> dict:
        with SiteDB(self.home) as db:
            return site_layers._published_of(db, self.store, self.repo)[2]

    async def _load(self) -> None:
        try:
            self._dims = await asyncio.to_thread(self._dims_now)
        except ValueError as e:
            self.set_status(str(e), "warning")
            self._dims = {}
        suites = sorted(self._dims) or [""]
        self.query_one("#spkg-suite", Select).set_options([(s or "—", s) for s in suites])
        self.query_one("#spkg-suite", Select).value = suites[0]
        await self._list()

    @on(Select.Changed, "#spkg-suite")
    def _suite_changed(self) -> None:
        comps = sorted(self._dims.get(self.query_one("#spkg-suite", Select).value, {})) or ["main"]
        self.query_one("#spkg-component", Select).set_options([(c, c) for c in comps])
        self.query_one("#spkg-component", Select).value = "main" if "main" in comps else comps[0]

    @on(Select.Changed, "#spkg-signing")
    def _signing_changed(self) -> None:
        value = self.query_one("#spkg-signing", Select).value
        text = {"unsigned": f"[$mmw-warn]⚠ {escape(_UNSIGNED_WARNING)}[/]",
                "resign": escape(_RESIGN_NOTE)}.get(value, "")
        self.query_one("#spkg-warning", Label).update(text)

    async def _list(self) -> None:
        def rows():
            with SiteDB(self.home) as db:
                return [(loc, name) for _lab, loc, name, repo in site_layers.list_site_packages(self.home, db)
                        if repo == self.repo]
        self._rows = await asyncio.to_thread(rows)
        listing = self.query_one("#spkg-debs", ListView)
        await listing.clear()
        for loc, name in self._rows:
            await listing.append(ListItem(Label(f"{escape(loc)}  {escape(name)}")))

    def _choices(self) -> dict:
        return {"signing": self.query_one("#spkg-signing", Select).value,
                "key": self.query_one("#spkg-key", Select).value or None,
                "contents": self.query_one("#spkg-contents", Checkbox).value,
                "drop_valid_until": self.query_one("#spkg-drop-vu", Checkbox).value}

    def _relayer(self) -> str:
        from mmw.site.ops import relayer
        result = relayer(self.home, self.repo)
        if result.held:
            return f"Held: {result.held}"
        return f"Serving it again — {result.placed} placed, {result.removed} removed."

    # ── save ────────────────────────────────────────────────────────────────

    @on(Button.Pressed, "#btn-spkg-save")
    def _save_pressed(self) -> None:
        self.start_flow(self._save)

    async def _save(self) -> None:
        c = self._choices()

        def save():
            with SiteDB(self.home) as db:
                site_layers.set_site_signing(self.home, db, self.repo, **c)
            return self._relayer()
        try:
            said = await asyncio.to_thread(save)
        except ValueError as e:
            self.set_status(str(e), "error")
            return
        self.set_status(f"Saved. {said}", "ok")

    # ── add, remove, sources ────────────────────────────────────────────────

    @on(Button.Pressed, "#btn-spkg-add")
    def _add_pressed(self) -> None:
        self.start_flow(self._add_flow)

    async def _add_flow(self) -> None:
        from mmw.tui.path_pick import PathPickPage
        picked = await self.app.push_screen_wait(PathPickPage(
            "Add packages", "A .deb file, or a folder of them", dirs_only=False, suffixes=(".deb",)))
        if picked is not None:
            await self.add_debs([Path(picked)], self.query_one("#spkg-suite", Select).value,
                                self.query_one("#spkg-component", Select).value)

    async def add_debs(self, paths: list[Path], suite: str, component: str) -> None:
        c = self._choices()

        def clashes():
            with SiteDB(self.home) as db:
                return site_layers.check_site_clashes(self.home, db, self.store, self.repo, paths,
                                                      suite, component)[2]
        try:
            found = await asyncio.to_thread(clashes)
            answers = await ask_clashes(self.app, found)
            if answers is None:
                self.set_status("Cancelled — nothing was added.")
                return

            def add():
                with SiteDB(self.home) as db:
                    layer = db.layer(self.repo)
                    new = layer is None or layer.signing == "upstream"
                    notes = site_layers.add_site_packages(
                        self.home, db, self.store, self.repo, paths, suite=suite, component=component,
                        signing=c["signing"] if new and c["signing"] != "upstream" else None,
                        key=c["key"] if new else None, decisions=answers)
                return notes, self._relayer()
            notes, said = await asyncio.to_thread(add)
        except ValueError as e:
            self.set_status(str(e), "error")
            return
        await self._list()
        self.set_status(f"{' '.join(notes[-1:])} {said}", "ok")

    @on(Button.Pressed, "#btn-spkg-remove")
    def _remove_pressed(self) -> None:
        self.start_flow(self._remove)

    async def _remove(self) -> None:
        index = self.query_one("#spkg-debs", ListView).index
        if index is None or not 0 <= index < len(self._rows):
            self.set_status("Highlight a package to remove.", "warning")
            return
        location, name = self._rows[index]

        def remove():
            site_layers.remove_site_package(self.home, self.repo, name, location=location)
            return self._relayer()
        try:
            said = await asyncio.to_thread(remove)
        except ValueError as e:
            self.set_status(str(e), "error")
            return
        await self._list()
        self.set_status(f"Removed {name}. {said}", "ok")

    @on(Button.Pressed, "#btn-spkg-sources")
    def _sources_pressed(self) -> None:
        self.start_flow(self._sources)

    async def _sources(self) -> None:
        from mmw.tui.sources_file import SourcesFilePage

        def texts():
            with SiteDB(self.home) as db:
                return {fmt: site_layers.site_sources_file(self.home, db, self.store, self.repo, fmt)
                        for fmt in ("deb822", "list")}
        try:
            got = await asyncio.to_thread(texts)
        except ValueError as e:
            self.set_status(str(e), "warning")
            return

        class _Named:
            url = self.repo
            name = self.repo.rsplit("/", 1)[-1] or "repository"
        self.app.push_screen(SourcesFilePage(_Named(), render=got.__getitem__))
