import os
import re
import tomllib
import tomli_w
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

CONFIG_FILENAME = "mmw.toml"

class _StrictModel(BaseModel):
    """Unknown keys in mmw.toml are config mistakes — fail loudly, don't ignore."""
    model_config = ConfigDict(extra="forbid")


# Debian/Ubuntu architecture names — the vocabulary of `binary-{arch}` index
# directories.  Used to recognise arch dirs when listing a repository.
# Vendor spellings for architectures that are not Debian arch names. `sbsa` is
# NVIDIA's "Server Base System Architecture" — 64-bit ARM; verified against the
# live mirror, where every package under .../ubuntu2404/sbsa is `_arm64.deb`.
_ARCH_ALIASES = {"sbsa": "arm64", "aarch64": "arm64", "x86_64": "amd64", "amd_64": "amd64"}

# A network name becomes a directory name (`certs/{name}/`, the bundle
# ledger) and a Textual widget id, so it is a path segment, not free text.
NETWORK_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")

# Every architecture Debian and Ubuntu have shipped. One list: `apt.py`
# excludes other arches' index files by it, and two copies had already
# drifted apart (each lacked arches the other had, both lacked loong64).
KNOWN_ARCHES = frozenset({
    "amd64", "arm64", "armhf", "armel", "i386", "ppc64el", "s390x", "riscv64",
    "loong64", "mipsel", "mips64el", "mips", "powerpc", "ppc64", "sparc", "sparc64",
    "ia64", "hppa", "alpha", "m68k", "sh4", "x32", "hurd-i386", "hurd-amd64",
    "kfreebsd-i386", "kfreebsd-amd64",
})


# A port that equals its scheme's default is not a distinction — `:80` is what
# `http://` already means. Two entries spelling one repository differently must
# not become two identities, two sync units and two published trees.
_DEFAULT_PORTS = {"http": 80, "https": 443, "ftp": 21}


def normalize_url(url: str) -> str:
    """Canonical form of a repository URI — the identity of a source.

    Trailing slashes, case in the host and a default port are not
    distinctions, so `HTTP://Archive.Ubuntu.com:80/ubuntu/` and
    `http://archive.ubuntu.com/ubuntu` are the same repository. The *path* is
    left exactly as written — a webserver may well serve `/Ubuntu` and
    `/ubuntu` as different trees.
    """
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    scheme = (parsed.scheme or "http").lower()
    port = parsed.port
    if port and port != _DEFAULT_PORTS.get(scheme):
        host = f"{host}:{port}"
    path = parsed.path.rstrip("/")
    return f"{scheme}://{host}{path}"


def derive_label(url: str) -> str:
    """Grouping label for GPG keys — the host's domain name.

    A naming convenience only, never identity: `archive.ubuntu.com` and
    `us.archive.ubuntu.com` both give "ubuntu", so a country mirror's keys land
    beside the primary's.
    """
    labels = [p for p in (urlparse(url).hostname or "").lower().split(".") if p]
    if not labels:
        return "custom"
    return labels[-2] if len(labels) >= 2 else labels[0]


# Path segments that name a distribution or packaging format rather than the
# repository itself. A URL commonly ends in one, so the *last* segment is often
# the least informative: Launchpad PPAs end in `/ubuntu`, Cloudsmith in
# `/deb/ubuntu`. Skipping these finds the segment that actually distinguishes.
_GENERIC_SEGMENTS = frozenset({
    "ubuntu", "debian", "raspbian", "deb", "apt", "linux", "dists", "pool",
    "stable", "release", "releases", "repo", "repos", "packages", "pub",
})


def arch_from_url(url: str) -> str:
    """The architecture a flat repository's URL names, or "" when it names none.

    Flat repos put the arch in the path (`/hpc-sdk/ubuntu/arm64`). Searched from
    the right, because the arch is conventionally the last meaningful segment.
    """
    segments = [s.lower() for s in urlparse(url).path.split("/") if s]
    for seg in reversed(segments):
        if seg in KNOWN_ARCHES:
            return seg
        if seg in _ARCH_ALIASES:
            return _ARCH_ALIASES[seg]
    return ""


