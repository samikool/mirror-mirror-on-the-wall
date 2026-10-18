"""The Repositories section's logic — no widgets, so it is testable on its own
and driven by RepositoriesPage.

Selection is by repository identity first, mirror names second: with
`arches = []` a repository can expand to no rows yet and still be syncable.
"""
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from mmw.core import config, pool as pool_mod
from mmw.core.resolve import available_mirrors, pooled_mirrors, resolve_sources
from mmw.providers import get_provider


class Row(NamedTuple):
    kind: str                                    # host | source | mirror | empty
    key: str                                     # unique and stable: a widget id
    source: config.SourceConfig | None
    mirror: config.MirrorConfig | None
    host: str


class RepositoryList:
    def __init__(self, cfg: config.Config):
        self.cfg = cfg
        # Two levels, because expanded mirror names are a *guess* until the
        # repository has been listed: `arches = []` means "all", so a source
        # can legitimately expand to zero rows and still be syncable. The
        # repository identity is the thing that always exists.
        self.selected_sources: set[str] = set()   # repository identities
        self.selection: set[str] = set()          # mirror names
        self.expanded: set[str] = set()           # source names
        # Type-to-find, matched against name and URL.
        self.filter = ""
        self.by_source: dict[str, list[config.MirrorConfig]] = {}
        self.available: list[config.MirrorConfig] = []

    def refresh(self) -> None:
        """Re-read what the pool actually holds. **Blocking** — a pool query
        (3290 ms against the live pool on its first call): call it off the
        event loop.

        Architectures are discovered, so config expansion is narrower than
        reality — a repository mirroring "all arches" would otherwise show one
        mirror here while the pool holds five.
        """
        # `by_source` holds **pool truth only**. Folding the config-expansion
        # fallback in here made `unresolved` believe a repository had been
        # listed, so an `arches = []` source rendered fabricated rows as if
        # they were real.
        pooled = pooled_mirrors(self.cfg, self.cfg.settings.mirror_dir)
        self.by_source = {}
        for m in pooled:
            self.by_source.setdefault(m.source or "", []).append(m)
        self.available = available_mirrors(self.cfg, self.cfg.settings.mirror_dir, pooled=pooled)
        # Drop selections for anything no longer configured, so a removed
        # source cannot keep voting on what gets synced.
        configured = {s.identity for s in self.cfg.sources}
        self.selected_sources &= configured
        self.selection &= {m.name for m in self.available}

    def unresolved(self, src: config.SourceConfig) -> bool:
        """True when this source's rows cannot be known without listing upstream:
        nothing in the pool yet, and an open dimension its provider cannot
        expand offline. Rather than render a guess, say so, and let sync
        resolve it."""
        if self.by_source.get(src.identity):
            return False                      # the pool knows; that is the truth
        return not get_provider(src.provider).expansion_known(src)

    def mirrors_of(self, src: config.SourceConfig) -> list[config.MirrorConfig]:
        """This source's units: from the pool, else its offline expansion
        (nothing synced yet, so an optimistic answer beats an empty one)."""
        if self.unresolved(src):
            return []
        return self.by_source.get(src.identity) or get_provider(src.provider).expand(src)

    def is_selected(self, src: config.SourceConfig) -> bool:
        return src.identity in self.selected_sources

    @staticmethod
    def host_of(src: config.SourceConfig) -> str:
        return get_provider(src.provider).group(src)

    def sorted_sources(self) -> list[config.SourceConfig]:
        """Grouped by host, then by name — one host commonly serves several
        repositories. Narrowed by the filter when one is active."""
        sources = self.cfg.sources
        if self.filter:
            needle = self.filter.lower()
            sources = [s for s in sources
                       if needle in get_provider(s.provider).search_text(s).lower()]
        return sorted(sources, key=lambda s: (self.host_of(s), s.name.lower()))

    def source_for_mirror(self, mirror: config.MirrorConfig) -> config.SourceConfig | None:
        for src in self.cfg.sources:
            if any(m.name == mirror.name for m in self.mirrors_of(src)):
                return src
        return None

    def rows(self) -> list[Row]:
        out: list[Row] = []
        last_host = None
        for src in self.sorted_sources():
            host = self.host_of(src)
            if host != last_host:
                out.append(Row("host", f"host-{host}", None, None, host))
                last_host = host
            out.append(Row("source", f"source-{src.name}", src, None, host))
            if src.name in self.expanded:
                mirrors = self.mirrors_of(src)
                if not mirrors:
                    out.append(Row("empty", f"empty-{src.name}", src, None, host))
                for m in mirrors:
                    out.append(Row("mirror", f"mirror-{m.name}", src, m, host))
        return out

    def toggle_expanded(self, src: config.SourceConfig) -> None:
        self.expanded ^= {src.name}

    def toggle_source(self, src: config.SourceConfig) -> None:
        """Ticking a *source* records its identity, not just the names of the
        rows under it: with `arches = []` there may be no rows yet, and an empty
        name set is a subset of everything — which is what once silently made
        every source untickable."""
        names = {m.name for m in self.mirrors_of(src)}
        if self.is_selected(src):
            self.selected_sources.discard(src.identity)
            self.selection -= names
        else:
            self.selected_sources.add(src.identity)
            self.selection |= names

    def toggle_mirror(self, mirror: config.MirrorConfig) -> None:
        self.selection ^= {mirror.name}
        src = self.source_for_mirror(mirror)
        if src is not None:
            self.sync_source_flag(src)

    def sync_source_flag(self, src: config.SourceConfig) -> None:
        """A source counts as selected once every row under it is."""
        mirrors = self.mirrors_of(src)
        if mirrors and all(m.name in self.selection for m in mirrors):
            self.selected_sources.add(src.identity)
        else:
            self.selected_sources.discard(src.identity)

    def toggle_all(self) -> None:
        everything = {s.identity for s in self.cfg.sources}
        if self.selected_sources == everything:
            self.selected_sources.clear()
            self.selection.clear()
        else:
            self.selected_sources = set(everything)
            self.selection = {m.name for m in self.available}

    def removal_targets(self, highlighted: config.SourceConfig | None) -> list[config.SourceConfig]:
        """The ticked repositories; else the highlighted one; else none."""
        ticked = [s for s in self.cfg.sources if self.is_selected(s)]
        if ticked:
            return ticked
        return [highlighted] if highlighted is not None else []

    def forget(self, targets: list[config.SourceConfig]) -> None:
        # Subtract, never clear. Clearing meant removing one of five ticked
        # repositories dropped the other four silently, so the next sync took
        # *everything* — an empty selection means "all".
        self.selection -= {m.name for s in targets for m in self.mirrors_of(s)}
        self.selected_sources -= {s.identity for s in targets}

    def _covered(self) -> set[str]:
        return {m.name for s in self.cfg.sources if self.is_selected(s)
                for m in self.mirrors_of(s)}

    def selection_summary(self) -> str:
        """Count repositories, not expanded rows: "3 sources" beside three
        ticked repositories is legible; "47 selected" is not."""
        n = len(self.selected_sources)
        extra = len(self.selection - self._covered())
        parts = []
        if n:
            parts.append(f"{n} source{'s' if n != 1 else ''}")
        if extra > 0:
            parts.append(f"{extra} mirror{'s' if extra != 1 else ''}")
        return ", ".join(parts)

    def sync_request(self) -> tuple[list[str], list[str]]:
        """(identities, extra mirror names) to hand sync. A selection, not
        MirrorConfigs: "all arches" is resolved by listing upstream at launch,
        so the concrete mirror set is not known here. An empty selection means
        everything."""
        return sorted(self.selected_sources), sorted(self.selection - self._covered())


