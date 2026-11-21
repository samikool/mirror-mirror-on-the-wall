"""Edges of adopting apply.py trees the final review found: adopting twice,
adopting into a mirror that serves the same base, foreign bundles, removing
an original that holds a root, renames onto stale keys."""
import json

import pytest

from mmw.site import home as H
from mmw.site.adopt import adopt_apply_tree, find_apply_tree, originals, remove_original
from mmw.site.db import SiteDB

from test_site_adopt_apply import BASE, far, _mmw_bundle  # noqa: F401
from test_bundle_snapshots import states  # noqa: F401


def test_converting_twice_is_refused(far):  # noqa: F811
    *_x, dest = far
    adopt_apply_tree(find_apply_tree(dest))
    assert find_apply_tree(dest) is None                      # the record is set aside once converted
    assert (dest / ".mmw" / "manifest.apply.jsonl").exists()


def test_a_converted_root_is_never_converted_again(far):  # noqa: F811
    *_x, dest = far
    tree = find_apply_tree(dest)
    adopt_apply_tree(tree)
    with pytest.raises(ValueError, match="already an mmw mirror"):
        adopt_apply_tree(tree)


def test_into_a_mirror_serving_the_base_joins_that_repository(far, tmp_path):  # noqa: F811
    *_x, dest = far
    home = H.init_root(tmp_path / "srv", name="main", set_id="s1", network="lab")
    first = adopt_apply_tree(find_apply_tree(dest), home=home)
    with SiteDB(home) as db:
        db.rename_repo(first.repo, "https://archive.ubuntu.com/ubuntu", "Ubuntu", [BASE])
        before = db.network_files()
    again = adopt_apply_tree(find_apply_tree(dest), home=home)
    with SiteDB(home) as db:
        assert [s.repo for s in db.repo_states()] == ["https://archive.ubuntu.com/ubuntu"]
        assert db.network_files() == before                   # a mirror's own files are never replaced
    assert again.repo == "https://archive.ubuntu.com/ubuntu"


def test_another_networks_tree_keeps_its_network_files_out(far, tmp_path):  # noqa: F811
    *_x, dest = far
    home = H.init_root(tmp_path / "srv", name="main", set_id="s1", network="elm")
    r = adopt_apply_tree(find_apply_tree(dest), home=home)
    with SiteDB(home) as db:
        assert db.network_files() == {}
    assert any("network lab" in line for line in r.lines)


def test_removing_an_original_that_holds_a_root_is_refused(far, tmp_path):  # noqa: F811
    *_x, dest = far
    home = H.init_root(tmp_path / "srv", name="main", set_id="s1", network="lab")
    adopt_apply_tree(find_apply_tree(dest), home=home)
    H.init_root(dest / "inner", name="other", set_id="s9", network="x")        # a mirror inside it since
    with pytest.raises(ValueError, match="holds a mirror"):
        remove_original(home, dest)
    assert dest.exists()


def test_refused_content_is_said_before_removing(far, tmp_path):  # noqa: F811
    from test_bundle_snapshots import _tree
    *_x, dest = far
    deb = next(p for p in _tree(dest) if p.endswith(".deb"))
    (dest / deb).write_bytes(b"damaged")
    home = H.init_root(tmp_path / "srv", name="main", set_id="s1", network="lab")
    adopt_apply_tree(find_apply_tree(dest), home=home)
    (o,) = originals(home)
    assert o.refused == [BASE]


def test_rename_over_a_stale_hold(far, tmp_path):  # noqa: F811
    *_x, dest = far
    r = adopt_apply_tree(find_apply_tree(dest.parent / "far"))
    from mmw.site.home import root_home
    with SiteDB(root_home(dest)) as db:
        db.set_meta("hold:https://x", "")
        db.set_meta(f"local-repo:{r.repo}", "oak")
        db.rename_repo(r.repo, "https://x", "X", [BASE])
        assert db.get_meta("local-repo:https://x") == "oak"


def test_a_foreign_bundle_claims_the_adopted_tree(far, tmp_path, monkeypatch):  # noqa: F811
    """An adopted tree is one repository whichever network's bundle brings it."""
    from mmw.core import snapshots
    from mmw.site.apply import apply_bundle
    from mmw.site.home import root_home
    from test_bundle_snapshots import _state
    from test_site_apply import _rewrite
    cfg, net, md, *_r, dest = far
    adopt_apply_tree(find_apply_tree(dest))
    _state(md, 4)
    monkeypatch.setattr(snapshots, "now_stamp", lambda: "20991231T000000Z")
    src = _mmw_bundle(cfg, net, md, tmp_path / "m.tar").out_path

    def rename(member, data):
        if member.name == ".mmw/manifest.jsonl":
            head, _, rest = data.partition(b"\n")
            h = json.loads(head)
            h["network"] = "elm"
            return json.dumps(h, sort_keys=True).encode() + b"\n" + rest
        return data
    other = _rewrite(src, tmp_path / "elm.tar", rename)
    apply_bundle(root_home(dest), str(other), say=lambda *a: None)
    with SiteDB(root_home(dest)) as db:
        (state,) = db.repo_states()
        assert state.repo != f"adopted:{BASE}" and state.bases == [BASE]
