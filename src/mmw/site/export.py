"""A far side's export: its repositories' timeline points, for another
network (format 3).

What goes is chosen per target: by default every held, complete point not
yet sent there (the export ledger), with only the files those need beyond
what went before; `since` a date, or `all`. Incomplete points and deleted
ones (stubs) never go — they are said instead.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from mmw.site.db import PointInfo, SiteDB, TrackInfo
from mmw.site.tracks import content_ident


@dataclass
class RepoExport:
    key: str
    label: str
    bases: list[str]
    points: list[tuple[TrackInfo, PointInfo, dict[str, str]]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _wanted(states, repos: set[str] | None):
    if repos is None:
        return states
    want = {r.lower() for r in repos}
    return [s for s in states if s.repo.lower() in want or s.label.lower() in want]


def _since_prefix(since: str) -> str:
    from mmw.site.versionref import _date_prefix
    found = _date_prefix(since.strip(), datetime.now(timezone.utc).date())
    if found is None:
        raise ValueError(f"{since!r} isn't a date")
    return found[0]


def plan_root_export(home: Path, target: str, *, repos: set[str] | None, mode: str,
                     since: str | None) -> list[RepoExport]:
    """This root's repositories' points to send `target`. `mode`: new | since | all."""
    from mmw.site.ops import _ensure_migrated, _roots, suite_of
    from mmw.site.timeline import is_complete
    from mmw.site.versionref import day
    if mode not in ("new", "since", "all"):
        raise ValueError(f"unknown export mode {mode!r}")
    prefix = _since_prefix(since) if mode == "since" else None
    _ensure_migrated(home)
    roots = _roots(home)
    out: list[RepoExport] = []
    with SiteDB(home) as db:
        for st in _wanted(db.repo_states(), repos):
            store = roots.store(roots.of(st))
            sent = db.exported(target, st.repo) if mode == "new" else {}
            rx = RepoExport(st.repo, st.label, st.bases)
            for t in db.tracks(st.repo):
                for p in reversed(db.points(t.id, stubs=True)):          # oldest first
                    what = f"{suite_of(t)} {day(p.date)}"
                    files = db.point_files(p.id) if p.held else {}
                    if mode == "new" and p.ident in sent and sent[p.ident] in ("", content_ident(files)):
                        continue                 # went there with these very files (gained some: goes again)
                    if prefix is not None and p.date.upper()[:len(prefix)] <= prefix:
                        continue
                    if not p.held:
                        rx.skipped.append(f"{what}: deleted here")
                        continue
                    # The store itself, not the cached mark: a file lost since import is
                    # said here, not found missing halfway through writing the tar.
                    missing = is_complete(db, store, p.id, cached=False)
                    if missing:
                        rx.skipped.append(f"{what}: incomplete here ({missing} file(s) missing)")
                        continue
                    rx.points.append((t, p, files))
            if rx.points or rx.skipped:
                out.append(rx)
    return out


def export_blobs(home: Path, target: str, plans: list[RepoExport], mode: str) -> dict[str, int]:
    """checksum → size of what the bundle carries: every file of the chosen
    points — less, by default, the files of points already sent there."""
    with SiteDB(home) as db:
        wanted = {c for rx in plans for _t, _p, files in rx.points for c in files.values()}
        if mode == "new":
            for rx in plans:
                sent = db.exported(target, rx.key)
                going = {p.ident for _t, p, _f in rx.points}      # a widened point: its old files went
                for t in db.tracks(rx.key):
                    for ident in sent:
                        if ident in going:
                            continue
                        p = db.point_by_ident(t.id, ident)
                        if p is not None:
                            wanted -= set(db.point_files(p.id).values())
        return db.blob_sizes(wanted)


# ── one root's part, as plain data (the same over ssh) ──────────────────────

def root_export_plan(home: Path, target: str, *, repos: list[str] | None, mode: str,
                     since: str | None) -> dict:
    """What this root sends `target`: its repositories' points with their
    files, the files to carry (checksum → size, from the store itself), each
    exported repository's keys/LABEL/ files, and this mirror's settings."""
    import json
    plans = plan_root_export(home, target, repos=set(repos) if repos else None, mode=mode, since=since)
    wanted = export_blobs(home, target, plans, mode)
    with SiteDB(home) as db:
        settings = db.get_meta("settings")
        # A repository this mirror deployed from a third network has its mirror
        # entry (what nginx serves it by) among the foreign ones, not in its own
        # network's settings: it goes too, or the other side serves it with no entry.
        foreign = json.loads(db.get_meta("foreign_mirrors") or "{}")
        extra = [m for rx in plans if rx.points for m in foreign.get(rx.key, [])]
        network = db.network_files()
        keys = {p: c for p, c in network.items()
                for rx in plans if rx.points and p.startswith(f"keys/{rx.label}/")}
        # Every row's size, sent this time or before: the other side plans its
        # disk space from the manifest.
        every = {c for rx in plans for _t, _p, files in rx.points for c in files.values()} | set(keys.values())
        sizes = db.blob_sizes(every)
    return {"settings": settings, "extra_mirrors": extra, "keys": keys, "sizes": sizes, "send": sorted(wanted),
            "repos": [{"key": rx.key, "label": rx.label, "bases": rx.bases, "skipped": rx.skipped,
                       "points": [{"track": t.name, "ident": p.ident, "date": p.date, "stamp": p.stamp,
                                   "snap_label": p.snap_label,
                                   "fields": {k: v for k, v in (("Suite", t.suite), ("Codename", t.codename)) if v},
                                   "files": files} for t, p, files in rx.points]}
                      for rx in plans]}


# ── the whole mirror's export ────────────────────────────────────────────────

@dataclass
class ExportResult:
    out: str
    target: str
    points: int
    files: int
    bytes: int
    skipped: list[str]
    per_repo: dict[str, list[str]]          # label → "suite date" of each point sent
    new_target: bool
    known_targets: list[str]


def export_bundle(mset, target: str, out: str, *, repos: set[str] | None = None, mode: str = "new",
                  since: str | None = None, dry_run: bool = False, progress=None,
                  stop_event=None, on_plan=None) -> ExportResult:
    """Write `target`'s bundle of this mirror's points to `out` (`-`: stdout).
    The ledger is written on each root only once the tar is whole.
    `on_plan(result)`: once it is planned, before anything is written — what
    is said is what is then written, from one plan."""
    import json
    import sys

    from mmw.core import disk_io
    from mmw.core.publisher.manifest import ManifestHeader, blob_member, now_stamp
    from mmw.core.publisher.members import TarMembers
    from mmw.site.versionref import day
    target = target.strip()
    if not target:
        raise ValueError("say which network it is for: --to NETWORK")
    known: list[str] = []
    for root in mset.roots:
        known += [t for t in root.export_targets() if t not in known]
    parts = [(root, root.export_plan(target, sorted(repos) if repos else None, mode, since)) for root in mset.roots]
    settings = next((p["settings"] for _r, p in parts if p.get("settings")), None)
    # Every root's foreign-deployed repositories' mirror entries, whichever root's settings go.
    extra = [m for _r, p in parts for m in p.get("extra_mirrors") or []]
    if extra:
        data = json.loads(settings or "{}")
        mirrors = data.setdefault("mirrors", [])
        mirrors += [m for m in extra if m not in mirrors]
        settings = json.dumps(data, sort_keys=True)
    header_points: dict = {}
    rows: list = []
    owner: dict[str, object] = {}            # checksum → the root that has it
    sizes: dict[str, int] = {}
    per_repo: dict[str, list[str]] = {}
    skipped: list[str] = []
    sent_rows: dict[str, list[tuple[str, str]]] = {}
    for root, plan in parts:
        for rx in plan["repos"]:
            skipped += [f"{rx['label']} {s}" for s in rx["skipped"]]
            if not rx["points"]:
                continue
            header_points[rx["key"]] = {"label": rx["label"], "bases": rx["bases"],
                                        "points": [{k: v for k, v in pt.items() if k != "files"}
                                                   for pt in rx["points"]]}
            holders: dict[tuple[str, str], list[int]] = {}
            for i, pt in enumerate(rx["points"]):
                for path, c in pt["files"].items():
                    holders.setdefault((path, c), []).append(i)
            rows += [(p, c, rx["key"], plan["sizes"].get(c), tuple(k)) for (p, c), k in sorted(holders.items())]
            per_repo[rx["label"]] = [f"{pt['fields'].get('Suite') or 'files'} {day(pt['date'])}"
                                     for pt in rx["points"]]
            sent_rows.setdefault(root.info.name, []).extend((rx["key"], pt["ident"], content_ident(pt["files"]))
                                                           for pt in rx["points"])
        for c in plan["send"]:
            owner.setdefault(c, root)
            sizes[c] = plan["sizes"].get(c, 0)
        for path, c in sorted(plan["keys"].items()):
            if not any(r[0] == path for r in rows):
                rows.append((path, c, None, plan["sizes"].get(c), ()))
            owner.setdefault(c, root)
            sizes[c] = plan["sizes"].get(c, 0)
    points = sum(len(m["points"]) for m in header_points.values())
    result = ExportResult(out, target, points, len(sizes), sum(sizes.values()), skipped, per_repo,
                          target not in known, known)
    if not points:
        why = (f"; skipped (incomplete or deleted here): {', '.join(skipped)}" if skipped else "")
        if mode == "new":
            raise ValueError(f"nothing new for {target}{why} — --all sends everything again")
        raise ValueError(f"nothing to send to {target}{why}")
    if on_plan is not None:
        on_plan(result)
    if dry_run:
        return result
    network = mset.network
    stamp = now_stamp()
    header = ManifestHeader(network=network, created=stamp, previous=None, full=False,
                            file_count=len(sizes), format=3, points=header_points, settings=settings)
    fields = {k: v for k, v in header.__dict__.items() if v is not None or k == "previous"}
    manifest = (json.dumps(fields, sort_keys=True) + "\n"
                + "".join(json.dumps({"p": p, "c": c, **({"r": r} if r else {}), **({"s": s} if s is not None else {}),
                                      **({"k": list(k)} if k else {})}) + "\n"
                          for p, c, r, s, k in rows)).encode()
    if out == "-":
        writer = TarMembers(sys.stdout.buffer)
    else:
        final = Path(out).expanduser()
        tmp = final.with_name(final.name + ".tmp")
        writer = TarMembers(disk_io.ops.open_file(tmp, "wb"), final_path=final, tmp_path=tmp)
    try:
        writer.add_bytes(".mmw/manifest.jsonl", manifest)
        for c in sorted(sizes):
            if stop_event is not None and stop_event.is_set():
                raise ValueError("stopped — nothing written, nothing recorded as sent")
            with owner[c].export_blob(c) as fh:
                writer.add_blob(blob_member(c), fh, sizes[c])
            if progress:
                progress("file", name=blob_member(c), bytes=sizes[c])
        writer.commit()
    except BaseException:
        writer.abort()
        raise
    # Every root records it, or none does: a root that can't (an ssh drop, a
    # lock) undoes the others' records and the tar goes, so trying again
    # sends the same — never "nothing new" from a root that wrote its record.
    roots = {r.info.name: r for r in mset.roots}
    recorded = []
    try:
        for name, sent in sent_rows.items():
            roots[name].record_export(target, sent, stamp)
            recorded.append(name)
    except BaseException as e:
        undone = []
        for name in recorded:
            try:
                roots[name].forget_export(target, stamp)
                undone.append(name)
            except Exception:  # noqa: BLE001 — said below
                pass
        if out != "-":
            disk_io.ops.unlink(Path(out), missing_ok=True)
        stuck = sorted(set(recorded) - set(undone))
        if not isinstance(e, Exception):
            raise                                   # Ctrl-C stays Ctrl-C, after the undo
        gone = ("so it was removed; try again" if out != "-" else
                "its records were undone: what it streamed counts as not sent, so the next export sends it again")
        raise ValueError(f"the bundle was written, but {next(n for n in sent_rows if n not in recorded)} "
                         f"couldn't record it ({e}) — {gone}"
                         + (f". {', '.join(stuck)} still record it as sent: --all sends it again" if stuck
                            else "")) from e
    return result
