"""The far side applies its own network's bundle of points (format 3, a
selection) as it applied format 2: network files and settings, and a suite
the selection no longer has retires; another network's changes none of it."""
import json
import tarfile

from mmw.site.apply import apply_bundle
from mmw.site.db import SiteDB

from test_dry_run_totals import two_mirrors  # noqa: F401
from test_site_apply import _bundle, _tree, env  # noqa: F401
from test_site_apply_timeline import set_a
from test_snapshot_repo_versions import _change_a, _key

quiet = lambda *a: None  # noqa: E731


def test_network_files_and_settings_apply(env, tmp_path):  # noqa: F811
    cfg, net, md, home, root = env
    set_a(md, 1)
    r = apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1")), say=quiet)
    assert r.updated and (root / "nginx/mirror.conf").is_file()
    with SiteDB(home) as db:
        assert json.loads(db.get_meta("settings"))["network"]["name"] == net.name
        assert db.applied()


def test_listed_but_unchanged_suite_is_kept(env, tmp_path):  # noqa: F811
    cfg, net, md, home, root = env
    set_a(md, 1)
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1")), say=quiet)
    b_files = {p for p in _tree(root) if p.startswith("example.com/b/")}
    set_a(md, 2)
    r = apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "2")), say=quiet)
    assert _key(cfg, "a") in r.updated
    assert {p for p in _tree(root) if p.startswith("example.com/b/")} == b_files


def test_a_suite_gone_from_the_selection_retires(env, tmp_path):  # noqa: F811
    cfg, net, md, home, root = env
    set_a(md, 1)
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1")), say=quiet)
    assert (root / "example.com/a/Release").exists()
    _change_a(md)                                # a has no Release any more: its suite is gone
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "2")), say=quiet)
    assert not (root / "example.com/a/Release").exists()


def test_another_networks_selection_changes_no_network_files(env, tmp_path, monkeypatch):  # noqa: F811
    from mmw.site.apply import apply_to_set
    from mmw.site.mset import open_set
    from test_site_apply import _rewrite
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg, net, md, home, root = env
    set_a(md, 1)
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1")), say=quiet)
    before = (root / "nginx/mirror.conf").read_bytes()
    set_a(md, 2)
    src = _bundle(cfg, net, md, tmp_path / "2")

    def rename(member, data):
        if member.name == ".mmw/manifest.jsonl":
            head, _, rest = data.partition(b"\n")
            h = json.loads(head)
            h["network"] = "elm"
            h["settings"] = json.dumps({**json.loads(h["settings"]), "network": {"name": "elm"}})
            return json.dumps(h, sort_keys=True).encode() + b"\n" + rest
        return data
    other = _rewrite(src, tmp_path / "elm.tar", rename)
    result = apply_to_set(open_set([str(root)]), str(other), place={"*": str(root)}, say=quiet)
    assert result.foreign
    assert (root / "nginx/mirror.conf").read_bytes() == before
    with tarfile.open(other) as tar:
        assert tar.getnames()[0] == ".mmw/manifest.jsonl"