def derive_name(url: str, taken: set[str] | None = None,
                suites: list[str] | None = None, provider: str = "apt") -> str:
    """Readable, unique name derived from the repository URI.

    `{domain}-{host prefix}` reads the way people already refer to these:
    archive.ubuntu.com/ubuntu → `ubuntu-archive`, ports.ubuntu.com → `ubuntu-ports`.
    One host serving several repositories (packages.microsoft.com/repos/code vs
    /repos/edge) collides, so path segments are appended until it is unique.
    Cosmetic and user-editable — identity is the URI.

    `provider="apt-flat"` additionally excludes vendor arch spellings
    (`x86_64`, `sbsa`, …) from the segments considered informative, and
    appends the normalized arch to the name — see the block below.
    """
    parsed = urlparse(url)
    labels = [p for p in (parsed.hostname or "").lower().split(".") if p]
    domain = derive_label(url)
    prefix = labels[0] if labels else "custom"
    base = domain if prefix == domain else f"{domain}-{prefix}"
    base_parts = set(base.split("-"))

    # Append the deepest path segment that says something the host does not.
    # One host commonly serves several repositories — packages.microsoft.com
    # /repos/code and /repos/edge must not both be "microsoft-packages" — while
    # archive.ubuntu.com/ubuntu stays "ubuntu-archive" because "ubuntu" is
    # already in the name.
    #
    # Generic tails are skipped so the *distinguishing* segment wins:
    # `…/canonical-nvidia/nvidia-desktop-edge/ubuntu` is about
    # nvidia-desktop-edge, not about ubuntu. When every segment is generic
    # (archive.ubuntu.com/ubuntu) nothing is appended and the host-derived base
    # stands on its own.
    # A repository that segregates by suite carries the suite in its path
    # (`…/baseos/ubuntu/noble/arm64`) — that says which release, not which
    # repository, so it is skipped when the suites are known.
    suite_words = {s.lower() for s in (suites or [])}
    # A flat repository is never arch-expanded (see config.py's own
    # `effective_arches` docstring) — its arch is a single value baked into
    # the URL and appears nowhere else in the mirror name, unlike a regular
    # apt source whose `to_mirrors()` already appends `-{arch}` per sync
    # unit. So for apt-flat: vendor arch spellings (`x86_64`, `sbsa`, …) are
    # excluded from "what's the informative segment" the same way
    # KNOWN_ARCHES's Debian-native spellings already are — reusing
    # `arch_from_url()`'s own `_ARCH_ALIASES` table rather than a second
    # copy of it — and the normalized arch is appended explicitly below, so
    # `…/ubuntu2204/x86_64` and `…/ubuntu2404/arm64` read as
    # `…-ubuntu2204-amd64` / `…-ubuntu2404-arm64` instead of one of them
    # losing its OS-version segment to disambiguation-by-accident.
    is_flat = provider == "apt-flat"
    flat_arch = arch_from_url(url) if is_flat else ""
    vendor_arch_words = set(_ARCH_ALIASES) if is_flat else set()
    segments = [
        s for s in parsed.path.strip("/").split("/")
        if s
        and s not in KNOWN_ARCHES
        and s.lower() not in vendor_arch_words
        and s.lower() not in _GENERIC_SEGMENTS
        and s.lower() not in suite_words
        and not set(s.split("-")) <= base_parts
    ]
    if segments:
        # Drop parts the name already carries: deb.debian.org/debian-security
        # reads better as "debian-deb-security" than "debian-deb-debian-security".
        tail = "-".join(p for p in segments[-1].split("-") if p not in base_parts)
        base = f"{base}-{tail}" if tail else base
    if flat_arch:
        base = f"{base}-{flat_arch}"

    taken = taken if taken is not None else set()
    name = base
    extras = segments[:-1]
    while name in taken and extras:
        name = f"{name}-{extras.pop()}"
    n = 2
    while name in taken:
        name = f"{base}-{n}"
        n += 1
    return name


Provider = Literal["apt", "apt-flat"]
"""The providers that exist. A free string let `provider = "apt_flat"` —
an underscore where the code wants a hyphen — load clean, expand to zero
mirrors, and exit 0 having mirrored nothing."""


