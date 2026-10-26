"""A version named the way a person names it.

`v3` stays the name. A version can also be named by the bundle that brought it:
its stamp (or the start of one), its date, or its label. Every version records
that bundle's stamp and label, and one bundle's stamp is shared by every
repository it changed, so a date also names a moment across the whole mirror.
"""
from __future__ import annotations

import re
import time
from typing import Iterable

from mmw.site.db import VersionInfo

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_NUMBER = re.compile(r"v?(\d{1,7})", re.I)            # 8+ digits is the start of a stamp
_STAMP = re.compile(r"\d{8}(T\d{0,6}Z?)?", re.I)
_ISO = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_DAY_MONTH = re.compile(r"(\d{1,2})[\s\-/]*([a-z]{3,})", re.I)
_MONTH_DAY = re.compile(r"([a-z]{3,})[\s\-/]*(\d{1,2})", re.I)
_NUMERIC_DM = re.compile(r"(\d{1,2})/(\d{1,2})")


def _when(stamp: str, with_time: bool = False) -> str:
    try:
        t = time.strptime(stamp, "%Y%m%dT%H%M%SZ")
    except ValueError:
        return stamp
    return time.strftime("%d %b %Y %H:%M" if with_time else "%d %b %Y", t)


def _month(name: str) -> int | None:
    name = name[:3].lower()
    return _MONTHS.index(name) + 1 if name in _MONTHS else None


def _day_month(text: str) -> tuple[int, int] | None:
    for pattern, order in ((_NUMERIC_DM, "dm"), (_DAY_MONTH, "dM"), (_MONTH_DAY, "Md")):
        m = pattern.fullmatch(text)
        if not m:
            continue
        if order == "dm":
            return int(m[1]), int(m[2])
        day, month = (m[1], m[2]) if order == "dM" else (m[2], m[1])
        number = _month(month)
        if number is not None:
            return int(day), number
    return None


def _dated(versions: list[VersionInfo], text: str) -> list[VersionInfo] | None:
    """The versions a stamp or a date names; None when `text` is neither."""
    if _STAMP.fullmatch(text):
        return [v for v in versions if v.stamp.upper().startswith(text.upper())]
    if m := _ISO.fullmatch(text):
        prefix = f"{int(m[1]):04d}{int(m[2]):02d}{int(m[3]):02d}"
        return [v for v in versions if v.stamp.startswith(prefix)]
    if dm := _day_month(text):
        found = [v for v in versions if v.stamp[4:8] == f"{dm[1]:02d}{dm[0]:02d}"]
        newest_year = max((v.stamp[:4] for v in found), default=None)
        return [v for v in found if v.stamp[:4] == newest_year]
    return None


def _kept(versions: list[VersionInfo]) -> str:
    return ", ".join(f"v{v.number} · {_when(v.stamp)}" for v in versions) or "none"


def resolve(versions: list[VersionInfo], text: str) -> VersionInfo:
    """The one version `text` names, among a repository's (newest first)."""
    text = text.strip()
    if m := _NUMBER.fullmatch(text):
        number = int(m[1])
        for v in versions:
            if v.number == number:
                return v
        raise ValueError(f"no v{number} kept here — kept: {_kept(versions)}")
    found = _dated(versions, text)
    if not found:                       # not a date, or a date with nothing on it: perhaps a label
        found = [v for v in versions if v.snap_label == text]
    if len(found) == 1:
        return found[0]
    if not found:
        raise ValueError(f"no version {text!r} kept here — kept: {_kept(versions)}")
    named = ", ".join(f"v{v.number} · {_when(v.stamp, True)}" for v in found)
    raise ValueError(f"{text!r} matches {named} — say which: its v-number or its stamp")


def bundle_stamp(versions: Iterable[VersionInfo], text: str) -> str:
    """The one bundle `text` names, across every repository's versions."""
    text = text.strip()
    if _NUMBER.fullmatch(text):
        raise ValueError(f"{text!r} is one repository's version — name a bundle by its date, stamp or label")
    versions = list(versions)
    found = _dated(versions, text)
    if not found:                       # not a date, or a date with nothing on it: perhaps a label
        found = [v for v in versions if v.snap_label == text]
    stamps = sorted({v.stamp for v in found}, reverse=True)
    if len(stamps) == 1:
        return stamps[0]
    if not stamps:
        known = sorted({v.stamp for v in versions}, reverse=True)
        raise ValueError(f"no bundle {text!r} here — bundles: {', '.join(_when(s) for s in known) or 'none'}")
    named = ", ".join(f"{_when(s, True)} ({s})" for s in stamps)
    raise ValueError(f"{text!r} matches more than one bundle: {named} — give its stamp")


def as_of(versions: list[VersionInfo], stamp: str) -> VersionInfo | None:
    """The version a repository had once the bundle `stamp` was applied: its
    newest at or before it (None when it arrived later)."""
    return next((v for v in sorted(versions, key=lambda v: v.stamp, reverse=True) if v.stamp <= stamp), None)
