"""A site with several roots: repositories placed in each, moved between
them, every root with its own store and its own nginx."""
import pytest
from typer.testing import CliRunner

from mmw import cli
from mmw.site import home as H
from mmw.site import ops
from mmw.site.apply import apply_bundle
from mmw.site.db import SiteDB

from test_site_apply import _bundle, _tree, env  # noqa: F401
from test_site_ops import _three
from test_snapshot_repo_versions import _key
from test_dry_run_totals import two_mirrors  # noqa: F401

quiet = lambda *a: None  # noqa: E731


def test_legacy_settings_load_as_main(tmp_path):
    (tmp_path / "h").mkdir()
    (tmp_path / "h/site.toml").write_text(f'root = "{tmp_path / "srv"}"\nkeep_versions = 3\n')
    s = H.load_site(tmp_path / "h")
    assert [(r.name, r.path, r.serve_path) for r in s.roots] == [("main", tmp_path / "srv", tmp_path / "srv")]
    assert s.root == tmp_path / "srv" and s.keep_versions == 3


def test_root_add_and_list(tmp_path):
    H.init_site(tmp_path / "h", tmp_path / "srv")
    run = lambda *a: CliRunner().invoke(cli.app, ["site", "root", *a, "--home", str(tmp_path / "h")])  # noqa: E731
    r = run("add", "pypi", str(tmp_path / "pypi"), "--serve-path", "/export/pypi")
    assert r.exit_code == 0, r.output
    s = H.load_site(tmp_path / "h")
    assert [(r.name, str(r.serve_path)) for r in s.roots][1] == ("pypi", "/export/pypi")
    assert (tmp_path / "pypi/.mmw/store").is_dir()
    r = run("list")
    assert "main" in r.output and "pypi" in r.output and "/export/pypi" in r.output
    assert run("add", "pypi", str(tmp_path / "x")).exit_code == 1               # names are unique


def test_repo_state_root_defaults_to_primary(env, tmp_path):  # noqa: F811
    cfg, home, root, _ = _three(env, tmp_path)
    with SiteDB(home) as db:
        assert db.repo_state(_key(cfg, "a")).root is None
        db.set_root(_key(cfg, "a"), "second")
        assert db.repo_state(_key(cfg, "a")).root == "second"


def test_space_reports_each_root(env, tmp_path):  # noqa: F811
    cfg, home, root, _ = _three(env, tmp_path)
    s = H.load_site(home)
    H.save_settings(home, s.model_copy(update={"roots": [*s.roots, H.RootConfig(
        name="second", path=tmp_path / "second", serve_path=tmp_path / "second")]}))
    (tmp_path / "second/.mmw/store").mkdir(parents=True)
    rep = ops.space(home)
    assert set(rep.roots) == {"main", "second"} and all(free > 0 for _used, free in rep.roots.values())


# ── several roots ────────────────────────────────────────────────────────────

@pytest.fixture
def split_env(tmp_path):
    """An mmw network of two repositories on two hostnames, and a site with
    two roots: main and second."""
    from mmw.core import config
    from mmw.core.pool import KIND_META, KIND_PKG, Pool
    from test_dry_run_totals import SHARED, ONLY_A, ONLY_B, _index, _sha
    md = tmp_path / "mirror"
    sources, contents = [], {"a": {"shared.deb": SHARED, "a.deb": ONLY_A}, "b": {"shared.deb": SHARED, "b.deb": ONLY_B}}
    with Pool(md) as pool:
        for name, files in contents.items():
            refs = []
            for fname, data in {**files, "Packages": _index(files)}.items():
                pool.add(data, _sha(data), "sha256", fname, len(data))
                refs.append((_sha(data), fname, KIND_META if fname == "Packages" else KIND_PKG))
            pool.reconcile_refs(name, refs)
            sources.append(config.SourceConfig(name=name, label="custom", provider="apt-flat",
                                               url=f"https://{name}.example.com/{name}/"))
    net = config.NetworkConfig(name="oak", root=tmp_path / "pub", far_side="mmw")
    cfg = config.Config(settings=config.Settings(mirror_dir=md), sources=sources, networks=[net])
    home = tmp_path / "site"
    H.init_site(home, tmp_path / "srv")
    H.add_root(home, "second", tmp_path / "srv2", serve_path="/export/second")
    return cfg, net, md, home, tmp_path / "srv", tmp_path / "srv2"


