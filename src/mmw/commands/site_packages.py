"""`mmw site packages` — local debs a site adds to a repository it serves."""
import sys
from pathlib import Path

import typer

from mmw.commands.common import eprint


def _local(root: Path | None, repo_ref: str | None = None):
    """(home, store, repo key or None) of the local root holding `repo_ref`."""
    from mmw.commands.site import _set
    from mmw.site.mset import LocalRoot
    mset = _set(root)
    if repo_ref is not None:
        r, key = mset.root_for_repo(repo_ref)
        if not isinstance(r, LocalRoot):
            raise FileNotFoundError(f"{repo_ref} is on a root reached over ssh — run this on that host")
        return r.home, r.store(), key
    from mmw.commands.signing_key import _the_local_root
    local = _the_local_root(mset, root)
    if local is None:
        raise FileNotFoundError("no root of this mirror is on this machine — run it where one is")
    return local.home, local.store(), None


def _run(act) -> int:
    from mmw.core.layers import PackagesError
    from mmw.core.signing import SigningError
    from mmw.site.lock import LockHeld
    try:
        return act() or 0
    except PackagesError as e:
        eprint(str(e))
        return 2 if e.usage else 1
    except (SigningError, LockHeld, ValueError, LookupError, FileNotFoundError, KeyError) as e:
        eprint(str(e).strip("'\""))
        return 1


def _relayer(home, key) -> None:
    from mmw.site.ops import relayer
    result = relayer(home, key)
    if result.held:
        eprint(f"held: {result.held}")
    for w in result.warnings:
        eprint(w)
    typer.echo(f"serving with local packages ({result.placed} placed, {result.removed} removed)"
               if not result.held else "still serving what it was")


def _ask(clash) -> str:
    what = ("the same version as upstream's, with different contents" if clash.kind == "same-version"
            else f"older than upstream's {clash.upstream_version}")
    while True:
        a = input(f"{clash.filename}: {clash.package} {clash.version} is {what}. "
                  "[k]eep mine / [d]rop mine / [c]ancel? ").strip().lower()[:1]
        if a in ("k", "d", "c"):
            return {"k": "keep", "d": "drop", "c": "cancel"}[a]


def add(paths, *, repo, suite, component, resign, unsigned, on_clash, root) -> int:
    from mmw.site.db import SiteDB
    from mmw.site.layers import add_site_packages, check_site_clashes

    def act():
        if resign and unsigned:
            eprint("--resign and --unsigned are one choice — pass one")
            return 2
        home, store, key = _local(root, repo)
        signing = "resign" if resign else "unsigned" if unsigned else None
        with SiteDB(home) as db:
            layer = db.layer(key)
            if signing is None and (layer is None or layer.signing == "upstream"):
                from mmw.core.layers import PackagesError, needs_signing
                raise PackagesError(needs_signing(db.repo_state(key).label), usage=True)   # before any question
            _s, _c, clashes = check_site_clashes(home, db, store, key, paths, suite, component)
            decisions = {c.filename: (on_clash or (_ask(c) if sys.stdin.isatty() else "keep")) for c in clashes}
            for line in add_site_packages(home, db, store, key, paths, suite=suite, component=component,
                                          signing=signing, key=resign, decisions=decisions):
                typer.echo(line)
        _relayer(home, key)
    return _run(act)


def list_(root) -> int:
    from mmw.site.db import SiteDB
    from mmw.site.layers import list_site_packages

    def act():
        home, _store, _ = _local(root)
        with SiteDB(home) as db:
            rows = list_site_packages(home, db)
        if not rows:
            typer.echo("No local packages.")
        for label, location, name, _repo in rows:
            typer.echo(f"{label}  {location}  {name}")
    return _run(act)


def remove(filename, *, repo, root, location=None) -> int:
    from mmw.site.layers import remove_site_package

    def act():
        home, _store, key = _local(root, repo)
        for p in remove_site_package(home, key, filename, location=location):
            typer.echo(f"removed {p}")
        _relayer(home, key)
    return _run(act)


def signing(repo, *, resign, unsigned, upstream, contents, drop_valid_until, root) -> int:
    from mmw.site.db import SiteDB
    from mmw.site.layers import set_site_signing

    def act():
        if sum(bool(x) for x in (resign, unsigned, upstream)) > 1:
            eprint("--resign, --unsigned and --upstream are one choice — pass one")
            return 2
        home, _store, key = _local(root, repo)
        mode = "resign" if resign else "unsigned" if unsigned else "upstream" if upstream else None
        with SiteDB(home) as db:
            set_site_signing(home, db, key, signing=mode, key=resign, contents=contents,
                             drop_valid_until=drop_valid_until)
        if mode == "unsigned":
            eprint("Unsigned is unsafe: clients need [trusted=yes] for this repository.")
        _relayer(home, key)
    return _run(act)


def sources_file(repo, fmt, root) -> int:
    from mmw.site.db import SiteDB
    from mmw.site.layers import site_sources_file

    def act():
        home, store, key = _local(root, repo)
        with SiteDB(home) as db:
            typer.echo(site_sources_file(home, db, store, key, fmt), nl=False)
    return _run(act)
