"""`mmw site …` — mmw on the far side of the airgap. A mirror is a set of
roots; this machine remembers where it has seen them (`mmw site open`)."""
import re
from pathlib import Path

from mmw.commands.common import eprint
from mmw.core import disk_io


def _set(root: Path | None, break_lock: bool = False, *, convert: bool = True):
    """The mirror: the set `root` belongs to, or the one this machine has opened."""
    from mmw.site import recents
    from mmw.site.mset import open_set, root_at
    set_id = None
    if root is not None:
        found = root_at(str(root))
        if found is None:
            raise FileNotFoundError(f"no mirror at {root}")
        recents.remember(str(root))
        set_id = found.info.set_id
    mset = open_set(set_id=set_id)
    if break_lock:
        for r in mset.roots:
            if r.break_lock():
                eprint(f"removed the lock in {r.info.name}")
    if convert:
        from mmw.site import ops
        ops.set_status(mset)
        for notice in conversion_notices(mset):
            eprint(notice)
    return mset


def conversion_notices(mset) -> list[str]:
    """The shared CLI/TUI door to the roots' one-time conversion messages."""
    notices = []
    for root in mset.roots:
        take = getattr(root, "converted_notice", None)
        notice = take() if take else None
        if notice:
            notices.append(f"{root.info.name}: {notice}")
    return notices


def removable_conversion_backups(mset) -> list[str]:
    paths = []
    for root in mset.roots:
        get = getattr(root, "conversion_backups", None)
        paths.extend(f"{root.info.name}: removable conversion backup: {path}" for path in (get() if get else []))
    return paths


def _repo(root: Path | None, repo: str, break_lock: bool = False):
    """The root holding repository `repo`, and its key."""
    from mmw.site import ops
    return ops.on_repo(_set(root, break_lock), repo)


def _fail(e: Exception) -> int:
    from mmw.site.lock import LockHeld
    eprint(str(e).strip("'\""))
    return 5 if isinstance(e, LockHeld) else 1


def _ssh_error():
    from mmw.site.ssh import SshError
    return SshError


_ERRORS = (ValueError, LookupError, OSError, KeyError, _ssh_error())


def _place(specs: list[str]) -> dict[str, str]:
    """`HOST=PATH` (or `*=PATH`) → {hostname: root location}."""
    out = {}
    for spec in specs:
        host, sep, loc = spec.partition("=")
        if not sep or not host or not loc:
            raise ValueError(f"--root wants HOST=PATH (or '*'=PATH), not {spec!r}")
        out[host] = loc
    return out


def apply(src: str, root: list[str] | None = None, forward: list[str] | None = None,
          forward_all: bool = False, dry_run: bool = False, break_lock: bool = False, yes: bool = False,
          deploy: list[str] | None = None) -> int:
    import sys
    from mmw.site.apply import ApplyError, MaybeUnmounted, apply_to_set
    from mmw.site.lock import LockHeld
    from mmw.site.mset import root_at
    try:
        place = _place(root or [])
        at = next((loc for loc in place.values() if root_at(loc) is not None), None)
        try:
            mset = _set(at, break_lock, convert=not dry_run)
        except LookupError:
            mset = None                             # a first apply: every hostname is placed
        if src != "-" and _offer_far_update(src, mset):
            return 0
        keys = None
        if forward and mset is not None:
            from mmw.site import ops
            keys = {ops.on_repo(mset, ref)[1] for ref in forward}
        try:
            r = apply_to_set(mset, src, place=place, forward=keys, forward_all=forward_all, dry_run=dry_run,
                             deploy=set(deploy or ()),
                             apply_anyway=set(place.values()) if yes else None)
        except MaybeUnmounted as e:
            eprint(str(e))
            if not sys.stdin.isatty() and not _interactive():
                eprint("pass --yes to apply anyway (no terminal to ask on)")
                return e.code
            if input("Apply anyway? [y/N] ").strip().lower() not in ("y", "yes"):
                return e.code
            r = apply_to_set(mset, src, place=place, forward=keys, forward_all=forward_all, dry_run=dry_run,
                             deploy=set(deploy or ()),
                             apply_anyway=set(e.locations))
    except ApplyError as e:
        eprint(str(e))
        return e.code
    except (LockHeld, *_ERRORS) as e:
        return _fail(e)
    from mmw.site.apply import nginx_steps
    print(f"applied {r.created}" if not dry_run else f"would apply {r.created}")
    for line in r.lines:
        print(line)
    for action in r.actions:
        print(f"! {action}")
    for name, verb, cmds in nginx_steps(r):
        print(f"! {verb} nginx in {name}:")
        for cmd in cmds:
            print(f"    {cmd}")
    # A refused suite is damage somewhere (the bundle or this mirror): scripts hear it.
    return 3 if any(x.refused or x.refused_suites for x in r.per_root.values()) else 0


def _interactive_far() -> bool:
    import sys
    return (sys.stdin.isatty() and sys.stdout.isatty() and sys.stderr.isatty()) or _interactive()


def _offer_far_update(src: str, mset) -> bool:
    """A newer mmw next to the bundle: asked before applying (the newest code
    applies it). True when mmw was updated and has started again."""
    import os
    from mmw.site.update import apply_far_update, far_offer, preflight
    if os.environ.get("MMW_NO_UPDATE_CHECK") == "1":
        return False
    far = far_offer(src, mset)
    if far is None or far.offer is None:
        return False
    offer = far.offer
    if not _interactive_far():
        eprint(f"mmw {offer.newest} is next to this bundle (you have {offer.current}) — "
               "run mmw site apply in a terminal to update first")
        return False
    from mmw.commands.update import ask
    if not ask(offer):
        return False
    problem = preflight(far)
    if problem:
        eprint(problem)
        return False
    try:
        apply_far_update(far, lambda line: eprint(line))
    except (ValueError, OSError, _ssh_error()) as e:
        eprint(f"the update failed: {e}")
        return False
    eprint(f"mmw updated to {offer.newest} — applying with it now")
    from mmw.tui.update_flow import relaunch
    relaunch(offer.current)                         # the same apply, run by the new mmw
    return True


