import os
import re
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib
import tomli_w
from pathlib import Path
from typing import Annotated, Any, Literal, Union
from urllib.parse import urlparse
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, field_validator, model_validator

from mmw.core import disk_io

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


Provider = Literal["apt", "apt-flat", "vscode", "pypi"]
"""Every provider a unit may belong to. A free string let `provider =
"apt_flat"` load clean, expand to zero mirrors, and exit 0 having mirrored
nothing."""
AptProvider = Literal["apt", "apt-flat"]


# The fields every apt unit used to declare. A unit is generic now — what
# varies between providers is its coordinates (`dims`) and its settings
# (`options`) — but every caller, test and recorded snapshot still says
# `arch=`/`suite=`/`gpg_key=`, so the old names are folded in on the way in
# and exposed as properties on the way out.
_LEGACY_DIM_FIELDS = ("arch", "suite", "component")
_OPTION_DEFAULTS: dict[str, Any] = {
    "include_sources": False,   # mirror deb-src in addition to deb binaries
    "gpg_key_url": None,        # fetched to keys/{label}/ during sync
    "gpg_key": None,            # armoured key, written to keys/ without a fetch
    "gpg_key_name": None,       # filename stem for the emitted key files
    "extra_files": [],          # upstream URLs mirrored at their own path
    "dist_upgrade": False,      # also mirror changelogs.ubuntu.com meta-release
}
_APT_PROVIDERS = ("apt", "apt-flat")


