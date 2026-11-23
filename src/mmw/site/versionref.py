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
from datetime import date, datetime, timezone

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
_HEX = re.compile(r"[0-9a-f]{6,64}", re.I)


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


def bundle_stamp(versions: Iterable[VersionInfo], text: str, today: date | None = None) -> str:
    """The one bundle `text` names — its date, stamp or label — across every
    repository's versions."""
    text = text.strip()
    if _NUMBER.fullmatch(text) and text.lower().startswith("v"):
        raise ValueError(f"{text!r} isn't a bundle — name it by its date, stamp or label")
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


def short(ident: str) -> str:
    """A point's ident as people see it: six hex characters."""
    return ident.split(":", 1)[-1][:6]


def _points_kept(points) -> str:
    return ", ".join(f"{_when(p.date)} · {short(p.ident)}" for p in points) or "none"


def resolve_point(points, text: str, today: date | None = None):
    """The one point `text` names, among a track's (newest first): a label,
    a date or stamp (of the Release, else of the bundle it came in), or six
    or more hex characters of its digest."""
    text = text.strip()
    if _NUMBER.fullmatch(text) and text.lower().startswith("v"):
        raise ValueError(f"versions are named by date now — e.g. 2026-11-03, or a label; kept: "
                         f"{_points_kept(points)}")
    found = [p for p in points if p.snap_label and p.snap_label.lower() == text.lower()]
    hint = ""
    if not found:
        dated = _date_prefix(text, today or datetime.now(timezone.utc).date())
        if dated is not None:
            prefix, assumed = dated
            found = [p for p in points if p.date.upper().startswith(prefix)]
            if not found:
                found = [p for p in points if p.stamp.upper().startswith(prefix)]
            if not found and assumed:
                hint = _other_years([VersionInfo(0, "", 0, "", "", p.date, None) for p in points], prefix)
        elif _HEX.fullmatch(text):
            found = [p for p in points if p.ident.split(":", 1)[-1].lower().startswith(text.lower())]
    if len(found) == 1:
        return found[0]
    if not found:
        raise ValueError(f"no point {text!r} kept here{hint} — kept: {_points_kept(points)}")
    named = ", ".join(f"{_when(p.date, True)} · {short(p.ident)}" for p in found)
    raise ValueError(f"{text!r} matches {named} — say which: its digest or its time")


def as_of_points(points, when: str, today: date | None = None):
    """The newest point dated on or before `when` (the whole of that day, or
    up to that stamp); None when every point is later."""
    dated = _date_prefix(when.strip(), today or datetime.now(timezone.utc).date())
    if dated is None:
        raise ValueError(f"{when!r} isn't a date or stamp")
    prefix = dated[0]
    within = [p for p in points if p.date.upper()[:len(prefix)] <= prefix]
    return max(within, key=lambda p: (p.date, p.stamp, p.ident), default=None)


def is_when(text: str, today: date | None = None) -> bool:
    """Whether `text` reads as a date or stamp (else a label or a digest)."""
    try:
        return _date_prefix(text.strip(), today or datetime.now(timezone.utc).date()) is not None
    except ValueError:
        return False


_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")


def check_label(name: str) -> str:
    """A point's name as both sides take it — a word that never reads as a
    date, a version or a digest — or ValueError saying why not."""
    name = name.strip()
    if not _LABEL.fullmatch(name):
        raise ValueError(f"{name!r} can't name a point — use letters, digits and . _ + - "
                         "(up to 64, no spaces), e.g. golden-q4")
    if is_when(name) or _NUMBER.fullmatch(name) or (_HEX.fullmatch(name) and len(name) >= 6):
        raise ValueError(f"{name!r} reads as a date, a version or a digest — pick a word, e.g. golden-q4")
    return name


def day(stamp: str) -> str:
    return f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}" if len(stamp) >= 8 else stamp


def day_time(stamp: str) -> str:
    """20261120T000000Z → 2026-11-20 00:00:00 UTC (a CLI line's date and time)."""
    try:
        t = time.strptime(stamp, "%Y%m%dT%H%M%SZ")
    except ValueError:
        return day(stamp)
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", t)


def short_day(stamp: str) -> str:
    """20261024T101500Z → 24 Oct (with its year when not this one), as TUI
    tables show a date."""
    try:
        t = time.strptime(stamp[:8], "%Y%m%d") if len(stamp) == 8 else time.strptime(stamp, "%Y%m%dT%H%M%SZ")
    except ValueError:
        return stamp
    return time.strftime("%d %b" + ("" if t.tm_year == time.gmtime().tm_year else " %Y"), t)


def name_of(point) -> str:
    """A point's full digest: what a program passes to name it — six hex
    characters can read as a date (123dec), the whole digest never can."""
    return point.ident.split(":", 1)[-1]
