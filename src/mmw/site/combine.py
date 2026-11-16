"""Selections: which slice of a suite each point covers, and combining
slices from different days into one served point.

A point is one upstream `Release` with the files this mirror holds of it —
some architectures and components. Two points of the same `Release` merge
by union (phase 1). Points from different days stay separate; combining
them (amd64 from the 2nd, arm64 from the 1st) needs a `Release` upstream
never signed, so mmw writes one, re-signs it with a site key, and serves
that key beside the repository — and says so before doing it.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from mmw.core import disk_io

# A component's slice: its binary-ARCH or source indexes — and its
# debian-installer/binary-ARCH ones, which go with that architecture's.
_SLICE = re.compile(r"([^/]+)/(debian-installer/)?(binary-([^/]+)|source)/")
_SIGNATURES = ("Release", "InRelease", "Release.gpg")


def selection(files: dict[str, str], track: str) -> dict[str, list[str]]:
    """{"arches": [...], "components": [...]} the point's indexes cover."""
    arches, comps = set(), set()
    prefix = track + "/" if track else ""
    for path in files:
        if not path.startswith(prefix) or "/by-hash/" in path:
            continue
        m = _SLICE.match(path[len(prefix):])
        name = path.rsplit("/", 1)[-1]
        if m and name.startswith(("Packages", "Sources")):
            if m.group(2):                  # installer indexes: their arch's main ones say it
                continue
            comps.add(m.group(1))
            arches.add(m.group(4) or "source")
    return {"arches": sorted(arches), "components": sorted(comps)}


def _combined(db, track_id: int, ident: str) -> dict | None:
    raw = db.get_meta(f"combined:{track_id}:{ident}")
    return json.loads(raw) if raw else None


def _selection_of(db, point_id: int, track: str) -> dict[str, list[str]]:
    """A point's slice — worked out from its files once, then kept."""
    import sqlite3
    got = db.point_selection(point_id)
    if got is None:
        got = selection(db.point_files(point_id), track)
        try:
            db.set_point_selection(point_id, got)
        except sqlite3.OperationalError:            # a busy database (an apply): worked out again next time
            pass
    return got


def notes(db, repo: str) -> dict[int, str]:
    """Point id → what it covers ("amd64,arm64 · main"), what it has that
    what its suite serves lacks, and whether mmw signed it (combined)."""
    from mmw.site.ops import _served_points
    served = _served_points(db, repo)
    out = {}
    for t in db.tracks(repo):
        if not t.name:
            continue
        pts = db.points(t.id)
        if not pts:
            continue
        sel = {p.id: _selection_of(db, p.id, t.name) for p in pts}
        # Compared with what is served (it may not be the newest): else the newest.
        base_id = served.get(t.id) if served.get(t.id) in sel else pts[0].id
        base = sel[base_id]
        for p in pts:
            s = sel[p.id]
            words = [f"{','.join(s['arches']) or '—'} · {' '.join(s['components']) or '—'}"]
            extra = [a for a in s["arches"] if a not in base["arches"]] + \
                    [c for c in s["components"] if c not in base["components"]]
            if p.id != base_id and extra:
                words.append(f"has {', '.join(extra)}; what is served doesn't" if base_id != pts[0].id
                             else f"has {', '.join(extra)}; the newest doesn't")
            made = _combined(db, t.id, p.ident)
            if made:
                words.append(f"mmw-signed ({made['key']}), combined from {', '.join(made['from'])}")
            out[p.id] = " · ".join(words)
    return out


@dataclass
class CombineResult:
    point: int
    suite: str
    key: str
    key_url: str
    sources: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)


def _release_text(db, store, files: dict[str, str], track: str) -> bytes:
    from mmw.core.apt_index import release_body
    if f"{track}/Release" in files:
        return store.path(files[f"{track}/Release"]).read_bytes()
    return release_body(store.path(files[f"{track}/InRelease"]).read_bytes())


_HASH_SECTIONS = ("MD5Sum", "SHA1", "SHA256", "SHA512")


def _parse_release(text: str) -> tuple[list[str], list[str], dict[str, dict[str, str]]]:
    """(header lines, hash sections in order, section → {path: its line})."""
    header, order, sections, current = [], [], {}, None
    for ln in text.splitlines():
        if ln[:1].isspace():
            if current is not None:
                parts = ln.split()
                if len(parts) == 3:
                    sections[current][parts[2]] = ln
            continue
        name = ln.split(":", 1)[0]
        if ln.endswith(":") and name in _HASH_SECTIONS:
            current = name
            order.append(name)
            sections[name] = {}
        else:
            current = None
            header.append(ln)
    return header, order, sections


