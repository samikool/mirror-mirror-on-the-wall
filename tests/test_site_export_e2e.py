"""Two far sides: A sends its history to B with `mmw site bundle`; B applies
it and its timeline is the union."""
import pytest
from typer.testing import CliRunner

from mmw import cli
from mmw.site.apply import apply_to_set
from mmw.site.db import SiteDB
from mmw.site.home import read_root
from mmw.site.mset import open_set

from test_dry_run_totals import two_mirrors  # noqa: F401
from test_site_apply_timeline import _add_c, _bundle, _served_day, _track, env, set_a  # noqa: F401
from test_snapshot_repo_versions import _key

quiet = lambda *a: None  # noqa: E731


@pytest.fixture(autouse=True)
def _xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def run(*a):
    return CliRunner().invoke(cli.app, ["site", *a])


def _far_side(cfg, net, md, tmp_path, name, days):
    root = tmp_path / name
    for i, d in enumerate(days):
        set_a(md, d)
        b = _bundle(cfg, net, md, tmp_path / f"{name}{d}", from_ref=None)
        apply_to_set(None if i == 0 else open_set([str(root)]), b, place={"*": str(root)}, say=quiet)
    return root


@pytest.fixture
def two_far_sides(env, tmp_path):  # noqa: F811
    cfg, oak, md, _home = env
    elm = oak.model_copy(update={"name": "elm", "root": tmp_path / "pub-elm"})
    cfg2 = cfg.model_copy(update={"networks": [oak, elm]})
    a_root = _far_side(cfg2, oak, md, tmp_path, "A", (1, 2, 4))
    b_root = _far_side(cfg2, elm, md, tmp_path, "B", (1, 3))
    return cfg2, md, a_root, b_root


def _nginx(root) -> str:
    return "".join(p.read_text() for p in sorted((root / "nginx").rglob("*.conf")))


def test_far_side_to_far_side_merges_history(two_far_sides, tmp_path):
    cfg, md, a_root, b_root = two_far_sides
    before = _nginx(b_root)
    out = tmp_path / "to-elm.tar"
    r = run("bundle", "--to", "elm", "--out", str(out), "--root", str(a_root))
    assert r.exit_code == 0, r.output
    assert r.output.startswith("first export to 'elm': nothing recorded as sent there, so everything goes\n")
    assert "oak → elm: " in r.output and f"wrote {out}" in r.output
    res = apply_to_set(open_set([str(b_root)]), str(out), say=quiet)
    assert res.foreign
    a = _key(cfg, "a")
    with SiteDB(read_root(str(b_root)).home) as db:
        assert [p.date[:8] for p in db.points(_track(db, a).id)] == ["20261104", "20261103", "20261102",
                                                                      "20261101"]
        assert _served_day(db, a) == "20261104"
    assert _nginx(b_root) == before


def test_second_export_sends_nothing_new(two_far_sides, tmp_path):
    _cfg, _md, a_root, _b = two_far_sides
    assert run("bundle", "--to", "elm", "--out", str(tmp_path / "1.tar"), "--root", str(a_root)).exit_code == 0
    r = run("bundle", "--to", "elm", "--out", str(tmp_path / "2.tar"), "--root", str(a_root))
    assert r.exit_code == 1 and "nothing new for elm" in r.output and not (tmp_path / "2.tar").exists()


def test_a_new_target_says_everything_goes(two_far_sides, tmp_path):
    _cfg, _md, a_root, _b = two_far_sides
    run("bundle", "--to", "elm", "--out", str(tmp_path / "1.tar"), "--root", str(a_root))
    r = run("bundle", "--to", "elmm", "--out", str(tmp_path / "2.tar"), "--root", str(a_root))
    assert r.exit_code == 0
    assert r.output.splitlines()[0] == ("first export to 'elmm': nothing recorded as sent there, so everything "
                                        "goes (known targets: elm)")