def _interactive() -> bool:
    """A test's CliRunner gives input on a stdin that isn't a tty; MMW_ASSUME_TTY says to ask anyway."""
    import os
    return os.environ.get("MMW_ASSUME_TTY") == "1"


def open_root(path: str, install: Path | None = None) -> int:
    from mmw.site import recents, ssh
    from mmw.site.mset import root_at
    try:
        kind, host, _p = ssh.parse_location(str(path))
        if install is not None:
            if kind != "ssh":
                raise ValueError("--install pushes mmw to an ssh root (host:/path); here, run install.sh yourself")
            ssh.check_access(host)
            ssh.install_remote(host, install)
            print(f"mmw installed on {host}")
        found = root_at(str(path))
    except _ERRORS as e:
        return _fail(e)
    if found is None:
        eprint(f"no mirror at {path}")
        return 1
    recents.remember(str(path))
    if found.info.set_id is not None:
        try:
            _set(Path(path))
        except _ERRORS as e:
            return _fail(e)
    print(f"opened {found.info.name} — a root of {found.info.network}")
    return 0


def roots(root: Path | None = None) -> int:
    try:
        mset = _set(root)
    except _ERRORS as e:
        return _fail(e)
    print(f"{mset.network}:")
    for r in mset.roots:
        hosts = ", ".join(sorted(r.hostnames())) or "no hostnames yet"
        print(f"  {r.info.name:<12} {r.info.location}  {hosts}")
    for name in mset.missing:
        why = mset.missing_reason.get(name, "")
        hint = why if why and why != "not opened on this machine" else "mount it, then: mmw site open PATH"
        print(f"  {name:<12} MISSING — {hint}")
    return 0




def _when(stamp: str) -> str:
    from mmw.site.versionref import when_list
    return when_list([stamp])[0]


def serve(repo: str, version: str, root: Path | None = None, yes: bool = False,
          break_lock: bool = False, track: str | None = None) -> int:
    import sys
    from mmw.site.lock import LockHeld
    try:
        r_, key = _repo(root, repo, break_lock)
        p = r_.serve_preview(key, version, track)
        print(f"{p.label}: now serving {', '.join(p.current) or 'nothing'}")
        print(f"  would serve {', '.join(p.target)} · relinks {p.relinks} file(s) · nothing to copy · "
              "indexes switch last · pinned until mmw site forward")
        if not yes:
            if not sys.stdin.isatty():
                eprint("pass --yes to serve it (no terminal to ask on)")
                return 1
            if input("Serve it? [y/N] ").strip().lower() not in ("y", "yes"):
                return 1
        r = r_.serve(key, version, track)
    except (LockHeld, *_ERRORS) as e:
        return _fail(e)
    if getattr(r, "held", None):
        eprint(f"{p.label}: not switched — held: {r.held}")
        return 1
    for w in getattr(r, "warnings", ()):
        eprint(w)
    print(f"{p.label} serves {', '.join(p.target)} ({r.placed} placed, {r.removed} removed)")
    return 0


def serve_bundle(when: str, root: Path | None = None, yes: bool = False, break_lock: bool = False) -> int:
    """Serve every repository as it was once the bundle `when` was applied."""
    import sys
    from mmw.site import ops
    from mmw.site.lock import LockHeld
    try:
        steps = ops.bundle_plan(_set(root, break_lock), when)
        for s in steps:
            print(s.line())
        if not any(s.changes for s in steps):
            print("nothing to change")
            return 0
        if not yes:
            if not sys.stdin.isatty():
                eprint("pass --yes to serve it (no terminal to ask on)")
                return 1
            if input("Serve these? [y/N] ").strip().lower() not in ("y", "yes"):
                return 1
        done = ops.serve_bundle(steps)
    except (LockHeld, *_ERRORS) as e:
        return _fail(e)
    print(f"served: {len(done)} repositor{'y' if len(done) == 1 else 'ies'} — each held where not its newest; "
          "mmw site forward --all moves them on")
    return 0


def forward(repo: str | None, root: Path | None = None, all_: bool = False, break_lock: bool = False) -> int:
    from mmw.site.lock import LockHeld
    if repo is None and not all_:
        eprint("name a repository, or --all for every held one")
        return 1
    try:
        if repo is not None:
            r_, key = _repo(root, repo, break_lock)
            moved = r_.forward(key)
        else:
            moved = [m for r in _set(root, break_lock).roots for m in r.forward(None)]
    except (LockHeld, *_ERRORS) as e:
        return _fail(e)
    print(f"moved forward: {len(moved)} repositor{'y' if len(moved) == 1 else 'ies'}")
    return 0


def hold(repo: str, root: Path | None = None, break_lock: bool = False) -> int:
    from mmw.site.lock import LockHeld
    try:
        r_, key = _repo(root, repo, break_lock)
        label = r_.hold(key)
    except (LockHeld, *_ERRORS) as e:
        return _fail(e)
    print(f"{label} is held: bundles store its new versions without serving them")
    return 0


def space(root: Path | None = None) -> int:
    from mmw.core.format import fmt_size
    from mmw.site import ops
    try:
        reports = ops.set_space(_set(root))
    except _ERRORS as e:
        return _fail(e)
    history = 0
    for name, rep in reports.items():
        for repo, rows in rep.repos.items():
            print(rep.labels[repo])
            for v in rows:
                tag = "serving" if v.serving else f"frees {fmt_size(v.frees)}"
                label = f" {v.snap_label}" if v.snap_label else ""
                print(f"  {v.suite:<20} {_day_time(v.date)}  {v.ident.split(':', 1)[-1][:6]}{label}  "
                      f"{fmt_size(v.total):>10}  {tag}")
        for _n, (used, free) in rep.roots.items():
            print(f"root {name}: {fmt_size(used)} used, {fmt_size(free)} free")
        history += rep.history_bytes
    print(f"older points hold {fmt_size(history)}")
    return 0


