"""What changed between two points: the packages added, removed, upgraded
and downgraded, read from each point's `Packages` indexes.

Both sides use it: `mmw history diff`, `mmw site diff`, the TUI's Changes,
the transfer preview and the far side's apply review. An index is read as a
stream — Debian `main` across four arches is ~250k stanzas — and each
index's parse is cached by its checksum, so successive points that share
indexes parse only what changed.
"""
import bz2
import gzip
import io
import lzma
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock

from mmw.core.apt_index import version_cmp

# Preferred first: the smallest to hold while it is read.
_VARIANTS = (".xz", ".gz", ".bz2", ".lzma", "")
_OPEN = {".xz": lzma.LZMAFile, ".lzma": lzma.LZMAFile, ".gz": lambda f: gzip.GzipFile(fileobj=f),
         ".bz2": bz2.BZ2File, "": lambda f: f}

Change = tuple[str, str, "str | None", "str | None"]       # (package, arch, old, new)


@dataclass(frozen=True)
class PointDiff:
    added: list[Change]
    removed: list[Change]
    upgraded: list[Change]
    downgraded: list[Change]

    @property
    def counts(self) -> tuple[int, int, int, int]:
        return len(self.added), len(self.removed), len(self.upgraded), len(self.downgraded)

    def __bool__(self) -> bool:
        return any(self.counts)


# ── reading ─────────────────────────────────────────────────────────────────

_CACHE: "OrderedDict[str, dict]" = OrderedDict()
_CACHE_ENTRIES = 1_500_000          # stanzas held across cached indexes (~ six Debian mains)
_lock = Lock()


def _indexes(files: dict[str, str]) -> list[str]:
    """One path per Packages index: its preferred variant present."""
    found: dict[str, dict[str, str]] = {}
    for path in files:
        if "/by-hash/" in path:
            continue
        head, _, name = path.rpartition("/")
        for ext in _VARIANTS:
            if name == "Packages" + ext:
                found.setdefault(f"{head}/Packages", {})[ext] = path
    return [next(v[ext] for ext in _VARIANTS if ext in v) for _stem, v in sorted(found.items())]


def _arch_of(path: str) -> str:
    """`binary-amd64` in an index's path: the arch a stanza without one has."""
    for part in reversed(path.split("/")):
        if part.startswith("binary-"):
            return part[len("binary-"):]
    return ""


def _parse(data: bytes, path: str) -> dict[tuple[str, str], str]:
    """(package, arch) → its highest version, streaming: the decompressed
    text is never held whole."""
    ext = next((e for e in _VARIANTS if e and path.endswith(e)), "")
    default_arch = _arch_of(path)
    out: dict[tuple[str, str], str] = {}
    package = version = arch = None

    def emit():
        if package is not None:
            key = (package, arch or default_arch)
            old = out.get(key)
            v = version or ""
            if old is None or version_cmp(v, old) > 0:
                out[key] = v

    try:
        with _OPEN[ext](io.BytesIO(data)) as raw:
            for line in raw:                     # each a line, decompressed as it is read
                if line[:1] in (b"\n", b"\r", b""):
                    emit()
                    package = version = arch = None
                elif line.startswith(b"Package:"):
                    package = line[8:].strip().decode("utf-8", "replace")
                elif line.startswith(b"Version:"):
                    version = line[8:].strip().decode("utf-8", "replace")
                elif line.startswith(b"Architecture:"):
                    arch = line[13:].strip().decode("utf-8", "replace")
    except (OSError, EOFError, lzma.LZMAError, ValueError) as e:
        raise ValueError(f"{path}: unreadable index ({e})") from None
    emit()
    return out


def _cached(checksum: str, path: str, read) -> dict | None:
    with _lock:
        hit = _CACHE.get(checksum)
        if hit is not None:
            _CACHE.move_to_end(checksum)
            return hit
    data = read(path)
    if data is None:
        return None
    parsed = _parse(data, path)
    del data
    with _lock:
        _CACHE[checksum] = parsed
        while len(_CACHE) > 1 and sum(len(v) for v in _CACHE.values()) > _CACHE_ENTRIES:
            _CACHE.popitem(last=False)
    return parsed


