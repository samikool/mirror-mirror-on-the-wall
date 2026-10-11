"""Add and edit a network — one `NetworkForm`, two pages.

`EditNetworkPage` shows every section of the form at once; `AddNetworkPage`
walks the same sections one per wizard step. Same fields, same checks
(`networks.form_problems`), so the two can no longer drift: Add used to skip
the network-name rule that Edit enforced.

The one deliberate difference is what the publish root means, and it is a
form *mode*, not a second implementation: Add picks the directory the network
is published **under** (the network name is created inside it); Edit shows and
saves the directory it publishes **into**, verbatim.
"""
import re
from dataclasses import dataclass
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.markup import escape
from textual.widgets import Checkbox, Input, Label, SelectionList, Static
from textual.widgets.selection_list import Selection

from mmw.core import config
from mmw.tui import networks
from mmw.tui.ui.form import Form, PathField, TextField, ToggleField
from mmw.tui.ui.page import Page
from mmw.tui.ui.wizard import Wizard
from mmw.tui.ui.widgets import Button, JumpSelectionList


@dataclass
class _Axis:
    """One repository's three dimension pickers inside a network editor."""
    source: str
    label: str
    suites: list[str]
    components: list[str]
    arches: list[str]
    chosen: "config.NetworkSelect | None"

    def _id(self, dim: str) -> str:
        return "axis-" + re.sub(r"[^a-zA-Z0-9_-]", "-", self.source) + "-" + dim

    def block_id(self) -> str:
        return "axis-block-" + re.sub(r"[^a-zA-Z0-9_-]", "-", self.source)

    def _picked(self, dim: str, available: list[str]) -> set[str]:
        """Empty in the stored select means all — show that as everything ticked."""
        if self.chosen is None:
            return set(available)
        stored = getattr(self.chosen, dim)
        return set(stored) if stored else set(available)

    def compose_widgets(self):
        yield Label(f"[b]{escape(self.label)}[/]  [dim]{escape(self.source)}[/]",
                    classes="axis-title")
        for dim, values in (
            ("suites", self.suites),
            ("components", self.components),
            ("arches", self.arches),
        ):
            if not values:
                continue
            picked = self._picked(dim, values)
            yield Label(f"[dim]{dim}[/]", classes="axis-dim")
            yield JumpSelectionList(
                *[Selection(v, v, v in picked) for v in values],
                id=self._id(dim),
                classes="axis-list",
                compact=True,
            )

    def to_select(self, node) -> "config.NetworkSelect | None":
        """None when nothing in this repository is ticked — it is left out."""
        picked = {}
        for dim, values in (
            ("suites", self.suites),
            ("components", self.components),
            ("arches", self.arches),
        ):
            if not values:
                continue
            try:
                sl = node.query_one("#" + self._id(dim), SelectionList)
            except Exception:
                continue
            chosen = list(sl.selected)
            if not chosen:
                return None
            # Everything ticked is stored as "all", so the network keeps
            # following the repository as new suites or arches appear.
            picked[dim] = [] if set(chosen) == set(values) else chosen
        return config.NetworkSelect(source=self.source, **picked)


def build_axes(mirrors, existing_select) -> "list[_Axis]":
    """Group the pool's mirrors into one axis set per repository."""
    by_source: dict[str, list] = {}
    for m in mirrors:
        by_source.setdefault(m.source or m.mirror_base, []).append(m)
    chosen_by_source = {config.normalize_url(s.source): s for s in (existing_select or [])}

    axes = []
    for source, group in sorted(by_source.items()):
        axes.append(_Axis(
            source=source,
            label=group[0].name.split("-")[0] if group else source,
            suites=sorted({m.suite for m in group if m.suite}),
            components=sorted({m.component for m in group if m.component}),
            arches=sorted({m.arch for m in group if m.arch}),
            chosen=chosen_by_source.get(config.normalize_url(source)),
        ))
    return axes