class MirrorConfig(_StrictModel):
    name: str
    provider: Provider
    source_url: str     # this mirror's own URL (repository root for apt)
    source: str = ""    # normalized repository URI this mirror belongs to
    label: str = ""     # keys/{label}/ grouping only — never identity
    arch: str = "amd64"
    suite: str | None = None
    component: str | None = None
    include_sources: bool = False  # mirror deb-src in addition to deb binaries
    gpg_key_url: str | None = None  # optional: fetched to keys/{name}/ during sync
    gpg_key: str | None = None      # armoured key, written to keys/ without a fetch
    gpg_key_name: str | None = None # filename stem for the emitted key files
    extra_files: list[str] = []     # upstream URLs mirrored at their own path
    dist_upgrade: bool = False      # also mirror changelogs.ubuntu.com meta-release

    @property
    def mirror_base(self) -> str:
        parsed = urlparse(self.source_url)
        host = parsed.hostname or "unknown"
        if parsed.port:
            # Distinguish repos on the same host at different ports.
            host = f"{host}_{parsed.port}"
        path = parsed.path.strip("/")
        return f"{host}/{path}" if path else host


class SuiteOverride(_StrictModel):
    """Per-suite component override — replaces the source-level components list for one suite."""
    suite: str
    components: list[str]