def _files(root):
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
            and not p.relative_to(root).as_posix().startswith(".mmw/")}


def test_two_roots_each_serve_their_repos(split_env, tmp_path):
    cfg, net, md, home, main, second = split_env
    kb = next(s.identity for s in cfg.sources if s.name == "b")
    r = apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), place={kb: "second"},
                     say=quiet)
    assert sorted(r.updated) == sorted(s.identity for s in cfg.sources)
    assert any(f.startswith("a.example.com/") for f in _files(main))
    assert not any(f.startswith("b.example.com/") for f in _files(main))
    assert any(f.startswith("b.example.com/") for f in _files(second))
    assert not any(f.startswith("a.example.com/") for f in _files(second))
    conf_main = (main / "nginx/mirror.conf").read_text()
    conf_second = (second / "nginx/mirror.conf").read_text()
    assert "a.example.com" in conf_main and "b.example.com" not in conf_main
    assert "b.example.com" in conf_second and "a.example.com" not in conf_second
    assert "/export/second" in (second / "nginx/compose.yaml").read_text()
    assert any(f.startswith("keys/") for f in _files(second)) == any(f.startswith("keys/") for f in _files(main))
    assert ops.verify(home) == []
    with SiteDB(home) as db:
        assert db.repo_state(kb).root == "second"


def test_shared_host_must_share_root(env, tmp_path):  # noqa: F811
    cfg, net, md, home, root = env
    H.add_root(home, "second", tmp_path / "srv2")
    with pytest.raises(ValueError, match="must share a root"):
        apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)),
                     place={_key(cfg, "b"): "second"}, say=quiet)
    with SiteDB(home) as db:
        assert db.repo_states() == []


def test_unmounted_root_refused(split_env, tmp_path):
    import shutil
    from mmw.site.apply import ApplyError
    cfg, net, md, home, main, second = split_env
    kb = next(s.identity for s in cfg.sources if s.name == "b")
    shutil.rmtree(second)
    with pytest.raises(ApplyError, match="not mounted") as e:
        apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), place={kb: "second"},
                     say=quiet)
    assert e.value.code == 5 and not _files(main)


def test_new_repo_goes_to_primary_or_place(split_env, tmp_path):
    cfg, net, md, home, main, second = split_env
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=quiet)
    with SiteDB(home) as db:
        assert all(s.root is None for s in db.repo_states())          # the primary
    assert not any(f.startswith(("a.example.com/", "b.example.com/")) for f in _files(second))


def test_old_bundle_without_settings_refused_on_multi_root(split_env, tmp_path):
    from mmw.site.apply import ApplyError
    from test_site_apply import _rewrite
    import json
    cfg, net, md, home, main, second = split_env
    good = _bundle(cfg, net, md, tmp_path / "1", from_ref=None)

    def strip(m, d):
        if m.name != ".mmw/manifest.jsonl":
            return d
        lines = d.decode().splitlines()
        header = json.loads(lines[0])
        header.pop("settings")
        return ("\n".join([json.dumps(header), *lines[1:]]) + "\n").encode()
    with pytest.raises(ApplyError, match="nginx per root") as e:
        apply_bundle(home, str(_rewrite(good, tmp_path / "old.tar", strip)), say=quiet)
    assert e.value.code == 5


def test_cli_apply_place(split_env, tmp_path):
    cfg, net, md, home, main, second = split_env
    b = _bundle(cfg, net, md, tmp_path / "1", from_ref=None)
    r = CliRunner().invoke(cli.app, ["site", "apply", str(b), "--place", "b=second", "--home", str(home)])
    assert r.exit_code == 0, r.output
    assert any(f.startswith("b.example.com/") for f in _files(second))


# ── moving ───────────────────────────────────────────────────────────────────

