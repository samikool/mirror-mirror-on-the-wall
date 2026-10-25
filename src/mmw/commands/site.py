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


def _placements(h: Path, src: str, specs: list[str]) -> dict[str, str]:
    """`REPO=ROOT` (a repository's name or URL) → {key: root}; names come
    from the bundle itself, since a new repository isn't in the site yet."""
    from mmw.core.repos import resolve_repo_refs
    from mmw.site.apply import plan_apply
    available = {r.repo: r.label for r in plan_apply(h, src).rows} if src != "-" else {}
    out = {}
    for spec in specs:
        ref, sep, root = spec.partition("=")
        if not sep or not root:
            raise ValueError(f"--place wants REPO=ROOT, not {spec!r}")
        key = ref if src == "-" else resolve_repo_refs([ref], available)[0]
        out[key] = root
    return out


def apply(src: str, home: Path | None = None, forward: list[str] | None = None,
          forward_all: bool = False, dry_run: bool = False, break_lock: bool = False,
          place: list[str] | None = None) -> int:
    from mmw.site.apply import ApplyError, apply_bundle
    from mmw.site.lock import LockHeld
    try:
        h = _home(home, break_lock)
        keys = _repo_keys(h, forward) if forward else None
        placed = _placements(h, src, place) if place else None
        r = apply_bundle(h, src, forward=keys, forward_all=forward_all, dry_run=dry_run, place=placed)
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


def space(home: Path | None = None) -> int:
    from mmw.core.format import fmt_size
    from mmw.site import ops
    try:
        rep = ops.space(_home(home, False))
    except (ValueError, FileNotFoundError) as e:
        eprint(str(e))
        return 1
    for repo, rows in rep.repos.items():
        print(rep.labels[repo])
        for v in rows:
            tag = "serving" if v.serving else f"frees {fmt_size(v.frees)}"
            label = f" {v.snap_label}" if v.snap_label else ""
            print(f"  v{v.number:<4} {v.stamp}{label}  {fmt_size(v.total):>10}  {tag}")
    for name, (used, free) in rep.roots.items():
        print(f"root {name}: {fmt_size(used)} used, {fmt_size(free)} free")
    print(f"old versions hold {fmt_size(rep.history_bytes)}")
    return 0


def delete(repo: str, version: str, home: Path | None = None, yes: bool = False,
           break_lock: bool = False) -> int:
    import sys
    from mmw.core.format import fmt_size
    from mmw.site import ops
    from mmw.site.lock import LockHeld
    try:
        h = _home(home, break_lock)
        if not yes:
            if not sys.stdin.isatty():
                eprint("pass --yes to delete it (no terminal to ask on)")
                return 1
            if input(f"Delete {repo} {version} from this site? [y/N] ").strip().lower() not in ("y", "yes"):
                return 1
        freed = ops.delete_version(h, repo, _version_number(version))
    except LockHeld as e:
        eprint(str(e))
        return 5
    except (ValueError, LookupError, FileNotFoundError) as e:
        eprint(str(e))
        return 1
    print(f"deleted {repo} {version}; freed {fmt_size(freed)}")
    return 0


def settings(home: Path | None = None, keep: int | None = None, keep_all: bool = False) -> int:
    try:
        h = _home(home, False)
        s = site_home.load_site(h)
        if keep_all or keep is not None:
            s = s.model_copy(update={"keep_versions": None if keep_all else keep})
            site_home.save_settings(h, s)
    except (ValueError, FileNotFoundError) as e:
        eprint(str(e))
        return 1
    kept = "every version" if s.keep_versions is None else f"the newest {s.keep_versions} per repository"
    print(f"root {s.root}; keeps {kept}")
    return 0


def status(home: Path | None = None) -> int:
    from mmw.site import ops
    try:
        states = ops.status(_home(home, False))
    except (ValueError, FileNotFoundError) as e:
        eprint(str(e))
        return 1
    if not states:
        print("nothing applied yet — `mmw site apply BUNDLE`")
        return 0
    from mmw.site.db import SiteDB
    with SiteDB(_home(home, False)) as db:
        for s in states:
            newest = db.versions(s.repo)[0].number
            serving = (f"v{s.serving.number} · {s.serving.stamp}"
                       f"{' ' + s.serving.snap_label if s.serving.snap_label else ''}") if s.serving else "nothing"
            state = "held" if s.held else "current"
            print(f"  {s.label:<36} {serving:<36} newest v{newest:<4} {state}")
    return 0