class NetworkForm(Vertical):
    """Every field of a network, in four sections the Add wizard steps through:
    `#nf-where`, `#nf-repos`, `#nf-dims`, `#nf-options`."""

    def __init__(self, mirrors: list[config.MirrorConfig], *, mode: str,
                 network: "config.NetworkConfig | None" = None,
                 existing_names: "set[str] | None" = None):
        super().__init__(id="network-form")
        self.mode = mode
        self._network = network
        self._existing = set(existing_names or ()) - ({network.name} if network else set())
        self._axes = build_axes(mirrors, network.select if network else None)
        if network is None:
            # Add starts with nothing ticked: excluding a repository is then
            # one click, not unticking every dimension it exposes.
            self._checked_sources: set[str] = set()
        elif not network.select:
            # No `select` means every mirrored repository.
            self._checked_sources = {a.source for a in self._axes}
        else:
            self._checked_sources = {a.source for a in self._axes if a.chosen is not None}

    def compose(self) -> ComposeResult:
        net = self._network
        add = self.mode == "add"
        with Vertical(id="nf-where"):
            yield Form(
                TextField("Name", net.name if net else "", id="inp-net-name",
                          hint="Letters, digits, '.', '_' and '-' — it names directories."),
                PathField(
                    "Publish under" if add else "Publish root",
                    str(net.root) if net and net.root else "", id="inp-root",
                    browse_id="btn-browse-root",
                    browse_title="Where should this network publish?",
                    browse_subtitle=("pick the base path — the network name is created inside it"
                                     if add else
                                     "pick the directory this network publishes into"),
                    hint=("Optional. The network name is created inside it." if add else
                          "Optional. Saved exactly as shown — nothing is appended."),
                ),
                PathField(
                    "Bundle output", str(net.bundle_dir) if net and net.bundle_dir else "",
                    id="inp-bundle-dir", browse_id="btn-browse-bundle",
                    browse_title="Where should bundles for this network be written?",
                    browse_subtitle="pick the output directory — nothing is created by picking it",
                    hint="Optional. Where `mmw bundle` writes this network's tarballs.",
                ),
            )
            if add:
                yield Label("", id="preview")
        with Vertical(id="nf-repos"):
            yield Label("[dim]Which repositories to include · ctrl+a toggles all[/]")
            yield JumpSelectionList(
                *[Selection(f"{a.label}  {a.source}", a.source, a.source in self._checked_sources)
                  for a in self._axes],
                id="repo-checklist",
                compact=True,
            )
        with Vertical(id="nf-dims"):
            yield Label("[dim]What this network publishes · ctrl+a toggles a list[/]")
            # `height: auto`, never `1fr` (mmw.tcss): a 1fr region here once
            # resolved against the visible height and rendered at one row.
            with Vertical(id="axis-scroll"):
                for axis in self._axes:
                    with Vertical(id=axis.block_id(), classes="axis-block"):
                        yield from axis.compose_widgets()
        with Vertical(id="nf-options"):
            yield Static("", id="nf-summary")
            yield Form(
                ToggleField("Prune", net.prune if net else True, id="prune-switch",
                            hint="Remove destination files no longer in the mirror on publish."),
                ToggleField("dist-upgrade", net.dist_upgrade if net else False, id="du-switch",
                            hint="Serve changelogs.ubuntu.com meta-release files + UPGRADE.md."),
                ToggleField("TLS certs", net.certs if net else False, id="certs-switch",
                            hint="Generate a key + CSR covering all mirror hostnames."),
                ToggleField("Certs in bundles", net.bundle_tls if net else False,
                            id="bundle-tls-switch",
                            hint="Puts the private key on the media; needs certs + a signed cert."),
                ToggleField("nginx config", net.nginx if net else True, id="nginx-switch",
                            hint="One nginx/mirror.conf, a server block per host."),
                ToggleField("Compress bundles", net.bundle_compress if net else True,
                            id="compress-switch",
                            hint="zstd; off writes a plain .tar — faster on slow disks, larger."),
            )

    def on_mount(self) -> None:
        self._apply_repo_visibility()
        self._update_preview()

    # ── repositories ↔ dimension blocks ──────────────────────────────────────

    def _apply_repo_visibility(self) -> None:
        """Show only the dimension pickers for a currently-checked repository.
        A block is hidden, never unmounted, so unchecking then rechecking keeps
        whatever suite/component/arch ticks were already made."""
        for axis in self._axes:
            try:
                self.query_one("#" + axis.block_id()).display = axis.source in self._checked_sources
            except Exception:
                pass

    @on(SelectionList.SelectedChanged, "#repo-checklist")
    def _repo_checklist_changed(self, event: SelectionList.SelectedChanged) -> None:
        self._checked_sources = set(self.query_one("#repo-checklist", SelectionList).selected)
        self._apply_repo_visibility()

    def checked_axes(self) -> "list[_Axis]":
        return [a for a in self._axes if a.source in self._checked_sources]

    def selects(self) -> "list[config.NetworkSelect]":
        return [s for s in (a.to_select(self) for a in self.checked_axes()) if s is not None]

    # ── values ───────────────────────────────────────────────────────────────

    def _value(self, id: str) -> str:
        return self.query_one(f"#{id}", Input).value.strip()

    def _flag(self, id: str) -> bool:
        return self.query_one(f"#{id}", Checkbox).value

    def network_name(self) -> str:
        return self._value("inp-net-name")

    def name_problem(self) -> str | None:
        return networks.name_problem(self.network_name(), self._existing)

    def _root(self) -> "Path | None":
        root = self._value("inp-root")
        if not root:
            return None
        return Path(root) / self.network_name() if self.mode == "add" else Path(root)

    @on(Input.Changed)
    def _update_preview(self, event: "Input.Changed | None" = None) -> None:
        if self.mode != "add":
            return
        try:
            preview = self.query_one("#preview", Label)
        except Exception:
            return
        root, bundle, name = (self._value("inp-root"), self._value("inp-bundle-dir"),
                              self.network_name())
        parts = []
        if root:
            parts.append(f"publish → [b]{escape(root)}/{escape(name) or '…'}[/]")
        if bundle:
            parts.append(f"bundle → [b]{escape(bundle)}[/]")
        if parts:
            preview.update("   ".join(parts))
        elif name:
            preview.update("[dim]no destination — this network can be edited and synced, "
                           "not published[/]")
        else:
            preview.update("")

    def summary(self) -> str:
        root = self._root()
        bundle = self._value("inp-bundle-dir")
        lines = [f"Network  [$accent]{escape(self.network_name())}[/]"]
        if root is not None:
            lines.append(f"Publish  [$mmw-path]{escape(str(root))}[/]")
        if bundle:
            lines.append(f"Bundle   [$mmw-path]{escape(bundle)}[/]")
        if root is None and not bundle:
            lines.append("[dim]no destination — this network can be edited and synced, "
                         "not published[/]")
        for s in self.selects():
            dims = "  ".join(f"{d}: {', '.join(v) if v else 'all'}"
                             for d, v in (("suites", s.suites), ("components", s.components),
                                          ("arches", s.arches)))
            lines.append(f"[$primary]●[/] {escape(s.source)}  [dim]{escape(dims)}[/]")
        return "\n".join(lines)

    def result(self) -> "tuple[config.NetworkConfig | None, str | None, str | None]":
        """`(network, None, None)`, or `(None, problem, id of the field to focus)`."""
        selects = self.selects()
        certs, bundle_tls = self._flag("certs-switch"), self._flag("bundle-tls-switch")
        problem = networks.form_problems(name=self.network_name(), existing_names=self._existing,
                                         selects=selects, certs=certs, bundle_tls=bundle_tls)
        if problem:
            focus = ("repo-checklist" if not selects else
                     "inp-net-name" if self.name_problem() else "bundle-tls-switch")
            return None, problem, focus
        bundle = self._value("inp-bundle-dir")
        return config.NetworkConfig.model_validate({
            **(self._network.model_dump() if self._network else {}),
            "name": self.network_name(),
            "root": self._root(),
            "bundle_dir": Path(bundle) if bundle else None,
            "select": selects,
            "prune": self._flag("prune-switch"),
            "dist_upgrade": self._flag("du-switch"),
            "certs": certs,
            "nginx": self._flag("nginx-switch"),
            "bundle_compress": self._flag("compress-switch"),
            "bundle_tls": bundle_tls,
        }), None, None


