"""Dated URLs: every held point at {base}/@YYYY-MM-DD/ — that day as of then,
hardlinked from the store; gone when its point goes; following a move."""
import os

import pytest

from mmw.site import home as H
from mmw.site import ops

from test_dry_run_totals import two_mirrors  # noqa: F401
from test_site_apply_timeline import _apply, _bundle, env, set_a  # noqa: F401


@pytest.fixture(autouse=True)
def _own_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def _days(env, days, tmp_path):  # noqa: F811
    cfg, net, md, home = env
    for d in days:
        set_a(md, d)
        _apply(home, _bundle(cfg, net, md, tmp_path / f"b{d}"))
    return home, home.parent.parent


def _dated(root):
    return sorted(p.name for p in (root / "example.com/a").iterdir() if p.name.startswith("@"))


def test_every_held_point_has_a_dated_tree(env, tmp_path):  # noqa: F811
    home, root = _days(env, (1, 2, 3), tmp_path)
    assert _dated(root) == ["@2026-11-01", "@2026-11-02", "@2026-11-03"]
    deb = root / "example.com/a/@2026-11-02/a2.deb"
    assert deb.read_bytes() == b"A2" * 40
    store = H.load_site(home).root / ".mmw" / "store"
    assert os.stat(deb).st_nlink >= 2 and any(store.rglob("*"))              # a hardlink, not a copy


def test_a_dated_tree_is_that_day_as_of(env, tmp_path):  # noqa: F811
    home, root = _days(env, (1, 3), tmp_path)
    assert not (root / "example.com/a/@2026-11-01/a2.deb").exists()          # day 1 had no a2
    assert (root / "example.com/a/@2026-11-03/a3.deb").exists()
    assert (root / "example.com/a/@2026-11-01/Release").read_bytes() != \
        (root / "example.com/a/@2026-11-03/Release").read_bytes()


def test_clean_frees_what_a_dated_tree_held(env, tmp_path):  # noqa: F811
    home, root = _days(env, (1, 2, 3, 4, 5), tmp_path)
    ops.keep_here(home, 3)
    old = root / "example.com/a/@2026-11-01"
    assert old.is_dir()
    assert ops.clean(home) > 0
    assert not old.exists() and _dated(root) == ["@2026-11-03", "@2026-11-04", "@2026-11-05"]


def test_dated_off_removes_them(env, tmp_path):  # noqa: F811
    home, root = _days(env, (1, 2), tmp_path)
    ops.keep_here(home, None, dated=False)
    assert _dated(root) == []
    ops.keep_here(home, None, dated=True)
    assert _dated(root) == ["@2026-11-01", "@2026-11-02"]


def test_status_lists_dated_urls(env, tmp_path):  # noqa: F811
    from typer.testing import CliRunner
    from mmw import cli
    home, root = _days(env, (1, 2), tmp_path)
    r = CliRunner().invoke(cli.app, ["site", "status", "--root", str(root)])
    assert r.exit_code == 0 and "example.com/a/@2026-11-02/" in r.output, r.output


def test_dated_trees_follow_a_move(env, tmp_path):  # noqa: F811
    from mmw.site.mset import open_set
    from mmw.site import recents
    cfg, net, md, home = env
    home, root = _days(env, (1, 2), tmp_path)
    recents.remember(str(root))
    mset = open_set()
    other = tmp_path / "other"
    ops.add_root(mset, str(other))
    ops.move_repo(open_set(), "a", "other")
    assert _dated(other.resolve()) == ["@2026-11-01", "@2026-11-02"]
    assert not (root / "example.com/a").exists() or _dated(root) == []


def test_deleting_a_point_by_hand_removes_its_dated_tree(env, tmp_path):  # noqa: F811
    home, root = _days(env, (1, 2, 3), tmp_path)
    ops.delete_point(home, "a", "2026-11-01")
    assert "@2026-11-01" not in _dated(root)


def test_a_folder_mmw_did_not_make_is_never_removed(env, tmp_path):  # noqa: F811
    home, root = _days(env, (1,), tmp_path)
    theirs = root / "example.com/a/@2020-01-01"
    theirs.mkdir()
    (theirs / "upstream.txt").write_text("not ours")
    ops.keep_here(home, None, dated=False)
    assert theirs.is_dir() and (theirs / "upstream.txt").exists()


def test_a_build_a_crash_cut_short_is_swept(env, tmp_path):  # noqa: F811
    home, root = _days(env, (1,), tmp_path)
    left = root / "example.com/a/.@2026-10-01.building"
    left.mkdir()
    (left / ".mmw-dated").write_text("x")
    _days(env, (2,), tmp_path)
    assert not left.exists()


def test_clean_deletes_only_what_was_confirmed(env, tmp_path):  # noqa: F811
    home, root = _days(env, (1, 2, 3, 4, 5), tmp_path)
    ops.keep_here(home, 3)
    plan = ops.clean_plan_here(home)
    one = next(i for i in plan if i.date == "2026-11-02")
    ops.clean(home, {one.point})
    assert [i.date for i in ops.clean_plan_here(home)] == ["2026-11-01"]


def test_a_bad_keep_never_reads(tmp_path):
    with pytest.raises(ValueError):
        H.SiteSettings(roots=[], keep=0)
    assert H.SiteSettings(roots=[], keep="5").keep == 5



def test_status_reads_each_roots_dated_urls_once(env, tmp_path, monkeypatch):  # noqa: F811
    """Two repositories on one root: its dated URLs are read once, not once each."""
    from typer.testing import CliRunner

    from mmw import cli
    from mmw.site.mset import LocalRoot
    home, root = _days(env, (1, 2), tmp_path)
    calls = []
    real = LocalRoot.dated_urls
    monkeypatch.setattr(LocalRoot, "dated_urls", lambda self: calls.append(1) or real(self))
    r = CliRunner().invoke(cli.app, ["site", "status", "--root", str(root)])
    assert r.exit_code == 0 and r.output.count("dated:") >= 1 and len(calls) == 1, (calls, r.output)