def delete(repo: str, version: str, root: Path | None = None, yes: bool = False,
           break_lock: bool = False, track: str | None = None) -> int:
    import sys
    from mmw.core.format import fmt_size
    from mmw.site.lock import LockHeld
    named = f"{repo} {version}" + (f" ({track})" if track else "")
    try:
        r_, key = _repo(root, repo, break_lock)
        if not yes:
            if not sys.stdin.isatty():
                eprint("pass --yes to delete it (no terminal to ask on)")
                return 1
            if input(f"Delete {named}? [y/N] ").strip().lower() not in ("y", "yes"):
                return 1
        try:
            freed = r_.delete(key, version, track)
        except ValueError as e:
            if "pass --yes" not in str(e):
                raise
            # The point being served: said, then done only when asked for.
            eprint(str(e).split(" — pass --yes", 1)[0] + " — " + str(e).split("; ", 1)[-1])
            if not yes and input("Delete it anyway? [y/N] ").strip().lower() not in ("y", "yes"):
                return 1
            freed = r_.delete(key, version, track, force=True)
    except (LockHeld, *_ERRORS) as e:
        return _fail(e)
    print(f"deleted {named}; freed {fmt_size(freed)} (its record stays in history)")
    return 0


def _keep_words(keep) -> str:
    from mmw.site.home import keep_words
    return keep_words(keep)


def settings(root: Path | None = None, keep: str | None = None, keep_all: bool = False,
             auto_clean: bool | None = None, dated: bool | None = None, costs: bool = False) -> int:
    from mmw.core.format import fmt_size
    from mmw.site import ops
    from mmw.site.lock import LockHeld
    try:
        mset = _set(root)
        if keep_all:
            keep = "all"
        if keep is not None:
            ops.set_keep(mset, keep)
        for r in mset.roots:
            if auto_clean is not None or dated is not None:
                if hasattr(r, "set_options"):
                    r.set_options(auto_clean=auto_clean, dated=dated)
                else:
                    print(f"root {r.info.name}: over ssh — run mmw site settings there for --auto-clean/--dated")
            print(f"root {r.info.name} ({r.info.location}): keeps {_keep_words(r.keep_setting())} per suite")
            if hasattr(r, "site_settings"):
                s = r.site_settings()
                print(f"  {'cleans after each apply' if s.auto_clean else 'cleans only when asked: mmw site clean'}"
                      f" · dated URLs {'on' if s.dated else 'off'}")
            if costs:                                 # reads every point's files: only when asked
                print("  Clean up would free now, keeping — " + ", ".join(
                    f"{_keep_words(k)}: {fmt_size(v)}" for k, v in r.keep_costs().items()))
            else:
                print("  what each setting would free: mmw site settings --costs")
    except (LockHeld, *_ERRORS) as e:
        return _fail(e)
    return 0


def clean(root: Path | None = None, dry_run: bool = False, yes: bool = False) -> int:
    """Clean up: what each root's setting lets go, with what each point frees."""
    import sys

    from mmw.site.lock import LockHeld

    from mmw.core.format import fmt_size
    try:
        mset = _set(root)
    except _ERRORS as e:
        return _fail(e)
    roots_here = [r for r in mset.roots if hasattr(r, "clean_plan")]
    for r in mset.roots:
        if not hasattr(r, "clean_plan"):
            print(f"root {r.info.name}: over ssh — run mmw site clean there")
    try:
        plans = [(r, r.clean_plan()) for r in roots_here]
    except (LockHeld, *_ERRORS) as e:                  # an ssh root that can't be reached: said, not a traceback
        return _fail(e)
    if not any(plan for _r, plan in plans):
        print("nothing to clean — every point is kept by the setting, or served, pinned or served before")
        return 0
    for r, plan in plans:
        for i in plan:
            print(f"  {r.info.name:<10} {i.label}  {i.suite} {i.date}  frees {fmt_size(i.frees)}")
    if dry_run:
        return 0
    if not yes:
        if not sys.stdin.isatty():
            eprint("pass --yes to clean up (no terminal to ask on)")
            return 1
        if input("Delete these points? Their records stay. [y/N] ").strip().lower() not in ("y", "yes"):
            return 1
    try:
        freed = sum(r.clean({i.point for i in plan}) for r, plan in plans if plan)
    except (LockHeld, *_ERRORS) as e:
        return _fail(e)
    print(f"cleaned up — freed {fmt_size(freed)}")
    return 0


def keep_suite(repo: str, track: str, value: str, root: Path | None = None) -> int:
    """One suite's own history setting ("default": the site's)."""
    try:
        r, key = _repo(root, repo)
        if not hasattr(r, "set_track_keep"):
            eprint(f"{r.info.name} is over ssh — run it on that machine")
            return 1
        r.set_track_keep(key, track, None if value == "default" else value)
    except _ERRORS as e:
        return _fail(e)
    print(f"{repo} {track}: keeps {'the site setting' if value == 'default' else value}")
    return 0


def status(root: Path | None = None) -> int:
    from mmw.site import ops
    try:
        mset = _set(root)
        rows = ops.set_status(mset)
    except _ERRORS as e:
        return _fail(e)
    if not rows:
        print("nothing applied yet — `mmw site apply BUNDLE --root '*'=PATH`")
        return 0
    several = len(mset.roots) > 1 or mset.missing
    roots_by = {r.info.name: r for r in mset.roots}
    from mmw.site.ops import suite_of
    from mmw.site.versionref import day
    dated_by_root: dict[str, list] = {}
    expired_by_root: dict[str, list] = {}
    for name, s in rows:
        hist = roots_by[name].history(s.repo)
        served, newer, pinned = [], False, False
        for t, points in hist:
            pinned = pinned or t.pinned is not None
            for i, (p, _total, _frees, mark) in enumerate(points):
                if mark == "serving":
                    served.append(f"{suite_of(t)} {day(p.date)}")
                    newer = newer or any(m in ("", "previous") for _p, _t, _f, m in points[:i])
        serving = ", ".join(served) or "nothing"
        state = ("held" if s.held else "pinned" if pinned else "newer stored" if newer else "current")
        where = f"{name:<10} " if several else ""
        layer = getattr(roots_by[name], "layer_details", None)        # a local root's only
        extra = layer(s.repo) if layer is not None else {}
        local = f" + local packages ({extra['local']})" if extra.get("local") else ""
        print(f"  {where}{s.label:<36} {serving:<36} {state}{local}")
        if extra.get("layer_hold"):
            print(f"  {where}{'':<36} held by its local packages: {extra['layer_hold']}")
        if name not in dated_by_root:                   # one read per root, not one per repository
            dated_by_root[name] = getattr(roots_by[name], "dated_urls", lambda: [])()
        dated = [u for lab, u in dated_by_root[name] if lab == s.label]
        if dated:
            span = dated[0] if len(dated) == 1 else f"{dated[0]} … {dated[-1]}"
            print(f"  {where}{'':<36} dated: {span} ({len(dated)})")
            if name not in expired_by_root:
                expired_by_root[name] = getattr(roots_by[name], "dated_expired", lambda: [])()
            old = [u for lab, u in expired_by_root[name] if lab == s.label]
            if old:
                print(f"  {where}{'':<36} past Valid-Until: {len(old)} (newest {old[-1]}) — apt refuses "
                      "them unless the sources line says [check-valid-until=no]")
        unchecked = getattr(roots_by[name], "unverified", None)
        unchecked = unchecked(s.repo) if unchecked is not None else []
        if unchecked:
            print(f"  {where}{'':<36} signature not checked: {', '.join(unchecked)}")
    for name in mset.missing:
        print(f"  {name:<10} MISSING — its repositories aren't shown; mount it, then: mmw site open PATH")
    return 0


