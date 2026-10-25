"""The mirror set: roots found through this machine's recents, by identity."""
import pytest

from mmw.site import home as H
from mmw.site import recents
from mmw.site.db import SiteDB
from mmw.site.mset import open_set


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def _root(path, name, set_id="s1", network="debian", members=None, bases=()):
    home = H.init_root(path, name=name, set_id=set_id, network=network)
    if members:
        s = H.load_site(home)
        H.save_settings(home, s.model_copy(update={"members": members}))
    with SiteDB(home) as db:
        for i, base in enumerate(bases):
            db.add_version(f"k{i}-{name}", base.split("/")[-1], 1, "d", "20261025T000000Z", None, [base], [])
    recents.remember(str(path))
    return path


def test_recents_round_trip(tmp_path):
    recents.remember("a")
    recents.remember("b")
    recents.remember("a")
    assert recents.known() == ["a", "b"]
    recents.forget("a")
    assert recents.known() == ["b"]


def test_open_set_groups_by_identity(tmp_path):
    _root(tmp_path / "r1", "main")
    _root(tmp_path / "r2", "nas")
    _root(tmp_path / "r3", "other", set_id="s2", network="ubuntu")
    with pytest.raises(ValueError, match="debian.*ubuntu|ubuntu.*debian"):
        open_set()
    assert sorted(r.info.name for r in open_set(set_id="s1").roots) == ["main", "nas"]


def test_missing_member_is_named(tmp_path):
    _root(tmp_path / "r1", "main", members=["main", "nas"])
    assert open_set().missing == ["nas"]


def test_one_root_two_paths_is_one_root(tmp_path):
    r = _root(tmp_path / "r1", "main")
    (tmp_path / "link").symlink_to(r)
    recents.remember(str(tmp_path / "link"))
    assert len(open_set().roots) == 1


def test_owner_and_root_for_repo(tmp_path):
    _root(tmp_path / "r1", "main", bases=["deb.debian.org/debian"])
    _root(tmp_path / "r2", "nas", bases=["pypi.org/simple"])
    m = open_set()
    assert m.owner("deb.debian.org").info.name == "main"
    assert m.owner("pypi.org").info.name == "nas"
    assert m.owner("example.org") is None
    root, key = m.root_for_repo("simple")
    assert (root.info.name, key) == ("nas", "k0-nas")
    with pytest.raises(KeyError, match="debian"):
        m.root_for_repo("nothing")


def test_no_set_opened_yet(tmp_path):
    with pytest.raises(LookupError, match="mmw site open"):
        open_set()


def test_free_bytes_of_a_root(tmp_path):
    _root(tmp_path / "r1", "main")
    assert open_set().roots[0].free_bytes() > 0