class MirrorConfig(_StrictModel):
    """One sync unit.

    `dims` are the unit's coordinates along its provider's dimensions
    (`{"suite": "noble", "component": "main", "arch": "amd64"}` for apt);
    `options` are provider settings that travel with it.
    """
    name: str
    provider: Provider
    source_url: str     # this unit's own URL (repository root for apt)
    source: str = ""    # normalized source URI this unit belongs to
    label: str = ""     # keys/{label}/ grouping only — never identity
    dims: dict[str, str] = {}
    options: dict[str, Any] = {}

    @model_validator(mode="before")
    @classmethod
    def _fold_legacy_fields(cls, data):
        if not isinstance(data, dict):
            return data
        data = dict(data)
        dims = dict(data.get("dims") or {})
        options = dict(data.get("options") or {})
        for key in _LEGACY_DIM_FIELDS:
            if key in data:
                value = data.pop(key)
                if value is not None:
                    dims[key] = value
        for key in _OPTION_DEFAULTS:
            if key in data:
                options[key] = data.pop(key)
        # A spelled-out default says nothing: drop it, so two units that mean
        # the same thing compare equal however they were built.
        options = {k: v for k, v in options.items()
                   if k not in _OPTION_DEFAULTS or v != _OPTION_DEFAULTS[k]}
        # The old `arch: str = "amd64"` default, kept for apt units only: an
        # apt unit always has an arch coordinate, even when nobody stated it.
        if data.get("provider") in _APT_PROVIDERS:
            dims.setdefault("arch", "amd64")
        data["dims"], data["options"] = dims, options
        return data

    # ── compatibility: the old apt fields, read and written through dims/options

    @property
    def arch(self) -> str:
        return self.dims.get("arch", "")

    @arch.setter
    def arch(self, value: str) -> None:
        self.dims["arch"] = value

    @property
    def suite(self) -> str | None:
        return self.dims.get("suite")

    @suite.setter
    def suite(self, value: str | None) -> None:
        self._set_dim("suite", value)

    @property
    def component(self) -> str | None:
        return self.dims.get("component")

    @component.setter
    def component(self, value: str | None) -> None:
        self._set_dim("component", value)

    def _set_dim(self, key: str, value: str | None) -> None:
        if value is None:
            self.dims.pop(key, None)
        else:
            self.dims[key] = value

    def _option(self, key: str):
        default = _OPTION_DEFAULTS[key]
        value = self.options.get(key, default)
        return list(value) if isinstance(default, list) else value

    @property
    def include_sources(self) -> bool:
        return bool(self._option("include_sources"))

    @include_sources.setter
    def include_sources(self, value: bool) -> None:
        self.options["include_sources"] = value

    @property
    def gpg_key_url(self) -> str | None:
        return self._option("gpg_key_url")

    @gpg_key_url.setter
    def gpg_key_url(self, value: str | None) -> None:
        self.options["gpg_key_url"] = value

    @property
    def gpg_key(self) -> str | None:
        return self._option("gpg_key")

    @gpg_key.setter
    def gpg_key(self, value: str | None) -> None:
        self.options["gpg_key"] = value

    @property
    def gpg_key_name(self) -> str | None:
        return self._option("gpg_key_name")

    @gpg_key_name.setter
    def gpg_key_name(self, value: str | None) -> None:
        self.options["gpg_key_name"] = value

    @property
    def extra_files(self) -> list[str]:
        # The stored list itself, so `m.extra_files.append(url)` sticks as it
        # did when this was a field.
        return self.options.setdefault("extra_files", [])

    @extra_files.setter
    def extra_files(self, value: list[str]) -> None:
        self.options["extra_files"] = list(value)

    @property
    def dist_upgrade(self) -> bool:
        return bool(self._option("dist_upgrade"))

    @dist_upgrade.setter
    def dist_upgrade(self, value: bool) -> None:
        self.options["dist_upgrade"] = value

    def _identity_tuple(self) -> tuple:
        options = {k: v for k, v in self.options.items()
                   if k not in _OPTION_DEFAULTS or v != _OPTION_DEFAULTS[k]}
        return (self.name, self.provider, self.source_url, self.source, self.label,
                self.dims, options)

    def __eq__(self, other) -> bool:
        if not isinstance(other, MirrorConfig):
            return NotImplemented
        return self._identity_tuple() == other._identity_tuple()

    __hash__ = None  # mutable, like every pydantic model

    def model_copy(self, *, update: dict | None = None, deep: bool = False) -> "MirrorConfig":
        """As pydantic's, plus: the old field names work in `update`, and a
        shallow copy gets its own `dims`/`options` — with flat fields, a copy
        never shared state with its original."""
        dims, options = dict(self.dims), {
            k: list(v) if isinstance(v, list) else v for k, v in self.options.items()}
        update = dict(update or {})
        for key in _LEGACY_DIM_FIELDS:
            if key in update:
                value = update.pop(key)
                if value is None:
                    dims.pop(key, None)
                else:
                    dims[key] = value
        for key in _OPTION_DEFAULTS:
            if key in update:
                options[key] = update.pop(key)
        update.setdefault("dims", dims)
        update.setdefault("options", options)
        return super().model_copy(update=update, deep=deep)

    def legacy_dict(self) -> dict:
        """This unit in the pre-seam field set (`arch`, `suite`, …, flat).

        What snapshots record: an older mmw validates recorded mirrors with
        `extra="forbid"` and knows no `dims`/`options`, so recording them would
        leave it unable to publish a snapshot this version took. Every unit
        today is an apt unit, so the old fields hold everything it carries.
        """
        return {
            "name": self.name, "provider": self.provider, "source_url": self.source_url,
            "source": self.source, "label": self.label, "arch": self.arch,
            "suite": self.suite, "component": self.component,
            **{key: self._option(key) for key in _OPTION_DEFAULTS},
        }

    @property
    def mirror_base(self) -> str:
        parsed = urlparse(self.source_url)
        host = parsed.hostname or "unknown"
        if parsed.port:
            # Distinguish repos on the same host at different ports.
            host = f"{host}_{parsed.port}"
        path = parsed.path.strip("/")
        return f"{host}/{path}" if path else host


Unit = MirrorConfig
"""The provider-neutral name for a sync unit (stage 1 keeps the class name)."""


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
    provider: AptProvider = "apt"
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


MARKETPLACE_URL = "https://marketplace.visualstudio.com"
_EXT_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]*\.[a-z0-9][a-z0-9_.-]*")


