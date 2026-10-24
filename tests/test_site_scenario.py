"""Spec phase 2's acceptance: ten deltas in, take one repository back to v3,
the eleventh bundle leaves it held there, forward it — the served tree right
at every step, and in the end exactly what the connected side publishes."""
from mmw.core.pool import KIND_META, KIND_PKG, Pool
from mmw.site import ops
from mmw.site.apply import apply_bundle
from mmw.site.db import SiteDB

from test_bundle_mmw import _change_b
from test_site_apply import _bundle, _published, _tree, env  # noqa: F401
from test_snapshot_repo_versions import _key
from test_dry_run_totals import SHARED, _index, _sha, two_mirrors  # noqa: F401


def _set_a(md, i):
    files = {"shared.deb": SHARED, f"a{i}.deb": f"release {i}".encode() * 5}
    with Pool(md) as pool:
        refs = []
        for fname, data in {**files, "Packages": _index(files)}.items():
            pool.add(data, _sha(data), "sha256", fname, len(data))
            refs.append((_sha(data), fname, KIND_META if fname == "Packages" else KIND_PKG))
        pool.reconcile_refs("a", refs)


def test_ten_deltas_revert_held_apply_forward(env, tmp_path):  # noqa: F811
    cfg, net, md, home, root = env
    ka, kb = _key(cfg, "a"), _key(cfg, "b")
    quiet = lambda *a: None  # noqa: E731
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "b0", from_ref=None)), say=quiet)
    assert ops.verify(home) == []
    trees = {1: _tree(root)}
    for i in range(2, 12):                                   # ten deltas: a v2 … v11
        _set_a(md, i)
        apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / f"b{i}")), say=quiet)
        assert ops.verify(home) == []
        trees[i] = _tree(root)
    with SiteDB(home) as db:
        assert db.repo_state(ka).serving.number == 11 and db.repo_state(kb).serving.number == 1

    ops.serve(home, "a", 3)
    assert _tree(root) == trees[3] and ops.verify(home) == []

    _set_a(md, 12)
    _change_b(md)
    r = apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "b12")), say=quiet)
    assert r.stored_held == [ka] and r.updated == [kb]
    with SiteDB(home) as db:
        assert db.repo_state(ka).serving.number == 3 and db.repo_state(ka).held
        assert db.versions(ka)[0].number == 12 and db.repo_state(kb).serving.number == 2
    assert ops.verify(home) == []

    ops.forward(home, "a")
    assert ops.verify(home) == []
    assert _tree(root) == _published(cfg, net, md, tmp_path, "final")