def _day_time(stamp: str) -> str:
    return f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[9:11]}:{stamp[11:13]}" if len(stamp) >= 13 else stamp


def history(repo: str, root: Path | None = None) -> int:
    from mmw.core.format import fmt_size
    try:
        r_, key = _repo(root, repo)
        tracks = r_.history(key)
    except _ERRORS as e:
        return _fail(e)
    from mmw.site.ops import suite_of
    try:
        said = r_.point_notes(key) if hasattr(r_, "point_notes") else {}
    except _ERRORS:                                    # notes are extra: the history shows without them
        said = {}
    for t, rows in tracks:
        print(suite_of(t))
        for p, total, frees, mark in rows:
            arrow = "▶" if mark == "serving" else " "
            label = f" {p.snap_label}" if p.snap_label else ""
            tail = mark if mark else f"frees {fmt_size(frees)}"
            print(f"{arrow} {_day_time(p.date)}  {p.ident.split(':', 1)[-1][:6]}{label}  "
                  f"{fmt_size(total):>10}  {tail}")
            if said.get(p.id):
                print(f"    {said[p.id]}")
    return 0


def combine(repo: str, track: str, whens: list[str], key: str, yes: bool = False, root: Path | None = None) -> int:
    """Combine slices of one suite from different days into one served point,
    re-signed with a site key — said first, done on --yes."""
    import sys
    try:
        r_, k = _repo(root, repo)
    except _ERRORS as e:
        return _fail(e)
    if not hasattr(r_, "combine"):
        eprint(f"{r_.info.name} is over ssh — run it on that machine")
        return 1
    print(f"Combining {repo} {track} from {', '.join(whens)} writes a new Release: it is no longer signed by "
          f"upstream. It is re-signed with the site key {key!r}; clients need that key, served beside the "
          f"repository at mmw-keys/{key}.gpg, before they can use it.")
    if not yes:
        if not sys.stdin.isatty():
            eprint("pass --yes to combine them (no terminal to ask on)")
            return 1
        if input("Combine them? [y/N] ").strip().lower() not in ("y", "yes"):
            return 1
    try:
        res = r_.combine(k, track, whens, key)
    except _ERRORS as e:
        return _fail(e)
    print(f"combined {track} from {', '.join(res.sources)} — now served, signed by {key}")
    print(f"  clients trust it with: {res.key_url}")
    for line in res.lines:
        print(line)
    return 0


def verify(root: Path | None = None, repair: bool = False, break_lock: bool = False) -> int:
    from mmw.site import ops
    from mmw.site.lock import LockHeld
    try:
        mset = _set(root, break_lock)
        if repair:
            fixed = sum(ops.set_verify(mset, repair_too=True).values())
            print(f"put {fixed} file(s) back from the store")
        problems = [f"{name}: {p}" for name, ps in ops.set_verify(mset).items() for p in ps]
    except (LockHeld, *_ERRORS) as e:
        return _fail(e)
    for p in problems:
        eprint(f"  ✗ {p}")
    if problems:
        eprint(f"{len(problems)} problem(s): `mmw site verify --repair` puts them back from the store")
        return 4
    print("every served file is what the mirror says it serves")
    return 0


def manage(root: Path | None = None) -> int:
    try:
        mset = _set(root)
    except LookupError:
        mset = None                                 # nothing opened yet: the TUI starts there
    except _ERRORS as e:
        return _fail(e)
    from mmw.cli import _run_tui
    from mmw.tui.site.app import SiteApp
    _run_tui(SiteApp(mset))                         # restores the terminal, then restarts after an update
    return 0


def move(repo: str, to_root: str, root: Path | None = None, yes: bool = False, break_lock: bool = False) -> int:
    import sys
    from mmw.core.format import fmt_size
    from mmw.site import ops
    from mmw.site.lock import LockHeld
    try:
        mset = _set(root, break_lock)
        p = ops.move_preview(mset, repo, to_root)
        what = repo
        if p.whole_host:
            # A hostname is served from one root: the whole host moves.
            what = f"the whole host {', '.join(p.hosts)}"
            print(f"{', '.join(p.hosts)} is served from one root, so {repo} can't move on its own: "
                  f"{', '.join(p.labels)} move together, and switch together once everything is copied")
        print(f"moving {what} to {to_root} copies {fmt_size(p.copies)} first")
        if not yes:
            if not sys.stdin.isatty():
                eprint("pass --yes to move it (no terminal to ask on)")
                return 1
            if input(f"Move {what} to {to_root}? [y/N] ").strip().lower() not in ("y", "yes"):
                return 1
        r = ops.move_repo(mset, repo, to_root)
    except (LockHeld, *_ERRORS) as e:
        return _fail(e)
    print(f"moved {', '.join(r.moved)}: {r.files} file(s), {fmt_size(r.copied_bytes)} copied")
    for note in r.tls_notes:
        print(f"! {note}")
    if r.nginx_changed:
        print(f"! nginx changed in {', '.join(r.nginx_changed)} — recreate them on the serving host")
    print("the old root still serves its copy until you run `mmw site tidy`")
    return 0


