"""Your own .debs in the TUI: a network's custom packages per upstream mirror,
the clash question, and the quick "add packages" flow from Sources. Every
change goes through `core/layers.py`, the same functions `mmw packages` uses."""
import asyncio
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import Checkbox, Label, ListItem, ListView, Select, SelectionList, Static
from textual.widgets.selection_list import Selection

from mmw.core import layers
from mmw.tui.ui.form import ChoiceField, Form, ToggleField
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button

_SIGNING = [("upstream's own signature — no custom packages", "upstream"),
            ("re-sign with an mmw key", "resign"),
            ("unsigned (unsafe)", "unsigned")]
_UNSIGNED_WARNING = ("Unsigned is unsafe: apt cannot tell these files came from you, and every "
                     "client needs [trusted=yes] for this repository.")
_RESIGN_NOTE = ("Clients trust this mmw key for this repository instead of upstream's — give them "
                "the sources file (s) and the key it names.")
_NO_KEY = ""


class ClashDialog(ModalScreen[str]):
    """keep / drop / cancel for one deb that clashes with upstream's."""

    def __init__(self, clash: layers.Clash):
        super().__init__()
        self.clash = clash

    def compose(self) -> ComposeResult:
        c = self.clash
        what = ("the same version as upstream's, with different contents" if c.kind == "same-version"
                else f"older than upstream's {c.upstream_version}, so clients would not pick it")
        with Vertical(id="dialog"):
            yield Label("Custom package clashes with upstream", classes="dialog-title")
            yield Static(f"[b]{escape(c.filename)}[/b] — {escape(c.package)} {escape(c.version)} is "
                         f"{escape(what)}.", id="clash-text")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Keep mine", id="btn-clash-keep", variant="primary", compact=True)
                yield Button("Drop mine", id="btn-clash-drop", compact=True)
                yield Button("Cancel", id="btn-clash-cancel", compact=True)

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id.removeprefix("btn-clash-"))


async def ask_clashes(app, clashes) -> dict[str, str] | None:
    """Each clash's answer, or None when one was cancelled."""
    answers: dict[str, str] = {}
    for clash in clashes:
        choice = await app.push_screen_wait(ClashDialog(clash))
        if choice == "cancel":
            return None
        answers[clash.filename] = choice
    return answers


def _key_options(app) -> list[tuple[str, str]]:
    from mmw.core.signing import Keyring, SigningError
    try:
        names = [k.name for k in Keyring(app.cfg.settings.mirror_dir).list()]
    except SigningError:
        names = []
    return [("no key", _NO_KEY), *[(n, n) for n in names]]


def _network_repos(cfg, net) -> list:
    """The apt upstream repositories a network selects."""
    from mmw.core.config import normalize_url
    chosen = {normalize_url(s.source) for s in net.select}
    return [s for s in cfg.sources if s.provider == "apt" and (not chosen or s.identity in chosen)]