class ExtensionSelection(_StrictModel):
    """Which extensions a VS Code source mirrors — a union of the three."""
    top_n: int | None = Field(default=None, ge=1)
    ids: list[str] = []
    all: bool = False
    prerelease: bool = False

    @model_validator(mode="after")
    def _ids_are_publisher_dot_name(self):
        self.ids = [i.strip().lower() for i in self.ids]
        bad = [i for i in self.ids if not _EXT_ID_RE.fullmatch(i)]
        if bad:
            raise ValueError(f"extension ids must be publisher.name: {', '.join(bad)}")
        return self


class VscodeSource(_StrictModel):
    """The Microsoft Marketplace: extensions, desktop builds, server builds.

    One per config. Empty kinds / platforms / qualities mean *all*."""
    provider: Literal["vscode"]
    name: str = "vscode"
    label: str = "vscode"
    url: str = MARKETPLACE_URL
    kinds: list[str] = []
    platforms: list[str] = []
    qualities: list[str] = []
    extensions: ExtensionSelection = ExtensionSelection()

    @model_validator(mode="after")
    def _known_values(self):
        from mmw.providers.vscode.platforms import KINDS, QUALITIES, TARGET_PLATFORMS
        if normalize_url(self.url) != MARKETPLACE_URL:
            raise ValueError(f"the vscode provider mirrors the marketplace ({MARKETPLACE_URL}), "
                             f"not {self.url}")
        for field, allowed in (("kinds", KINDS), ("platforms", TARGET_PLATFORMS),
                               ("qualities", QUALITIES)):
            bad = [v for v in getattr(self, field) if v not in allowed]
            if bad:
                raise ValueError(f"unknown {field}: {', '.join(bad)} "
                                 f"(expected: {', '.join(allowed)})")
        return self

    @property
    def identity(self) -> str:
        return normalize_url(self.url)

    def configured_units(self) -> set:
        return set()

    def covers(self, unit) -> bool:
        if (unit.source or "") != self.identity:
            return False
        for field, key in (("kinds", "kind"), ("platforms", "platform"), ("qualities", "quality")):
            chosen, have = getattr(self, field), unit.dims.get(key)
            if chosen and have and have not in chosen:
                return False
        return True


PYPI_URL = "https://pypi.org/simple/"
_FLOOR_RE = re.compile(r"\d+\.\d+")
_ARCHIVE_SUFFIXES = (".whl", ".tar.gz", ".zip", ".tar.bz2", ".tgz")


def check_requirement_line(line: str) -> str | None:
    """One requirements.txt line as mmw accepts it: names, specifiers, extras
    and markers. None for a comment or blank line. Options (`-r`, `-e`, …),
    URLs, paths and hashes are refused — a mirror resolves against PyPI only."""
    from packaging.requirements import InvalidRequirement, Requirement
    text = line.split(" #", 1)[0].strip()
    if not text or text.startswith("#"):
        return None
    refused = (text.startswith(("-", ".", "/", "~")) or "--hash" in text or "://" in text
               or text.startswith("git+") or text.lower().endswith(_ARCHIVE_SUFFIXES))
    if not refused:
        try:
            refused = Requirement(text).url is not None
        except InvalidRequirement:
            refused = True
    if refused:
        raise ValueError(f"unsupported requirements line: '{line.strip()}' (only names, versions, "
                         "extras and markers — no options, URLs, paths or hashes)")
    return text


