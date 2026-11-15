"""A repository's files, split into tracks: one per upstream `Release`.

A track is the directory holding a `Release`/`InRelease` — a suite
(`<base>/dists/bookworm`) or a flat repository's base. Its point is that
Release (identified by the digest of its text, ordered by its signed
`Date:`) with every file under its directory plus the pool files its
`Packages`/`Sources` indexes name. A pool file several suites name belongs
to each. Files no Release claims (a README, a whole PyPI or VS Code tree)
are the `""` track, identified by content and ordered by the bundle stamp.
"""
from __future__ import annotations

import bz2
import gzip
import hashlib
import lzma
import posixpath
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Callable

from mmw.core.apt_index import release_body

_RELEASE_NAMES = ("Release", "InRelease")
_FIELDS = ("Origin", "Label", "Suite", "Codename")
# One variant of each index is enough: prefer the cheapest to read.
_VARIANTS = ("", ".xz", ".gz", ".bz2", ".lzma")
_DECOMPRESS = {"": lambda b: b, ".xz": lzma.decompress, ".lzma": lzma.decompress,
               ".gz": gzip.decompress, ".bz2": bz2.decompress}
_INDEX_STEMS = ("Packages", "Sources")


@dataclass(frozen=True)
class PointDraft:
    track: str                 # dir holding the Release, or "" for files no Release claims
    ident: str                 # "release:<sha256 of the Release text>" or "content:<sha256 of the rows>"
    date: str                  # YYYYMMDDTHHMMSSZ: the Release's Date:, else the fallback stamp
    fields: dict[str, str]     # Origin, Label, Suite, Codename (those present)
    files: dict[str, str]      # path -> checksum
    unreadable: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default=())


def release_ident(release: bytes | None, inrelease: bytes | None) -> str:
    """The digest naming one upstream Release, the same whether a mirror
    holds `Release`, `InRelease`, or both."""
    text = release if release is not None else release_body(inrelease or b"")
    return "release:" + hashlib.sha256(text).hexdigest()


def release_date(release_text: str, fallback: str) -> tuple[str, str | None]:
    """(the Release's Date: as a stamp, None) — or (fallback, why)."""
    value = next((ln.split(":", 1)[1].strip() for ln in release_text.splitlines()
                  if ln.startswith("Date:")), None)
    if value is None:
        return fallback, "has no Date:"
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        when = when.astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError, OverflowError):
        return fallback, f"has an unreadable Date: {value!r}"
    if not 1900 <= when.year <= 9999:
        return fallback, f"has an impossible Date: {value!r}"
    return when.strftime("%Y%m%dT%H%M%SZ"), None


def _fields(text: str) -> dict[str, str]:
    out = {}
    for ln in text.splitlines():
        if not ln or ln[0].isspace() or ":" not in ln:
            continue
        key, value = ln.split(":", 1)
        if key in _FIELDS:
            out[key] = value.strip()
    return out


def content_ident(files: dict[str, str]) -> str:
    h = hashlib.sha256()
    for path in sorted(files):
        h.update(f"{path}\0{files[path]}\n".encode())
    return "content:" + h.hexdigest()


def _track_dirs(rows: dict[str, str], bases: list[str]) -> list[str]:
    """Dirs holding a top-level Release: a base, or under `<base>/dists/` —
    not a per-component Release inside another track."""
    candidates = sorted({p.rsplit("/", 1)[0] for p in rows if p.rsplit("/", 1)[-1] in _RELEASE_NAMES
                         and "/" in p}, key=len)
    accepted: list[str] = []
    for d in candidates:
        if not any(d == b or d.startswith(b + "/dists/") for b in bases):
            continue
        if any(d.startswith(a + "/") for a in accepted):
            continue
        accepted.append(d)
    return accepted


def _base_of(d: str, bases: list[str]) -> str:
    return max((b for b in bases if d == b or d.startswith(b + "/")), key=len)


def _index_lines(data: bytes, ext: str):
    yield from _DECOMPRESS[ext](data).decode("utf-8", "replace").splitlines()