class SourceConfig(_StrictModel):
    """One upstream repository — identified by its URI — and what to pull from it.

    `url` is the identity. `name` and `label` are derived from it when absent:
    the file you hand-write (or paste) can be just a URL plus what you want.
    """
    url: str                          # repository URI (host + path) — the identity
    provider: Provider = "apt"
    name: str = ""                    # derived from url when empty
    label: str = ""                   # derived from url when empty; keys/{label}/
    arches: list[str] = []            # one sync unit per arch
    suites: list[str] = []
    components: list[str] = []
    suite_overrides: list[SuiteOverride] = []
    include_sources: bool = False
    gpg_key_url: str | None = None
    gpg_key: str | None = None        # armoured key material
    gpg_key_name: str | None = None   # filename stem for the emitted key files
    extra_files: list[str] = []       # upstream URLs mirrored at their own path
    dist_upgrade: bool = False        # mirror changelogs.ubuntu.com meta-release too

    @model_validator(mode="after")
    def _upstream_urls_have_a_path(self):
        """A key URL and every extra file are published at their own path,
        so each needs one: refused at load, not discovered at sync."""
        from mmw.core.upstream import upstream_rel
        for url in ([self.gpg_key_url] if self.gpg_key_url else []) + self.extra_files:
            if upstream_rel(url) is None:
                raise ValueError(
                    f"{url!r} cannot be mirrored at its own path — it needs to be an "
                    "http(s) URL naming a file, with no query string"
                )
        return self

    @model_validator(mode="after")
    def _fill_derived(self):
        """A source needs only a URL; name and label follow from it."""
        if not self.name:
            self.name = derive_name(self.url, suites=self.suites, provider=self.provider)
        if not self.label:
            self.label = derive_label(self.url)
        return self

    @property
    def identity(self) -> str:
        """What makes two entries the same repository."""
        return normalize_url(self.url)

    def configured_units(self) -> set[tuple[str, str, str, str]]:
        """(label, suite, component, arch) tuples this source already covers —
        the wizard uses it to pre-select only what isn't configured yet."""
        units: set[tuple[str, str, str, str]] = set()
        for suite in self.suites:
            for comp in _effective_components(self, suite):
                for arch in self.effective_arches:
                    units.add((self.label, suite, comp, arch))
        return units

    def covers(self, mirror: "MirrorConfig") -> bool:
        """Does this source, as configured now, still ask for `mirror`?

        The pool remembers every mirror it ever synced, and publish enumerates
        from the pool — so without this, narrowing `suites`, `components` or
        `arches` kept publishing what was dropped, frozen at its last sync and
        never pruned. Empty still means all, on every dimension. A flat
        repository has no dimensions: its URL is all of it.
        """
        if (mirror.source or "") != self.identity:
            return False
        if self.provider == "apt-flat":
            return True
        if self.suites and (mirror.suite or "") not in self.suites:
            return False
        comps = _effective_components(self, mirror.suite) if mirror.suite else self.components
        if comps and (mirror.component or "") not in comps:
            return False
        return not (self.arches and mirror.arch not in self.arches)

    @property
    def effective_arches(self) -> list[str]:
        """Arches to expand into sync units.

        An empty list means "every architecture upstream serves" — resolved by
        directory listing at sync time (`core/resolve.py`, stage 3). This
        property is offline and pure, so for an apt source it cannot do that
        listing itself; it returns `[]` and leaves resolution to the sync path,
        exactly like an unresolved `components` list already does. Inventing an
        arch here (the old `amd64` fallback) is what the "never invent
        an architecture" rule forbids: a repository nobody has listed yet has no
        business being labelled as anything, least of all before the first sync
        — `to_mirrors()` yields no sync units and callers that display "what
        will this expand to" (the network editor, `RepositoryList.unresolved()`)
        render that honestly as "all — resolved at sync".

        **A flat repository is never arch-expanded** — the architecture is baked
        into its URL, so there is nothing to expand and nothing to guess.
        Labelling one `amd64` was not cosmetic: `NetworkSelect.matches` filters
        on arch, so an arm64-only network silently dropped NVIDIA's `sbsa` and
        `hpc-sdk/ubuntu/arm64` repositories from its published tree while their
        packages sat correctly in the pool. Use what the URL names, and leave it
        unknown when it names nothing.
        """
        if self.arches:
            return self.arches
        if self.provider == "apt-flat":
            return [arch_from_url(self.url)]
        return []

    def to_mirrors(self) -> list[MirrorConfig]:
        """Expand this source into individual MirrorConfig sync units.

        Arch is a dimension, not a property of the source: every arch becomes
        its own sync unit with the arch in its name, so two arches of the same
        suite/repo can never collide on a single `refs` mirror_name.
        """
        arches = self.effective_arches

        if self.provider == "apt-flat":
            # Flat repos encode the arch in the URL itself — nothing to expand.
            return [MirrorConfig(
                name=self.name,
                provider="apt-flat",
                label=self.label,
                source=self.identity,
                dist_upgrade=self.dist_upgrade,
                source_url=self.url,
                arch=arches[0],
                gpg_key_url=self.gpg_key_url,
                extra_files=self.extra_files,
                gpg_key=self.gpg_key,
                gpg_key_name=self.gpg_key_name,
                include_sources=self.include_sources,
            )]

        if self.provider == "apt":
            if not self.suites:
                # Single-URL APT with no suite structure
                return [MirrorConfig(
                    name=f"{self.name}-{arch}",
                    provider="apt",
                    label=self.label,
                    source=self.identity,
                    dist_upgrade=self.dist_upgrade,
                    source_url=self.url,
                    arch=arch,
                    gpg_key_url=self.gpg_key_url,
                    extra_files=self.extra_files,
                    gpg_key=self.gpg_key,
                    gpg_key_name=self.gpg_key_name,
                    include_sources=self.include_sources,
                ) for arch in arches]
            override_map = {o.suite: o.components for o in self.suite_overrides}
            mirrors = []
            for suite in self.suites:
                comps = override_map.get(suite, self.components)
                for comp in comps:
                    for arch in arches:
                        mirrors.append(MirrorConfig(
                            name=f"{self.name}-{suite}-{comp}-{arch}",
                            provider="apt",
                            label=self.label,
                            source=self.identity,
                            dist_upgrade=self.dist_upgrade,
                            source_url=self.url,
                            arch=arch,
                            suite=suite,
                            component=comp,
                            gpg_key_url=self.gpg_key_url,
                            extra_files=self.extra_files,
                            gpg_key=self.gpg_key,
                            gpg_key_name=self.gpg_key_name,
                            include_sources=self.include_sources,
                        ))
            return mirrors

        return []


# The TUI's colour theme when none is chosen. Lives here, not in tui/, because
# `save()` needs it to decide whether `theme` is worth writing.
DEFAULT_THEME = "mmw"