class PypiSource(_StrictModel):
    """pypi.org: the straight mirror (`all`) — every project's latest
    installable release plus the older releases its dependencies need, per
    target. One per config. Empty pythons / platforms / arches mean *all*.
    `glibc` / `musl` / `macos` are the oldest far-side OS versions: a release
    counts as installable only if a client there can install it."""
    provider: Literal["pypi"]
    name: str = "pypi"
    label: str = "pypi"
    url: str = PYPI_URL
    pythons: list[str] = []
    platforms: list[str] = []
    arches: list[str] = []
    glibc: str = "2.31"
    musl: str = "1.2"
    macos: str = "11.0"
    sdists: bool = False
    all: bool = False
    requirements: list[str] = []
    requirements_file: str | None = None
    # Where mmw.toml lives, so a relative requirements_file means "next to the
    # config". Set by `load`; absent for drafts (then: the working directory).
    _config_dir: Path | None = PrivateAttr(default=None)

    @field_validator("requirements", mode="before")
    @classmethod
    def _requirement_lines(cls, value):
        lines = value.splitlines() if isinstance(value, str) else list(value or [])
        return [kept for kept in (check_requirement_line(str(line)) for line in lines) if kept]

    @field_validator("requirements_file", mode="before")
    @classmethod
    def _blank_file_is_none(cls, value):
        return value.strip() or None if isinstance(value, str) else value

    def __eq__(self, other):
        # Where the config was loaded from is not part of what the source is.
        return type(other) is type(self) and self.model_dump() == other.model_dump()

    __hash__ = None

    def requirement_lines(self, config_dir: Path | None = None) -> list[str]:
        """The inline list then the file's lines, de-duplicated in order. The
        file is read now, every time: it may change between syncs. A relative
        file is next to mmw.toml: `config_dir`, else where it was loaded from."""
        lines = list(self.requirements)
        if self.requirements_file:
            path = Path(self.requirements_file).expanduser()
            if not path.is_absolute():
                path = (config_dir or self._config_dir or Path.cwd()) / path
            try:
                text = disk_io.ops.read_text(path, encoding="utf-8-sig")   # Windows editors add a BOM
            except FileNotFoundError:
                raise FileNotFoundError(f"requirements_file not found: {path}") from None
            lines += [kept for kept in (check_requirement_line(line) for line in text.splitlines()) if kept]
        return list(dict.fromkeys(lines))

    @model_validator(mode="after")
    def _known_values(self):
        from mmw.providers.pypi.targets import ARCHES, PLATFORMS, PYTHONS
        if normalize_url(self.url) != normalize_url(PYPI_URL):
            raise ValueError(f"the pypi provider mirrors pypi.org ({PYPI_URL}), not {self.url}")
        for field, allowed in (("pythons", PYTHONS), ("platforms", PLATFORMS), ("arches", ARCHES)):
            bad = [v for v in getattr(self, field) if v not in allowed]
            if bad:
                raise ValueError(f"unknown {field}: {', '.join(bad)} "
                                 f"(expected: {', '.join(allowed)})")
        for field in ("glibc", "musl", "macos"):
            if not _FLOOR_RE.fullmatch(getattr(self, field)):
                raise ValueError(f"{field} must be a MAJOR.MINOR version, not {getattr(self, field)!r}")
        if tuple(int(x) for x in self.glibc.split(".")) < (2, 17):
            # manylinux2014: the oldest glibc uv resolves for, and the oldest
            # current binary wheels target.
            raise ValueError(f"glibc floor {self.glibc} is older than 2.17, the oldest uv and current "
                             "wheels support")
        return self

    @property
    def identity(self) -> str:
        return normalize_url(self.url)

    @property
    def floors(self):
        from mmw.providers.pypi.targets import Floors
        return Floors(glibc=self.glibc, musl=self.musl, macos=self.macos)

    def configured_units(self) -> set:
        return set()

    def covers(self, unit) -> bool:
        if (unit.source or "") != self.identity:
            return False
        for field, key in (("pythons", "python"), ("platforms", "platform"), ("arches", "arch")):
            chosen, have = getattr(self, field), unit.dims.get(key)
            if chosen and have and have not in chosen:
                return False
        return True


AnySource = Annotated[Union[SourceConfig, VscodeSource, PypiSource], Field(discriminator="provider")]


# The TUI's colour theme when none is chosen. Lives here, not in tui/, because
# `save()` needs it to decide whether `theme` is worth writing.
DEFAULT_THEME = "mmw"


