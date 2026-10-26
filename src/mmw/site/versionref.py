"""A version named the way a person names it.

`v3` stays the name. A version can also be named by the bundle that brought it:
its stamp (or the start of one), its date, or its label. Every version records
that bundle's stamp and label, and one bundle's stamp is shared by every
repository it changed, so a date also names a moment across the whole mirror.
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timezone
from typing import Iterable

from mmw.site.db import VersionInfo

_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september",
           "october", "november", "december")
_MONTH = "|".join(sorted({*_MONTHS, *(m[:3] for m in _MONTHS), "sept"}, key=len, reverse=True))
_NUMBER = re.compile(r"v?(\d{1,7})", re.I)            # 8+ digits is the start of a stamp
_STAMP = re.compile(r"(\d{4})(\d{2})(\d{2})(T\d{0,6}Z?)?", re.I)
_ISO = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_DAY_MONTH = re.compile(rf"(\d{{1,4}})[\s\-/]*({_MONTH})\.?", re.I)
_MONTH_DAY = re.compile(rf"({_MONTH})\.?[\s\-/]*(\d{{1,4}})", re.I)
_NUMERIC_DM = re.compile(r"(\d{1,2})/(\d{1,2})")


def _when(stamp: str, with_time: bool = False) -> str:
    try:
        t = time.strptime(stamp, "%Y%m%dT%H%M%SZ")
    except ValueError:
        return stamp
    return time.strftime("%d %b %Y %H:%M" if with_time else "%d %b %Y", t)


def _month(name: str) -> int:
    name = name.lower().rstrip(".")
    return next(i for i, m in enumerate(_MONTHS, 1) if m == name or m[:3] == name[:3])


def _day(year: int, month: int, day: int, text: str) -> str:
    try:
        return date(year, month, day).strftime("%Y%m%d")
    except ValueError:
        raise ValueError(f"{text!r} isn't a date") from None


def _date_prefix(text: str, today: date) -> tuple[str, bool] | None:
    """The stamp prefix a stamp or date names, and whether its year was
    assumed; None when `text` is neither. A day and month alone means the
    most recent one, up to today — never a year further back by surprise."""
    if m := _STAMP.fullmatch(text):
        _day(int(m[1]), int(m[2]), int(m[3]), text)
        return text.upper(), False
    if m := _ISO.fullmatch(text):
        return _day(int(m[1]), int(m[2]), int(m[3]), text), False
    day_month = None
    if m := _NUMERIC_DM.fullmatch(text):
        day_month = int(m[1]), int(m[2])
    elif m := _DAY_MONTH.fullmatch(text):
        day_month = int(m[1]), _month(m[2])
    elif m := _MONTH_DAY.fullmatch(text):
        day_month = int(m[2]), _month(m[1])
    if day_month is None:
        return None
    day, month = day_month
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        raise ValueError(f"{text!r} isn't a date")
    year = today.year if (month, day) <= (today.month, today.day) else today.year - 1
    for y in range(year, year - 8, -1):          # 29 Feb: the last year that had one
        try:
            return date(y, month, day).strftime("%Y%m%d"), True
        except ValueError:
            continue
    raise ValueError(f"{text!r} isn't a date")


def when_list(stamps: list[str]) -> list[str]:
    """Each stamp as a date — with its time where two share a day."""
    days = [s[:8] for s in stamps]
    return [_when(s, days.count(s[:8]) > 1) for s in stamps]


def _kept(versions: list[VersionInfo]) -> str:
    return ", ".join(f"v{v.number} · {_when(v.stamp)}" for v in versions) or "none"


def _other_years(versions: list[VersionInfo], prefix: str) -> str:
    """A hint when a day and month matched nothing this year: the same day in others."""
    same = sorted({v.stamp[:8] for v in versions if v.stamp[4:8] == prefix[4:8]}, reverse=True)
    if not same:
        return ""
    named = ", ".join(f"{s[:4]}-{s[4:6]}-{s[6:]}" for s in same)
    return f" — that day in another year: {named} (say the year)"


def _named(versions: list[VersionInfo], text: str, today: date | None) -> tuple[list[VersionInfo], str]:
    """The versions `text` names (a label, then a stamp or date), and a hint
    for when it names none."""
    label = [v for v in versions if v.snap_label and v.snap_label.lower() == text.lower()]
    if label:
        return label, ""
    dated = _date_prefix(text, today or datetime.now(timezone.utc).date())
    if dated is None:
        return [], ""
    prefix, assumed = dated
    found = [v for v in versions if v.stamp.upper().startswith(prefix)]
    return found, (_other_years(versions, prefix) if assumed and not found else "")


def resolve(versions: list[VersionInfo], text: str, today: date | None = None) -> VersionInfo:
    """The one version `text` names, among a repository's (newest first)."""
    text = text.strip()
    if m := _NUMBER.fullmatch(text):
        number = int(m[1])
        for v in versions:
            if v.number == number:
                return v
        raise ValueError(f"no v{number} kept here — kept: {_kept(versions)}")
    found, hint = _named(versions, text, today)
    if len(found) == 1:
        return found[0]
    if not found:
        raise ValueError(f"no version {text!r} kept here{hint} — kept: {_kept(versions)}")
    named = ", ".join(f"v{v.number} · {_when(v.stamp, True)}" for v in found)
    raise ValueError(f"{text!r} matches {named} — say which: its v-number or its stamp")


def bundle_stamp(versions: Iterable[VersionInfo], text: str, today: date | None = None) -> str:
    """The one bundle `text` names, across every repository's versions."""
    text = text.strip()
    if _NUMBER.fullmatch(text):
        raise ValueError(f"{text!r} is one repository's version — name a bundle by its date, stamp or label")
    versions = list(versions)
    found, hint = _named(versions, text, today)
    stamps = sorted({v.stamp for v in found}, reverse=True)
    if len(stamps) == 1:
        return stamps[0]
    if not stamps:
        known = sorted({v.stamp for v in versions}, reverse=True)
        raise ValueError(f"no bundle {text!r} here{hint} — bundles: {', '.join(when_list(known)) or 'none'}")
    named = ", ".join(f"{_when(s, True)} ({s})" for s in stamps)
    raise ValueError(f"{text!r} matches more than one bundle: {named} — give its stamp")


def as_of(versions: list[VersionInfo], stamp: str) -> VersionInfo | None:
    """The version a repository had once the bundle `stamp` was applied: its
    newest at or before it (None when it arrived later)."""
    return next((v for v in sorted(versions, key=lambda v: v.stamp, reverse=True) if v.stamp <= stamp), None)