class NetworkPackagesPage(Page):
    """A network's custom packages, one upstream mirror at a time."""

    TITLE = "Custom packages"

    def __init__(self, network):
        super().__init__()
        self.network_name = network.name
        self.subtitle = network.name
        self._dims: dict = {}
        self._rows: list[tuple[str, str]] = []
        self._shown: str | None = None

    def _net(self):
        return next(n for n in self.app.cfg.networks if n.name == self.network_name)

    def _repo(self):
        name = self.query_one("#pkg-repo", Select).value
        return next((s for s in self.app.cfg.sources if s.name == name), None)

    def compose_body(self) -> ComposeResult:
        repos = _network_repos(self.app.cfg, self._net())
        options = [(s.name, s.name) for s in repos] or [("no apt mirrors in this network", "")]
        yield Form(
            ChoiceField("Repository", options, options[0][1], id="pkg-repo",
                        hint="An upstream mirror this network publishes."),
            ChoiceField("Signing", _SIGNING, "upstream", id="pkg-signing",
                        hint="Custom packages change the index, so upstream's signature cannot stay."),
            ChoiceField("Key", _key_options(self.app), _NO_KEY, id="pkg-key",
                        hint="The mmw signing key, when re-signing (Settings → Signing keys)."),
            ToggleField("apt-file support", False, id="pkg-contents",
                        hint="Do you want apt-file to pick up these custom packages? Rewrites the "
                             "Contents files, which can make signing take significantly longer."),
            ToggleField("Drop Valid-Until", False, id="pkg-drop-vu",
                        hint="Re-signing only: the airgapped copy never expires — but a client can "
                             "no longer tell a stale mirror from a fresh one."),
            ChoiceField("Add into suite", [("—", "")], "", id="pkg-suite"),
            ChoiceField("Component", [("main", "main")], "main", id="pkg-component"),
        )
        yield Label("", id="pkg-warning")
        yield ListView(id="pkg-debs")

    def compose_actions(self) -> ComposeResult:
        yield Button("Add…", id="btn-pkg-add", compact=True)
        yield Button("Remove", id="btn-pkg-remove", compact=True)
        yield Button("Sources file", id="btn-pkg-sources", compact=True)
        yield Button("Save", id="btn-pkg-save", variant="primary", compact=True)

    def on_mount(self) -> None:
        self._show_layer()

    @on(Select.Changed, "#pkg-repo")
    def _repo_changed(self) -> None:
        # A Select also posts Changed for its initial value: only another
        # repository may reset the choices on screen.
        repo = self._repo()
        if self.is_mounted and repo is not None and repo.name != self._shown:
            self._show_layer()

    def _show_layer(self) -> None:
        """The repository's saved choices, at once — never from a worker that
        could land after the user has started changing them — then what needs
        the pool (its suites, its packages) in the background."""
        repo = self._repo()
        if repo is None:
            return
        self._shown = repo.name
        layer = self._net().layer_for(repo.url)
        self.query_one("#pkg-signing", Select).value = layer.signing if layer else "upstream"
        keys = dict((v, k) for k, v in _key_options(self.app))
        self.query_one("#pkg-key", Select).value = (layer.key if layer and layer.key in keys else _NO_KEY)
        self.query_one("#pkg-contents", Checkbox).value = bool(layer and layer.contents)
        self.query_one("#pkg-drop-vu", Checkbox).value = bool(layer and layer.drop_valid_until)
        self.run_worker(self._load(), group="pkg-load", exclusive=True, exit_on_error=False)

    async def _load(self) -> None:
        repo = self._repo()
        if repo is None:
            return
        cfg, mirror_dir = self.app.cfg, self.app.cfg.settings.mirror_dir
        self._dims = await asyncio.to_thread(layers.published_dims, self._net(), cfg, mirror_dir, repo)
        suites = sorted(self._dims) or [""]
        self.query_one("#pkg-suite", Select).set_options([(s or "—", s) for s in suites])
        self.query_one("#pkg-suite", Select).value = suites[0]
        await self._list()

    @on(Select.Changed, "#pkg-suite")
    def _suite_changed(self) -> None:
        comps = sorted(self._dims.get(self.query_one("#pkg-suite", Select).value, {})) or ["main"]
        self.query_one("#pkg-component", Select).set_options([(c, c) for c in comps])
        self.query_one("#pkg-component", Select).value = "main" if "main" in comps else comps[0]

    @on(Select.Changed, "#pkg-signing")
    def _signing_changed(self) -> None:
        value = self.query_one("#pkg-signing", Select).value
        text = {"unsigned": f"[$mmw-warn]⚠ {escape(_UNSIGNED_WARNING)}[/]",
                "resign": escape(_RESIGN_NOTE)}.get(value, "")
        self.query_one("#pkg-warning", Label).update(text)

    async def _list(self) -> None:
        repo = self._repo()
        rows = await asyncio.to_thread(layers.list_packages, self.app.cfg, self.app.cfg.settings.mirror_dir,
                                       self.network_name)
        where = f"{self.network_name}: {repo.name}" if repo else ""
        self._rows = [(loc, name) for w, loc, name in rows if w == where]
        listing = self.query_one("#pkg-debs", ListView)
        await listing.clear()
        for loc, name in self._rows:
            await listing.append(ListItem(Label(f"{escape(loc)}  {escape(name)}")))

    def _choices(self) -> dict:
        key = self.query_one("#pkg-key", Select).value
        return {"signing": self.query_one("#pkg-signing", Select).value, "key": key or None,
                "contents": self.query_one("#pkg-contents", Checkbox).value,
                "drop_valid_until": self.query_one("#pkg-drop-vu", Checkbox).value}

    # ── save ────────────────────────────────────────────────────────────────

    @on(Button.Pressed, "#btn-pkg-save")
    def _save_pressed(self) -> None:
        self.start_flow(self._save)

    async def _save(self) -> None:
        repo = self._repo()
        if repo is None:
            return
        c = self._choices()
        try:
            await self.app.save_config(lambda cfg: layers.set_signing(
                cfg, self.network_name, repo.name, signing=c["signing"], key=c["key"],
                contents=c["contents"], drop_valid_until=c["drop_valid_until"]))
        except ValueError as e:
            self.set_status(str(e).splitlines()[-1] if "\n" in str(e) else str(e), "error")
            return
        self.set_status(f"Saved — publish {self.network_name} to serve it.", "ok")

    # ── add, remove ─────────────────────────────────────────────────────────

    @on(Button.Pressed, "#btn-pkg-add")
    def _add_pressed(self) -> None:
        self.start_flow(self._add_flow)

    async def _add_flow(self) -> None:
        from mmw.tui.path_pick import PathPickPage
        picked = await self.app.push_screen_wait(PathPickPage(
            "Add packages", "A .deb file, or a folder of them", dirs_only=False, suffixes=(".deb",)))
        if picked is None:
            return
        await self.add_debs([Path(picked)], self.query_one("#pkg-suite", Select).value,
                            self.query_one("#pkg-component", Select).value)

    async def add_debs(self, paths: list[Path], suite: str, component: str) -> None:
        repo = self._repo()
        cfg, mirror_dir = self.app.cfg, self.app.cfg.settings.mirror_dir
        c = self._choices()
        layer = self._net().layer_for(repo.url)
        new_layer = layer is None or layer.signing == "upstream"
        if new_layer and c["signing"] not in ("resign", "unsigned"):
            self.set_status("Choose re-sign or unsigned first — " + layers.needs_signing(repo.name)
                            .split(". ", 1)[0] + ".", "error")
            return
        try:
            clashes = await asyncio.to_thread(layers.check_clashes, cfg, mirror_dir, paths, into=repo.name,
                                              network=self.network_name, suite=suite, component=component)
            answers = await ask_clashes(self.app, clashes)
            if answers is None:
                self.set_status("Cancelled — nothing was added.")
                return
            out = await asyncio.to_thread(
                layers.add_packages, cfg, mirror_dir, paths, into=repo.name, network=self.network_name,
                suite=suite, component=component,
                signing=c["signing"] if new_layer else None, key=c["key"] if new_layer else None,
                ask=lambda clash: answers[clash.filename])
        except ValueError as e:
            self.set_status(str(e), "error")
            return
        await self.app.save_config(lambda _cfg: out.cfg)
        await self._list()
        self.set_status(f"Added {len(out.copied)} package(s). " + " ".join(out.notes[-1:]), "ok")

    @on(Button.Pressed, "#btn-pkg-remove")
    def _remove_pressed(self) -> None:
        self.start_flow(self._remove)

    async def _remove(self) -> None:
        index = self.query_one("#pkg-debs", ListView).index
        if index is None or not 0 <= index < len(self._rows):
            self.set_status("Highlight a package to remove.", "warning")
            return
        _, name = self._rows[index]
        try:
            await asyncio.to_thread(layers.remove_package, self.app.cfg, self.app.cfg.settings.mirror_dir,
                                    name, into=self._repo().name, network=self.network_name)
        except ValueError as e:
            self.set_status(str(e), "error")
            return
        await self._list()
        self.set_status(f"Removed {name} — the next publish drops it.", "ok")

    @on(Button.Pressed, "#btn-pkg-sources")
    def _sources_pressed(self) -> None:
        from mmw.tui.sources_file import SourcesFilePage
        repo = self._repo()
        cfg, mirror_dir, net = self.app.cfg, self.app.cfg.settings.mirror_dir, self.network_name
        try:
            layers.layer_sources_file(cfg, mirror_dir, net, repo.name, "deb822")
        except ValueError as e:
            self.set_status(str(e), "warning")
            return
        self.app.push_screen(SourcesFilePage(
            repo, name=f"{repo.name}-{net}",
            render=lambda fmt: layers.layer_sources_file(cfg, mirror_dir, net, repo.name, fmt)))


