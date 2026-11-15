"""Points imported into tracks, the newest complete (or pinned) one chosen
per track, the choice built into a state — and old sites migrated."""
import io

import pytest
from site_apt_rows import Repo

from mmw.site import home as H
from mmw.site.db import SiteDB
from mmw.site.roots import Roots
from mmw.site.store import Store
from mmw.site.timeline import choose, ensure_state, import_rows, is_complete, migrate

DATES = {1: "Fri, 01 Nov 2026 00:00:00 UTC", 2: "Sat, 02 Nov 2026 00:00:00 UTC",
         3: "Sun, 03 Nov 2026 00:00:00 UTC", 4: "Mon, 04 Nov 2026 00:00:00 UTC"}
REPO = "https://ex.org/debian/"


def _snap(n: int, missing: bool = False) -> Repo:
    """Debian bookworm as synced on day `n`: one package per day so far."""
    r = Repo()
    pkgs = [r.deb(f"pool/main/p/p_{i}.deb") for i in range(1, n + 1)]
    r.suite("bookworm", DATES[n], pkgs)
    return r


@pytest.fixture
def site(tmp_path):
    home = H.init_root(tmp_path / "srv", name="main", set_id="s1", network="oak")
    roots = Roots(H.load_site(home))
    store = roots.store(roots.primary)
    with SiteDB(home) as db:
        yield db, store, roots


def _put(store: Store, r: Repo, skip: set[str] = frozenset()) -> None:
    for cs, data in r.blobs.items():
        if cs not in skip:
            store.add_stream(io.BytesIO(data), expect=cs)


def _import(db, store, r: Repo, stamp: str, *, skip=frozenset()):
    _put(store, r, skip)
    sizes = {cs: len(b) for cs, b in r.blobs.items()}
    return import_rows(db, store, REPO, "debian", [r.base], r.rows, sizes, stamp, None, set())


def _dates(db):
    (track,) = [t for t in db.tracks(REPO) if t.name]
    return [p.date[:8] for p in db.points(track.id)]


def test_union_of_two_histories(site):
    db, store, _ = site
    for n in (1, 2, 4):                        # network A
        _import(db, store, _snap(n), f"2026110{n}T120000Z")
    for n in (1, 3):                           # network B
        _import(db, store, _snap(n), f"2026111{n}T120000Z")
    assert _dates(db) == ["20261104", "20261103", "20261102", "20261101"]


def test_choose_newest_complete(site):
    db, store, _ = site
    _import(db, store, _snap(1), "20261101T120000Z")
    newest = _snap(2)
    missing = next(cs for p, cs in newest.rows.items() if p.endswith("p_2.deb"))
    out = _import(db, store, newest, "20261102T120000Z", skip={missing})
    assert any(o.status == "incomplete" and o.missing == 1 for o in out)
    chosen, _w = choose(db, store, REPO)
    (track,) = [t for t in db.tracks(REPO) if t.name]
    assert db.point(chosen[track.id]).date.startswith("20261101")
    assert is_complete(db, store, db.points(track.id)[0].id) == 1


def test_choose_honours_pin(site):
    db, store, _ = site
    for n in (1, 2):
        _import(db, store, _snap(n), f"2026110{n}T120000Z")
    (track,) = [t for t in db.tracks(REPO) if t.name]
    old = db.points(track.id)[-1]
    db.set_pin(track.id, old.id)
    assert choose(db, store, REPO)[0][track.id] == old.id


def test_pinned_incomplete_falls_back_with_warning(site):
    db, store, _ = site
    first = _snap(1)
    _import(db, store, first, "20261101T120000Z")
    _import(db, store, _snap(2), "20261102T120000Z")
    (track,) = [t for t in db.tracks(REPO) if t.name]
    db.set_pin(track.id, db.points(track.id)[-1].id)
    # A file only the pinned point holds goes missing.
    store.path(first.rows["ex.org/debian/dists/bookworm/Release"]).unlink()
    chosen, warnings = choose(db, store, REPO)
    assert db.point(chosen[track.id]).date.startswith("20261102")
    assert len(warnings) == 1 and "pinned" in warnings[0]


def test_ensure_state_reuses(site):
    db, store, _ = site
    _import(db, store, _snap(1), "20261101T120000Z")
    chosen, _ = choose(db, store, REPO)
    vid, _w = ensure_state(db, store, REPO, chosen)
    assert ensure_state(db, store, REPO, chosen)[0] == vid
    files = db.version_files(vid)
    assert "ex.org/debian/pool/main/p/p_1.deb" in files and "ex.org/debian/dists/bookworm/Release" in files


def test_releaseless_repo_is_one_stamp_ordered_track(site):
    db, store, _ = site
    for n, data in ((1, b"one"), (2, b"two")):
        r = Repo("example.com/a")
        r.put("a.deb", data)
        r.put("Packages", b"Package: a\n" + data)
        _put(store, r)
        import_rows(db, store, "A", "a", [r.base], r.rows, {cs: len(b) for cs, b in r.blobs.items()},
                    f"2026110{n}T000000Z", None, set())
    (track,) = db.tracks("A")
    assert track.name == "" and [p.date for p in db.points(track.id)] == ["20261102T000000Z", "20261101T000000Z"]


def test_migrate_keeps_what_is_served_and_holds(site):
    db, store, roots = site
    versions = []
    for n in (1, 2, 3):
        r = _snap(n)
        _put(store, r)
        versions.append(db.add_version(REPO, "debian", n, f"d{n}", f"2026110{n}T120000Z", None, [r.base],
                                       [(p, cs, len(r.blobs[cs])) for p, cs in r.rows.items()]))
    db.set_serving(REPO, versions[1], held=True)
    served_before = db.version_files(versions[1])
    assert migrate(db, roots) is True
    assert db.version_files(db.repo_state(REPO).serving.id) == served_before
    (track,) = [t for t in db.tracks(REPO) if t.name]
    assert db.point(track.pinned).date.startswith("20261102")
    assert len(db.points(track.id)) == 3
    assert db.state_points(versions[1])
    assert migrate(db, roots) is False