def tidy(root: Path | None = None, break_lock: bool = False) -> int:
    from mmw.site import ops
    from mmw.site.lock import LockHeld
    try:
        mset = _set(root, break_lock)
        done = ops.tidy_set(mset)
    except (LockHeld, *_ERRORS) as e:
        return _fail(e)
    print(f"cleared the old copies of: {', '.join(done)}" if done else "nothing to tidy")
    for note in removable_conversion_backups(mset):
        print(note)
    return 0


def _passphrase(prompt: str = "CA key passphrase: ", confirm: bool = False) -> bytes | None:
    import getpass
    import os
    import sys
    env = os.environ.get("MMW_CA_PASSPHRASE")
    if env:
        return env.encode()
    if not sys.stdin.isatty():
        return None
    first = getpass.getpass(prompt)
    if confirm and getpass.getpass("Again: ") != first:
        eprint("the two passphrases differ — nothing made")
        return None
    return first.encode()


def ca_load(cert: Path, key: Path) -> int:
    from mmw.core.certs import CAKeyEncrypted
    from mmw.site import certs
    try:
        try:
            subject = certs.load_ca(cert, key, None)
        except CAKeyEncrypted:
            password = _passphrase()
            if password is None:
                eprint(f"{key} is encrypted — run this in a terminal, or set MMW_CA_PASSPHRASE")
                return 1
            subject = certs.load_ca(cert, key, password)
    except (ValueError, FileNotFoundError) as e:
        eprint(str(e))
        return 1
    print(f"CA {subject} loaded (mmw keeps only where it is: {cert}, {key})")
    return 0


def ca_make(out: Path, name: str, encrypt: bool = False) -> int:
    from mmw.site import certs
    try:
        password = None
        if encrypt:
            password = _passphrase("Passphrase for the new CA key: ", confirm=True)
            if not password:
                eprint("--encrypt needs a passphrase: run this in a terminal, or set MMW_CA_PASSPHRASE")
                return 1
        crt, key = certs.make_ca(out, name, password)
        certs.load_ca(crt, key, password)
    except (ValueError, FileExistsError, FileNotFoundError) as e:
        eprint(str(e))
        return 1
    print(f"made CA {name!r}: {crt}, {key}")
    print(certs.TRUST.format(crt=crt, key=key, name=name))
    return 0


def ca_show() -> int:
    from mmw.site import certs
    try:
        info = certs.show_ca()
    except (ValueError, FileNotFoundError) as e:
        eprint(str(e))
        return 1
    if info is None:
        print("no CA yet — mmw site ca load --cert … --key …, or mmw site ca make --out …")
        return 0
    subject, expires, crt, key = info
    print(f"{subject}\n  expires {expires:%Y-%m-%d}\n  certificate {crt}\n  key {key}")
    return 0


def certs(root: Path | None = None, days: int = 730, force: bool = False, out: Path | None = None) -> int:
    from mmw.core.certs import CAKeyEncrypted
    from mmw.site import certs as site_certs
    try:
        h = _set(root)
        try:
            results = site_certs.sign_set(h, password=None, days=days, force=force)
        except CAKeyEncrypted:
            password = _passphrase()
            if password is None:
                eprint("the CA key is encrypted — run this in a terminal, or set MMW_CA_PASSPHRASE")
                return 1
            results = site_certs.sign_set(h, password=password, days=days, force=force)
    except _ERRORS as e:
        eprint(str(e))
        return 1
    recreate = []
    for r in results:
        hosts = f" ({len(r.hostnames)} hostname(s))" if r.hostnames else ""
        print(f"  {r.root:<12} {r.action}{hosts}")
        for note in r.notes:
            print(f"    {note}")
        if r.action == "signed" or any("recreate" in n for n in r.notes):
            recreate.append(r.root)
    if recreate:
        print(f"! recreate nginx in {', '.join(recreate)} on the serving host")
    if out is not None:
        local = [(r.info.name, Path(r.info.path)) for r in h.roots if r.is_local]
        try:
            # An older site records its CA in a root's own settings: whichever has one.
            settings = next((s for s in (r.site_settings() for r in h.roots if r.is_local)
                             if s is not None and s.ca_cert is not None), None)
            names = site_certs.collect_certs(local, h.network, out, settings=settings)
        except (ValueError, OSError) as e:
            eprint(str(e))
            return 1
        print(f"collected in {out}: {', '.join(names)}")
        skipped = [r.info.name for r in h.roots if not r.is_local]
        if skipped:
            print(f"  (over ssh, collect on that machine: {', '.join(skipped)})")
    return 0


def bundle(to: str, out: str, repos: list[str] | None, since: str | None, all_: bool, dry_run: bool,
           root: Path | None = None, break_lock: bool = False) -> int:
    """`mmw site bundle`: this mirror's points for another network."""
    from mmw.core.format import fmt_size
    from mmw.site.export import export_bundle
    from mmw.site.lock import LockHeld
    if since and all_:
        eprint("choose one: --since or --all")
        return 2
    mode = "all" if all_ else "since" if since else "new"
    say = eprint if out == "-" else print            # stdout carries the tar itself
    try:
        mset = _set(root, break_lock)
        def summary(plan) -> None:
            if plan.new_target and mode == "new":
                known = f" (known targets: {', '.join(plan.known_targets)})" if plan.known_targets else ""
                say(f"first export to '{plan.target}': nothing recorded as sent there, so everything goes{known}")
            n = len(plan.per_repo)
            say(f"{mset.network} → {plan.target}: {plan.points} point(s) of {n} "
                f"repositor{'y' if n == 1 else 'ies'}, {plan.files} file(s), {fmt_size(plan.bytes)}")
            for label, points in sorted(plan.per_repo.items()):
                say(f"  {label}  {', '.join(points)}")
            for s in plan.skipped:
                say(f"  skipped: {s}")
        # One plan: what is said is what is written.
        export_bundle(mset, to, out, repos=set(repos) if repos else None, mode=mode, since=since,
                      dry_run=dry_run, on_plan=summary)
        if dry_run:
            say(f"would write {out}")
            return 0
    except (LockHeld, *_ERRORS) as e:
        return _fail(e)
    if out != "-":
        print(f"wrote {out}")
    return 0