class Settings(_StrictModel):
    mirror_dir: Path
    # At least one: 0 built an `asyncio.Semaphore(0)` every download waited on
    # forever, and a `ThreadPoolExecutor(0)` that refused to start.
    max_connections: int = Field(default=os.cpu_count() or 10, ge=1)
    # Disk lanes, per job (`mmw.core.disk_io`). Defaults measured on one HDD —
    # see docs/internals/superpowers/specs/2026-10-20-disk-io-layer-design.md;
    # 1 / 1 / 0 is exactly the serial behaviour mmw had before.
    disk_write_threads: int = Field(default=8, ge=1)   # link / unlink / rename on a cold cache
    disk_read_threads: int = Field(default=16, ge=1)   # stat / open / small reads on a cold cache
    disk_readahead: int = Field(default=16, ge=0)      # files opened ahead of a stream; 0 = off
    theme: str = DEFAULT_THEME
    # The pool database file. Default: where it has always been, inside the
    # pool. A fast disk here keeps listings off a slow pool disk; `.pool/tmp`
    # and `.pool/packages` stay with the pool. Hand-edited: move the file
    # (and its -wal/-shm) yourself — mmw only opens it here.
    database: Path | None = None

    @model_validator(mode="after")
    def _database_defaults_to_the_pool(self):
        if self.database is None:
            from mmw.core.pool import default_database
            self.database = default_database(self.mirror_dir)
        return self

    def with_mirror_dir(self, mirror_dir: Path) -> "Settings":
        """A new pool directory; a database at the old default follows it,
        one placed elsewhere stays."""
        from mmw.core.pool import default_database
        database = self.database
        if database == default_database(self.mirror_dir):
            database = default_database(mirror_dir)
        return self.model_copy(update={"mirror_dir": mirror_dir, "database": database})


_DISK_KEYS = ("disk_write_threads", "disk_read_threads", "disk_readahead")


class NetworkSelect(_StrictModel):
    """One source's slice of a network, by dimension.

    `dims` holds one list per dimension of the source's provider (`suites`,
    `components`, `arches` for apt). Empty means all — the same rule as a
    source. TOML spells each dimension as its own key, which is folded in
    here, so hand-written files stay flat. Selecting by dimension rather than
    by unit name is what lets "all arches" stay true as new architectures
    appear: a frozen list of names silently stops publishing them.
    """
    source: str                          # source URI
    dims: dict[str, list[str]] = {}

    @model_validator(mode="before")
    @classmethod
    def _fold_dimension_keys(cls, data):
        if not isinstance(data, dict):
            return data
        if "dims" in data:
            raise ValueError(
                "write a select's dimensions as flat keys (suites = [...]), not a dims table")
        dims: dict[str, list[str]] = {}
        for key, values in data.items():
            if key == "source":
                continue
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise ValueError(f"select key {key!r} must be a list of strings")
            dims[key] = values
        folded: dict = {"dims": dims}
        if "source" in data:
            folded["source"] = data["source"]
        return folded

    @model_validator(mode="after")
    def _keys_are_dimensions(self):
        from mmw.providers import all_providers
        known = {d.name for p in all_providers() for d in p.dimensions}
        unknown = sorted(set(self.dims) - known)
        if unknown:
            raise ValueError(
                f"unknown select key(s) {', '.join(unknown)} for {self.source} — "
                f"expected one of: {', '.join(sorted(known))}")
        return self

    @property
    def suites(self) -> list[str]:
        return self.dims.get("suites", [])

    @property
    def components(self) -> list[str]:
        return self.dims.get("components", [])

    @property
    def arches(self) -> list[str]:
        return self.dims.get("arches", [])

    def matches(self, mirror) -> bool:
        from mmw.core.config import normalize_url  # local: same module at import time
        from mmw.providers import get_provider
        if normalize_url(self.source) != (mirror.source or ""):
            return False
        provider = get_provider(mirror.provider)
        for name, values in self.dims.items():
            if not values:
                continue
            dim = provider.dimension(name)
            if dim is None:
                continue
            have = mirror.dims.get(dim.unit_key) or ""
            if not have:
                # A flat repository has no arch coordinate; filtering it by
                # arch would drop it from every arch-specific network,
                # silently — the failure this dimension model exists to prevent.
                if dim.absent_matches:
                    continue
                return False
            if have not in values:
                return False
        return True


