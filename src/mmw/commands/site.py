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
