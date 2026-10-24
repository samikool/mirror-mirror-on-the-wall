"""What the TUI needs from apply: a plan read from the manifest alone,
applying only some repositories, and progress as it goes."""
import pytest

from mmw.site.apply import ApplyError, apply_bundle, plan_apply
from mmw.site.db import SiteDB

from test_site_apply import _bundle, _rewrite, _tree, env  # noqa: F401
from test_snapshot_repo_versions import _change_a, _key
from test_bundle_mmw import _change_b
from test_dry_run_totals import two_mirrors  # noqa: F401

quiet = lambda *a: None  # noqa: E731


def test_plan_reads_manifest_only(env, tmp_path):  # noqa: F811
    cfg, net, md, home, root = env
    good = _bundle(cfg, net, md, tmp_path / "1", from_ref=None)
    bad = _rewrite(good, tmp_path / "bad.tar", lambda m, d: d[:-1] + b"!" if m.name.endswith("a.deb") else d)
    plan = plan_apply(home, str(bad))                     # never reads the payload's bytes
    assert {r.repo for r in plan.rows} == {_key(cfg, "a"), _key(cfg, "b")}


def test_plan_rows_and_sizes(env, tmp_path):  # noqa: F811
    cfg, net, md, home, root = env
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=quiet)
    _change_a(md)
    plan = plan_apply(home, str(_bundle(cfg, net, md, tmp_path / "2")))
    (row,) = plan.rows
    assert (row.repo, row.label, row.from_number, row.to_number, row.serving_number) == (_key(cfg, "a"), "a", 1, 2, 1)
    assert row.files_sent >= 2 and row.bytes_sent > 0 and not row.held and row.root == root
    assert plan.bytes >= row.bytes_sent and plan.network == net.name


def test_plan_of_a_stage_directory(env, tmp_path):  # noqa: F811
    from mmw.core.publisher.bundle import stage_network
    cfg, net, md, home, root = env
    staged = stage_network(net, cfg, md, out_dir=tmp_path / "s", from_ref=None).out_path
    assert {r.label for r in plan_apply(home, str(staged)).rows} == {"a", "b"}


def test_plan_apply_rejects_non_bundles(env, tmp_path):  # noqa: F811
    _cfg, _net, _md, home, _root = env
    junk = tmp_path / "junk.txt"
    junk.write_text("hello")
    (tmp_path / "emptydir").mkdir()
    for src in (junk, tmp_path / "emptydir"):
        with pytest.raises(ApplyError) as e:
            plan_apply(home, str(src))
        assert e.value.code in (2, 5)
    with pytest.raises(ValueError):
        plan_apply(home, "-")


def test_untick_stores_and_holds(env, tmp_path):  # noqa: F811
    cfg, net, md, home, root = env
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=quiet)
    _change_a(md)
    _change_b(md)
    before = _tree(root)
    r = apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "2")), only={_key(cfg, "b")}, say=quiet)
    assert r.updated == [_key(cfg, "b")] and r.stored_held == [_key(cfg, "a")]
    with SiteDB(home) as db:
        st = db.repo_state(_key(cfg, "a"))
        assert st.serving.number == 1 and st.held and db.versions(_key(cfg, "a"))[0].number == 2
    assert {k: v for k, v in _tree(root).items() if k.startswith("example.com/a/")} == \
           {k: v for k, v in before.items() if k.startswith("example.com/a/")}


def test_progress_events(env, tmp_path):  # noqa: F811
    cfg, net, md, home, root = env
    events = []
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=quiet,
                 progress=lambda event, **data: events.append((event, data)))
    kinds = [e for e, _ in events]
    assert kinds[0] == "receive" and "check" in kinds and kinds[-1] == "done"
    assert {d["label"] for e, d in events if e == "switch"} == {"a", "b"}
    assert kinds.index("check") < kinds.index("switch")


def test_stop_before_switching_changes_nothing(env, tmp_path):  # noqa: F811
    import threading
    cfg, net, md, home, root = env
    stop = threading.Event()
    stop.set()
    with pytest.raises(ApplyError, match="stopped") as e:
        apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=quiet, stop_event=stop)
    assert e.value.code == 5 and _tree(root) == {}
    with SiteDB(home) as db:
        assert db.repo_states() == []
