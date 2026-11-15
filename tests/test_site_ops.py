"""Serve any kept version of a repository, hold it, move it forward."""
import pytest
from typer.testing import CliRunner

from mmw import cli
from mmw.site import ops
from mmw.site.apply import apply_bundle
from mmw.site.db import SiteDB

from site_points import nth, pinned
from test_site_apply import _bundle, _tree, env  # noqa: F401 — the site fixture
from test_snapshot_repo_versions import _change_a, _key
from test_dry_run_totals import two_mirrors  # noqa: F401


def _three(env, tmp_path):  # noqa: F811
    """A site with a at v1, v2, v3 (serving v3) and b at v1."""
    cfg, net, md, home, root = env
    quiet = lambda *a: None  # noqa: E731
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=quiet)
    trees = [_tree(root)]
    for i in (2, 3):
        _change_a(md) if i == 2 else _bump(md)
        apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / str(i))), say=quiet)
        trees.append(_tree(root))
    return cfg, home, root, trees


def _bump(md):
    from mmw.core.pool import KIND_META, KIND_PKG, Pool
    from test_dry_run_totals import SHARED, _index, _sha
    files = {"shared.deb": SHARED, "a3.deb": b"three" * 9}
    with Pool(md) as pool:
        refs = []
        for fname, data in {**files, "Packages": _index(files)}.items():
            pool.add(data, _sha(data), "sha256", fname, len(data))
            refs.append((_sha(data), fname, KIND_META if fname == "Packages" else KIND_PKG))
        pool.reconcile_refs("a", refs)


def test_serve_older_pins_and_matches_it(env, tmp_path):  # noqa: F811
    cfg, home, root, trees = _three(env, tmp_path)
    ops.serve(home, "a", nth(home, "a", 1))
    assert _tree(root) == trees[0] and pinned(home, _key(cfg, "a"))
    ops.serve(home, "A", nth(home, "a", 3))
    assert _tree(root) == trees[2]
    ops.forward(home, "a")                               # forward lets it move on again
    assert not pinned(home, _key(cfg, "a"))


def test_serve_unknown_version_lists_available(env, tmp_path):  # noqa: F811
    _cfg, home, _root, _ = _three(env, tmp_path)
    with pytest.raises(ValueError, match="kept: .*·.*·"):
        ops.serve(home, "a", "abcdef")
    with pytest.raises(LookupError, match="this network has"):
        ops.serve(home, "nope", "abcdef")


def test_forward_all_clears_holds(env, tmp_path):  # noqa: F811
    cfg, home, root, trees = _three(env, tmp_path)
    ops.serve(home, "a", nth(home, "a", 2))
    assert ops.forward(home, None) == [_key(cfg, "a")]
    assert _tree(root) == trees[2] and not pinned(home, _key(cfg, "a"))
    with SiteDB(home) as db:
        assert not any(s.held for s in db.repo_states())


def test_hold_then_apply_keeps_serving(env, tmp_path):  # noqa: F811
    cfg, net, md, home, root = env
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=lambda *a: None)
    ops.hold(home, "a")
    before = _tree(root)
    _change_a(md)
    r = apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "2")), say=lambda *a: None)
    assert r.stored_held == [_key(cfg, "a")] and _tree(root) == before


def test_cli_serve_needs_yes_without_tty(env, tmp_path):  # noqa: F811
    _cfg, home, root, trees = _three(env, tmp_path)
    run = lambda *a: CliRunner().invoke(cli.app, ["site", *a])  # noqa: E731
    first, newest = nth(home, "a", 1), nth(home, "a", 3)
    r = run("serve", "a", first)
    assert r.exit_code == 1 and "--yes" in r.output and _tree(root) == trees[2]
    r = run("serve", "a", first, "--yes")
    assert r.exit_code == 0, r.output
    assert "now serving files" in r.output and newest in r.output and "nothing to copy" in r.output
    assert _tree(root) == trees[0]
    r = run("forward", "--all")
    assert r.exit_code == 0 and _tree(root) == trees[2]
    r = run("hold", "a")
    assert r.exit_code == 0 and "held" in r.output


def test_serve_says_each_file_it_places(env, tmp_path):  # noqa: F811
    _cfg, home, _root, _ = _three(env, tmp_path)
    placed = []
    ops.serve(home, "a", nth(home, "a", 1), progress=placed.append)
    assert placed and all(isinstance(p, str) for p in placed)
