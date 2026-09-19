"""Import apt configuration files verbatim — no discovery, no network.

A sources.list (or deb822 .sources) already states everything a mirror needs:
URL, suite, components, arch.  Nothing has to be probed, so this module is
pure normalization — both deb formats in, `SourceConfig` objects out.

A deb822 stanza maps onto SourceConfig field for field:

    Types: deb                      →  provider = "apt"
    URIs: http://archive/ubuntu     →  url
    Suites: noble noble-updates     →  suites
    Components: main universe       →  components
    Architectures: amd64            →  arches

The one-line format is the same data denormalized — one line per suite — so
several lines regroup by URL back into a single source.
"""
import re
from dataclasses import dataclass, field, replace

from mmw.core.config import SourceConfig, SuiteOverride, derive_name


@dataclass
class AptEntry:
    """One (url, suite) pair pulled out of an apt config file."""
    url: str
    suite: str | None            # None → flat repo ("deb URL /"), no dists/ tree
    components: list[str] = field(default_factory=list)
    arches: list[str] = field(default_factory=list)
    include_sources: bool = False
    gpg_key: str | None = None       # armoured key from an inline `Signed-By:`
    gpg_key_name: str | None = None  # keyring basename a `Signed-By:` path named
    # apt's own runtime substitution token ("$(ARCH)" or "$ARCH"), if the URL
    # carried one. Left un-substituted in `url` — see `_parse_one_line_body`.
    arch_placeholder: str | None = None


def detect_arch_placeholder(text: str) -> str | None:
    """Which apt runtime-substitution token (`$(ARCH)` or `$ARCH`), if any,
    appears in `text`. Shared by the one-line parser and by anything probing a
    bare URL directly (the TUI's "Probe URL" field never goes through
    `parse_apt_sources` at all, since it isn't a config file) — one place
    recognizing the token, not two copies of the same check."""
    if "$(ARCH)" in text:
        return "$(ARCH)"
    if "$ARCH" in text:
        return "$ARCH"
    return None


def expand_placeholder_entry(entry: AptEntry, archs: list[str]) -> list[AptEntry]:
    """One concrete entry per already-resolved architecture.

    Pure — takes the resolution as input rather than performing it, so this
    stays usable from both the offline `mmw add` path (an `--arch` override
    needs no network) and a network-capable caller (`discover.
    expand_arch_placeholder` via `core.draft`) without duplicating the
    substitution logic in either place.
    """
    if not entry.arch_placeholder:
        return [entry]
    return [
        replace(entry, url=entry.url.replace(entry.arch_placeholder, arch),
                arch_placeholder=None, arches=[arch])
        for arch in archs
    ]


def parse_apt_sources(text: str) -> list[AptEntry]:
    """Parse either apt format into every entry it declares.

    deb822 is detected by its `Types:`/`URIs:` field header; anything else is
    read as classic one-line `deb` records.
    """
    text = _unescape(text)
    if not text:
        return []
    lowered = text.lower()
    if "types:" in lowered or "uris:" in lowered:
        return _parse_deb822(text)
    return _parse_one_line(text)


def entries_to_sources(
    entries: list[AptEntry],
    *,
    label: str | None = None,
    default_arch: str = "",
) -> list[SourceConfig]:
    """Group entries into one SourceConfig per (url, provider).

    Suites of the same URL collapse into a single source; a suite whose
    components differ from the first becomes a SuiteOverride, matching what
    the wizard's build_source_configs() emits.
    """
    grouped: dict[str, list[AptEntry]] = {}
    for e in entries:
        grouped.setdefault(e.url, []).append(e)

    taken: set[str] = set()
    sources: list[SourceConfig] = []
    for url, group in grouped.items():
        flat = group[0].suite is None
        arches = _union(*(e.arches for e in group))
        if not arches and default_arch:
            arches = [default_arch]

        comps_by_suite: dict[str, list[str]] = {}
        for e in group:
            if e.suite is None:
                continue
            comps_by_suite[e.suite] = _union(comps_by_suite.get(e.suite, []), e.components)

        suites = list(comps_by_suite)
        common = comps_by_suite[suites[0]] if suites else []
        overrides = [
            SuiteOverride(suite=s, components=comps_by_suite[s])
            for s in suites[1:]
            if comps_by_suite[s] != common
        ]

        sources.append(SourceConfig(
            name=_unique_name(url, taken, suites),
            provider="apt-flat" if flat else "apt",
            url=url,
            arches=arches,
            suites=suites,
            components=common,
            suite_overrides=overrides,
            include_sources=any(e.include_sources for e in group),
            gpg_key=next((e.gpg_key for e in group if e.gpg_key), None),
            gpg_key_name=next((e.gpg_key_name for e in group if e.gpg_key_name), None),
            **({"label": label} if label else {}),
        ))
    return sources


