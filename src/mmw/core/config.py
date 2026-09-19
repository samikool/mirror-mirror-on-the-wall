import os
import tomllib
import tomli_w
from pathlib import Path
from urllib.parse import urlparse
from pydantic import BaseModel, ConfigDict, model_validator

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

KNOWN_ARCHES = frozenset({
    "amd64", "arm64", "armhf", "armel", "ppc64el", "mipsel", "mips64el",
    "mips", "powerpc", "hppa", "m68k", "sh4", "x32", "i386", "riscv64",
    "s390x", "alpha", "sparc64", "hurd-i386", "hurd-amd64",
    "kfreebsd-i386", "kfreebsd-amd64",
})


def normalize_url(url: str) -> str:
    """Canonical form of a repository URI — the identity of a source.

    Trailing slashes and case in the host are not distinctions, so
    `HTTP://Archive.Ubuntu.com/ubuntu/` and `http://archive.ubuntu.com/ubuntu`
    are the same repository.
    """
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.port:
        host = f"{host}:{parsed.port}"
    scheme = (parsed.scheme or "http").lower()
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


class MirrorConfig(_StrictModel):
    name: str
    provider: str       # "apt" or "apt-flat"
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
    provider: str = "apt"             # "apt" or "apt-flat"
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
    dist_upgrade: bool = False        # mirror changelogs.ubuntu.com meta-release too

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

    @property
    def effective_arches(self) -> list[str]:
        """Arches to expand into sync units.

        An empty list means "every architecture upstream serves" — resolved by
        directory listing at sync time (`core/resolve.py`, stage 3). This
        property is offline and pure, so for an apt source it cannot do that
        listing itself; it returns `[]` and leaves resolution to the sync path,
        exactly like an unresolved `components` list already does. Inventing an
        arch here (the old `amd64` fallback) is what design-notes.md's "never invent
        an architecture" rule forbids: a repository nobody has listed yet has no
        business being labelled as anything, least of all before the first sync
        — `to_mirrors()` yields no sync units and callers that display "what
        will this expand to" (the network editor, `manage.py`'s `_unresolved()`)
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
                            gpg_key=self.gpg_key,
                            gpg_key_name=self.gpg_key_name,
                            include_sources=self.include_sources,
                        ))
            return mirrors

        return []


class Settings(_StrictModel):
    mirror_dir: Path
    max_connections: int = os.cpu_count() or 10


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
    certs: bool = False       # manage TLS key/CSR for this network
    nginx: bool = True        # generate conf.d-style nginx site files
    bundle_compress: bool = True  # zstd-compress the bundle tarball


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
        """Two mirrors sharing a name share a `refs` mirror_name — the second
        sync's reconcile would silently delete the first one's index.  Fail at
        load time instead."""
        for label, names in (
            ("source", [s.name for s in self.sources]),
            ("mirror", [m.name for m in self.mirrors]),
        ):
            seen, dupes = set(), []
            for n in names:
                if n in seen and n not in dupes:
                    dupes.append(n)
                seen.add(n)
            if dupes:
                raise ValueError(f"duplicate {label} name(s): {', '.join(sorted(dupes))}")
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

        before = target.model_dump()

        target.arches = _union_or_all(target.arches, src.arches)
        target.include_sources = target.include_sources or src.include_sources
        target.gpg_key_url = target.gpg_key_url or src.gpg_key_url
        target.gpg_key = target.gpg_key or src.gpg_key
        target.gpg_key_name = target.gpg_key_name or src.gpg_key_name

        if target.suites or src.suites:
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
    if s.dist_upgrade:
        d["dist_upgrade"] = True
    return d


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
            **({"bundle_compress": False} if not n.bundle_compress else {}),
        }
        for n in cfg.networks
    ]

    # Omit empty tables: an emptied list serialises as a bare `networks = []`
    # ahead of the [[sources]] tables, which reads as noise in a hand-edited file.
    data = {
        "settings": {
            "mirror_dir": str(cfg.settings.mirror_dir),
            "max_connections": cfg.settings.max_connections,
        },
        **({"sources": sources_data} if sources_data else {}),
        **({"networks": networks_data} if networks_data else {}),
    }
    with open(path, "wb") as f:
        tomli_w.dump(data, f)
    return path