class Settings(_StrictModel):
    mirror_dir: Path
    # At least one: 0 built an `asyncio.Semaphore(0)` every download waited on
    # forever, and a `ThreadPoolExecutor(0)` that refused to start.
    max_connections: int = Field(default=os.cpu_count() or 10, ge=1)
    theme: str = DEFAULT_THEME


class NetworkSelect(_StrictModel):
    """One repository's slice of a network, by dimension.

    Empty means all — the same rule as `SourceConfig`. Selecting by dimension
    rather than by mirror name is what lets "all arches" stay true as new
    architectures appear: a frozen list of names silently stops publishing them.
    """
    source: str                     # repository URI
    suites: list[str] = []          # empty = every suite mirrored for it
    components: list[str] = []
    arches: list[str] = []

    def matches(self, mirror) -> bool:
        from mmw.core.config import normalize_url  # local: same module at import time
        if normalize_url(self.source) != (mirror.source or ""):
            return False
        if self.suites and (mirror.suite or "") not in self.suites:
            return False
        if self.components and (mirror.component or "") not in self.components:
            return False
        # A mirror with no architecture is not arch-expanded — a flat
        # repository, whose arch lives in its URL. Filtering it by arch would
        # drop it from every arch-specific network, silently, which is exactly
        # the failure this dimension model exists to prevent.
        return not (self.arches and mirror.arch and mirror.arch not in self.arches)


_LEGACY_NETWORK_KEYS = frozenset({"bundle_tls", "bundle_compress"})


class NetworkConfig(_StrictModel):
    """A named downselect of the mirror — what one site gets.

    `root` and `bundle_dir` are NOT part of what a network is: they are
    remembered destinations for the `publish` and `bundle` verbs. A network
    may have neither, one, or both. A verb whose destination is unset says so
    rather than inventing one.
    """
    name: str
    root: Path | None = None          # publish's destination
    bundle_dir: Path | None = None    # bundle's destination
    select: list[NetworkSelect] = []  # empty = every mirrored repository
    prune: bool = True        # remove destination files no longer in source
    dist_upgrade: bool = False  # publish changelogs.ubuntu.com files + UPGRADE.md
    certs: bool = False       # TLS key/CSR; serve HTTPS wherever it lands — bundles carry cert + key
    nginx: bool = True        # generate conf.d-style nginx site files

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_keys(cls, data):
        """Retired keys, accepted and ignored (unknown keys are otherwise
        errors) and gone after the next save:

        - `bundle_tls` once chose separately whether bundles carried the cert
          and key. It is folded into `certs` now — a certs network is HTTPS
          wherever it lands.
        - `bundle_compress` zstd-compressed the bundle. Bundles are plain
          tars now: the payload is packages, already compressed."""
        if isinstance(data, dict) and _LEGACY_NETWORK_KEYS.intersection(data):
            data = {k: v for k, v in data.items() if k not in _LEGACY_NETWORK_KEYS}
        return data

    @model_validator(mode="after")
    def _name_is_a_safe_path_segment(self):
        """A network's name is interpolated straight into filesystem paths —
        `{mirror_dir}/certs/{name}/`, the bundle ledger directory, a Textual
        widget id. `name = "../../../tmp/pwn"` escaped every one of them, and a
        name with a space or a slash breaks the widget id instead. One rule,
        checked on the model, so every consumer gets it."""
        if not NETWORK_NAME_RE.fullmatch(self.name or "") or self.name in (".", ".."):
            raise ValueError(
                f"invalid network name {self.name!r}: use only letters, digits, "
                "'.', '_' and '-' — the name is used as a directory name"
            )
        return self


