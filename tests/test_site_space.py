"""What each kept version costs, and letting old ones go — never the one
being served, never a held repository's newest."""
import pytest

from mmw.site import home as H
from mmw.site import ops
from mmw.site.apply import apply_bundle
from mmw.site.db import SiteDB
from mmw.site.roots import Roots
from mmw.site.store import Store

from test_site_apply import env  # noqa: F401
from test_site_ops import _three
from test_snapshot_repo_versions import _key
from test_dry_run_totals import two_mirrors  # noqa: F401


def _points(home, key):
    from site_points import nth
    return [nth(home, key, n) for n in (1, 2, 3)]


def test_space_frees_counts_only_unshared(env, tmp_path):  # noqa: F811
    cfg, home, root, _ = _three(env, tmp_path)
    rep = ops.space(home)
    rows = rep.repos[_key(cfg, "a")]                    # newest first
    assert len(rows) == 3 and rows[0].serving and not rows[2].serving and rows[0].suite == "files"
    assert 0 < rows[2].frees < rows[2].total             # shared.deb is shared: not freed
    assert rep.history_bytes == rows[1].frees + rows[2].frees
    assert rep.used > 0 and rep.free > 0


def test_delete_refuses_served_previous_and_pinned(env, tmp_path):  # noqa: F811
    cfg, home, root, _ = _three(env, tmp_path)
    p1, p2, p3 = _points(home, "a")
    with pytest.raises(ValueError, match="being served"):
        ops.delete_point(home, "a", p3)
    with pytest.raises(ValueError, match="served before"):
        ops.delete_point(home, "a", p2)
    ops.serve(home, "a", p1)                             # pinned at the oldest
    with pytest.raises(ValueError, match="pinned"):
        ops.delete_point(home, "a", p1)
    with SiteDB(home) as db:
        (t,) = db.tracks(_key(cfg, "a"))
        assert len(db.points(t.id)) == 3


def test_delete_frees_store_blobs(env, tmp_path):  # noqa: F811
    cfg, home, root, _ = _three(env, tmp_path)
    before = sum(1 for p in (root / ".mmw/store").rglob("*") if p.is_file())
    freed = ops.delete_point(home, "a", _points(home, "a")[0])
    after = sum(1 for p in (root / ".mmw/store").rglob("*") if p.is_file())
    assert freed > 0 and after < before
    with SiteDB(home) as db:
        assert all(Store(root).has(c) for c in db.referenced_checksums())


def test_retention_keeps_newest_and_protected(env, tmp_path):  # noqa: F811
    cfg, home, root, _ = _three(env, tmp_path)
    with SiteDB(home) as db:
        gone = ops.apply_retention(db, Roots(H.load_site(home)), 1)
        assert [(r, s) for r, s, _d in gone] == [(_key(cfg, "a"), "files")]    # the oldest; v2 is previous
        (t,) = db.tracks(_key(cfg, "a"))
        assert len(db.points(t.id)) == 2


def test_retention_runs_after_apply(env, tmp_path):  # noqa: F811
    cfg, net, md, home, root = env
    s = H.load_site(home)
    H.save_settings(home, s.model_copy(update={"keep_versions": 1}))
    from test_site_apply import _bundle
    from test_site_ops import _bump
    from test_snapshot_repo_versions import _change_a
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=lambda *a: None)
    _change_a(md)
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "2")), say=lambda *a: None)
    _bump(md)
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "3")), say=lambda *a: None)
    with SiteDB(home) as db:
        (t,) = db.tracks(_key(cfg, "a"))
        assert len(db.points(t.id)) == 2                  # newest, and the one served before it


def test_cli_space_delete_settings(env, tmp_path):  # noqa: F811
    from typer.testing import CliRunner
    from mmw import cli
    _cfg, home, _root, _ = _three(env, tmp_path)
    p1, _p2, p3 = _points(home, "a")
    run = lambda *a: CliRunner().invoke(cli.app, ["site", *a])  # noqa: E731
    r = run("space")
    assert r.exit_code == 0 and "files" in r.output and "serving" in r.output and "frees" in r.output
    r = run("delete", "a", p1, "--yes")
    assert r.exit_code == 0 and "freed" in r.output
    r = run("delete", "a", p3)                            # no terminal: asks for --yes
    assert r.exit_code == 1 and "--yes" in r.output
    r = run("delete", "a", p3, "--yes")                   # the served one: warned, then done
    assert r.exit_code == 0 and "being served" in r.output and "freed" in r.output
    r = run("settings", "--keep", "2")
    assert r.exit_code == 0 and H.load_site(home).keep_versions == 2
    r = run("settings", "--keep-all")
    assert r.exit_code == 0 and H.load_site(home).keep_versions is None