def without_sources(cfg: config.Config, targets: list[config.SourceConfig]) -> config.Config:
    gone = {s.identity for s in targets}
    return cfg.model_copy(update={"sources": [s for s in cfg.sources if s.identity not in gone]})


def replace_source(cfg: config.Config, original_identity: str,
                   edited: config.SourceConfig) -> config.Config:
    # Replace by identity, never merge: `config.merge_sources()` is union-only
    # by design, so it can widen a selection but never narrow one — which is
    # the entire reason editing exists.
    #
    # A changed URL is carried into every network that selected the old one.
    # Left alone, those `[[networks.select]]` entries matched nothing: the
    # network silently stopped publishing the repository, and its next
    # pruning publish deleted the repository's tree from the root.
    networks = cfg.networks
    if edited.identity != original_identity:
        networks = [n.model_copy(update={"select": [
            sel.model_copy(update={"source": edited.url})
            if config.normalize_url(sel.source) == original_identity else sel
            for sel in n.select]}) for n in cfg.networks]
    return cfg.model_copy(update={
        "sources": [edited if s.identity == original_identity else s for s in cfg.sources],
        "networks": networks})


def pool_scale(mirror_dir: Path) -> tuple[int, int] | None:
    """(packages, bytes) in the pool, or None when it cannot be read.
    **Blocking** — opens SQLite on the pool's disk."""
    if not (mirror_dir / ".pool").exists():
        return None
    try:
        pool = pool_mod.Pool(mirror_dir, read_only=True)
    except Exception:
        return None
    try:
        st = pool.stats()
        return int(st.get("packages", 0)), int(st.get("total_size", 0))
    except Exception:
        return None
    finally:
        try:
            pool.close()
        except Exception:
            pass


