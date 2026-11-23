"""A point's cached "complete" mark is set only from what the store holds:
an apply that dies before its files land leaves no mark, so dated trees are
never built from files that never arrived."""
import pytest
from site_apt_rows import Repo

from mmw.site import dated
from mmw.site.db import SiteDB
from mmw.site.home import init_root
from mmw.site.roots import Roots
from mmw.site.home import load_site
from mmw.site.timeline import import_points, is_complete
from mmw.site.tracks import split_tracks

BASE = "ex.org/debian"
REPO = "debian"


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    home = init_root(tmp_path / "srv", name="main", set_id="s1", network="oak")
    roots = Roots(load_site(home))
    return home, roots.store(roots.primary)


def _points():
    repo = Repo(BASE)
    repo.suite("bookworm", "01 Nov 2026 00:00:00 UTC", [])
    stamp = "20261101T000000Z"
    return repo, [(d, stamp, None) for d in split_tracks(repo.rows, [BASE], repo.read, stamp)]


def test_a_dying_apply_leaves_no_complete_mark(site):
    home, store = site
    repo, points = _points()
    arrived = set(repo.blobs)                    # counted as arrived; never put in the store
    sizes = {c: len(b) for c, b in repo.blobs.items()}
    with SiteDB(home) as db:
        outcomes = import_points(db, store, REPO, "Debian", [BASE], points, sizes, arrived)
        (pid,) = [o.point for o in outcomes]
        assert outcomes[0].status == "new"       # arrived counts toward what the apply reports
        # The apply dies here, before the blobs land.
        assert not db.known_complete(pid)
        assert dated.days(db, store, REPO) == {}


def test_marked_once_the_files_land(site):
    import io
    home, store = site
    repo, points = _points()
    sizes = {c: len(b) for c, b in repo.blobs.items()}
    with SiteDB(home) as db:
        outcomes = import_points(db, store, REPO, "Debian", [BASE], points, sizes, set(repo.blobs))
        (pid,) = [o.point for o in outcomes]
        assert is_complete(db, store, pid) and not db.known_complete(pid)   # still nothing in the store
        for c, data in repo.blobs.items():
            store.add_stream(io.BytesIO(data), expect=c)
        assert is_complete(db, store, pid) == 0 and db.known_complete(pid)
        assert list(dated.days(db, store, REPO)) == ["2026-11-01"]