class EditNetworkPage(Page):
    """Edit an existing network. Dismisses the edited NetworkConfig, or None."""

    def __init__(self, network: config.NetworkConfig, all_mirrors: list[config.MirrorConfig],
                 existing_names: "set[str] | None" = None):
        super().__init__()
        self.TITLE = network.name
        self._network = network
        self.form = NetworkForm(all_mirrors, mode="edit", network=network,
                                existing_names=existing_names)

    # Views kept for callers and tests that read the editor's state.
    _axes = property(lambda self: self.form._axes)
    _checked_sources = property(lambda self: self.form._checked_sources)

    def compose_body(self) -> ComposeResult:
        yield self.form

    def compose_actions(self) -> ComposeResult:
        yield Button("Cancel", id="btn-cancel", compact=True)
        yield Button("Save", id="btn-save", variant="primary", compact=True)

    def on_mount(self) -> None:
        self.query_one("#repo-checklist").focus()
        self.call_after_refresh(self.query_one("#page-body").scroll_home, animate=False)

    @on(Button.Pressed, "#btn-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-save")
    def _save(self) -> None:
        net, problem, focus = self.form.result()
        if net is None:
            self.set_status(problem, "error")
            self.notify(problem, severity="warning")
            self.query_one(f"#{focus}").focus()
            return
        self.dismiss(net)