def _is_index(rel: str) -> bool:
    return "/by-hash/" not in rel and rel.rsplit("/", 1)[-1].startswith(("Packages", "Sources"))


def _set_field(text: str, name: str, value: str | None) -> str:
    lines, done = [], False
    for ln in text.splitlines():
        if ln.startswith(f"{name}:"):
            done = True
            if value is not None:
                lines.append(f"{name}: {value}")
            continue
        lines.append(ln)
    if not done and value is not None:
        at = next((i for i, ln in enumerate(lines) if ln.endswith(":") and not ln.startswith(" ")), len(lines))
        lines.insert(at, f"{name}: {value}")
    return "\n".join(lines) + "\n"


def combine(home: Path, ref: str, suite: str, whens: list[str], key: str) -> CombineResult:
    """One served point from the slices of the points `whens` name (two or
    more) in suite `suite`: each slice's indexes from the newest point that
    has it, pool files their union, a new Release re-signed with site key
    `key`. Then served (the newest point of the suite)."""
    from mmw.core.keys import dearmor
    from mmw.core.publisher.manifest import now_stamp
    from mmw.core.signing import Keyring, SigningError
    from mmw.site.layers import StoreBlobs
    from mmw.site.ops import _reserve, _render, _session, _track_named, resolve_repo
    from mmw.site.timeline import import_points, is_complete
    from mmw.site.tracks import PointDraft, _fields
    from mmw.site.versionref import day, resolve_point

    keyring = Keyring(home)
    try:
        if not any(k.name == key for k in keyring.list()):
            raise ValueError(f"no signing key {key!r} here — make one (mmw site signing-key generate {key}) "
                             "or import one (mmw site signing-key import NAME --file KEY)")
        with _session(home) as (db, roots):
            repo = resolve_repo(db, ref)
            st = db.repo_state(repo)
            store = roots.store(roots.of(st))
            t = _track_named(db, repo, suite)
            chosen = {}
            for w in whens:
                p = resolve_point(db.points(t.id), w)
                chosen[p.id] = p
            if len(chosen) < 2:
                raise ValueError("combining takes two or more points of the suite — name each by its date")
            for p in chosen.values():
                if not p.held or is_complete(db, store, p.id, cached=False):
                    raise ValueError(f"{suite} {day(p.date)} isn't complete here — its files are needed")
            sources = sorted(chosen.values(), key=lambda p: (p.date, p.stamp), reverse=True)   # newest first
            prefix = t.name + "/"
            files_of = {p.id: db.point_files(p.id) for p in sources}
            # Each slice from the newest chosen point holding its index; other suite files newest-wins.
            def slice_of(path: str) -> str | None:
                m = _SLICE.match(path[len(prefix):]) if path.startswith(prefix) else None
                # An installer's indexes are their architecture's slice: one day for both.
                return f"{m.group(1)}/{m.group(3)}/" if m else None

            owner: dict[str, int] = {}
            for p in sources:                                          # newest first: the first wins
                for path in files_of[p.id]:
                    sl = slice_of(path)
                    if sl is not None and _is_index(path[len(prefix):]):
                        owner.setdefault(sl, p.id)
            if not owner:
                raise ValueError(f"{suite} has no architecture or component slices to combine — a flat "
                                 "repository is one slice: serve the point you want instead")
            suite_files: dict[str, str] = {}
            for p in sources:
                for path, cs in sorted(files_of[p.id].items()):
                    if not path.startswith(prefix):
                        continue
                    rel = path[len(prefix):]
                    if "/by-hash/" in rel or rel in _SIGNATURES:
                        continue
                    sl = slice_of(path)
                    if sl is None or owner.get(sl) == p.id:
                        suite_files.setdefault(path, cs)
            pool: dict[str, str] = {}
            warnings: list[str] = []
            for p in sources:
                for path, cs in files_of[p.id].items():
                    if path.startswith(prefix):
                        continue
                    if path in pool and pool[path] != cs:
                        warnings.append(f"{path} differs between the days chosen — serving the newest's; "
                                        "an older slice naming it will not verify it")
                    pool.setdefault(path, cs)
            # The new Release: each path's entries from the Release of the point it comes from.
            parsed = {p.id: _parse_release(_release_text(db, store, files_of[p.id], t.name).decode("utf-8", "replace"))
                      for p in sources}
            header, order, _ = parsed[sources[0].id]
            entries: dict[str, list[str]] = {}
            for section in order:
                listed = sorted({path for p in sources for path in parsed[p.id][2].get(section, {})})
                lines = []
                for rel in listed:
                    sl = slice_of(prefix + rel)
                    if sl is not None:
                        src = owner.get(sl)
                    else:
                        src = next((p.id for p in sources if prefix + rel in files_of[p.id]), None) or \
                            next(p.id for p in sources if rel in parsed[p.id][2].get(section, {}))
                    line = parsed[src][2].get(section, {}).get(rel) if src is not None else None
                    if line is not None:
                        lines.append(line)
                entries[section] = lines
            sel = selection(suite_files, t.name)
            text = "\n".join(header) + "\n"
            if sel["arches"] and [a for a in sel["arches"] if a != "source"]:
                text = _set_field(text, "Architectures", " ".join(a for a in sel["arches"] if a != "source"))
            if sel["components"]:
                text = _set_field(text, "Components", " ".join(sel["components"]))
            for gone in ("Valid-Until", "Acquire-By-Hash", "Signed-By"):
                text = _set_field(text, gone, None)
            release = (text + "".join(f"{section}:\n" + "".join(f"{ln}\n" for ln in entries[section])
                                      for section in order)).encode()
            try:
                inrelease = keyring.clearsign(key, release)
                detached = keyring.detach_sign(key, release)
                public = dearmor(keyring.public_armor(key))
            except SigningError as e:
                raise ValueError(f"signing with {key!r} failed: {e}") from None
            blobs = StoreBlobs(store)
            base = next((b for b in sorted(st.bases, key=len, reverse=True)
                         if t.name.startswith(b + "/") or t.name == b), None)
            if base is None:
                raise ValueError(f"{suite} isn't under any base of {st.label}")
            files = {**pool, **suite_files}
            sizes = {}
            stored = [cs for cs in (hashlib.sha256(b).hexdigest() for b in (release, inrelease, detached, public))
                      if not store.has(cs)]                       # new here: gone again if this fails
            for rel, body in ((f"{t.name}/Release", release), (f"{t.name}/InRelease", inrelease),
                              (f"{t.name}/Release.gpg", detached), (f"{base}/mmw-keys/{key}.gpg", public)):
                cs = hashlib.sha256(body).hexdigest()
                blobs.add_bytes(body, cs, rel.rsplit("/", 1)[-1])
                files[rel] = cs
                sizes[cs] = len(body)
            sizes.update(db.blob_sizes(set(files.values()) - set(sizes)))
            stamp = now_stamp()
            ident = "combined:" + hashlib.sha256(release).hexdigest()
            # Dated as the newest day it holds: a later upstream Release still outranks it.
            draft = PointDraft(t.name, ident, sources[0].date, _fields(release.decode("utf-8", "replace")), files)
            try:
                (outcome,) = import_points(db, store, repo, st.label, st.bases, [(draft, stamp, None)],
                                           sizes, set())
                if outcome.point is None or outcome.status == "conflict":
                    raise ValueError(f"the combined point was refused: {outcome.note}")
            except BaseException:
                keep = roots.referenced(db, roots.of(st))
                for cs in stored:                                 # what this left in the store, unowned
                    if cs not in keep:
                        disk_io.ops.unlink(store.path(cs), missing_ok=True)
                raise
            labels = [day(p.date) for p in sorted(sources, key=lambda p: p.date)]
            db.set_meta(f"combined:{t.id}:{ident}", json.dumps({"key": key, "from": labels}))
            switched = _reserve(db, roots, repo)
            _render(roots, db)
            lines = [f"  {st.label}  {suite}: {w}" for w in (*warnings, *switched.warnings)]
            pinned = db.track(t.id).pinned
            if pinned is not None and pinned != outcome.point:
                lines.append(f"  {st.label}  {suite} is pinned to {day(db.point(pinned).date)} — serve the "
                             f"combined point: mmw site serve '{st.label}' {ident.split(':', 1)[1][:12]} "
                             f"--track {suite}")
    finally:
        keyring.stop_agent()
    return CombineResult(outcome.point, suite, key, f"{base}/mmw-keys/{key}.gpg", labels, lines)