class Config(_StrictModel):
    settings: Settings
    sources: list[SourceConfig] = []
    networks: list[NetworkConfig] = []

    @property
    def mirrors(self) -> list[MirrorConfig]:
        """Flat list of all sync units, derived from sources."""
        return [m for s in self.sources for m in s.to_mirrors()]

    def configured_units(self) -> set[tuple[str, str, str, str]]:
        """Everything already configured, across all sources."""
        return {u for s in self.sources for u in s.configured_units()}

    @model_validator(mode="after")
    def _names_must_be_unique(self):
        """Two things sharing a name share the state filed under it. Fail at
        load time rather than let one silently stand in for the other.

        - **mirror**: two mirrors sharing a name share a `refs` mirror_name, so
          the second sync's reconcile deletes the first one's index.
        - **source**: the name is the handle every screen and message uses.
        - **network**: two networks called `lab` share `certs/lab/` and one
          bundle ledger, and `mmw publish -n lab` silently picks the first.
        - **source URI**: identity is the URI, so two entries normalizing to one
          identity are one repository written twice — synced twice, and
          `merge_sources()` (which keys on identity) can only ever see one of
          them, so an import quietly updates one and leaves the other stale.
        """
        for label, names in (
            ("source name", [s.name for s in self.sources]),
            ("network name", [n.name for n in self.networks]),
            ("mirror name", [m.name for m in self.mirrors]),
            ("source URI", [s.identity for s in self.sources]),
        ):
            seen, dupes = set(), []
            for n in names:
                if n in seen and n not in dupes:
                    dupes.append(n)
                seen.add(n)
            if dupes:
                raise ValueError(f"duplicate {label}(s): {', '.join(sorted(dupes))}")
        return self


def _union_or_all(a: list[str], b: list[str]) -> list[str]:
    """Union under empty-means-all semantics.

    An empty list is not "nothing", it is "everything upstream serves", so it
    absorbs anything merged into it. Plain union would *narrow* a source that
    means "all arches" the moment someone imported one explicit arch for it.
    """
    if not a or not b:
        return []
    return _union(a, b)


def _union(a: list[str], b: list[str]) -> list[str]:
    """Order-preserving union — existing entries keep their position."""
    out = list(a)
    for x in b:
        if x not in out:
            out.append(x)
    return out


def _effective_components(src: SourceConfig, suite: str) -> list[str]:
    override = {o.suite: o.components for o in src.suite_overrides}
    return override.get(suite, src.components)


def _unique_source_name(base: str, url: str, taken: set[str]) -> str:
    """A free name for a source whose preferred name is taken by a different URL.

    Prefers the URL's host label (`ubuntu` + ports.ubuntu.com → `ubuntu-ports`),
    falling back to a numeric suffix.
    """
    label = (urlparse(url).hostname or "").split(".")[0]
    if label and not base.endswith(f"-{label}"):
        candidate = f"{base}-{label}"
        if candidate not in taken:
            return candidate
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    return f"{base}-{i}"


def merge_sources(
    existing: list[SourceConfig], new: list[SourceConfig]
) -> tuple[list[SourceConfig], int, int]:
    """Fold newly built sources into a configured list. Returns (list, added, merged).

    A source is *the same source* when its URI matches — identity is the
    repository, nothing else. Adding arm64 to a repository already mirrored for
    amd64 merges into that entry (arches, suites and per-suite components
    unioned) instead of being dropped as a duplicate name. A new source whose
    *name* is taken by a different URI — ports.ubuntu.com against
    archive.ubuntu.com — is renamed rather than discarded.
    """
    result = list(existing)
    by_identity = {s.identity: s for s in result}
    taken = {s.name for s in result}
    added = merged = 0

    for src in new:
        target = by_identity.get(src.identity)
        if target is None:
            if src.name in taken:
                src.name = _unique_source_name(src.name, src.url, taken)
            taken.add(src.name)
            by_identity[src.identity] = src
            result.append(src)
            added += 1
            continue

        if target.provider != src.provider:
            # Same URI, two shapes: one entry says `dists/` and the other says
            # "this directory *is* the repository". Merging would pick one at
            # random and mirror the wrong thing; they cannot both be kept
            # either, because identity is the URI. Say so.
            raise ValueError(
                f"{src.identity} is configured as {target.provider!r} but the "
                f"import says {src.provider!r} — one URI is one repository, so "
                "these cannot both be right"
            )

        before = target.model_dump()

        target.arches = _union_or_all(target.arches, src.arches)
        target.include_sources = target.include_sources or src.include_sources
        target.dist_upgrade = target.dist_upgrade or src.dist_upgrade
        target.gpg_key_url = target.gpg_key_url or src.gpg_key_url
        target.extra_files = list(dict.fromkeys(target.extra_files + src.extra_files))
        target.gpg_key = target.gpg_key or src.gpg_key
        target.gpg_key_name = target.gpg_key_name or src.gpg_key_name

        if not target.suites or not src.suites:
            # **Empty means all, on suites too.** A plain `_union` here made an
            # import naming `jammy` *narrow* a repository that was mirroring
            # every suite — the exact failure `_union_or_all` exists to
            # prevent, and it was doing it to two dimensions out of three.
            #
            # With the suites open there is no per-suite component map to
            # build: `comps_by_suite` is seeded from `target.suites`, so an
            # empty one loses the "all components" fact entirely. Components
            # fall back to their source-level union (still empty-means-all),
            # and per-suite overrides are meaningless without a suite list.
            target.components = _union_or_all(target.components, src.components)
            target.suites = []
            target.suite_overrides = []
        else:
            # Merge per suite so an existing suite never inherits components the
            # user picked for a different one.
            comps_by_suite = {
                suite: _effective_components(target, suite) for suite in target.suites
            }
            for suite in src.suites:
                incoming = _effective_components(src, suite)
                if suite in comps_by_suite:
                    comps_by_suite[suite] = _union_or_all(comps_by_suite[suite], incoming)
                else:
                    comps_by_suite[suite] = incoming
            suites = _union(target.suites, src.suites)
            common = comps_by_suite[suites[0]]
            target.suites = suites
            target.components = common
            target.suite_overrides = [
                SuiteOverride(suite=s, components=comps_by_suite[s])
                for s in suites[1:]
                if comps_by_suite[s] != common
            ]

        if target.model_dump() != before:
            merged += 1

    return result, added, merged