_LEGACY_NETWORK_KEYS = frozenset({"bundle_tls", "bundle_compress", "gallery_port"})


DEFAULT_RETENTION = "90d"
DEFAULT_RUNTIME_PYTHON = "3.12"
_RETENTION_RE = re.compile(r"([1-9]\d*)d")


def retention_days(value: str) -> int | None:
    """A network's `retention`: "current" → 0, "<N>d" → N, "forever" → None."""
    if value == "current":
        return 0
    if value == "forever":
        return None
    match = _RETENTION_RE.fullmatch(value or "")
    if match is None:
        raise ValueError(f"retention must be current, forever or a number of days like 90d, not {value!r}")
    return int(match.group(1))


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
    certs: bool = True        # TLS key/CSR; serve HTTPS wherever it lands — bundles carry cert + key
    nginx: bool = True        # ship the nginx image, nginx/compose.yaml and mirror.conf
    # How long a release that left a provider's keep set stays published
    # (providers with version history — PyPI). Widening is not retroactive.
    retention: str = DEFAULT_RETENTION
    # What runs on the airgapped side: `apply.py` alone, or mmw itself.
    # Partial bundles and `mmw stage` need mmw there.
    far_side: Literal["apply", "mmw"] = "apply"
    # mmw's own runtime, shipped to an `mmw` far side: the machine
    # architectures it may run on, and the Python it runs on.
    runtime_arches: list[Literal["x86_64", "aarch64"]] = ["x86_64"]
    runtime_python: str = DEFAULT_RUNTIME_PYTHON

    @field_validator("runtime_python")
    @classmethod
    def _valid_runtime_python(cls, value):
        if not re.fullmatch(r"3\.\d+", value):
            raise ValueError(f"runtime_python is a Python version like 3.12, not {value!r}")
        return value

    @field_validator("runtime_arches")
    @classmethod
    def _some_runtime_arch(cls, value):
        if not value:
            raise ValueError("runtime_arches needs at least one architecture")
        return sorted(set(value))

    @field_validator("retention")
    @classmethod
    def _valid_retention(cls, value):
        retention_days(value)
        return value

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_keys(cls, data):
        """Retired keys, accepted and ignored (unknown keys are otherwise
        errors) and gone after the next save:

        - `bundle_tls` once chose separately whether bundles carried the cert
          and key. It is folded into `certs` now — a certs network is HTTPS
          wherever it lands.
        - `bundle_compress` zstd-compressed the bundle. Bundles are plain
          tars now: the payload is packages, already compressed.
        - `gallery_port` was the host port of a standalone VS Code gallery
          container. The gallery runs in the nginx Compose stack now, where
          nginx reaches it at `gallery:8080`."""
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
    sources: list[AnySource] = []
    networks: list[NetworkConfig] = []

    @model_validator(mode="before")
    @classmethod
    def _sources_default_to_apt(cls, data):
        """`provider` was optional (apt) before there was a second provider;
        the union needs it to choose a model."""
        if isinstance(data, dict) and isinstance(data.get("sources"), list):
            data = {**data, "sources": [
                {**s, "provider": s.get("provider", "apt")} if isinstance(s, dict) else s
                for s in data["sources"]]}
        return data

    @model_validator(mode="after")
    def _one_vscode_source(self):
        if sum(1 for s in self.sources if s.provider == "vscode") > 1:
            raise ValueError("only one vscode source is allowed (it is the whole marketplace)")
        return self

    @model_validator(mode="after")
    def _one_pypi_source(self):
        if sum(1 for s in self.sources if s.provider == "pypi") > 1:
            raise ValueError("only one pypi source is allowed (it is the whole of pypi.org)")
        return self

    @property
    def mirrors(self) -> list[MirrorConfig]:
        """Flat list of all sync units, derived from sources by their providers."""
        from mmw.providers import get_provider
        return [m for s in self.sources for m in get_provider(s.provider).expand(s)]

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

    @model_validator(mode="after")
    def _selects_use_their_providers_dimensions(self):
        """A select key must be a dimension of *that* source's provider —
        `components` on a flat repository is a mistake, not a no-op."""
        from mmw.providers import get_provider
        by_identity = {s.identity: s for s in self.sources}
        for n in self.networks:
            for sel in n.select:
                src = by_identity.get(normalize_url(sel.source))
                if src is None:
                    continue
                names = {d.name for d in get_provider(src.provider).dimensions}
                # An empty list filters nothing, so it is harmless on any
                # provider — and such files loaded before the seam.
                wrong = sorted(k for k, v in sel.dims.items() if v and k not in names)
                if wrong:
                    raise ValueError(
                        f"network '{n.name}': {', '.join(wrong)} is not a dimension of "
                        f"{src.provider} source {src.identity}")
        return self

    @model_validator(mode="after")
    def _destinations_do_not_collide(self):
        """Publish destinations that would destroy each other, refused at load.

        - a root that is, or holds, the mirror directory (see
          `destination_problem`): the first pruning publish deleted the pool;
        - two networks whose roots are the same or nested: each publish
          prunes the other's repositories as stale;
        - a bundle_dir inside a publish root: the tarballs are served to every
          client, and the next pruning publish deletes them.
        """
        roots: list[tuple[str, Path]] = []
        for n in self.networks:
            if n.root is not None:
                problem = destination_problem(n.root, self.settings.mirror_dir)
                if problem:
                    raise ValueError(f"network '{n.name}': root {problem}")
                roots.append((n.name, _abs(n.root)))
        for i, (a, ra) in enumerate(roots):
            for b, rb in roots[i + 1:]:
                if _within(ra, rb) or _within(rb, ra):
                    raise ValueError(
                        f"networks '{a}' and '{b}' publish into overlapping roots ({ra}, {rb}) "
                        "— each publish would prune the other's files")
        for n in self.networks:
            if n.bundle_dir is None:
                continue
            for name, r in roots:
                if _within(_abs(n.bundle_dir), r):
                    raise ValueError(
                        f"network '{n.name}': bundle_dir {n.bundle_dir} is inside network "
                        f"'{name}''s publish root — served to every client, and deleted by "
                        "its next pruning publish")
        return self