def history(repo: str, home: Path | None = None) -> int:
    from mmw.core.format import fmt_size
    from mmw.site import ops
    try:
        rows = ops.history(_home(home, False), repo)
    except (ValueError, LookupError, FileNotFoundError) as e:
        eprint(str(e))
        return 1
    for v, total, frees, serving in rows:
        mark = "▶" if serving else " "
        label = f" {v.snap_label}" if v.snap_label else ""
        tail = "serving" if serving else f"frees {fmt_size(frees)}"
        print(f"{mark} v{v.number:<4} {v.stamp}{label}  {fmt_size(total):>10}  {tail}")
    return 0


def verify(home: Path | None = None, repair: bool = False, break_lock: bool = False) -> int:
    from mmw.site import ops
    from mmw.site.lock import LockHeld
    try:
        if repair:
            fixed = ops.repair(_home(home, break_lock))
            print(f"put {fixed} file(s) back from the store")
        problems = ops.verify(_home(home, False))
    except LockHeld as e:
        eprint(str(e))
        return 5
    except (ValueError, FileNotFoundError) as e:
        eprint(str(e))
        return 1
    for p in problems:
        eprint(f"  ✗ {p}")
    if problems:
        eprint(f"{len(problems)} problem(s): `mmw site verify --repair` puts them back from the store")
        return 4
    print("every served file is what the site says it serves")
    return 0


def manage(home: Path | None = None) -> int:
    try:
        h = site_home.resolve_home(home)
        site_home.load_site(h)
    except (ValueError, FileNotFoundError) as e:
        eprint(str(e))
        return 1
    from mmw.tui.site.app import SiteApp
    SiteApp(h).run()
    return 0


def root_add(name: str, path: Path, serve_path: Path | None = None, home: Path | None = None) -> int:
    try:
        r = site_home.add_root(_home(home, False), name, path, serve_path)
    except (ValueError, FileNotFoundError) as e:
        eprint(str(e))
        return 1
    print(f"root {r.name}: {r.path} (the serving host sees {r.serve_path})")
    return 0


def root_list(home: Path | None = None) -> int:
    try:
        s = site_home.load_site(_home(home, False))
    except (ValueError, FileNotFoundError) as e:
        eprint(str(e))
        return 1
    for i, r in enumerate(s.roots):
        primary = "  (primary)" if i == 0 else ""
        print(f"  {r.name:<12} {r.path}  serving host: {r.serve_path}{primary}")
    return 0


def move(repo: str, root: str, home: Path | None = None, yes: bool = False, break_lock: bool = False) -> int:
    import sys
    from mmw.core.format import fmt_size
    from mmw.site import ops
    from mmw.site.lock import LockHeld
    try:
        h = _home(home, break_lock)
        size = ops.move_size(h, repo, root)
        print(f"moving {repo} to {root} copies {fmt_size(size)} into its store")
        if not yes:
            if not sys.stdin.isatty():
                eprint("pass --yes to move it (no terminal to ask on)")
                return 1
            if input("Move it? [y/N] ").strip().lower() not in ("y", "yes"):
                return 1
        r = ops.move(h, repo, root)
    except LockHeld as e:
        eprint(str(e))
        return 5
    except (ValueError, LookupError, FileNotFoundError) as e:
        eprint(str(e))
        return 1
    print(f"moved: {r.files} file(s), {fmt_size(r.copied_bytes)} copied")
    for note in r.tls_notes:
        print(f"! {note}")
    if r.nginx_changed:
        print(f"! nginx changed in {', '.join(r.nginx_changed)} — recreate them on the serving host")
    print("the old root still serves its copy until you run `mmw site tidy` (the next apply does it too)")
    return 0


def tidy(home: Path | None = None, break_lock: bool = False) -> int:
    from mmw.site import ops
    from mmw.site.lock import LockHeld
    try:
        done = ops.tidy(_home(home, break_lock))
    except LockHeld as e:
        eprint(str(e))
        return 5
    except (ValueError, FileNotFoundError) as e:
        eprint(str(e))
        return 1
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


def ca_load(cert: Path, key: Path, home: Path | None = None) -> int:
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


def ca_make(out: Path, name: str, encrypt: bool = False, home: Path | None = None) -> int:
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


def ca_show(home: Path | None = None) -> int:
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


def certs(home: Path | None = None, days: int = 730, force: bool = False) -> int:
    from mmw.core.certs import CAKeyEncrypted
    from mmw.site import certs as site_certs
    try:
        h = _home(home, False)
        try:
            results = site_certs.sign_roots(h, password=None, days=days, force=force)
        except CAKeyEncrypted:
            password = _passphrase()
            if password is None:
                eprint("the CA key is encrypted — run this in a terminal, or set MMW_CA_PASSPHRASE")
                return 1
            results = site_certs.sign_roots(h, password=password, days=days, force=force)
    except (ValueError, FileNotFoundError) as e:
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