# ── deb822 ────────────────────────────────────────────────────────────────────

def _parse_deb822(text: str) -> list[AptEntry]:
    """Stanzas are separated by blank lines — parsing the file as one block
    lets a later stanza overwrite an earlier one, which silently drops repos."""
    entries: list[AptEntry] = []
    for stanza in _stanzas(text):
        entries.extend(_parse_stanza(stanza))
    return entries


def _stanzas(text: str) -> list[str]:
    """Split deb822 into records: on blank lines, **and on a repeated `Types:`**.

    `cat /etc/apt/sources.list.d/*.sources` is how a machine's whole apt
    configuration arrives, and those files end with a newline, not a blank
    line — so concatenation glues the last stanza of one file directly onto the
    first of the next and the later `URIs:` silently wins. Measured on a real
    DGX Spark: ten repositories parsed as one.

    deb822 forbids a duplicate field within a record, so a second unindented
    `Types:` can only be a new record. Indented lines are continuations (an
    inline `Signed-By:` key block), and are never treated as a field.
    """
    stanzas: list[str] = []
    current: list[str] = []
    seen_types = False

    def flush() -> None:
        nonlocal seen_types
        if any(line.strip() for line in current):
            stanzas.append("\n".join(current))
        current.clear()
        seen_types = False

    for raw in text.splitlines():
        if not raw.strip():
            flush()
            continue
        if raw[:1] not in (" ", "\t") and ":" in raw:
            key = raw.split(":", 1)[0].strip().lower()
            if key == "types":
                if seen_types:
                    flush()
                seen_types = True
        current.append(raw)
    flush()
    return stanzas


def _parse_deb822_fields(stanza: str) -> dict[str, str]:
    """deb822 fields, honouring continuation lines.

    A line starting with a space or tab **continues the previous field**; a lone
    `.` on such a line is a blank line. Parsing every line independently — as
    this used to — lets the `Version:`/`Comment:` headers inside an inline
    `Signed-By:` PGP block be read as stanza fields, so a pasted key could
    override the real `Suites:`. Launchpad PPAs ship exactly that shape.
    """
    fields: dict[str, str] = {}
    current: str | None = None
    for raw in stanza.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw[:1] in (" ", "\t"):
            if current is None:
                continue
            body = raw.strip()
            fields[current] += "\n" + ("" if body == "." else body)
            continue
        if ":" not in raw:
            current = None
            continue
        key, _, val = raw.partition(":")
        current = key.strip().lower()
        fields[current] = val.strip()
    return fields