class AddNetworkPage(Wizard):
    """Where → Repositories → What to publish → Options.

    Repository selection is its own step, ahead of the per-repository
    suite/component/arch pickers, and starts with nothing ticked. Dimensions
    default to "all" once a repository is picked, since narrowing what an
    already-wanted repository serves is comparatively rare."""

    TITLE = "Add network"
    STEPS = ("Where", "Repositories", "What to publish", "Options")
    FINISH_LABEL = "Add network ✓"
    _SECTIONS = ("nf-where", "nf-repos", "nf-dims", "nf-options")
    _FIRST = ("inp-net-name", "repo-checklist", None, "prune-switch")

    def __init__(self, mirrors: list[config.MirrorConfig], existing: "set[str] | None" = None):
        super().__init__()
        self.form = NetworkForm(mirrors, mode="add", existing_names=existing)

    _axes = property(lambda self: self.form._axes)
    _checked_sources = property(lambda self: self.form._checked_sources)

    def compose_body(self) -> ComposeResult:
        yield from super().compose_body()
        yield self.form

    async def enter(self, step: int) -> None:
        for i, section in enumerate(self._SECTIONS):
            self.query_one(f"#{section}").display = i == step
        if step == 3:
            self.query_one("#nf-summary", Static).update(self.form.summary())
        first = self._FIRST[step]
        if first is None:
            # The first dimension list of a ticked repository.
            lists = [w for w in self.query("#nf-dims SelectionList")
                     if all(a.display for a in w.ancestors_with_self)]
            if lists:
                lists[0].focus()
        else:
            self.query_one(f"#{first}").focus()

    async def leave(self, step: int) -> bool:
        if step == 0:
            problem = self.form.name_problem()
        elif step == 1:
            problem = (None if self.form.checked_axes() else
                       "Select at least one repository — nothing selected publishes nothing.")
        elif step == 2:
            problem = (None if self.form.selects() else
                       "Select at least one suite/component/arch — nothing selected "
                       "publishes nothing.")
        else:
            _, problem, _ = self.form.result()
        if problem:
            self.set_status(problem, "error")
            self.notify(problem, severity="warning")
            return False
        self.set_status("")
        return True

    def result(self) -> config.NetworkConfig:
        net, _, _ = self.form.result()
        return net