def pool_busy(mirror_dir: Path) -> bool:
    """True when a sync holds the pool. **Blocking** (takes and drops a lock)."""
    if not mirror_dir.exists():
        return False
    return pool_mod.pool_is_busy(mirror_dir)


def prune_pool(cfg: "config.Config") -> tuple[int, int]:
    """(removed, freed bytes). **Blocking.** Raises `pool.PoolBusy` while a
    sync runs: everything it has downloaded so far is unreferenced until its
    reconcile lands, so pruning now would delete the sync's own work — and
    `ValueError` with no repositories configured, which would delete it all.
    Constructing the Pool is itself disk work (mkdirs, SQLite, a tmp sweep)."""
    from mmw.core.resolve import uncovered_mirror_names
    pool = pool_mod.Pool(cfg.settings.mirror_dir)
    try:
        return pool.prune_orphans([s.identity for s in cfg.sources],
                                  uncovered_mirror_names(cfg, pool))
    finally:
        pool.close()


# ── Editing one repository ──────────────────────────────────────────────────

def format_overrides(overrides: "list[config.SuiteOverride]") -> str:
    """`[[sources.suite_overrides]]` as the editor shows it: one
    `suite: comp, comp` line per override."""
    return "".join(f"{o.suite}: {', '.join(o.components)}\n" for o in overrides)


def parse_overrides(text: str) -> "list[config.SuiteOverride]":
    """The inverse of `format_overrides`. Raises ValueError naming the line."""
    out: list[config.SuiteOverride] = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        suite, sep, comps = line.partition(":")
        components = [c.strip() for c in comps.replace(" ", ",").split(",") if c.strip()]
        if not sep or not suite.strip() or not components:
            raise ValueError(f"suite override line {n} should read `suite: comp, comp`")
        out.append(config.SuiteOverride(suite=suite.strip(), components=components))
    return out


def edited_source(draft, *, original: config.SourceConfig, url: str, name: str, label: str,
                  key_url: str, include_sources: bool, dist_upgrade: bool,
                  extra_files: list[str], overrides_text: str, other_names: set[str],
                  other_urls: set[str], suites_started_empty: bool,
                  ) -> "tuple[config.SourceConfig | None, str | None, str | None]":
    """The edit-repository form's checks, in order, as data.

    Returns `(source, None, None)`, or `(None, message, id of the field to
    focus)`. `draft` is the page's `SourceDraft` (dimensions and key)."""
    from mmw.core.upstream import upstream_rel

    # An empty suites list has two meanings and only one is a mistake. A
    # repository that arrived with `suites = []` is the documented "empty
    # means all" state (resolve_source() lists dists/ at sync time; `mmw add`
    # writes exactly this when suites are left open), so refusing it would
    # make such a repository impossible to open and save. Clearing one that
    # *did* state its suites down to zero is the real mistake.
    if draft.provider == "apt" and not draft.chosen["suites"] and not suites_started_empty:
        return None, ("Choose at least one suite, or cancel to keep this repository "
                      "mirroring the suites it already has."), "btn-suites"
    url = url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        return None, "✗ the repository URL must start with http:// or https://", "edit-source-url"
    # Another repository's URL would write two entries for one repository,
    # which the loader refuses.
    if config.normalize_url(url) in other_urls:
        return None, "✗ another repository already has that URL — edit that one instead.", \
            "edit-source-url"
    key_url = key_url.strip()
    if key_url and not key_url.startswith(("http://", "https://")):
        return None, "✗ the key URL must start with http:// or https://", "edit-source-key-url"
    bad = [u for u in extra_files if upstream_rel(u) is None]
    if bad:
        return None, (f"✗ {bad[0]} cannot be served at its own path — it needs to be an "
                      "http(s) URL naming a file, with no query string"), "edit-source-extra-files"
    try:
        overrides = parse_overrides(overrides_text)
    except ValueError as e:
        return None, f"✗ {e}", "edit-source-overrides"
    # The name is the one field that can produce a config the loader rejects
    # (`Config._names_must_be_unique` only runs on construction, and nothing
    # re-validates on assignment), so it is checked here rather than
    # discovered on the next `mmw` invocation, locking the user out.
    name = name.strip()
    if name in other_names:
        return None, (f"✗ '{name}' is already the name of another repository — "
                      "pick a different one."), "edit-source-name"
    draft.url = url
    draft.name = name or draft.name
    draft.include_sources = include_sources
    source = draft.to_source()
    # `SourceDraft.to_source()` has no field for these, so they are
    # stamped back on. Dropping them would silently switch off dist-upgrade
    # support or a key-URL fetch on every edit.
    source.dist_upgrade = dist_upgrade
    source.gpg_key_url = key_url or None
    source.suite_overrides = overrides
    source.extra_files = extra_files
    # `label` is the key grouping directory (`keys/{label}/`): letting it be
    # re-derived on a no-op save would relocate every key on the next sync.
    # Blank means "derive it from the URL" — re-validated so derivation runs.
    source.label = label.strip()
    try:
        source = config.SourceConfig.model_validate(source.model_dump())
    except Exception as e:  # noqa: BLE001 — shown to the user
        return None, f"✗ {e}", None
    return source, None, None