def _parse_stanza(stanza: str) -> list[AptEntry]:
    fields = _parse_deb822_fields(stanza)

    if fields.get("enabled", "").lower() in ("no", "false"):
        return []          # source switched off on the source machine
    types = fields.get("types", "").split()
    if "deb" not in types:
        return []          # deb-src-only stanza: nothing to mirror as binaries
    uris = [u for u in fields.get("uris", "").split() if _is_mirrorable(u)]
    if not uris:
        return []

    suites = fields.get("suites", "").split()
    components = fields.get("components", "").split()
    arches = _split_list(fields.get("architectures", ""))
    include_sources = "deb-src" in types

    # `Signed-By:` is either a keyring path (nothing to mirror — the key is
    # already on the machine) or the armoured key inline, which is what
    # Launchpad PPAs ship. Inline key material is worth keeping: on an airgapped
    # network the key URL is exactly what cannot be reached.
    signed_by = fields.get("signed-by", "").strip()
    inline = "BEGIN PGP PUBLIC KEY BLOCK" in signed_by
    gpg_key = signed_by if inline else None
    # A keyring *path* names a file on the source machine. We cannot read it,
    # but remembering the basename lets the user point at a directory later and
    # have it matched up (`/usr/share/keyrings/cuda_debian_prod.gpg`).
    gpg_key_name = None
    if not inline and signed_by and "\n" not in signed_by:
        gpg_key_name = signed_by.rsplit("/", 1)[-1].strip() or None

    entries = []
    for uri in uris:
        for suite in suites or [None]:
            entries.append(AptEntry(
                url=uri.rstrip("/"),
                suite=None if suite in (None, "/") else suite,
                components=list(components),
                arches=list(arches),
                include_sources=include_sources,
                gpg_key=gpg_key,
                gpg_key_name=gpg_key_name,
            ))
    return entries


# ── classic one-line ──────────────────────────────────────────────────────────

def _parse_one_line(text: str) -> list[AptEntry]:
    entries: list[AptEntry] = []
    src_keys: set[tuple[str, str | None]] = set()

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        kind, _, rest = line.partition(" ")
        if kind not in ("deb", "deb-src"):
            continue
        parsed = _parse_one_line_body(rest.strip())
        if parsed is None:
            continue
        if kind == "deb-src":
            src_keys.add((parsed.url, parsed.suite))
        else:
            entries.append(parsed)

    # A deb-src line is not its own mirror — it turns on sources for the
    # binary entry it accompanies.
    for e in entries:
        if (e.url, e.suite) in src_keys:
            e.include_sources = True
    return entries


def _parse_one_line_body(rest: str) -> AptEntry | None:
    arches: list[str] = []
    if rest.startswith("["):
        end = rest.find("]")
        if end == -1:
            return None
        for opt in rest[1:end].split():
            if opt.startswith("arch="):
                arches = _split_list(opt[5:])
        rest = rest[end + 1:].strip()
    # apt fills $(ARCH)/$ARCH in from the *local machine's* dpkg architecture
    # at update time — mmw has no such machine, so guessing one here (this
    # used to hardcode "amd64") silently drops every other architecture a
    # repository serves. Record which token appeared and leave it in the URL
    # verbatim; a network-capable caller (discover.expand_arch_placeholder)
    # resolves it by checking which arch actually exists.
    placeholder = detect_arch_placeholder(rest)
    parts = rest.split()
    if len(parts) < 2 or not _is_mirrorable(parts[0]):
        return None
    suite = parts[1]
    return AptEntry(
        url=parts[0].rstrip("/"),
        suite=None if suite == "/" else suite,
        components=parts[2:],
        arches=arches,
        arch_placeholder=placeholder,
    )


# ── helpers ───────────────────────────────────────────────────────────────────

def _unescape(text: str) -> str:
    """Accept `echo -e` style input carrying literal \\n sequences."""
    text = text.strip()
    if "\\n" in text and "\n" not in text:
        text = text.replace("\\n", "\n").strip()
    return text


def _split_list(raw: str) -> list[str]:
    return [p.strip() for p in re.split(r"[,\s]+", raw) if p.strip()]


def _union(*lists: list[str]) -> list[str]:
    """Order-preserving union."""
    out: list[str] = []
    for items in lists:
        for item in items:
            if item not in out:
                out.append(item)
    return out


def _is_mirrorable(uri: str) -> bool:
    """Only http(s) repos can be mirrored — cdrom:/file:/copy: cannot.

    Rejecting them also contains the damage from a cdrom label like
    `cdrom:[Ubuntu 20.04.4 LTS _Focal Fossa_ ...]/`, whose spaces would
    otherwise be read as one URI per word.
    """
    return uri.lower().startswith(("http://", "https://"))


def _unique_name(url: str, taken: set[str], suites: list[str] | None = None) -> str:
    """derive_name(), remembering what this import already claimed."""
    name = derive_name(url, taken, suites=suites)
    taken.add(name)
    return name