def config_path(directory: Path = Path(".")) -> Path:
    return directory / CONFIG_FILENAME


def load(directory: Path = Path(".")) -> Config:
    path = config_path(directory)
    if not path.exists():
        raise FileNotFoundError(f"No config found at {path}. Run `mmw init` first.")
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return Config.model_validate(data)


def delete(directory: Path = Path(".")) -> None:
    config_path(directory).unlink(missing_ok=True)


def source_to_dict(s: SourceConfig) -> dict:
    """TOML form of one source — the single serializer, shared by `save()` and
    `mmw add --dry-run` so a preview always matches what gets written.

    `url` comes first (it is the identity); `name` and `label` are omitted when
    they are exactly what would be derived, so a hand-written file stays minimal.
    """
    d: dict = {"url": s.url, "provider": s.provider}
    # The suites hint must match what derived the name in the first place, or
    # a name that *is* the derived one gets written out as an override.
    if s.name != derive_name(s.url, suites=s.suites or None, provider=s.provider):
        d["name"] = s.name
    if s.label != derive_label(s.url):
        d["label"] = s.label
    if s.arches:
        d["arches"] = s.arches
    if s.suites:
        d["suites"] = s.suites
    if s.components:
        d["components"] = s.components
    if s.suite_overrides:
        d["suite_overrides"] = [
            {"suite": o.suite, "components": o.components} for o in s.suite_overrides
        ]
    if s.include_sources:
        d["include_sources"] = True
    if s.gpg_key_url:
        d["gpg_key_url"] = s.gpg_key_url
    if s.gpg_key:
        # Kept in the config, not only on disk: an airgapped network is exactly
        # where the key URL cannot be reached, so mmw.toml alone must reproduce
        # the mirror. keys/ is an output directory and gets rebuilt.
        d["gpg_key"] = s.gpg_key
    if s.gpg_key_name:
        d["gpg_key_name"] = s.gpg_key_name
    if s.extra_files:
        d["extra_files"] = s.extra_files
    if s.dist_upgrade:
        d["dist_upgrade"] = True
    return d


