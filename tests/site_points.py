"""Naming a site's points in tests: the nth oldest (1 = oldest) kept point
of a repository with one suite, as its six-hex digest."""


def nth(home, ref: str, n: int) -> str:
    from mmw.site import ops
    hist = ops.history(home, ref)
    named = [h for h in hist if h[0].name] or hist          # a Release's suite, if it has one
    ((_track, rows),) = named
    kept = [p for p, _total, _frees, mark in rows if mark != "stub"][::-1]
    return kept[n - 1].ident.split(":", 1)[-1][:6]


def pinned(home, key: str) -> bool:
    from mmw.site.db import SiteDB
    with SiteDB(home) as db:
        return any(t.pinned is not None for t in db.tracks(key))