def _join(base: str, rel: str) -> str:
    """`base/rel` as the rows spell it (`./pool/x.deb` is `pool/x.deb`)."""
    return posixpath.normpath(f"{base}/{rel}")


def _referenced(lines, stem: str, base: str) -> set[str]:
    out: set[str] = set()
    if stem == "Packages":
        for ln in lines:
            if ln.startswith("Filename:"):
                out.add(_join(base, ln.split(":", 1)[1].strip()))
        return out
    directory, in_files = None, False
    for ln in lines:
        if not ln.strip():
            directory, in_files = None, False
        elif ln[0].isspace():
            if in_files and directory is not None:
                parts = ln.split()
                if len(parts) >= 3:
                    out.add(_join(base, f"{directory}/{parts[-1]}"))
        else:
            key = ln.split(":", 1)[0]
            in_files = key == "Files" or key.startswith("Checksums-")
            if key == "Directory":
                directory = ln.split(":", 1)[1].strip()
    return out


def _indexes(rows: dict[str, str], d: str) -> dict[str, list[str]]:
    """Index stem path → its variants present, preferred first."""
    found: dict[str, list[str]] = {}
    for p in rows:
        if not p.startswith(d + "/") or "/by-hash/" in p:
            continue
        name = p.rsplit("/", 1)[-1]
        for stem in _INDEX_STEMS:
            for ext in _VARIANTS:
                if name == stem + ext:
                    found.setdefault(p[: len(p) - len(ext)] if ext else p, []).append(p)
    order = {ext: i for i, ext in enumerate(_VARIANTS)}
    for stem, variants in found.items():
        variants.sort(key=lambda v: order[v[len(stem):]])
    return found


def split_tracks(rows: dict[str, str], bases: list[str],
                 read: Callable[[str], bytes | None], fallback_stamp: str) -> list[PointDraft]:
    """One draft per Release dir, plus a `""` draft for what none claims."""
    drafts: list[PointDraft] = []
    claimed: set[str] = set()
    dirs = _track_dirs(rows, bases)
    # Each row to the track dir above it, in one pass (dirs never nest).
    under: dict[str, dict[str, str]] = {d: {} for d in dirs}
    for path, cs in rows.items():
        parts = path.split("/")
        for k in range(len(parts) - 1, 0, -1):
            d = "/".join(parts[:k])
            if d in under:
                under[d][path] = cs
                break
    for d in dirs:
        base = _base_of(d, bases)
        files = dict(under[d])
        unreadable: list[str] = []
        warnings: list[str] = []
        for stem, variants in _indexes(under[d], d).items():
            for v in variants:
                data = read(v)
                if data is not None:
                    try:
                        names = _referenced(_index_lines(data, v[len(stem):]), stem.rsplit("/", 1)[-1], base)
                    except (OSError, EOFError, lzma.LZMAError, ValueError):
                        continue
                    files.update({p: rows[p] for p in names if p in rows})
                    break
            else:
                unreadable.append(variants[0])
        release_path, inrelease_path = f"{d}/Release", f"{d}/InRelease"
        release = read(release_path) if release_path in rows else None
        inrelease = read(inrelease_path) if inrelease_path in rows and release is None else None
        if release is None and inrelease is None:
            # Not readable here: still one Release, named by its row.
            ident = "release:" + (rows[release_path] if release_path in rows else "inrelease-" + rows[inrelease_path])
            text = ""
            unreadable.append(release_path if release_path in rows else inrelease_path)
        else:
            ident = release_ident(release, inrelease)
            text = (release if release is not None else release_body(inrelease)).decode("utf-8", "replace")
        date, why = release_date(text, fallback_stamp)
        if why:
            warnings.append(f"{d}: Release {why} — ordered by when it arrived")
        drafts.append(PointDraft(d, ident, date, _fields(text), files, tuple(unreadable), tuple(warnings)))
        claimed |= set(files)
    rest = {p: c for p, c in rows.items() if p not in claimed}
    if rest:
        drafts.append(PointDraft("", content_ident(rest), fallback_stamp, {}, rest))
    return drafts