def _hash_progress(where: Path):
    """Every few thousand files hashed, a line on stderr — adopting a big
    tree takes a while."""
    import sys
    seen = [0, 0]

    def tick(_rel: str, size: int) -> None:
        seen[0] += 1
        seen[1] += size
        if seen[0] % 5000 == 0:
            from mmw.core.format import fmt_size
            print(f"  {where}: {seen[0]:,} files read ({fmt_size(seen[1])})", file=sys.stderr)
    return tick


def _adopt_apply(managed, root: Path | None, dry_run: bool, yes: bool, break_lock: bool,
                 say=print, err=eprint, hashing=_hash_progress) -> int:
    """A tree apply.py manages: converted in place (no --root), or adopted
    into the mirror --root names."""
    import sys

    from mmw.site import recents
    from mmw.site.adopt import apt_bases, check_apply_tree
    from mmw.site.lock import LockHeld
    bases = apt_bases(managed.rows)
    say(f"An apply.py tree of {managed.network} at {managed.path} (as of {managed.created}): "
          f"{len(managed.rows):,} files")
    for b in bases:
        say(f"  {b}  — an APT repository (named by the network's next bundle)")
    say("  the rest — the network's own files (keys, nginx, other repositories)")
    target = None
    if root is not None:
        try:
            target = _set(root, break_lock).roots[0]
        except _ERRORS as e:
            return (err(str(e).strip("'\"")) or 1)
        if not target.is_local:
            err(f"{target.info.name} is over ssh — run mmw site adopt on that machine")
            return 1
        say(f"→ into {target.info.name}")
    else:
        say("→ in place: it becomes a root of an mmw mirror; served files stay as they are")
    if dry_run:
        try:
            checked = check_apply_tree(managed, progress=hashing(managed.path))
        except _ERRORS as e:
            return (err(str(e)) or 1)
        say(f'Checked {len(checked.present):,} files against the recorded manifest.')
        for what, why in checked.refused.items():
            err(f'  {what} refused: {why}')
        for what, n in checked.missing.items():
            say(f'  {what}: {n} file(s) missing from the original tree')
        for rel in checked.missing_paths[:10]:
            say(f'  missing: {rel}')
        if len(checked.missing_paths) > 10:
            say(f'  … and {len(checked.missing_paths) - 10:,} more missing files')
        if target is None and (checked.refused or any(b != 'network files' for b in checked.missing)):
            err('Not ready to upgrade — restore the named files and Look again; nothing has been changed.')
            return 1
        return 0
    if not yes:
        if not sys.stdin.isatty():
            err("pass --yes to adopt it (no terminal to ask on)")
            return 1
        if input("Adopt it? [y/N] ").strip().lower() not in ("y", "yes"):
            return 1
    try:
        if target is None:
            from mmw.site.adopt import adopt_apply_tree
            if break_lock:
                from mmw.site.home import root_home
                from mmw.site.lock import break_lock as clear_lock
                clear_lock(root_home(managed.path))
            r = adopt_apply_tree(managed, progress=hashing(managed.path))
        else:
            r = target.adopt_apply_tree(managed, progress=hashing(managed.path))
    except (*_ERRORS, LockHeld) as e:
        if target is None:
            from mmw.site.home import root_home
            if disk_io.ops.exists(root_home(managed.path) / 'adoption.json'):
                err('Upgrade is unfinished; progress is saved. Fix the problem, then Look and Adopt the same folder again.')
        return (err(str(e).strip("'\"")) or (5 if isinstance(e, LockHeld) else 1))
    complete = r.converted
    if target is None and complete:
        recents.remember(str(managed.path))
    for line in r.lines:
        say(line)
    for what, why in r.refused.items():
        say(f"  {what} refused: {why}")
    for what, n in r.incomplete.items():
        say(f"  {what} stored incomplete: {n} file(s) missing")
    if target is None and not complete:
        err('Not adopted — restore the named files and Look again; the original mirror is unchanged.')
        return 1
    say(f"adopted ({r.linked} linked" + (f", {r.copied} copied" if r.copied else "") + ") — the "
          "network's next mmw bundle names its repositories")
    return 0


def _exists(path: Path) -> bool:
    try:
        disk_io.ops.stat(path)
        return True
    except OSError:
        return False


def _adopt_host(path: Path, root: Path | None, repo: str | None) -> tuple[str, str, str | None] | None:
    """Where adopting `path` runs, when not here: (host, the folder there,
    the root there or None for that host's own). A `HOST:/PATH` folder runs
    on HOST; a plain one on the root over ssh that --root or --repo names, or
    — when it isn't a folder here — the mirror's first root, when over ssh."""
    from mmw.site import ssh
    kind, host, far_path = ssh.parse_location(str(path))
    root_kind, root_host, root_path = ssh.parse_location(str(root)) if root is not None else ("local", None, None)
    if kind == "ssh":
        if root is not None:
            if root_kind != "ssh" or root_host != host:
                raise ValueError(f"{path} is on {host}, but the root {root} isn't — adopting links the files "
                                 "into the root's store, so both must be on one machine")
            return host, far_path, root_path
        if repo is not None:
            r, _key = _set(None).root_for_repo(repo)
            if r.is_local or r.host != host:
                raise ValueError(f"{path} is on {host}, but {repo} is in {r.info.name} — adopting links the "
                                 "files into the root's store, so both must be on one machine")
            return host, far_path, r.path
        try:
            mine = [r for r in _set(None).roots if not r.is_local and r.host == host]
        except _ERRORS:
            mine = []                                   # no mirror opened here: the one on that host
        return host, far_path, mine[0].path if mine else None
    if root_kind == "ssh":
        return root_host, str(path), root_path
    if repo is not None:
        r, _key = _set(root).root_for_repo(repo)
        return None if r.is_local else (r.host, str(path), r.path)
    if root is None and not _exists(path):
        try:
            first = _set(None).roots[0]
        except (*_ERRORS, IndexError):
            return None
        if not first.is_local:
            return first.host, str(path), first.path
    return None