def packages_of(files: dict[str, str], read) -> dict[tuple[str, str], str]:
    """(package, arch) → version across a point's Packages indexes (the
    first variant present of each). `read(path)` gives a path's bytes. The
    result is shared with the cache: read it, never change it. An index that
    can't be read is left out."""
    parts = [p for path in _indexes(files) if (p := _cached(files[path], path, read)) is not None]
    if len(parts) == 1:
        return parts[0]
    out: dict[tuple[str, str], str] = {}
    for part in parts:
        for key, v in part.items():
            old = out.get(key)
            if old is None or version_cmp(v, old) > 0:
                out[key] = v
    return out


def clear_cache() -> None:
    with _lock:
        _CACHE.clear()


# ── comparing ───────────────────────────────────────────────────────────────

def diff(a: dict, b: dict) -> PointDiff:
    """From `a` to `b`, each `packages_of` a point."""
    added, removed, upgraded, downgraded = [], [], [], []
    for key in sorted(a.keys() | b.keys()):
        old, new = a.get(key), b.get(key)
        if old is None:
            added.append((*key, None, new))
        elif new is None:
            removed.append((*key, old, None))
        elif old != new:
            order = version_cmp(new, old)
            if order > 0:
                upgraded.append((*key, old, new))
            elif order < 0:
                downgraded.append((*key, old, new))
    return PointDiff(added, removed, upgraded, downgraded)


def summary(d: PointDiff) -> str:
    """'+3 ~37 −1', and '↓N' when something went down."""
    a, r, u, down = d.counts
    return f"+{a} ~{u} −{r}" + (f" ↓{down}" if down else "")


def _n(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def brings(d: PointDiff) -> str:
    """'brings 37 upgrades, 3 new packages, 1 removal' — what the transfer
    preview and the apply review say; 'no package changes' when none."""
    a, r, u, down = d.counts
    words = [w for n, w in ((u, _n(u, "upgrade", "upgrades")), (a, _n(a, "new package", "new packages")),
                            (r, _n(r, "removal", "removals")), (down, _n(down, "downgrade", "downgrades")))
             if n]
    return "brings " + ", ".join(words) if words else "no package changes"


def brings_counts(counts) -> str:
    """`brings` from `PointDiff.counts` (as a bundle's header carries them)."""
    a, r, u, down = counts
    return brings(PointDiff([None] * a, [None] * r, [None] * u, [None] * down))


def lines(d: PointDiff, limit: int | None = None) -> list[str]:
    """Each change on its own line, upgrades first:
    `~ alpha 1.0 → 1.1 (amd64)`, `+ gamma 2.0 (all)`, `− delta 0.5 (amd64)`."""
    out = [f"~ {p} {o} → {n} ({a})" for p, a, o, n in d.upgraded]
    out += [f"↓ {p} {o} → {n} ({a})" for p, a, o, n in d.downgraded]
    out += [f"+ {p} {n} ({a})" for p, a, _o, n in d.added]
    out += [f"− {p} {o} ({a})" for p, a, o, _n in d.removed]
    if limit is not None and len(out) > limit:
        return [*out[:limit], f"… and {len(out) - limit} more"]
    return out


# ── one repository's suites, as both sides' `diff` commands say them ───────────

@dataclass(frozen=True)
class SuiteDiff:
    label: str                 # the repository, as the user knows it
    suite: str
    old: str                   # the two points' dates (stamps)
    new: str
    changes: PointDiff


def describe(diffs, limit: int | None = None) -> str:
    """`Label / jammy: 2026-11-01 … → 2026-11-04 … · +1 ~2 −0`, then each change."""
    from mmw.site.versionref import day_time
    out: list[str] = []
    for d in diffs:
        if out:
            out.append("")
        out.append(f"{d.label} / {d.suite}: {day_time(d.old)} → {day_time(d.new)} · {summary(d.changes)}")
        out += [f"  {line}" for line in lines(d.changes, limit)] or ["  no package changes"]
    return "\n".join(out)


def to_json(diffs) -> list[dict]:
    return [{"label": d.label, "suite": d.suite, "old": d.old, "new": d.new,
             "changes": {k: getattr(d.changes, k) for k in ("added", "removed", "upgraded", "downgraded")}}
            for d in diffs]


def from_json(data) -> list[SuiteDiff]:
    return [SuiteDiff(d["label"], d["suite"], d["old"], d["new"],
                      PointDiff(*([tuple(c) for c in d["changes"].get(k, [])]
                                  for k in ("added", "removed", "upgraded", "downgraded"))))
            for d in data]
