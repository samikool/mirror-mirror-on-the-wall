"""One repository being authored — the shared model behind every add flow.

`RepositoryDraft` is deliberately not a widget. The single-repository step edits
one of these; a pasted bundle naming eight repositories holds eight. Probing,
the "empty means all" rule, and the conversion to `SourceConfig` therefore have
exactly one implementation regardless of how the repository arrived.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from mmw.core import discover
from mmw.core.apt_sources import AptEntry, expand_placeholder_entry
from mmw.core.config import SourceConfig, derive_name, normalize_url

# The three axes of an apt repository.
DIMENSIONS = ("suites", "components", "arches")


@dataclass
class RepositoryDraft:
    """A repository plus what the user has chosen to mirror from it."""

    url: str
    provider: str = "apt"
    name: str = ""
    gpg_key: str | None = None        # armoured key material
    gpg_key_name: str | None = None   # stem for the key files, from `Signed-By:`
    # Where `gpg_key` was fetched from, when it was: sync then also mirrors
    # it byte-for-byte at that path, so a vendor's `curl …/gpgkey` works.
    gpg_key_url: str | None = None
    include_sources: bool = False

    # Per dimension: what the repository offers, what is ticked, and whether the
    # offer came from the repository itself.
    available: dict[str, list[str]] = field(default_factory=lambda: {d: [] for d in DIMENSIONS})
    chosen: dict[str, list[str]] = field(default_factory=lambda: {d: [] for d in DIMENSIONS})
    enumerated: dict[str, bool] = field(default_factory=lambda: {d: False for d in DIMENSIONS})

    probed: bool = False
    error: str = ""
    # Where `gpg_key` came from, for the review pane: "inline" (the pasted
    # config carried it), "pasted", "fetched", or "dir" (a scanned directory).
    key_source: str = ""

    def __post_init__(self) -> None:
        self.url = self.url.rstrip("/")
        if not self.name:
            self.name = derive_name(self.url, suites=self.chosen.get("suites") or None,
                                    provider=self.provider)

    # ── identity ─────────────────────────────────────────────────────────────

    @property
    def identity(self) -> str:
        return normalize_url(self.url)

    # ── dimensions ───────────────────────────────────────────────────────────

    def set_dimension(self, dim: str, available: list[str],
                      chosen: "list[str] | None" = None, *, enumerated: bool = False) -> None:
        """Offer a dimension. `chosen=None` ticks everything.

        A config that already filtered (arm64 only, say) passes `chosen`, so the
        rest stay visible while only its own are ticked. `enumerated` records
        that the offer came from the repository — see `stored()`.
        """
        self.available[dim] = list(available)
        self.chosen[dim] = list(available if chosen is None else chosen)
        self.enumerated[dim] = enumerated

    def stored(self, dim: str) -> list[str]:
        """What goes in the config.

        Everything ticked is stored as *empty* — "all" — so the repository keeps
        following upstream as new suites or architectures appear; a frozen full
        list would silently stop picking them up.

        **Only a probed list may collapse.** When the repository could not be
        enumerated, `available` is merely what a pasted config named, and
        collapsing it would turn an explicit `Suites: noble` into every suite.
        """
        chosen, available = self.chosen[dim], self.available[dim]
        if self.enumerated[dim] and available and len(chosen) == len(available):
            return []
        return list(chosen)

    def summary(self, dim: str) -> str:
        """One-line description of a dimension, for a summary row.

        Derived from `stored()`, never from `chosen` directly, so the row
        always describes **what would actually be written to the config**.
        Reading `chosen` against `available` got this wrong in both
        directions, and both were misleading in the dangerous direction:

        - An *unprobed* draft has `available == chosen` (a pasted config or
          `from_source()` seeds both from the same stated list), so the old
          `len(chosen) == len(available)` test reported an explicit
          `Suites: bookworm trixie` as **"all (2)"** — implying the
          repository follows upstream when it is in fact pinned to two.
          `stored()` already gets this right by requiring `enumerated`;
          this now defers to it instead of re-deriving the rule and
          disagreeing with it.
        - A dimension that is genuinely open (`suites = []`, the documented
          "empty means all") reported **"none selected"**, which reads as
          "this mirrors nothing" — the exact opposite of the truth.
        """
        stored = self.stored(dim)
        available = self.available[dim]
        if not stored:
            if self.enumerated[dim] and available and not self.chosen[dim]:
                # Everything was actively unticked from a real listing.
                # `stored()` is `[]`, which the config reads as "all", so say
                # that plainly rather than let the row imply nothing is
                # mirrored — the two states look identical in the data and
                # only the user's intent distinguishes them.
                return "none ticked — stored as all"
            return "all — resolved at sync"
        # "of N" only means something when N came from listing the repository.
        # For an unprobed draft `available` is just a copy of the stated list,
        # so quoting it would be circular.
        suffix = f"  of {len(available)}" if self.enumerated[dim] else ""
        if len(stored) <= 4:
            return ", ".join(stored) + suffix
        return f"{', '.join(stored[:4])}  +{len(stored) - 4}{suffix}"

    # ── probing ──────────────────────────────────────────────────────────────

    def probe(self) -> None:
        """Fill in what the repository publishes. Blocking — run off the UI thread.

        Anything the draft already carries stays selected; probing only widens
        what is *offered*. A flat repository has no dists/ tree to enumerate.
        """
        self.probed = True
        self.error = ""
        if self.provider == "apt-flat":
            return

        stated = list(self.chosen["suites"])
        suites = discover.list_suites(self.url)
        if suites:
            self.set_dimension("suites", _merge(suites, stated),
                               chosen=stated or suites, enumerated=True)
        elif not stated:
            self.error = "no suites found"
            return

        suite = (self.chosen["suites"] or [None])[0]
        if suite is not None:
            self.probe_suite(suite)

    def probe_suite(self, suite: str) -> None:
        """Components and architectures follow from a chosen suite. Blocking.

        Called on its own when the user picks a different suite, so the two
        entry points cannot drift apart.
        """
        stated_c = list(self.chosen["components"])
        stated_a = list(self.chosen["arches"])

        comps = discover.list_components(self.url, suite)
        arches: list[str] = []
        for comp in comps or stated_c:
            arches = discover.list_arches(self.url, suite, comp)
            if arches:
                break
        if not comps or not arches:
            # No directory listing at all (S3/CDN-backed vendor repositories
            # answer every dists/ request with 404) — the Release file is then
            # the only evidence, and for those repositories it is accurate.
            rel_comps, rel_arches = discover.release_dimensions(self.url, suite)
            comps = comps or rel_comps
            arches = arches or rel_arches

        if comps:
            self.set_dimension("components", _merge(comps, stated_c),
                               chosen=stated_c or comps, enumerated=True)
        if arches:
            self.set_dimension("arches", _merge(arches, stated_a),
                               chosen=stated_a or arches, enumerated=True)

    # ── GPG key ──────────────────────────────────────────────────────────────

    def key_state(self) -> str:
        """`have` | `needs` | `none` — what the review pane renders.

        `needs` means the config named a keyring (`Signed-By: /usr/share/…`)
        whose contents live on the source machine, not in the file we were
        given. That is exactly what the key-directory scan resolves.
        """
        if self.gpg_key:
            return "have"
        if self.gpg_key_name:
            return "needs"
        return "none"

    def set_key(self, armour: str | None, *, name: str | None = None, source: str = "",
                url: str | None = None) -> None:
        """A new key replaces the old one — and its URL: a key that was not
        fetched from anywhere has none to mirror."""
        self.gpg_key = armour or None
        self.gpg_key_url = url if armour else None
        self.key_source = source if armour else ""
        if name:
            self.gpg_key_name = name

    # ── result ───────────────────────────────────────────────────────────────

    def to_source(self) -> SourceConfig:
        return SourceConfig(
            name=self.name or derive_name(self.url, suites=self.stored("suites") or None,
                                          provider=self.provider),
            provider=self.provider,
            url=self.url,
            suites=self.stored("suites"),
            components=self.stored("components"),
            arches=self.stored("arches"),
            include_sources=self.include_sources,
            gpg_key=self.gpg_key,
            gpg_key_name=self.gpg_key_name,
            gpg_key_url=self.gpg_key_url,
        )

    @classmethod
    def from_source(cls, source: SourceConfig) -> "RepositoryDraft":
        """The mirror image of `to_source()` — an existing config entry, opened
        for editing.

        Every dimension is carried across **unenumerated**: `source.suites` /
        `components` / `arches` are whatever is on disk, empty or not, and
        `set_dimension` is deliberately not used here because it always stamps
        `enumerated=False` anyway — the point worth stating is that nothing has
        been listed yet, so `stored()` (see its docstring) hands back exactly
        what went in, empty lists included. That is what makes a round trip
        through `to_source()` with no probing in between reproduce the source
        exactly: an explicit list survives as itself, and an empty ("all") list
        survives as empty rather than being materialized into whatever the
        repository happens to serve today. Only a later `probe()` may widen
        `available` and flip a dimension to `enumerated=True` — the existing
        "ticking everything collapses back to all" behaviour then applies,
        unchanged and intentionally so.

        `dist_upgrade` has no `RepositoryDraft` field and is dropped here; a
        consumer that needs it must carry it separately.
        """
        draft = cls(
            url=source.url,
            provider=source.provider,
            name=source.name,
            gpg_key=source.gpg_key,
            gpg_key_name=source.gpg_key_name,
            gpg_key_url=source.gpg_key_url,
            include_sources=source.include_sources,
        )
        if source.gpg_key:
            draft.key_source = "inline"
        for dim in DIMENSIONS:
            stored = list(getattr(source, dim))
            draft.available[dim] = list(stored)
            draft.chosen[dim] = list(stored)
            draft.enumerated[dim] = False
        return draft


def probe_all(drafts: list[RepositoryDraft], max_workers: int = 8) -> None:
    """Probe a whole bundle at once.

    A cat'd DGX sources tree names a dozen repositories, each costing several
    HTTP round trips; sequentially that is a visible stall before the review
    pane can open. Each draft only ever touches its own fields, so there is
    nothing to lock.
    """
    if not drafts:
        return
    with ThreadPoolExecutor(max_workers=min(max_workers, len(drafts))) as pool:
        for _ in pool.map(_probe_one, drafts):
            pass


def _probe_one(draft: RepositoryDraft) -> None:
    try:
        draft.probe()
    except Exception as e:                      # one bad repo must not stop the rest
        draft.probed = True
        draft.error = str(e) or type(e).__name__


def _merge(found: list[str], stated: list[str]) -> list[str]:
    """Everything the repository offers, plus anything the config named that the
    listing missed — a stated value must never vanish from the picker."""
    return found + [v for v in stated if v not in found]


def resolve_placeholders(entries: list[AptEntry]) -> list[AptEntry]:
    """Fan out any `$(ARCH)`-templated entry into one concrete entry per
    architecture it actually serves. Blocking — run off the UI thread, same as
    `probe()`.

    This is deliberately separate from `drafts_from_entries` (which stays pure
    and synchronous): a paste is parsed and shown on the UI thread, so nothing
    that reaches into `discover` may live there. Call this first, off-thread,
    and feed its result into `drafts_from_entries` — which then needs no
    placeholder-specific case of its own, since every entry it sees already
    names a real URL.

    An entry that resolves to no architecture is dropped rather than surfaced
    as a broken draft; the caller sees one fewer repository, not one that
    can't be probed.
    """
    resolved: list[AptEntry] = []
    for e in entries:
        if not e.arch_placeholder:
            resolved.append(e)
            continue
        found = discover.expand_arch_placeholder(e.url, e.arch_placeholder)
        resolved.extend(expand_placeholder_entry(e, found))
    return resolved


def drafts_from_entries(entries: list[AptEntry]) -> list[RepositoryDraft]:
    """Group parsed apt entries into one draft per repository.

    A cat'd bundle of `/etc/apt/sources.list.d/*` becomes one draft per distinct
    URL, carrying whatever that config already stated as the pre-selection.
    """
    grouped: dict[str, list[AptEntry]] = {}
    for e in entries:
        grouped.setdefault(e.url.rstrip("/"), []).append(e)

    taken: set[str] = set()
    drafts = []
    for url, group in grouped.items():
        flat = group[0].suite is None
        stated = {
            "suites": list(dict.fromkeys(e.suite for e in group if e.suite)),
            "components": list(dict.fromkeys(c for e in group for c in e.components)),
            "arches": list(dict.fromkeys(a for e in group for a in e.arches)),
        }
        name = derive_name(url, taken, suites=stated["suites"] or None,
                          provider="apt-flat" if flat else "apt")
        taken.add(name)
        draft = RepositoryDraft(
            url=url,
            provider="apt-flat" if flat else "apt",
            name=name,
            gpg_key=next((e.gpg_key for e in group if e.gpg_key), None),
            gpg_key_name=next((e.gpg_key_name for e in group if e.gpg_key_name), None),
            include_sources=any(e.include_sources for e in group),
        )
        for dim in DIMENSIONS:
            if stated[dim]:
                # Not enumerated: this is what the config named, not what the
                # repository offers. Probing may widen it later.
                draft.set_dimension(dim, stated[dim], chosen=stated[dim])
        drafts.append(draft)
    return drafts