def rename_network_state(mirror_dir: Path, old: str, new: str) -> list[str]:
    """Move what is filed under a network's name — its TLS key and cert
    (`certs/{name}/`), its bundle ledger (`bundles/{name}/`), and its
    snapshots, bundle log and publish records in the pool database — to the
    new name. Without this a rename silently starts over: a fresh private key
    that no signed cert matches, no record of where the last bundle went (so
    the next one is a full one the far side refuses as out of order), and
    snapshots stranded under a name nothing can reach any more.

    Never overwrites: state already at the new name is left alone and
    reported. Returns one line per thing it could not do.
    """
    problems: list[str] = []
    if (mirror_dir / ".pool" / "mmw.db").exists():
        from mmw.core.pool import Pool
        try:
            with Pool(mirror_dir) as pool:
                problem = pool.rename_network(old, new)
        except Exception as e:  # noqa: BLE001 — reported, like the moves below
            problem = f"could not move the pool's records of '{old}': {e}"
        if problem:
            problems.append(problem)
    for kind in ("certs", "bundles"):
        src, dst = mirror_dir / kind / old, mirror_dir / kind / new
        if not src.exists():
            continue
        if dst.exists():
            problems.append(f"{dst} already exists — {src} was left where it is")
            continue
        try:
            src.rename(dst)
        except OSError as e:
            problems.append(f"could not move {src} to {dst}: {e}")
    return problems


def save(cfg: Config, directory: Path = Path(".")) -> Path:
    path = config_path(directory)

    sources_data = []
    for s in cfg.sources:
        d = source_to_dict(s)
        sources_data.append(d)

    networks_data = [
        {
            "name": n.name,
            # root/bundle_dir are destinations belonging to the publish/bundle
            # verbs, not the network itself — a network may have neither, so
            # each is written only when set. `str(None)` would silently write
            # the literal string "None" and round-trip as a bogus path.
            **({"root": str(n.root)} if n.root is not None else {}),
            **({"bundle_dir": str(n.bundle_dir)} if n.bundle_dir is not None else {}),
            "prune": n.prune,
            **(
                {"select": [
                    {
                        "source": s.source,
                        **({"suites": s.suites} if s.suites else {}),
                        **({"components": s.components} if s.components else {}),
                        **({"arches": s.arches} if s.arches else {}),
                    }
                    for s in n.select
                ]}
                if n.select else {}
            ),
            **({"nginx": False} if not n.nginx else {}),
            **({"dist_upgrade": True} if n.dist_upgrade else {}),
            **({"certs": True} if n.certs else {}),
        }
        for n in cfg.networks
    ]

    # Omit empty tables: an emptied list serialises as a bare `networks = []`
    # ahead of the [[sources]] tables, which reads as noise in a hand-edited file.
    data = {
        "settings": {
            "mirror_dir": str(cfg.settings.mirror_dir),
            "max_connections": cfg.settings.max_connections,
            # Omitted at the default, like the network flags above: a
            # hand-edited file should only say what was actually chosen.
            **({"theme": cfg.settings.theme}
               if cfg.settings.theme != DEFAULT_THEME else {}),
        },
        **({"sources": sources_data} if sources_data else {}),
        **({"networks": networks_data} if networks_data else {}),
    }

    # **Never write a file `load()` will reject.** pydantic does not
    # re-validate on assignment (`validate_assignment` is off, deliberately —
    # the TUI edits models in place), so any attribute the TUI or a command
    # sets could produce a config that saves fine and then locks the user out
    # of *every* command, with nothing but a ValidationError to explain it.
    # The check is run against `data` — exactly the dict `load()` will hand to
    # `Config.model_validate` — so it catches serialization bugs too, not just
    # model-level ones (a `root` that round-tripped as the string "None" is
    # precisely how this class of bug last showed up).
    try:
        Config.model_validate(data)
    except ValidationError as e:
        raise ValueError(
            f"refusing to write an unloadable {CONFIG_FILENAME}: {e}"
        ) from e

    # Staged and renamed: written in place, a full disk or a kill mid-write
    # left a truncated mmw.toml — every command then refuses to start, and the
    # file is hand-edited, so there is often no other copy. The target is
    # resolved first so a symlinked mmw.toml is replaced at its real location
    # rather than turned into a regular file.
    target = path.resolve() if path.is_symlink() else path
    tmp = target.with_name(target.name + ".tmp")
    try:
        with open(tmp, "wb") as f:
            tomli_w.dump(data, f)
        if target.exists():
            os.chmod(tmp, target.stat().st_mode & 0o7777)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return path