def _abs(p: Path) -> Path:
    """Absolute and normalized, without touching the filesystem (a validator
    runs on every load, and the paths need not exist yet)."""
    return Path(os.path.normpath(os.path.abspath(os.path.expanduser(str(p)))))


def _within(inner: Path, outer: Path) -> bool:
    """`inner` is `outer` or somewhere under it."""
    return inner == outer or outer in inner.parents


def destination_problem(root: Path, mirror_dir: Path) -> str | None:
    """Why `root` cannot be a publish destination for the pool at
    `mirror_dir`, or None.

    A pruning publish removes every top-level directory of its root that no
    repository it publishes owns (`TreeSink.prune_stale_bases`), and removing
    a network can delete its whole root. A root that IS the mirror directory,
    or holds it, therefore deleted `.pool/`, `certs/`, `bundles/` — the pool
    itself — on the first publish. Checked at load and again at publish
    time (resolving symlinks there), since `--root` never passes the config.
    """
    r, m = _abs(root), _abs(mirror_dir)
    if r == Path(r.anchor):
        return f"{root} is the filesystem root"
    if _within(m, r):
        return (f"{root} holds the mirror directory {mirror_dir} — a pruning publish "
                "deletes what it does not publish there, the pool included")
    return None


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


def _merge_apt_into(target: SourceConfig, src: SourceConfig) -> None:
    """Union `src` into the apt source `target` in place — empty means all on
    every dimension, per-suite components kept apart."""
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
        from mmw.providers import get_provider
        merged_src = get_provider(src.provider).merge(target, src)
        if merged_src is not target:
            result[result.index(target)] = merged_src
            by_identity[src.identity] = merged_src
            target = merged_src
        if target.model_dump() != before:
            merged += 1

    return result, added, merged