_HASHED = re.compile(r"^\s+.+: [\d,]+ files read \(.+\)$")      # _hash_progress's lines


def _adopt_over_ssh(host: str, far_path: str, far_root: str | None, repo, as_base, key, unverified: bool,
                    dry_run: bool, yes: bool, break_lock: bool, say, err, said) -> int:
    """`mmw site adopt` on `host`, its lines streamed here. Its terminal
    isn't this one, so asking happens here: it looks (--dry-run), this side
    asks, then it adopts (--yes). A key file here goes over ssh's stdin."""
    import sys

    from mmw.site import ssh
    args = ["site", "adopt", far_path, *(["--root", far_root] if far_root else []),
            *(["--repo", repo] if repo else []), *(["--as", as_base] if as_base else []),
            *(["--key", "-"] if key is not None else []), *(["--unverified"] if unverified else []),
            *(["--break-lock"] if break_lock else [])]
    data = disk_io.ops.read_bytes(Path(key)) if key is not None else None

    def to_err(line: str) -> None:
        (said if said is not None and _HASHED.match(line) else err)(line.strip() if said else line)

    def run(*extra: str) -> int:
        return ssh.stream(host, ssh.mmw_argv(*args, *extra), out=say, err=to_err, input=data)
    try:
        ssh.check_remote_mmw(host)
        say(f"On {host}:")
        if dry_run or yes:
            return run("--dry-run" if dry_run else "--yes")
        if not sys.stdin.isatty():
            err("pass --yes to adopt them (no terminal to ask on)")
            return 1
        code = run("--dry-run")
        if code != 0:
            return code
        if input("Adopt them? [y/N] ").strip().lower() not in ("y", "yes"):
            return 1
        return run("--yes")
    except _ERRORS as e:
        return (err(str(e).strip("'\"")) or 1)


def adopt(path: Path, root: Path | None = None, repo: str | None = None, as_base: str | None = None,
          key: Path | None = None, unverified: bool = False, dry_run: bool = False, yes: bool = False,
          break_lock: bool = False, say=print, err=eprint, hashing=_hash_progress, said=None) -> int:
    """Take over the APT trees under `path`: report them by suite, check each,
    and adopt them — the trees themselves are never changed. `hashing(where)`:
    a `(rel, size)` callback per file read (default: a line on stderr now and then).

    Adopting links the files into a root's store, so it runs where the files
    and the root are: a `[user@]host:/path`, or a root over ssh, runs `mmw
    site adopt` on that host, its lines streamed back (`said(text)`: its
    how-far lines, when given; else they go to `err`)."""
    import sys
    import tempfile

    from mmw.site.adopt import check_tree, find_apply_tree, find_trees, keyring_from, new_repo_key
    from mmw.site.versionref import day
    try:
        far = _adopt_host(Path(path), root, repo)
    except _ERRORS as e:
        return (err(str(e).strip("'\"")) or 1)
    if far is not None:
        return _adopt_over_ssh(*far, repo, as_base, key, unverified, dry_run, yes, break_lock, say, err, said)
    if not _exists(Path(path)):
        err(f"{path} doesn't exist on this machine — for a folder on another, give it as HOST:/PATH")
        return 1
    if str(key) == "-":                       # sent over ssh by the side that asked (see _adopt_over_ssh)
        key_bytes = sys.stdin.buffer.read()
    elif key is not None:
        key_bytes = disk_io.ops.read_bytes(Path(key))
    else:
        key_bytes = None
    try:
        managed = find_apply_tree(Path(path))
    except _ERRORS as exc:
        return (err(str(exc)) or 1)
    if managed is not None:
        return _adopt_apply(managed, root, dry_run, yes, break_lock, say, err, hashing)
    trees = find_trees(Path(path))
    if not trees:
        err(f"no APT trees under {path} (an archive root has dists/*/Release, or a Release beside Packages)")
        return 1
    try:
        mset = _set(root, break_lock)
        if repo is not None:
            target, repo_key = mset.root_for_repo(repo)
        else:
            target, repo_key = mset.roots[0], None
    except _ERRORS as e:
        return (err(str(e).strip("'\"")) or 1)
    if not target.is_local:
        err(f"{target.info.name} is over ssh — run mmw site adopt on that machine")
        return 1

    groups: dict[tuple, list] = {}
    for t in trees:
        groups.setdefault(t.identity(), []).append(t)
    dest: dict[tuple, tuple] = {}         # identity → (repo key, label, base, the root it goes to)
    candidates = [r for r in mset.roots if r.is_local] if repo is None else [target]
    for ident, members in groups.items():
        match, match_root = None, target
        for r in candidates:                        # a suite already here, in any root of the set
            match = r.adoption_match(members[0], repo_key)
            if match is not None:
                match_root = r
                break
        origin, _label, name = ident
        say(f"Found {len(members)} tree{'s' if len(members) > 1 else ''} of {origin or '?'} {name or '?'}:")
        for t in sorted(members, key=lambda t: min(x.date for x in t.tracks)):
            suites = ", ".join(f"{x.dir.rsplit('/', 1)[-1] or name} {day(x.date)}" for x in t.tracks)
            say(f"  {t.path}  {suites}")
        if match is not None:
            dest[ident] = (*match, match_root)
            say(f"→ {match[1]} (already here, at {match[2]})")
        elif as_base:
            base = as_base.strip("/")
            dest[ident] = (None, origin or base, base, target)
            say(f"→ a new repository served at https://{base}/ ({new_repo_key(base)})")
        else:
            err(f"{origin} {name} is new here — say where it is served: --as HOST/PATH "
                   "(e.g. --as deb.debian.org/debian)")
            return 1

    with tempfile.TemporaryDirectory() as tmp:
        keys = [key_bytes] if key_bytes is not None else target.network_keys()
        keyring = keyring_from(keys, Path(tmp) / "keys.gpg") if keys else None
        if keyring is None and not unverified:
            if not (sys.stdin.isatty() and not yes):
                err("no key to check the signatures with — pass --key FILE (upstream's public key), "
                       "or --unverified to adopt without checking (marked 'signature not checked')")
                return 1
            if input("No key to check signatures with. Adopt unverified? [y/N] ").strip().lower() not in ("y", "yes"):
                return 1
        checked = []
        for t in sorted(trees, key=lambda t: min(x.date for x in t.tracks)):
            check = check_tree(t, keyring, progress=hashing(t.path))
            checked.append((t, check))
            sig = sorted(set(check.signature.values()))
            notes = [", ".join(sig).replace("not checked", "signature not checked")]
            signers = sorted(set(check.signed_by.values()))
            if signers:                          # any key under keys/ can verify: say whose it was
                notes.append("by " + "; ".join(signers))
            if check.missing:
                notes.append(f"{len(check.missing)} file(s) missing")
            if check.refused:
                notes.append("refused: " + "; ".join(f"{d.rsplit('/', 1)[-1] or 'files'}: {why}"
                                                     for d, why in check.refused.items()))
            say(f"  {t.path}: {' · '.join(notes)}")
            if check.unhashed:
                n = sum(check.unhashed.values())
                say(f"    not adopted: {n} index entr{'y' if n == 1 else 'ies'} with no SHA256 (MD5 only) — "
                    "nothing to check those files by, so they stay out")
            stray = check.strays
            if stray:
                parts = ([f"{len(stray['pool'])} pool file(s) no index names"] if stray.get("pool") else []) + \
                        ([f"{len(stray['other'])} other file(s)"] if stray.get("other") else []) + \
                        ([f"{len(stray['damaged'])} damaged (not what their name or SHA256SUMS says)"]
                         if stray.get("damaged") else []) + \
                        ([f"{len(stray['unreadable'])} unreadable"] if stray.get("unreadable") else [])
                if parts:
                    say(f"    not adopted: {', '.join(parts)} (apt can't reach them)")
                if stray.get("unlisted"):              # a full mirror keeps older generations' by-hash files
                    say(f"    not adopted: {len(stray['unlisted'])} by-hash file(s) of earlier Releases — "
                        "nothing here vouches for them (this Release doesn't list them)")
        if dry_run:
            return 0
        if not yes:
            if not sys.stdin.isatty():
                err("pass --yes to adopt them (no terminal to ask on)")
                return 1
            if input("Adopt them? [y/N] ").strip().lower() not in ("y", "yes"):
                return 1
        adopted = 0
        for t, check in checked:
            key_here, label, base, into = dest[t.identity()]
            try:
                r = into.adopt_tree(t, check, repo=key_here, label=label, base=base,
                                    progress=hashing(t.path))
            except _ERRORS as e:
                err(f"{t.path}: {e}")
                continue
            if key_here is None:                       # the next tree of this group joins it
                dest[t.identity()] = (r.repo, r.label, base, into)
            adopted += 1
            how = f"{r.linked} linked" + (f", {r.copied} copied" if r.copied else "")
            say(f"adopted {t.path} ({how})")
            for line in r.lines:
                say(line)
            for suite, why in r.refused.items():
                say(f"  {r.label}  {suite} refused: {why}")
            for suite, n in r.incomplete.items():
                say(f"  {r.label}  {suite} stored incomplete: {n} file(s) missing")
            if r.unverified:
                say(f"  {r.label}  signature not checked: {', '.join(r.unverified)}")
    say(f"{path} was not changed — remove the adopted trees yourself once you're satisfied")
    return 0 if adopted else 1


