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

from mmw.core.config import SourceConfig, SuiteOverride, derive_name, normalize_url


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


# A deb822 field header: an unindented `Name:` at the start of a line. Matched
# against the line, never the blob — `# see Types: below` in a comment is not a
# field, and a one-line `deb http://…` is not one either (the text before its
# first colon carries a space, so it cannot be a field name).
_FIELD_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]*\s*:")


def parse_apt_sources(text: str) -> list[AptEntry]:
    """Parse either apt format — or both at once — into every entry declared.

    **One input commonly holds both.** `mmw add -f /etc/apt/sources.list.d/`
    is the documented headline workflow and that directory is exactly where a
    deb822 `ubuntu.sources` sits beside a one-line `docker.list`: the format
    is per *file*, and joining them gives a blob that is neither. Picking one
    parser for the whole blob — by substring-testing for `types:` anywhere in
    it, comments included — silently dropped every `deb ` line in a directory
    that contained any `.sources` file at all, and exited 0.

    So the blob is split into records first and each is given to the parser
    that fits it.
    """
    text = _unescape(text)
    if not text:
        return []
    deb822_text, one_line_text = _split_formats(text)
    entries: list[AptEntry] = []
    if deb822_text.strip():
        entries.extend(_parse_deb822(deb822_text))
    if one_line_text.strip():
        entries.extend(_parse_one_line(one_line_text))
    return entries


def _split_formats(text: str) -> tuple[str, str]:
    """Separate a mixed blob into (deb822 text, one-line text).

    deb822 lines are kept together in one string — blank-line separators and
    all — rather than split into per-stanza blocks, so `_parse_deb822` can
    still pair a `Types: deb-src` record with the `Types: deb` record it
    accompanies.
    """
    lines_822: list[str] = []
    one_liners: list[str] = []
    in_822 = False

    def end_record() -> None:
        nonlocal in_822
        if in_822:
            lines_822.append("")      # blank line: ends the deb822 record
            in_822 = False

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            end_record()
            continue
        if in_822 and raw[:1] in (" ", "\t"):
            lines_822.append(raw)     # continuation (an inline Signed-By: block)
            continue
        if stripped.startswith("#"):
            (lines_822 if in_822 else one_liners).append(raw)
            continue
        if stripped.split(None, 1)[0].lower() in ("deb", "deb-src"):
            end_record()
            one_liners.append(raw)
            continue
        if _FIELD_RE.match(stripped):
            in_822 = True
            lines_822.append(raw)
            continue
        (lines_822 if in_822 else one_liners).append(raw)

    return "\n".join(lines_822), "\n".join(one_liners)


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
    # **Grouped by identity, not by the raw string.** `config.merge_sources()`
    # and `Config`'s uniqueness check both key on `normalize_url`, so grouping
    # on the literal URL here previewed two sources where `save()` would write
    # one — contradicting the "a preview always matches what gets written"
    # rule `source_to_dict()` exists to keep. The first spelling seen wins as
    # the URL actually written.
    grouped: dict[str, list[AptEntry]] = {}
    urls: dict[str, str] = {}
    for e in entries:
        key = normalize_url(e.url)
        urls.setdefault(key, e.url)
        grouped.setdefault(key, []).append(e)

    taken: set[str] = set()
    sources: list[SourceConfig] = []
    for key, group in grouped.items():
        url = urls[key]
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
    src_keys: set[tuple[str, str | None]] = set()
    for stanza in _stanzas(text):
        got, keys = _parse_stanza(stanza)
        entries.extend(got)
        src_keys |= keys

    # A `Types: deb-src` record is not its own mirror — it turns on sources for
    # the binary record it accompanies, exactly as a `deb-src` *line* does.
    # Only the one-line door paired them, so the same repository imported from
    # a .sources file silently lost `include_sources`.
    for e in entries:
        if (e.url, e.suite) in src_keys:
            e.include_sources = True
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


def _parse_stanza(stanza: str) -> tuple[list[AptEntry], set[tuple[str, str | None]]]:
    """One record → (binary entries, the (url, suite) keys a `deb-src` record
    names). A deb-src-only record contributes no mirror, only keys."""
    fields = _parse_deb822_fields(stanza)
    none: tuple[list[AptEntry], set[tuple[str, str | None]]] = ([], set())

    if fields.get("enabled", "").lower() in ("no", "false"):
        return none        # source switched off on the source machine
    types = fields.get("types", "").split()
    uris = [u for u in fields.get("uris", "").split() if _is_mirrorable(u)]
    if not uris:
        return none

    suites = fields.get("suites", "").split()
    if "deb" not in types:
        # deb-src-only stanza: nothing to mirror as binaries, but it still says
        # "this repository's sources are wanted".
        if "deb-src" not in types:
            return none
        return [], {
            _flat_target(u.rstrip("/"), suite)
            for u in uris for suite in (suites or [None])
        }
    components = fields.get("components", "").split()
    arches = _split_list(fields.get("architectures", ""))
    include_sources = "deb-src" in types

    # `Signed-By:` is either a keyring path (nothing to mirror — the key is
    # already on the machine) or the armoured key inline, which is what
    # Launchpad PPAs ship. Inline key material is worth keeping: on an airgapped
    # network the key URL is exactly what cannot be reached.
    gpg_key, gpg_key_name = _signed_by(fields.get("signed-by", ""))

    entries = []
    for uri in uris:
        # apt fills `$(ARCH)`/`$ARCH` in from the *local machine's* dpkg
        # architecture. deb822 is Ubuntu 24.04+'s default format, and this door
        # never checked for the token at all — so the literal `$(ARCH)` was
        # written into mmw.toml and every index request 404'd. Same call, same
        # result as the one-line door.
        placeholder = detect_arch_placeholder(uri)
        for suite in suites or [None]:
            url, dist = _flat_target(uri.rstrip("/"), suite)
            entries.append(AptEntry(
                url=url,
                suite=dist,
                components=list(components),
                arches=list(arches),
                include_sources=include_sources,
                gpg_key=gpg_key,
                gpg_key_name=gpg_key_name,
                arch_placeholder=placeholder,
            ))
    return entries, set()


