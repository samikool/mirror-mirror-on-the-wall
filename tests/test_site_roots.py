"""A site with several roots: repositories placed in each, moved between
them, every root with its own store and its own nginx."""
from typer.testing import CliRunner

from mmw import cli
from mmw.site import home as H
from mmw.site import ops
from mmw.site.db import SiteDB

from test_site_apply import _bundle, _tree, env  # noqa: F401
from test_site_ops import _three
from test_snapshot_repo_versions import _key
from test_dry_run_totals import two_mirrors  # noqa: F401

quiet = lambda *a: None  # noqa: E731


def test_legacy_settings_load_as_main(tmp_path):
    (tmp_path / "h").mkdir()
    (tmp_path / "h/site.toml").write_text(f'root = "{tmp_path / "srv"}"\nkeep_versions = 3\n')
    s = H.load_site(tmp_path / "h")
    assert [(r.name, r.path, r.serve_path) for r in s.roots] == [("main", tmp_path / "srv", tmp_path / "srv")]
    assert s.root == tmp_path / "srv" and s.keep_versions == 3


def test_root_add_and_list(tmp_path):
    H.init_site(tmp_path / "h", tmp_path / "srv")
    run = lambda *a: CliRunner().invoke(cli.app, ["site", "root", *a, "--home", str(tmp_path / "h")])  # noqa: E731
    r = run("add", "pypi", str(tmp_path / "pypi"), "--serve-path", "/export/pypi")
    assert r.exit_code == 0, r.output
    s = H.load_site(tmp_path / "h")
    assert [(r.name, str(r.serve_path)) for r in s.roots][1] == ("pypi", "/export/pypi")
    assert (tmp_path / "pypi/.mmw/store").is_dir()
    r = run("list")
    assert "main" in r.output and "pypi" in r.output and "/export/pypi" in r.output
    assert run("add", "pypi", str(tmp_path / "x")).exit_code == 1               # names are unique


def test_repo_state_root_defaults_to_primary(env, tmp_path):  # noqa: F811
    cfg, home, root, _ = _three(env, tmp_path)
    with SiteDB(home) as db:
        assert db.repo_state(_key(cfg, "a")).root is None
        db.set_root(_key(cfg, "a"), "second")
        assert db.repo_state(_key(cfg, "a")).root == "second"


def test_space_reports_each_root(env, tmp_path):  # noqa: F811
    cfg, home, root, _ = _three(env, tmp_path)
    s = H.load_site(home)
    H.save_settings(home, s.model_copy(update={"roots": [*s.roots, H.RootConfig(
        name="second", path=tmp_path / "second", serve_path=tmp_path / "second")]}))
    (tmp_path / "second/.mmw/store").mkdir(parents=True)
    rep = ops.space(home)
    assert set(rep.roots) == {"main", "second"} and all(free > 0 for _used, free in rep.roots.values())
