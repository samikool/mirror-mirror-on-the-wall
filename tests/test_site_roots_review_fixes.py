"""Findings from the phase 4 review, each pinned before its fix."""
import shutil

import pytest

from mmw.site import home as H
from mmw.site import ops
from mmw.site.apply import apply_bundle
from mmw.site.db import SiteDB

from test_site_apply import _bundle, env  # noqa: F401
from test_site_roots import _b_files, _files, split_env  # noqa: F401
from test_snapshot_repo_versions import _change_a
from test_dry_run_totals import two_mirrors  # noqa: F401

quiet = lambda *a: None  # noqa: E731


def _kb(cfg):
    return next(s.identity for s in cfg.sources if s.name == "b")


def _two_root_site(split_env, tmp_path):  # noqa: F811
    cfg, net, md, home, main, second = split_env
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), place={_kb(cfg): "second"},
                 say=quiet)
    return cfg, net, md, home, main, second


def test_delta_after_gc_on_a_multi_root_site(split_env, tmp_path):  # noqa: F811
    cfg, net, md, home, main, second = _two_root_site(split_env, tmp_path)
    _change_a(md)
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "2")), say=quiet)
    ops.delete_version(home, "a", 1)                       # a gc on the primary
    _change_a(md)                                          # (back to a's first content: a fresh version)
    from test_site_ops import _bump
    _bump(md)
    r = apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "3")), say=quiet)
    assert r.updated and ops.verify(home) == []


@pytest.mark.parametrize("multi", [False, True])
def test_dry_run(split_env, tmp_path, multi):  # noqa: F811
    cfg, net, md, home, main, second = split_env
    if not multi:
        s = H.load_site(home)
        H.save_settings(home, s.model_copy(update={"roots": s.roots[:1]}))
    r = apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), dry_run=True, say=quiet)
    assert len(r.lines) == 2 and not _files(main)
    with SiteDB(home) as db:
        assert db.repo_states() == []


def test_move_cut_off_before_anything_recorded_runs_again(split_env, tmp_path, monkeypatch):  # noqa: F811
    from mmw.site import db as db_mod
    cfg, net, md, home, main, second = split_env
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=quiet)
    served = _b_files(main)
    real = db_mod.SiteDB.set_root

    def boom(self, *a, **kw):
        raise KeyboardInterrupt
    monkeypatch.setattr(db_mod.SiteDB, "set_root", boom)
    with pytest.raises(KeyboardInterrupt):
        ops.move(home, "b", "second")
    monkeypatch.setattr(db_mod.SiteDB, "set_root", real)
    from mmw.site.lock import break_lock
    break_lock(home)
    assert _b_files(main) == served                       # still served from where it was
    ops.move(home, "b", "second")
    assert _b_files(second) == served and ops.verify(home) == []


def test_move_keeps_the_old_copy_until_tidy(split_env, tmp_path):  # noqa: F811
    cfg, net, md, home, main, second = split_env
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=quiet)
    served = _b_files(main)
    ops.move(home, "b", "second")
    assert _b_files(main) == served and _b_files(second) == served     # both serve until nginx moves
    ops.hold(home, "a")                                                # another command: no tidy yet
    assert _b_files(main) == served
    assert ops.tidy(home) == ["b"]
    assert _b_files(main) == set() and ops.verify(home) == []


def test_root_added_later_then_move(env, tmp_path):  # noqa: F811
    from test_site_roots import split_env as _split
    cfg, net, md, home, main, second = _split.__wrapped__(tmp_path / "x")
    s = H.load_site(home)
    H.save_settings(home, s.model_copy(update={"roots": s.roots[:1]}))   # a single-root site …
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=quiet)
    H.save_settings(home, H.load_site(home).model_copy(update={"roots": s.roots}))   # … that grows a root
    ops.move(home, "b", "second")
    ops.tidy(home)
    assert "b.example.com" in (second / "nginx/mirror.conf").read_text()
    assert "b.example.com" not in (main / "nginx/mirror.conf").read_text()
    assert any(f.startswith("keys/") for f in _files(second)) == any(f.startswith("keys/") for f in _files(main))
    assert ops.verify(home) == []


def test_primary_with_nothing_left_has_no_stale_nginx(split_env, tmp_path):  # noqa: F811
    cfg, net, md, home, main, second = split_env
    s = H.load_site(home)
    H.save_settings(home, s.model_copy(update={"roots": s.roots[:1]}))
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=quiet)
    H.save_settings(home, H.load_site(home).model_copy(update={"roots": s.roots}))
    ops.move(home, "b", "second")
    ops.tidy(home)
    conf = main / "nginx/mirror.conf"
    assert not conf.exists() or "b.example.com" not in conf.read_text()


def test_forward_with_an_unmounted_root(split_env, tmp_path):  # noqa: F811
    cfg, net, md, home, main, second = _two_root_site(split_env, tmp_path)
    ops.hold(home, "b")
    shutil.move(str(second), str(tmp_path / "away"))
    with pytest.raises(FileNotFoundError, match="not mounted"):
        ops.forward(home, "b")
    assert not second.exists()
    ops.hold(home, "a")                                    # the rest of the site still works