def check_signatures(root: Path | None = None, key: Path | None = None, break_lock: bool = False) -> int:
    """Check the signatures of points adopted unverified, now that a key is here."""
    import tempfile

    from mmw.site.adopt import keyring_from
    try:
        roots_here = [r for r in _set(root, break_lock).roots if r.is_local]
    except _ERRORS as e:
        return _fail(e)
    checked, code = 0, 0
    for r in roots_here:
        keys = [disk_io.ops.read_bytes(Path(key))] if key is not None else r.network_keys()
        if not keys:
            eprint(f"{r.info.name}: no key to check with — pass --key FILE (upstream's public key)")
            code = 1
            continue
        with tempfile.TemporaryDirectory() as tmp:
            for label, what, verdict in r.recheck_signatures(keyring_from(keys, Path(tmp) / "k.gpg")):
                checked += 1
                print(f"  {label}  {what}  {verdict}")
    if not checked and not code:
        print("nothing to check — every adopted point's signature was checked when it was adopted "
              "(a tree apply.py managed came from a bundle the connected side checked)")
    return code


def adopted(root: Path | None = None, remove: Path | None = None, yes: bool = False) -> int:
    """The trees adopted from, with what removing each frees; `remove` one."""
    import sys

    from mmw.core.format import fmt_size
    try:
        roots_here = [r for r in _set(root).roots if r.is_local]
        every_root = [Path(r.info.path) for r in _set(root).roots if r.is_local]
    except _ERRORS as e:
        return _fail(e)
    if remove is None:
        listed = 0
        for r in roots_here:
            for o in r.originals():
                listed += 1
                state = (f"{o.files:,} files, removing it frees {fmt_size(o.frees)}" if o.present
                         else "gone already")
                print(f"  {o.path}  adopted {o.stamp[:8]} — {state}")
        if not listed:
            print("no adopted trees left")
        return 0
    target = Path(remove).resolve()
    for r in roots_here:
        found = r.original(target)                       # this tree only: never every recorded one
        if found is None:
            continue
        print(f"Remove {target}? It frees {fmt_size(found.frees)}; what was adopted from it stays served.")
        if found.refused:
            print(f"  ! not adopted from it (refused): {', '.join(found.refused)} — removing it loses those files")
        if not yes:
            if not sys.stdin.isatty():
                eprint("pass --yes to remove it (no terminal to ask on)")
                return 1
            if input("Remove it? [y/N] ").strip().lower() not in ("y", "yes"):
                return 1
        from mmw.site.lock import LockHeld
        try:
            freed = r.remove_original(target, every_root)
        except (LockHeld, ValueError, OSError) as e:
            return _fail(e)
        print(f"removed {target} — {fmt_size(freed)} freed")
        return 0
    eprint(f"{target} is not an adopted tree — `mmw site adopted` lists them")
    return 1