def config_path(directory: Path = Path(".")) -> Path:
    return directory / CONFIG_FILENAME


def load(directory: Path = Path(".")) -> Config:
    path = config_path(directory)
    if not disk_io.ops.exists(path):
        raise FileNotFoundError(f"No config found at {path}. Run `mmw init` first.")
    with disk_io.ops.open_file(path, "rb") as f:
        data = tomllib.load(f)
    cfg = Config.model_validate(data)
    for source in cfg.sources:
        if isinstance(source, PypiSource):
            source._config_dir = Path(directory).resolve()
    from mmw.core.pool import register_database
    register_database(cfg.settings.mirror_dir, cfg.settings.database)
    disk_io.configure(cfg.settings)
    return cfg


def delete(directory: Path = Path(".")) -> None:
    disk_io.ops.unlink(config_path(directory), missing_ok=True)


def _select_to_dict(sel: NetworkSelect) -> dict:
    """TOML form of one select: flat dimension keys, in registry dimension
    order (suites, components, arches for apt), empty ones omitted."""
    from mmw.providers import all_providers
    order: list[str] = []
    for p in all_providers():
        order += [d.name for d in p.dimensions if d.name not in order]
    keys = sorted((k for k, v in sel.dims.items() if v),
                  key=lambda k: order.index(k) if k in order else len(order))
    return {"source": sel.source, **{k: sel.dims[k] for k in keys}}


def source_to_dict(s) -> dict:
    """TOML form of one source — the single serializer, shared by `save()` and
    every `--dry-run` preview so a preview always matches what gets written."""
    from mmw.providers import get_provider
    return get_provider(s.provider).to_toml(s)


def _apt_source_to_dict(s: SourceConfig) -> dict:
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
    from mmw.core.pool import database_path
    if disk_io.ops.exists(database_path(mirror_dir)):
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
        if not disk_io.ops.exists(src):
            continue
        if disk_io.ops.exists(dst):
            problems.append(f"{dst} already exists — {src} was left where it is")
            continue
        try:
            disk_io.ops.rename(src, dst)
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
                {"select": [_select_to_dict(s) for s in n.select]}
                if n.select else {}
            ),
            **({"nginx": False} if not n.nginx else {}),
            **({"dist_upgrade": True} if n.dist_upgrade else {}),
            **({"certs": False} if not n.certs else {}),
            **({"retention": n.retention} if n.retention != DEFAULT_RETENTION else {}),
            **({"far_side": "mmw"} if n.far_side == "mmw" else {}),
            **({"runtime_arches": n.runtime_arches} if n.runtime_arches != ["x86_64"] else {}),
            **({"runtime_python": n.runtime_python} if n.runtime_python != DEFAULT_RUNTIME_PYTHON else {}),
        }
        for n in cfg.networks
    ]

    # Omit empty tables: an emptied list serialises as a bare `networks = []`
    # ahead of the [[sources]] tables, which reads as noise in a hand-edited file.
    data = {
        "settings": {
            "mirror_dir": str(cfg.settings.mirror_dir),
            "database": str(cfg.settings.database),
            "max_connections": cfg.settings.max_connections,
            # Omitted at the default, like the network flags above: a
            # hand-edited file should only say what was actually chosen.
            **({"theme": cfg.settings.theme}
               if cfg.settings.theme != DEFAULT_THEME else {}),
            **{key: getattr(cfg.settings, key) for key in _DISK_KEYS
               if getattr(cfg.settings, key) != Settings.model_fields[key].default},
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
    target = path.resolve() if disk_io.ops.is_symlink(path) else path
    tmp = target.with_name(target.name + ".tmp")
    try:
        with disk_io.ops.open_file(tmp, "wb") as f:
            tomli_w.dump(data, f)
        if disk_io.ops.exists(target):
            disk_io.ops.chmod(tmp, disk_io.ops.stat(target).st_mode & 0o7777)
        disk_io.ops.replace(tmp, target)
    finally:
        disk_io.ops.unlink(tmp, missing_ok=True)
    return path
