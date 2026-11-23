"""Keeping history on the far side: a setting (newest N, all, or an age),
a per-suite override, Clean up that says what each point frees — and never
a deletion as a side effect, unless auto_clean asks for one."""
import pytest
from typer.testing import CliRunner

from mmw import cli
from mmw.site import home as H
from mmw.site import ops
from mmw.site.db import SiteDB

from test_dry_run_totals import two_mirrors  # noqa: F401
from test_site_apply_timeline import _apply, _bundle, env, set_a  # noqa: F401
from test_snapshot_repo_versions import _key


@pytest.fixture(autouse=True)
def _own_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def _days(env, days, tmp_path, keep=None):  # noqa: F811
    cfg, net, md, home = env
    if keep is not None:
        ops.keep_here(home, keep)
    for d in days:
        set_a(md, d)
        _apply(home, _bundle(cfg, net, md, tmp_path / f"b{d}"))
    return cfg, home


def _held_days(home, repo):
    with SiteDB(home) as db:
        (t,) = [t for t in db.tracks(repo) if t.name]
        return sorted(p.date[6:8] for p in db.points(t.id))


def test_default_keeps_all_and_never_cleans_on_apply(env, tmp_path):  # noqa: F811
    cfg, home = _days(env, range(1, 6), tmp_path)
    assert H.load_site(home).keep == "all" and not H.load_site(home).auto_clean
    assert ops.clean_plan_here(home) == []
    assert _held_days(home, _key(cfg, "a")) == ["01", "02", "03", "04", "05"]   # nothing went on its own


def test_clean_plan_says_what_each_point_frees(env, tmp_path):  # noqa: F811
    cfg, home = _days(env, range(1, 6), tmp_path, keep=3)
    plan = ops.clean_plan_here(home)
    assert [(i.suite, i.date) for i in plan] == [("stable", "2026-11-02"), ("stable", "2026-11-01")]
    assert all(i.frees >= 0 for i in plan) and plan[0].frees > 0          # day 2's a2.deb is only its own
    freed = ops.clean(home)
    assert freed > 0 and _held_days(home, _key(cfg, "a")) == ["03", "04", "05"]


def test_clean_never_touches_protected_points(env, tmp_path):  # noqa: F811
    cfg, home = _days(env, range(1, 6), tmp_path)
    ops.serve(home, "a", "2026-11-01")                  # pinned to day 1
    days = {i.date for i in ops.clean_plan_here(home)}
    assert "2026-11-01" not in days


def test_a_suite_override_keeps_its_history(env, tmp_path):  # noqa: F811
    cfg, home = _days(env, range(1, 6), tmp_path, keep=3)
    ops.set_track_keep(home, "a", "stable", "all")
    assert ops.clean_plan_here(home) == []
    ops.set_track_keep(home, "a", "stable", None)        # back to the site's setting
    assert len(ops.clean_plan_here(home)) == 2


def test_keep_an_age(env, tmp_path):  # noqa: F811
    cfg, home = _days(env, range(1, 6), tmp_path)
    ops.keep_here(home, "2d")
    plan = ops.clean_plan_here(home, today="20261105T120000Z")
    # Within 2 days of 5 Nov: days 4 and 5 (and served/previous anyway); 1–3 go.
    assert [i.date for i in plan] == ["2026-11-03", "2026-11-02", "2026-11-01"]


def test_old_keep_versions_migrates_to_auto_clean(env, tmp_path):  # noqa: F811
    cfg, net, md, home = env
    text = "keep_versions = 2\n" + (home / H.SETTINGS).read_text()
    (home / H.SETTINGS).write_text(text)
    s = H.load_site(home)
    assert s.keep == 2 and s.auto_clean
    _days(env, range(1, 6), tmp_path)
    assert _held_days(home, _key(cfg, "a")) == ["04", "05"]          # cleaned after each apply, as before


def test_cli_clean_dry_run_and_yes(env, tmp_path):  # noqa: F811
    cfg, home = _days(env, range(1, 6), tmp_path, keep=3)
    root = str(home.parent.parent)
    r = CliRunner().invoke(cli.app, ["site", "clean", "--root", root, "--dry-run"])
    assert r.exit_code == 0 and "2026-11-02" in r.output and "frees" in r.output, r.output
    assert len(_held_days(home, _key(cfg, "a"))) == 5
    r = CliRunner().invoke(cli.app, ["site", "clean", "--root", root])
    assert r.exit_code == 1 and "--yes" in r.output
    r = CliRunner().invoke(cli.app, ["site", "clean", "--root", root, "--yes"])
    assert r.exit_code == 0 and "freed" in r.output
    assert len(_held_days(home, _key(cfg, "a"))) == 3


def test_settings_show_what_each_preset_costs(env, tmp_path):  # noqa: F811
    cfg, home = _days(env, range(1, 6), tmp_path, keep=3)
    costs = ops.keep_costs_here(home)
    assert set(costs) == {1, 3, 10, "all"} and costs["all"] == 0 and costs[1] >= costs[3] >= costs[10] >= 0
    r = CliRunner().invoke(cli.app, ["site", "settings", "--root", str(home.parent.parent)])
    assert r.exit_code == 0 and "keeps the newest 3" in r.output and "--costs" in r.output, r.output
    r = CliRunner().invoke(cli.app, ["site", "settings", "--root", str(home.parent.parent), "--costs"])
    assert r.exit_code == 0 and "every point:" in r.output, r.output


def test_an_age_setting_is_among_the_costs(env, tmp_path):  # noqa: F811
    cfg, home = _days(env, range(1, 4), tmp_path)
    H.save_settings(home, H.load_site(home).model_copy(update={"keep": "30d"}))
    assert "30d" in ops.keep_costs_here(home)


@pytest.mark.parametrize("value", ["all", "5", "30d"])
def test_cli_keep_values(env, value):  # noqa: F811
    cfg, net, md, home = env
    r = CliRunner().invoke(cli.app, ["site", "settings", "--root", str(home.parent.parent), "--keep", value])
    assert r.exit_code == 0, r.output
    assert str(H.load_site(home).keep) == value
