"""What each kept version costs, and letting old ones go — never the one
being served, never a held repository's newest."""
import pytest

from mmw.site import home as H
from mmw.site import ops
from mmw.site.apply import apply_bundle
from mmw.site.db import SiteDB
from mmw.site.store import Store

from test_site_apply import env  # noqa: F401
from test_site_ops import _three
from test_snapshot_repo_versions import _key
from test_dry_run_totals import two_mirrors  # noqa: F401


def test_space_frees_counts_only_unshared(env, tmp_path):  # noqa: F811
    cfg, home, root, _ = _three(env, tmp_path)
    rep = ops.space(home)
    a = {v.number: v for v in rep.repos[_key(cfg, "a")]}
    assert set(a) == {1, 2, 3} and a[3].serving and not a[1].serving
    assert 0 < a[1].frees < a[1].total                   # shared.deb is shared: not freed
    assert rep.history_bytes == a[1].frees + a[2].frees
    assert rep.used > 0 and rep.free > 0


def test_delete_refuses_served_and_held_newest(env, tmp_path):  # noqa: F811
    cfg, home, root, _ = _three(env, tmp_path)
    with pytest.raises(ValueError, match="being served"):
        ops.delete_version(home, "a", 3)
    ops.serve(home, "a", 1)                              # held at v1: v3 is the held repository's newest
    with pytest.raises(ValueError, match="newest of a held repository"):
        ops.delete_version(home, "a", 3)
    with SiteDB(home) as db:
        assert [v.number for v in db.versions(_key(cfg, "a"))] == [3, 2, 1]


def test_delete_frees_store_blobs(env, tmp_path):  # noqa: F811
    cfg, home, root, _ = _three(env, tmp_path)
    before = sum(1 for p in (root / ".mmw/store").rglob("*") if p.is_file())
    freed = ops.delete_version(home, "a", 1)
    after = sum(1 for p in (root / ".mmw/store").rglob("*") if p.is_file())
    assert freed > 0 and after < before
    with SiteDB(home) as db:
        assert all(Store(root).has(c) for c in db.referenced_checksums())


def test_retention_keeps_newest_and_protected(env, tmp_path):  # noqa: F811
    cfg, home, root, _ = _three(env, tmp_path)
    with SiteDB(home) as db:
        assert ops.apply_retention(db, Store(root), 1) == [(_key(cfg, "a"), 2), (_key(cfg, "a"), 1)]
        assert [v.number for v in db.versions(_key(cfg, "a"))] == [3]


def test_retention_runs_after_apply(env, tmp_path):  # noqa: F811
    cfg, net, md, home, root = env
    s = H.load_site(home)
    H.save_settings(home, s.model_copy(update={"keep_versions": 1}))
    from test_site_apply import _bundle
    from test_snapshot_repo_versions import _change_a
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=lambda *a: None)
    _change_a(md)
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "2")), say=lambda *a: None)
    with SiteDB(home) as db:
        assert [v.number for v in db.versions(_key(cfg, "a"))] == [2]


def test_cli_space_delete_settings(env, tmp_path):  # noqa: F811
    from typer.testing import CliRunner
    from mmw import cli
    _cfg, home, _root, _ = _three(env, tmp_path)
    run = lambda *a: CliRunner().invoke(cli.app, ["site", *a, "--home", str(home)])  # noqa: E731
    r = run("space")
    assert r.exit_code == 0 and "v3" in r.output and "serving" in r.output and "frees" in r.output
    r = run("delete", "a", "v1", "--yes")
    assert r.exit_code == 0 and "freed" in r.output
    r = run("delete", "a", "v3", "--yes")
    assert r.exit_code == 1 and "being served" in r.output
    r = run("settings", "--keep", "2")
    assert r.exit_code == 0 and H.load_site(home).keep_versions == 2
    r = run("settings", "--keep-all")
    assert r.exit_code == 0 and H.load_site(home).keep_versions is None