def _split_three(split_env, tmp_path):
    """b at v1–v3 in main, serving v3."""
    cfg, net, md, home, main, second = split_env
    for i in (1, 2, 3):
        if i == 2:
            from test_bundle_mmw import _change_b
            _change_b(md)
        elif i == 3:
            _bump_b(md)
        apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / f"m{i}", from_ref=None if i == 1 else None)),
                     say=quiet)
    return cfg, home, main, second, next(s.identity for s in cfg.sources if s.name == "b")


def _bump_b(md):
    from mmw.core.pool import KIND_META, KIND_PKG, Pool
    from test_dry_run_totals import SHARED, _index, _sha
    files = {"shared.deb": SHARED, "b3.deb": b"bee" * 30}
    with Pool(md) as pool:
        refs = []
        for fname, data in {**files, "Packages": _index(files)}.items():
            pool.add(data, _sha(data), "sha256", fname, len(data))
            refs.append((_sha(data), fname, KIND_META if fname == "Packages" else KIND_PKG))
        pool.reconcile_refs("b", refs)


def _b_files(root):
    return {f for f in _files(root) if f.startswith("b.example.com/")}


def test_move_copies_switches_and_cleans(split_env, tmp_path):
    cfg, home, main, second, kb = _split_three(split_env, tmp_path)
    served = _b_files(main)
    r = ops.move(home, "b", "second")
    assert r.copied_bytes > 0 and r.files > 0
    assert _b_files(main) == set() and _b_files(second) == served
    assert "b.example.com" in (second / "nginx/mirror.conf").read_text()
    assert "b.example.com" not in (main / "nginx/mirror.conf").read_text()
    assert ops.verify(home) == []
    with SiteDB(home) as db:
        assert db.repo_state(kb).root == "second"


def test_move_held_repo_keeps_versions(split_env, tmp_path):
    cfg, home, main, second, kb = _split_three(split_env, tmp_path)
    ops.serve(home, "b", 1)
    v1 = _b_files(main)
    ops.move(home, "b", "second")
    assert _b_files(second) == v1
    with SiteDB(home) as db:
        st = db.repo_state(kb)
        assert st.serving.number == 1 and st.held
    for n in (2, 3, 1):
        ops.serve(home, "b", n)                     # every kept version is servable from the new root
        assert ops.verify(home) == []


def test_move_resumes(split_env, tmp_path, monkeypatch):
    from mmw.site import apply as apply_mod
    cfg, home, main, second, kb = _split_three(split_env, tmp_path)
    served = _b_files(main)
    real = apply_mod._ensure
    calls = []

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real(*a, **kw)
    monkeypatch.setattr(apply_mod, "_ensure", flaky)
    with pytest.raises(KeyboardInterrupt):
        ops.move(home, "b", "second")
    from mmw.site.lock import break_lock
    break_lock(home)
    assert _b_files(main) == served                 # the old root still serves
    monkeypatch.setattr(apply_mod, "_ensure", real)
    ops.move(home, "b", "second")
    assert _b_files(second) == served and _b_files(main) == set() and ops.verify(home) == []


def test_move_between_roots_on_one_fs(split_env, tmp_path):
    cfg, home, main, second, kb = _split_three(split_env, tmp_path)
    assert main.stat().st_dev == second.stat().st_dev
    ops.move(home, "b", "second")
    blob = next((second / ".mmw/store").rglob("*"))
    assert blob.exists()
    ops.move(home, "b", "main")
    assert _b_files(second) == set() and ops.verify(home) == []


def test_move_refuses_shared_host_split(env, tmp_path):  # noqa: F811
    cfg, home, root, _ = _three(env, tmp_path)
    H.add_root(home, "second", tmp_path / "srv2")
    with pytest.raises(ValueError, match="must share a root"):
        ops.move(home, "b", "second")


def test_cli_move(split_env, tmp_path):
    cfg, home, main, second, kb = _split_three(split_env, tmp_path)
    r = CliRunner().invoke(cli.app, ["site", "move", "b", "second", "--yes", "--home", str(home)])
    assert r.exit_code == 0, r.output
    assert "copies" in r.output and "recreate" in r.output and _b_files(second)