def merge_new_sources(cfg: config.Config, new_sources: list[config.SourceConfig],
                      ) -> tuple[config.Config, int, int]:
    """`(config, added, updated)`. Merge rather than name-match: a new arch
    for an already-configured repository extends it instead of being dropped.
    Nothing new returns `cfg` itself."""
    merged, added, updated = config.merge_sources(cfg.sources, new_sources)
    if not (added or updated):
        return cfg, 0, 0
    return cfg.model_copy(update={"sources": merged}), added, updated


def added_summary(added: int, updated: int) -> str:
    parts = [f"added {added}"] if added else []
    if updated:
        parts.append(f"updated {updated}")
    return ", ".join(parts)


# ── What a sync selection means ─────────────────────────────────────────────

def split_selection(selection) -> tuple[set[str], set[str]]:
    """(repository identities, mirror names) from a list's selection; a plain
    list of names (older callers) is names only."""
    if isinstance(selection, tuple):
        sources, names = selection
        return set(sources or ()), set(names or ())
    return set(), set(selection or ())


def narrow_to_selection(mirrors: list[config.MirrorConfig], errors: list[str], selection,
                        ) -> tuple[list[config.MirrorConfig], str | None, bool]:
    """Resolved mirrors narrowed to a selection: `(mirrors, problem, is_error)`.

    **Identities are what survive resolution.** A mirror name chosen before the
    first sync names the config expansion, which resolution may widen right
    past (`arches = []` expands to one invented `amd64` offline and to five
    real arches upstream). Selecting the repository keeps meaning it — and a
    selection that resolves to nothing is an error, never "everything":
    syncing terabytes nobody asked for is far worse than syncing nothing."""
    sources, names = split_selection(selection)
    if sources or names:
        chosen = [m for m in mirrors if m.source in sources or m.name in names]
        if not chosen:
            return [], "Nothing selected could be resolved — is it still configured?", True
        mirrors = chosen
    if not mirrors:
        return [], ("No mirrors could be resolved." if errors else "Nothing to sync."), bool(errors)
    return mirrors, None, False


def resolve_retry_selection(cfg: config.Config, selection, *, progress=None, stop_event=None,
                            config_dir=None):
    """Resolve a recovery selection from the pool, with no upstream calls."""
    from mmw.core.resolve import pooled_mirrors
    mirrors = [m for m in pooled_mirrors(cfg, cfg.settings.mirror_dir) if get_provider(m.provider).retries_failures]
    chosen, problem, _ = narrow_to_selection(mirrors, [], selection)
    return chosen, ([problem + " Retry supports previously synced VS Code mirrors."]
                    if problem else [])


def resolve_selection(cfg: config.Config, selection, *, progress=None, stop_event=None,
                      config_dir=None) -> tuple[list[config.MirrorConfig], list[str]]:
    """Expand "all" dimensions by listing upstream, then narrow to `selection`.
    Quiet — the notes come back as messages. **Blocking** (network).

    Only the sources the selection touches are resolved: ticking one apt
    repository must not ask the Marketplace for its top 200 first. A mirror
    name belongs to the source whose name prefixes it (over-matching a
    prefix-sharing neighbour only resolves one source too many)."""
    ids, names = split_selection(selection)
    sources = cfg.sources
    if ids or names:
        sources = [s for s in cfg.sources if s.identity in ids
                   or any(n == s.name or n.startswith(s.name + "-") for n in names)]
    from mmw.providers.base import ResolveContext
    mirrors, notes, errors = resolve_sources(
        sources, progress=progress,
        context=ResolveContext.from_settings(cfg.settings, stop_event, config_dir=config_dir))
    messages = [*notes, *(f"⚠ {e}" for e in errors)]
    chosen, problem, _ = narrow_to_selection(mirrors, errors, selection)
    if problem:
        messages.append(problem)
    return chosen, messages
