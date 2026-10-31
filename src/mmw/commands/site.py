"""`mmw site …` — mmw on the far side of the airgap. A mirror is a set of
roots; this machine remembers where it has seen them (`mmw site open`)."""
from pathlib import Path

from mmw.commands.common import eprint


def _set(root: Path | None, break_lock: bool = False):
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
    return mset


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


_ERRORS = (ValueError, LookupError, FileNotFoundError, KeyError, _ssh_error())


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
          forward_all: bool = False, dry_run: bool = False, break_lock: bool = False, yes: bool = False) -> int:
    import sys
    from mmw.site.apply import ApplyError, MaybeUnmounted, apply_to_set
    from mmw.site.lock import LockHeld
    from mmw.site.mset import root_at
    try:
        place = _place(root or [])
        at = next((loc for loc in place.values() if root_at(loc) is not None), None)
        try:
            mset = _set(at, break_lock)
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
                             apply_anyway=set(place.values()) if yes else None)
        except MaybeUnmounted as e:
            eprint(str(e))
            if not sys.stdin.isatty() and not _interactive():
                eprint("pass --yes to apply anyway (no terminal to ask on)")
                return e.code
            if input("Apply anyway? [y/N] ").strip().lower() not in ("y", "yes"):
                return e.code
            r = apply_to_set(mset, src, place=place, forward=keys, forward_all=forward_all, dry_run=dry_run,
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
    return 3 if any(x.refused for x in r.per_root.values()) else 0


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


def _version(r_, key: str, text: str):
    """The version `text` names — v3, 3, or its bundle's date, stamp or label."""
    from mmw.site.versionref import resolve
    return resolve([v for v, *_ in r_.history(key)], text)


def _when(stamp: str) -> str:
    from mmw.site.versionref import when_list
    return when_list([stamp])[0]


def serve(repo: str, version: str, root: Path | None = None, yes: bool = False,
          break_lock: bool = False) -> int:
    import sys
    from mmw.site.lock import LockHeld
    try:
        r_, key = _repo(root, repo, break_lock)
        number = _version(r_, key, version).number
        p = r_.serve_preview(key, number)
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
        r = r_.serve(key, number)
    except (LockHeld, *_ERRORS) as e:
        return _fail(e)
    print(f"{p.label} serves v{p.target.number} ({r.placed} placed, {r.removed} removed)")
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
                print(f"  v{v.number:<4} {v.stamp}{label}  {fmt_size(v.total):>10}  {tag}")
        for _n, (used, free) in rep.roots.items():
            print(f"root {name}: {fmt_size(used)} used, {fmt_size(free)} free")
        history += rep.history_bytes
    print(f"old versions hold {fmt_size(history)}")
    return 0


def delete(repo: str, version: str, root: Path | None = None, yes: bool = False,
           break_lock: bool = False) -> int:
    import sys
    from mmw.core.format import fmt_size
    from mmw.site.lock import LockHeld
    try:
        r_, key = _repo(root, repo, break_lock)
        v = _version(r_, key, version)
        named = f"{repo} v{v.number} ({_when(v.stamp)}{' ' + v.snap_label if v.snap_label else ''})"
        if not yes:
            if not sys.stdin.isatty():
                eprint("pass --yes to delete it (no terminal to ask on)")
                return 1
            if input(f"Delete {named}? [y/N] ").strip().lower() not in ("y", "yes"):
                return 1
        freed = r_.delete(key, v.number)
    except (LockHeld, *_ERRORS) as e:
        return _fail(e)
    print(f"deleted {named}; freed {fmt_size(freed)}")
    return 0


def settings(root: Path | None = None, keep: int | None = None, keep_all: bool = False) -> int:
    from mmw.site import ops
    from mmw.site.lock import LockHeld
    try:
        mset = _set(root)
        if keep_all or keep is not None:
            ops.set_keep(mset, None if keep_all else keep)
        for r in mset.roots:
            k = r.keep_versions()
            kept = "every version" if k is None else f"the newest {k} per repository"
            print(f"root {r.info.name} ({r.info.location}): keeps {kept}")
    except (LockHeld, *_ERRORS) as e:
        return _fail(e)
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
    for name, s in rows:
        newest = roots_by[name].newest(s.repo)
        serving = (f"v{s.serving.number} · {s.serving.stamp}"
                   f"{' ' + s.serving.snap_label if s.serving.snap_label else ''}") if s.serving else "nothing"
        state = "held" if s.held else "current"
        where = f"{name:<10} " if several else ""
        layer = getattr(roots_by[name], "layer_details", None)        # a local root's only
        extra = layer(s.repo) if layer is not None else {}
        local = f" + local packages ({extra['local']})" if extra.get("local") else ""
        print(f"  {where}{s.label:<36} {serving:<36} newest v{newest:<4} {state}{local}")
        if extra.get("layer_hold"):
            print(f"  {where}{'':<36} held by its local packages: {extra['layer_hold']}")
    for name in mset.missing:
        print(f"  {name:<10} MISSING — its repositories aren't shown; mount it, then: mmw site open PATH")
    return 0


def history(repo: str, root: Path | None = None) -> int:
    from mmw.core.format import fmt_size
    try:
        r_, key = _repo(root, repo)
        rows = r_.history(key)
    except _ERRORS as e:
        return _fail(e)
    for v, total, frees, serving in rows:
        mark = "▶" if serving else " "
        label = f" {v.snap_label}" if v.snap_label else ""
        tail = "serving" if serving else f"frees {fmt_size(frees)}"
        print(f"{mark} v{v.number:<4} {v.stamp}{label}  {fmt_size(total):>10}  {tail}")
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
        done = ops.tidy_set(_set(root, break_lock))
    except (LockHeld, *_ERRORS) as e:
        return _fail(e)
    print(f"cleared the old copies of: {', '.join(done)}" if done else "nothing to tidy")
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


def certs(root: Path | None = None, days: int = 730, force: bool = False) -> int:
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
    return 0