def test_deploy_from_a_far_side_export(env, tmp_path):  # noqa: F811
    cfg, oak, md, _home = env
    elm = oak.model_copy(update={"name": "elm", "root": tmp_path / "pub-elm"})
    cfg_a = _add_c(cfg, md).model_copy(update={"networks": [oak, elm]})
    a_root = _far_side(cfg_a, oak, md, tmp_path, "A", (1,))
    b_root = _far_side(cfg.model_copy(update={"networks": [oak, elm]}), elm, md, tmp_path, "B", (1,))
    out = tmp_path / "to-elm.tar"
    assert run("bundle", "--to", "elm", "--out", str(out), "--root", str(a_root)).exit_code == 0
    res = apply_to_set(open_set([str(b_root)]), str(out), deploy={"c"}, place={"example.net": str(b_root)},
                       say=quiet)
    assert res.skipped == [] and (b_root / "example.net/c/c.deb").exists()
    assert "example.net" in _nginx(b_root)


def test_cli_since_and_all_conflict(two_far_sides, tmp_path):
    _cfg, _md, a_root, _b = two_far_sides
    r = run("bundle", "--to", "elm", "--out", str(tmp_path / "x.tar"), "--root", str(a_root),
            "--since", "2026-11-02", "--all")
    assert r.exit_code == 2 and "--since or --all" in r.output


def test_cli_dry_run(two_far_sides, tmp_path):
    _cfg, _md, a_root, _b = two_far_sides
    r = run("bundle", "--to", "elm", "--out", str(tmp_path / "x.tar"), "--root", str(a_root), "--dry-run")
    assert r.exit_code == 0 and "would write" in r.output and not (tmp_path / "x.tar").exists()


def test_stdout_carries_only_the_tar(two_far_sides, tmp_path):
    import io
    import tarfile
    _cfg, _md, a_root, _b = two_far_sides
    r = CliRunner().invoke(cli.app, ["site", "bundle", "--to", "elm", "--out", "-", "--root", str(a_root)])
    assert r.exit_code == 0
    assert b"first export" not in r.stdout_bytes and b"oak \xe2\x86\x92 elm" not in r.stdout_bytes
    with tarfile.open(fileobj=io.BytesIO(r.stdout_bytes)) as tar:
        assert tar.getnames()[0] == ".mmw/manifest.jsonl"


def test_a_repository_deployed_from_a_third_network_is_served_after_it_travels_on(env, tmp_path):  # noqa: F811
    """A deploys c to B; B sends it on to C, which deploys it too: C's nginx
    serves it (its mirror entry went with it)."""
    cfg, oak, md, _home = env
    elm = oak.model_copy(update={"name": "elm", "root": tmp_path / "pub-elm"})
    pine = oak.model_copy(update={"name": "pine", "root": tmp_path / "pub-pine"})
    cfg_a = _add_c(cfg, md).model_copy(update={"networks": [oak, elm, pine]})
    a_root = _far_side(cfg_a, oak, md, tmp_path, "A", (1,))
    plain = cfg.model_copy(update={"networks": [oak, elm, pine]})
    b_root = _far_side(plain, elm, md, tmp_path, "B", (1,))
    c_root = _far_side(plain, pine, md, tmp_path, "C", (1,))
    out = tmp_path / "to-elm.tar"
    assert run("bundle", "--to", "elm", "--out", str(out), "--root", str(a_root)).exit_code == 0
    apply_to_set(open_set([str(b_root)]), str(out), deploy={"c"}, place={"example.net": str(b_root)}, say=quiet)
    on = tmp_path / "to-pine.tar"
    assert run("bundle", "--to", "pine", "--out", str(on), "--root", str(b_root), "--repo", "c").exit_code == 0
    apply_to_set(open_set([str(c_root)]), str(on), deploy={"c"}, place={"example.net": str(c_root)}, say=quiet)
    assert (c_root / "example.net/c/c.deb").exists() and "example.net" in _nginx(c_root)
