"""`mmw site …` — mmw on the far side of the airgap."""
from pathlib import Path

from mmw.commands.common import eprint
from mmw.site import home as site_home


def init(root: Path, home: Path | None = None) -> int:
    home = Path(home) if home is not None else site_home.default_home(root)
    try:
        settings = site_home.init_site(home, root)
    except FileExistsError as e:
        eprint(str(e))
        return 1
    print(f"Site ready: serving {settings.root}, state in {home}.")
    print(f"export MMW_SITE_HOME={home}")
    return 0


def _home(home: Path | None, break_lock: bool) -> Path:
    from mmw.site.lock import break_lock as _break
    h = site_home.resolve_home(home)
    if break_lock and _break(h):
        eprint(f"removed the lock in {h}")
    return h


def apply(src: str, home: Path | None = None, forward: list[str] | None = None,
          forward_all: bool = False, dry_run: bool = False, break_lock: bool = False) -> int:
    from mmw.site.apply import ApplyError, apply_bundle
    from mmw.site.lock import LockHeld
    try:
        h = _home(home, break_lock)
        keys = _repo_keys(h, forward) if forward else None
        r = apply_bundle(h, src, forward=keys, forward_all=forward_all, dry_run=dry_run)
    except ApplyError as e:
        eprint(str(e))
        return e.code
    except LockHeld as e:
        eprint(str(e))
        return 5
    except (ValueError, LookupError, FileNotFoundError) as e:
        eprint(str(e))
        return 1
    print(f"applied {r.created}" if not dry_run else f"would apply {r.created}")
    for line in r.lines:
        print(line)
    for action in r.actions:
        print(f"! {action}")
    return 3 if r.refused else 0


def _repo_keys(h: Path, refs: list[str]) -> set[str]:
    from mmw.core.repos import resolve_repo_refs
    from mmw.site.db import SiteDB
    with SiteDB(h) as db:
        return set(resolve_repo_refs(refs, {s.repo: s.label for s in db.repo_states()}))


def _version_number(text: str) -> int:
    return int(text[1:] if text.lower().startswith("v") else text)


def serve(repo: str, version: str, home: Path | None = None, yes: bool = False,
          break_lock: bool = False) -> int:
    import sys
    from mmw.site import ops
    from mmw.site.lock import LockHeld
    try:
        h = _home(home, break_lock)
        number = _version_number(version)
        p = ops.serve_preview(h, repo, number)
        now = f"v{p.current.number}" if p.current else "nothing"
        print(f"{p.label}: now serving {now} · serve v{p.target.number} ({p.target.stamp}"
              f"{' ' + p.target.snap_label if p.target.snap_label else ''}) · relinks {p.relinks} file(s) · "
              "nothing to copy · indexes switch last")
        if not yes:
            if not sys.stdin.isatty():
                eprint("pass --yes to serve it (no terminal to ask on)")
                return 1
            if input("Serve it? [y/N] ").strip().lower() not in ("y", "yes"):
                return 1
        r = ops.serve(h, repo, number)
    except LockHeld as e:
        eprint(str(e))
        return 5
    except (ValueError, LookupError, FileNotFoundError) as e:
        eprint(str(e))
        return 1
    print(f"{p.label} serves v{p.target.number} ({r.placed} placed, {r.removed} removed)")
    return 0


def forward(repo: str | None, home: Path | None = None, all_: bool = False, break_lock: bool = False) -> int:
    from mmw.site import ops
    from mmw.site.lock import LockHeld
    if repo is None and not all_:
        eprint("name a repository, or --all for every held one")
        return 1
    try:
        moved = ops.forward(_home(home, break_lock), repo)
    except LockHeld as e:
        eprint(str(e))
        return 5
    except (ValueError, LookupError, FileNotFoundError) as e:
        eprint(str(e))
        return 1
    print(f"moved forward: {len(moved)} repositor{'y' if len(moved) == 1 else 'ies'}")
    return 0


def hold(repo: str, home: Path | None = None, break_lock: bool = False) -> int:
    from mmw.site import ops
    from mmw.site.lock import LockHeld
    try:
        label = ops.hold(_home(home, break_lock), repo)
    except LockHeld as e:
        eprint(str(e))
        return 5
    except (ValueError, LookupError, FileNotFoundError) as e:
        eprint(str(e))
        return 1
    print(f"{label} is held: bundles store its new versions without serving them")
    return 0
