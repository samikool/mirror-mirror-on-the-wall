"""The site database: every version kept, what each repository serves, and
any switch left halfway."""
import pytest

from mmw.site.db import SiteDB


@pytest.fixture
def db(tmp_path):
    with SiteDB(tmp_path) as d:
        yield d


ROWS1 = [("h/a/x.deb", "c1", 10), ("h/a/Packages", "c2", 3)]
ROWS2 = [("h/a/x.deb", "c1", 10), ("h/a/y.deb", "c3", 7), ("h/a/Packages", "c4", 4)]


def _v(db, number, rows, digest=None):
    return db.add_version("R", "a", number, digest or f"d{number}", f"2026100{number}T000000Z", None,
                          ["h/a"], rows)


def test_add_version_idempotent_and_conflict(db):
    v1 = _v(db, 1, ROWS1)
    assert _v(db, 1, ROWS1) == v1
    with pytest.raises(ValueError):
        _v(db, 1, ROWS1, digest="other")
    assert db.version_files(v1) == {"h/a/x.deb": "c1", "h/a/Packages": "c2"}
    assert db.version("R", 1).id == v1 and db.version("R", 9) is None
    _v(db, 2, ROWS2)
    assert [v.number for v in db.versions("R")] == [2, 1]
    assert db.repo_state("R").bases == ["h/a"] and db.repo_state("R").label == "a"


def test_serving_and_held(db):
    v1, v2 = _v(db, 1, ROWS1), _v(db, 2, ROWS2)
    assert db.repo_state("R").serving is None
    db.set_serving("R", v1, held=True)
    st = db.repo_state("R")
    assert st.serving.id == v1 and st.held
    db.set_serving("R", v2, held=False)
    assert [(s.repo, s.serving.number, s.held) for s in db.repo_states()] == [("R", 2, False)]


def test_network_files_replace(db):
    db.set_network_files({"nginx/mirror.conf": "n1", "apply.py": "p1"}, sizes={"n1": 1, "p1": 2})
    db.set_network_files({"apply.py": "p2"}, sizes={"p2": 2})
    assert db.network_files() == {"apply.py": "p2"}


def test_switch_record_lifecycle(db):
    v1 = _v(db, 1, ROWS1)
    db.begin_switch("R", None, v1)
    db.switch_phase("R", "meta")
    assert db.pending_switches() == [("R", None, v1, "meta")]
    db.end_switch("R")
    assert db.pending_switches() == []


def test_sizes_and_exclusive_bytes(db):
    v1, v2 = _v(db, 1, ROWS1), _v(db, 2, ROWS2)
    assert db.version_sizes(v1) == (13, 3)          # c1 is shared with v2
    assert db.version_sizes(v2) == (21, 11)
    db.delete_version(v1)
    assert db.version("R", 1) is None
    assert db.referenced_checksums() == {"c1", "c3", "c4"}


def test_applied_log(db):
    db.record_applied("20261001T000000Z", None, "full")
    assert db.applied()[0][:3] == ("20261001T000000Z", None, "full")


def test_journal_mode_is_delete(db):
    assert db._conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
