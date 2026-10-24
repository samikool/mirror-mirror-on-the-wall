"""Snapshots carry a version per repository: a new one only when that
repository's files changed, backfilled for snapshots taken before."""
import json

from typer.testing import CliRunner

from mmw import cli
from mmw.core import config
from mmw.core.pool import KIND_META, KIND_PKG, Pool
from mmw.core.snapshots import ensure_repo_versions, repo_digests, repo_version_files, take_snapshot

from test_dry_run_totals import SHARED, _index, _sha, two_mirrors  # noqa: F401 — the shared pool fixture


def _change_a(mirror_dir):
    """Mirror `a` gains a package; `b` is untouched."""
    files = {"shared.deb": SHARED, "a.deb": b"a" * 300, "a2.deb": b"A" * 50}
    with Pool(mirror_dir) as pool:
        refs = []
        for fname, data in files.items():
            pool.add(data, _sha(data), "sha256", fname, len(data))
            refs.append((_sha(data), fname, KIND_PKG))
        idx = _index(files)
        pool.add(idx, _sha(idx), "sha256", "Packages", len(idx))
        refs.append((_sha(idx), "Packages", KIND_META))
        pool.reconcile_refs("a", refs)


def _key(cfg, name):
    return next(s.identity for s in cfg.sources if s.name == name)


def test_unchanged_repo_reuses_version(two_mirrors):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    s1 = take_snapshot(net, cfg, mirror_dir)
    _change_a(mirror_dir)
    s2 = take_snapshot(net, cfg, mirror_dir)
    with Pool(mirror_dir) as p:
        a, b = p.snapshot_repo_versions(s1.id), p.snapshot_repo_versions(s2.id)
    ka, kb = _key(cfg, "a"), _key(cfg, "b")
    assert set(a) == set(b) == {ka, kb}
    assert b[kb].id == a[kb].id and b[kb].number == 1
    assert (a[ka].number, b[ka].number) == (1, 2) and b[ka].stamp == s2.stamp


def test_digest_ignores_network_level_files():
    rb = {"r": ["h/p"], "empty": ["h/q"]}
    with_key = repo_digests([("h/p/a", "1"), ("keys/x", "2")], rb)
    assert with_key == repo_digests([("h/p/a", "1")], rb)
    assert set(with_key) == {"r", "empty"}                     # a repository with no files still has one
    assert with_key["r"] != repo_digests([("h/p/a", "9")], rb)["r"]


def test_backfill_old_snapshots_oldest_first(two_mirrors):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    old1 = take_snapshot(net, cfg, mirror_dir)
    _change_a(mirror_dir)
    old2 = take_snapshot(net, cfg, mirror_dir)
    with Pool(mirror_dir) as p:
        # Make both look pre-upgrade: no versions, no "repos" in their settings.
        for s in (old1, old2):
            data = json.loads(s.settings)
            del data["repos"]
            p._conn.execute("UPDATE snapshots SET settings = ? WHERE id = ?", (json.dumps(data), s.id))
        p._conn.execute("DELETE FROM snapshot_repos")
        p._conn.execute("DELETE FROM repo_versions")
        p._conn.commit()
        imported = p.create_snapshot(net.name, "20200101T000000Z", kind="imported", complete=False,
                                     rows=[("example.com/a/a.deb", _sha(b"a" * 300))])
        assert ensure_repo_versions(p, net.name) == 2
        assert ensure_repo_versions(p, net.name) == 0                 # idempotent
        assert p.snapshot_repo_versions(imported) == {}
        versions = sorted(p.repo_versions(net.name, _key(cfg, "a")), key=lambda v: v.number)
        assert [v.stamp for v in versions] == [old1.stamp, old2.stamp]


def test_repo_version_files_none_once_snapshots_deleted(two_mirrors):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    s1 = take_snapshot(net, cfg, mirror_dir)
    with Pool(mirror_dir) as p:
        v = p.snapshot_repo_versions(s1.id)[_key(cfg, "a")]
        rows = dict(repo_version_files(p, v))
        assert rows and all(path.startswith("example.com/a/") for path in rows)
        assert rows == {k: c for k, c in p.snapshot_files(s1.id) if k.startswith("example.com/a/")}
        p.delete_snapshot(s1.id)
        assert repo_version_files(p, v) is None


def test_snapshot_list_repos_flag(two_mirrors, monkeypatch):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    cfg_dir = mirror_dir.parent
    config.save(cfg, cfg_dir)
    take_snapshot(net, cfg, mirror_dir)
    _change_a(mirror_dir)
    take_snapshot(net, cfg, mirror_dir)
    monkeypatch.setattr(cli, "_CFG_DIR", cli._CFG_DIR)
    r = CliRunner().invoke(cli.app, ["--config", str(cfg_dir), "snapshot", "list", "-n", "oak", "--repos"])
    assert r.exit_code == 0, r.output
    newest = r.output.split("\n  20")[1]                # the first snapshot block, newest first
    assert "a v2 (changed)" in newest and "b v1" in newest and "b v1 (changed)" not in newest


def test_take_backfills_older_snapshots_first(two_mirrors):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    old1 = take_snapshot(net, cfg, mirror_dir)
    _change_a(mirror_dir)
    old2 = take_snapshot(net, cfg, mirror_dir)
    with Pool(mirror_dir) as p:                       # both pre-upgrade: no versions yet
        p._conn.execute("DELETE FROM snapshot_repos")
        p._conn.execute("DELETE FROM repo_versions")
        p._conn.commit()
    new = take_snapshot(net, cfg, mirror_dir)         # the first thing done after upgrading
    with Pool(mirror_dir) as p:
        numbers = {s.stamp: p.snapshot_repo_versions(s.id)[_key(cfg, "a")].number for s in (old1, old2, new)}
        b_first = p.snapshot_repo_versions(old1.id)[_key(cfg, "b")]
    assert numbers == {old1.stamp: 1, old2.stamp: 2, new.stamp: 2}
    assert b_first.stamp == old1.stamp                # b's one version was introduced by the oldest