def _label(target: str) -> str:
    """`network\trepo` (or a folder repository's name) as people read it."""
    return " · ".join(target.split("\t"))


class AddPackagesPage(Page):
    """Quick add: some .debs, into any repositories — folder repositories, or
    upstream mirrors as a network serves them."""

    TITLE = "Add packages"

    def __init__(self, paths: list[Path] | None = None):
        super().__init__()
        self.paths = list(paths or [])

    def compose_body(self) -> ComposeResult:
        cfg = self.app.cfg
        targets = [Selection(f"folder · {s.name}", s.name) for s in cfg.sources if s.provider == "apt-local"]
        targets += [Selection(f"{n.name} · {s.name} (upstream mirror)", f"{n.name}\t{s.name}")
                    for n in cfg.networks for s in _network_repos(cfg, n)]
        yield Label(self._paths_text(), id="quick-paths")
        yield SelectionList(*targets, id="quick-targets")
        yield Form(
            ChoiceField("New layers", [("re-sign with an mmw key", "resign"), ("unsigned (unsafe)", "unsigned")],
                        "resign", id="quick-signing",
                        hint="For an upstream mirror that has no custom packages yet."),
            ChoiceField("Key", _key_options(self.app), _NO_KEY, id="quick-key"),
        )

    def compose_actions(self) -> ComposeResult:
        yield Button("Choose…", id="btn-quick-pick", compact=True)
        yield Button("Add", id="btn-quick-add", variant="primary", compact=True)

    def _paths_text(self) -> str:
        return ", ".join(p.name for p in self.paths) or "No packages chosen — Choose… a .deb or a folder."

    @on(Button.Pressed, "#btn-quick-pick")
    def _pick_pressed(self) -> None:
        self.start_flow(self._pick)

    async def _pick(self) -> None:
        from mmw.tui.path_pick import PathPickPage
        picked = await self.app.push_screen_wait(PathPickPage(
            "Add packages", "A .deb file, or a folder of them", dirs_only=False, suffixes=(".deb",)))
        if picked is not None:
            self.paths = [Path(picked)]
            self.query_one("#quick-paths", Label).update(self._paths_text())

    @on(Button.Pressed, "#btn-quick-add")
    def _add_pressed(self) -> None:
        self.start_flow(self._add)

    async def _add(self) -> None:
        targets = self.query_one("#quick-targets", SelectionList).selected
        if not self.paths or not targets:
            self.set_status("Choose packages and at least one repository.", "warning")
            return
        signing = self.query_one("#quick-signing", Select).value
        key = self.query_one("#quick-key", Select).value or None
        mirror_dir = self.app.cfg.settings.mirror_dir
        added, notes = 0, []
        for target in targets:
            network, _, into = target.rpartition("\t")
            network = network or None
            cfg = self.app.cfg
            try:
                kw = {}
                if network:
                    net = next(n for n in cfg.networks if n.name == network)
                    repo = next(s for s in cfg.sources if s.name == into)
                    layer = net.layer_for(repo.url)
                    if layer is None or layer.signing == "upstream":
                        kw = {"signing": signing, "key": key if signing == "resign" else None}
                    clashes = await asyncio.to_thread(layers.check_clashes, cfg, mirror_dir, self.paths,
                                                      into=into, network=network)
                    answers = await ask_clashes(self.app, clashes)
                    if answers is None:
                        notes.append(f"{_label(target)}: cancelled")
                        continue
                    kw["ask"] = lambda clash, answers=answers: answers[clash.filename]
                out = await asyncio.to_thread(layers.add_packages, cfg, mirror_dir, self.paths, into=into,
                                              network=network, **kw)
            except ValueError as e:
                notes.append(f"{_label(target)}: {e}")
                continue
            if out.cfg is not cfg:
                await self.app.save_config(lambda _c, new=out.cfg: new)
            added += len(out.copied)
        self.set_status(f"Added {added} package file(s)." + (" " + " ".join(notes) if notes else ""),
                        "error" if notes else "ok")
