"""Findings from the phase 2 review, each pinned before its fix."""

import pytest

from mmw.site import ops
from mmw.site.apply import ApplyError, apply_bundle
from mmw.site.db import SiteDB
from mmw.site.store import Store

from test_site_apply import _bundle, _rewrite, _tree, env  # noqa: F401
from test_site_ops import _three
from test_snapshot_repo_versions import _change_a, _key
from test_dry_run_totals import two_mirrors  # noqa: F401

quiet = lambda *a: None  # noqa: E731


def _pending_switch(env, tmp_path, monkeypatch):  # noqa: F811
    """a: v1 served, an apply to v2 killed mid-switch."""
    cfg, net, md, home, root = env
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=quiet)
    _change_a(md)
    real = Store.place

    def boom(self, cs, dest):
        if dest.name == "Packages":
            raise KeyboardInterrupt
        return real(self, cs, dest)
    monkeypatch.setattr(Store, "place", boom)
    with pytest.raises(KeyboardInterrupt):
        apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "2")), say=quiet)
    monkeypatch.setattr(Store, "place", real)
    from mmw.site.lock import break_lock
    break_lock(home)
    return cfg, home, root


def test_delete_refuses_a_pending_switch_target(env, tmp_path, monkeypatch):  # noqa: F811
    cfg, home, root = _pending_switch(env, tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        ops.delete_version(home, "a", 2)
    assert any(p.startswith("example.com/a/") for p in _tree(root))
    with SiteDB(home) as db:
        assert db.repo_state(_key(cfg, "a")).serving.number == 2          # delete resumed the switch first


def test_hold_resumes_switches(env, tmp_path, monkeypatch):  # noqa: F811
    cfg, home, root = _pending_switch(env, tmp_path, monkeypatch)
    ops.hold(home, "b")
    with SiteDB(home) as db:
        assert db.pending_switches() == [] and db.repo_state(_key(cfg, "a")).serving.number == 2


def test_switch_refuses_a_missing_target(env, tmp_path):  # noqa: F811
    from mmw.site.switch import switch_repo
    cfg, home, root, _ = _three(env, tmp_path)
    with SiteDB(home) as db:
        with pytest.raises(ValueError):
            switch_repo(db, Store(root), root, _key(cfg, "a"), 99999)


@pytest.mark.parametrize("fraction", [0.5, 0.02])
def test_byte_truncated_tar_is_exit_5(env, tmp_path, fraction):  # noqa: F811
    cfg, net, md, home, root = env
    good = _bundle(cfg, net, md, tmp_path / "1", from_ref=None).read_bytes()
    cut = tmp_path / "cut.tar"
    cut.write_bytes(good[:max(int(len(good) * fraction), 700)])
    with pytest.raises(ApplyError, match="truncated") as e:
        apply_bundle(home, str(cut), say=quiet)
    assert e.value.code == 5 and _tree(root) == {}


def test_missing_network_file_leaves_the_db_unchanged(env, tmp_path):  # noqa: F811
    cfg, net, md, home, root = env
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=quiet)
    _change_a(md)
    delta = _bundle(cfg, net, md, tmp_path / "2")
    with SiteDB(home) as db:                         # the site lost a network file's blob
        cs = db.network_files()["apply.py"]
    Store(root).path(cs).unlink()
    (root / "apply.py").unlink()
    with SiteDB(home) as db:
        db.set_network_files({**db.network_files(), "apply.py": "0" * 64}, sizes={})
    with pytest.raises(ApplyError):
        apply_bundle(home, str(_rewrite(delta, tmp_path / "noapply.tar",
                                        lambda m, d: None if m.name == "apply.py" else d)), say=quiet)
    with SiteDB(home) as db:
        assert [v.number for v in db.versions(_key(cfg, "a"))] == [1]


def test_an_older_bundle_changes_nothing_served(env, tmp_path):  # noqa: F811
    cfg, net, md, home, root = env
    full = _bundle(cfg, net, md, tmp_path / "1", from_ref=None)
    apply_bundle(home, str(full), say=quiet)
    _change_a(md)
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "2")), say=quiet)
    now = _tree(root)
    r = apply_bundle(home, str(full), say=quiet)
    assert r.updated == [] and _tree(root) == now
    assert any("already" in line or "older" in line for line in r.lines)
    with SiteDB(home) as db:
        st = db.repo_state(_key(cfg, "a"))
        assert st.serving.number == 2 and not st.held


def test_verify_repair_puts_it_right(env, tmp_path):  # noqa: F811
    cfg, home, root, _ = _three(env, tmp_path)
    (root / "example.com/a/Packages").unlink()
    victim = root / "example.com/b/b.deb"
    victim.unlink()
    victim.write_bytes(b"tampered")
    assert len(ops.verify(home)) == 2
    assert ops.repair(home) == 2
    assert ops.verify(home) == []


def test_a_renumbered_repository_is_refused_alone(env, tmp_path):  # noqa: F811
    cfg, net, md, home, root = env
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=quiet)
    with SiteDB(home) as db:                         # this site's a v1 is not the pool's any more
        db._conn.execute("UPDATE versions SET digest = 'elsewhere' WHERE repo = ?", (_key(cfg, "a"),))
        db._conn.commit()
    r = apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "2", from_ref=None)), say=quiet)
    assert _key(cfg, "a") in r.refused and "mmw site delete" in r.refused[_key(cfg, "a")]


def test_the_last_version_of_a_repository_cannot_go(env, tmp_path):  # noqa: F811
    cfg, net, md, home, root = env
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=quiet)
    with SiteDB(home) as db:
        db._conn.execute("UPDATE repos SET serving = NULL")
        db._conn.commit()
    with pytest.raises(ValueError, match="only version"):
        ops.delete_version(home, "a", 1)


def test_nginx_text_files_are_not_store_hardlinks(env, tmp_path):  # noqa: F811
    cfg, net, md, home, root = env
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=quiet)
    with SiteDB(home) as db:
        files = db.network_files()
    texts = [p for p in files if p.startswith("nginx/") and not p.endswith(".tar")]
    assert texts
    for p in texts:
        assert (root / p).stat().st_ino != Store(root).path(files[p]).stat().st_ino
    assert ops.verify(home) == []


def test_cli_verify_repair(env, tmp_path):  # noqa: F811
    from typer.testing import CliRunner
    from mmw import cli
    _cfg, home, root, _ = _three(env, tmp_path)
    (root / "example.com/a/Packages").unlink()
    run = lambda *a: CliRunner().invoke(cli.app, ["site", "verify", *a, "--home", str(home)])  # noqa: E731
    r = run()
    assert r.exit_code == 4 and "--repair" in r.output
    r = run("--repair")
    assert r.exit_code == 0 and "put 1 file(s) back" in r.output
