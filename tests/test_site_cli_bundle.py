"""A version by its bundle's date on the command line, and the whole mirror
served as it was after one bundle."""
import itertools

from typer.testing import CliRunner

from mmw import cli
from mmw.core import snapshots as _snaps
from mmw.site import ops
from mmw.site.mset import open_set
from test_site_apply import _tree, env  # noqa: F401 — the site fixture
from test_site_ops import _three
from test_dry_run_totals import two_mirrors  # noqa: F401


def _dated(env, tmp_path, monkeypatch):  # noqa: F811
    """_three's site, its bundles made on 1, 2 and 3 Oct 2026: a at v1–v3 (serving v3), b at v1."""
    days = itertools.count(1)
    monkeypatch.setattr(_snaps, "now_stamp", lambda: f"202610{next(days):02d}T120000Z")
    return _three(env, tmp_path)


def run(*a):
    return CliRunner().invoke(cli.app, ["site", *a])


def test_serve_by_date(env, tmp_path, monkeypatch):  # noqa: F811
    _cfg, home, root, trees = _dated(env, tmp_path, monkeypatch)
    r = run("serve", "a", "2oct", "--yes")
    assert r.exit_code == 0, r.output
    assert "serve v2" in r.output and _tree(root) == trees[1]


def test_delete_by_date(env, tmp_path, monkeypatch):  # noqa: F811
    cfg, home, root, trees = _dated(env, tmp_path, monkeypatch)
    r = run("delete", "a", "2026-10-01", "--yes")
    assert r.exit_code == 0, r.output
    assert [v.number for v, *_ in ops.history(home, "a")] == [3, 2]


def test_a_day_with_no_bundle_says_what_is_kept(env, tmp_path, monkeypatch):  # noqa: F811
    _dated(env, tmp_path, monkeypatch)
    r = run("serve", "a", "9oct", "--yes")
    assert r.exit_code == 1 and "v3 · 03 Oct 2026" in r.output


def test_serve_bundle_rolls_every_repo_back(env, tmp_path, monkeypatch):  # noqa: F811
    _cfg, home, root, trees = _dated(env, tmp_path, monkeypatch)
    r = run("serve-bundle", "2oct", "--yes")
    assert r.exit_code == 0, r.output
    assert "v3 → v2" in r.output and "already v1" in r.output     # b: the bundle didn't change it
    assert _tree(root) == trees[1]


def test_serve_bundle_needs_yes_without_tty(env, tmp_path, monkeypatch):  # noqa: F811
    _cfg, home, root, trees = _dated(env, tmp_path, monkeypatch)
    r = run("serve-bundle", "1oct")
    assert r.exit_code == 1 and "--yes" in r.output and _tree(root) == trees[2]


def test_serve_bundle_names_no_bundle(env, tmp_path, monkeypatch):  # noqa: F811
    _dated(env, tmp_path, monkeypatch)
    r = run("serve-bundle", "9oct", "--yes")
    assert r.exit_code == 1 and "no bundle" in r.output and "03 Oct 2026" in r.output


def test_bundle_plan_leaves_a_repo_that_came_later(env, tmp_path, monkeypatch):  # noqa: F811
    _dated(env, tmp_path, monkeypatch)
    mset = open_set()
    root = mset.roots[0]
    real = root.history
    # b as though it had first arrived with the 3 Oct bundle
    monkeypatch.setattr(root, "history", lambda key: [h for h in real(key)
                                                      if not key.endswith("b") or h[0].stamp >= "20261003"])
    steps = {s.label: s for s in ops.bundle_plan(mset, "2oct")}
    later = next(s for label, s in steps.items() if label != "a")
    assert later.target is None and "not here yet" in later.line()


def test_delete_names_the_version_it_means(env, tmp_path, monkeypatch):  # noqa: F811
    _dated(env, tmp_path, monkeypatch)
    r = run("delete", "a", "2026-10-01", "--yes")
    assert r.exit_code == 0 and "deleted a v1 (01 Oct 2026)" in r.output, r.output


def test_serve_bundle_says_when_its_version_from_then_was_deleted(env, tmp_path, monkeypatch):  # noqa: F811
    _dated(env, tmp_path, monkeypatch)
    assert run("delete", "a", "v2", "--yes").exit_code == 0
    steps = {s.label: s for s in ops.bundle_plan(open_set(), "2026-10-02")}
    a = steps["a"]
    assert a.target is None and not a.changes and "02 Oct 2026" in a.line() and "deleted" in a.line()


def test_serve_bundle_names_a_missing_root(env, tmp_path, monkeypatch):  # noqa: F811
    _dated(env, tmp_path, monkeypatch)
    mset = open_set()
    mset.missing.append("nas")
    mset.missing_reason["nas"] = "not mounted"
    lines = [s.line() for s in ops.bundle_plan(mset, "2026-10-02")]
    assert "root nas: not mounted — its repositories are left as they are" in lines


def test_serve_bundle_failing_part_way_says_what_was_and_wasnt(env, tmp_path, monkeypatch):  # noqa: F811
    import pytest
    from mmw.site.ops import BundleStep
    _dated(env, tmp_path, monkeypatch)
    v = ops.history(open_set().roots[0].home, "a")[0][0]

    class Root:
        def __init__(self, fails):
            self.fails = fails

        def serve(self, key, number):
            if self.fails:
                raise OSError("lock held")
    steps = [BundleStep(Root(False), "x", "x", None, v), BundleStep(Root(True), "y", "y", None, v),
             BundleStep(Root(False), "z", "z", None, v)]
    with pytest.raises(ValueError, match="served: x; not served: y, z"):
        ops.serve_bundle(steps)