# ── classic one-line ──────────────────────────────────────────────────────────

def _parse_one_line(text: str) -> list[AptEntry]:
    entries: list[AptEntry] = []
    src_keys: set[tuple[str, str | None]] = set()

    for line in text.splitlines():
        # apt strips from `#` to end-of-line *anywhere*, not only at the start.
        # Without this, `… jammy main restricted # added by me` mirrored four
        # extra "components" — `dists/jammy/#/`, `dists/jammy/added/` and so on
        # — one aborted sync unit each.
        line = line.split("#", 1)[0].strip()
        if not line:
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
    gpg_key: str | None = None
    gpg_key_name: str | None = None
    if rest.startswith("["):
        end = rest.find("]")
        if end == -1:
            return None
        options = rest[1:end]
        for opt in options.split():
            if opt.startswith("arch="):
                arches = _split_list(opt[5:])
        # The same handle the deb822 door records. Docker and Kubernetes still
        # publish one-liners, and `gpg_key_name` is what `mmw add --keys-dir`,
        # the review pane's directory scan and KeyPage all match a supplied
        # keyring on — so dropping it made the whole supply-the-key-later
        # workflow unreachable for every one-line config.
        marker = options.find("signed-by=")
        if marker != -1:
            value = options[marker + len("signed-by="):]
            # Options are whitespace-separated, so a value is one token — unless
            # it is armour, which carries spaces of its own and can only be the
            # last thing in the block.
            if not value.lstrip().startswith("-----BEGIN PGP"):
                value = value.split()[0] if value.split() else ""
            gpg_key, gpg_key_name = _signed_by(value)
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
    url, dist = _flat_target(parts[0].rstrip("/"), parts[1])
    return AptEntry(
        url=url,
        suite=dist,
        components=parts[2:] if dist is not None else [],
        arches=arches,
        gpg_key=gpg_key,
        gpg_key_name=gpg_key_name,
        arch_placeholder=placeholder,
    )


def _signed_by(value: str) -> tuple[str | None, str | None]:
    """A `Signed-By:` / `signed-by=` value → (inline armour, keyring basename).

    Either the armoured key itself — what Launchpad PPAs ship, and worth
    keeping, because on an airgapped network the key URL is exactly what
    cannot be reached — or a path to a keyring on the machine the config came
    from. That file is the one thing a config cannot carry, so the basename is
    remembered as the handle that lets it be supplied later.
    """
    value = value.strip()
    if not value:
        return None, None
    if "BEGIN PGP PUBLIC KEY BLOCK" in value:
        return value, None
    if "\n" in value:
        return None, None
    name = value.split(",")[0].rsplit("/", 1)[-1].strip()
    return None, name or None


def _flat_target(url: str, distribution: str | None) -> tuple[str, str | None]:
    """Apply apt's exact-path rule: **any** distribution ending in `/` names a
    flat repository, not a suite under `dists/`.

    Only a bare `/` used to be recognised, so `deb http://v.example/repo ./`
    — the spelling in most vendor install docs — became a suite literally
    called `./` and every index request went to `dists/.//`. A non-root form
    like `stable/` names a path *relative to the URL*, so it folds into the
    URL rather than being dropped.
    """
    if distribution is None or not distribution.endswith("/"):
        return url, distribution
    rel = distribution.rstrip("/")
    while rel.startswith("./"):
        rel = rel[2:]
    rel = "" if rel == "." else rel.strip("/")
    return (f"{url}/{rel}" if rel else url), None


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


# ── a sources file given by URL ──────────────────────────────────────────────

def is_sources_file_url(url: str) -> bool:
    """True for the URL of an apt sources file (`.list` / `.sources`) — what
    NVIDIA's install docs `curl` — rather than of a repository: it is read like
    a pasted config, never probed as a repository."""
    from mmw.core.upstream import upstream_rel
    path = url.split("?", 1)[0].split("#", 1)[0].lower()
    return path.endswith((".list", ".sources")) and upstream_rel(url) is not None


def remember_sources_url(targets, url: str) -> None:
    """Keep the sources file a repository was added from as one of its
    `extra_files`, so the vendor's own `curl …/foo.list` works against the
    mirror. `targets` are SourceConfigs or RepositoryDrafts; a URL that cannot
    be served at its own path (a query string, no file name) is skipped."""
    from mmw.core.upstream import upstream_rel
    if upstream_rel(url) is None:
        return
    for t in targets:
        if url not in t.extra_files:
            t.extra_files = [*t.extra_files, url]
