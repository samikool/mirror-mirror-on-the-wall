"""`mmw site apply`: everything lands in the store and is checked against
the manifest before the served tree changes; then each repository switches,
unless it is held."""
import io
import tarfile
from pathlib import Path

import pytest

from mmw.core.publisher import publish_network
from mmw.core.publisher.bundle import bundle_network, stage_network
from mmw.site import home as H
from mmw.site.apply import ApplyError, apply_bundle
from mmw.site.db import SiteDB

from test_bundle_mmw import _change_b, _mmw
from test_dry_run_totals import two_mirrors  # noqa: F401 — the shared pool fixture
from test_snapshot_repo_versions import _change_a, _key


@pytest.fixture
def env(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    home = H.init_root(tmp_path / "srv", name="main", set_id="s1", network=net.name)
    from mmw.site import recents
    recents.remember(str(tmp_path / "srv"))
    return cfg, net, md, home, tmp_path / "srv"


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and not p.relative_to(root).as_posix().startswith(".mmw/")
            and not p.relative_to(root).as_posix().startswith("nginx/")}


def _published(cfg, net, md, tmp_path, name="pub"):
    root = tmp_path / name
    publish_network(net.model_copy(update={"root": root}), cfg, md)
    return _tree(root)


def _bundle(cfg, net, md, out, **kw):
    return bundle_network(net, cfg, md, out_path=out, **kw).out_path


def test_apply_full_then_delta(env, tmp_path):
    cfg, net, md, home, root = env
    r = apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=lambda *a: None)
    assert sorted(r.updated) == sorted([_key(cfg, "a"), _key(cfg, "b")])
    assert _tree(root) == _published(cfg, net, md, tmp_path, "p1")
    assert (root / "nginx/mirror.conf").is_file() and "__MMW_ROOT__" not in (root / "nginx/mirror.conf").read_text()
    _change_a(md)
    r = apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "2")), say=lambda *a: None)
    assert r.updated == [_key(cfg, "a")]
    assert _tree(root) == _published(cfg, net, md, tmp_path, "p2")
    with SiteDB(home) as db:
        assert [v.number for v in db.versions(_key(cfg, "a"))] == [2, 1]


def test_apply_from_stage_and_stdin(env, tmp_path, monkeypatch):
    cfg, net, md, home, root = env
    staged = stage_network(net, cfg, md, out_dir=tmp_path / "s", from_ref=None).out_path
    apply_bundle(home, str(staged), say=lambda *a: None)
    first = _tree(root)
    _change_a(md)
    data = _bundle(cfg, net, md, tmp_path / "2").read_bytes()

    class In:
        buffer = io.BytesIO(data)
    monkeypatch.setattr("sys.stdin", In)
    r = apply_bundle(home, "-", say=lambda *a: None)
    assert r.updated == [_key(cfg, "a")] and _tree(root) != first


def test_apply_refuses_format_1(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = two_mirrors                                     # an apply.py network
    home = tmp_path / "site"
    H.init_site(home, tmp_path / "srv")
    with pytest.raises(ApplyError, match="apply.py bundle") as e:
        apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=lambda *a: None)
    assert e.value.code == 5


def _rewrite(src: Path, dst: Path, change):
    """Copy a bundle member by member, letting `change(member, data)` return
    new bytes, or None to drop the member."""
    with tarfile.open(src) as a, tarfile.open(dst, "w") as b:
        for m in a:
            data = a.extractfile(m).read() if m.isfile() else None
            new = change(m, data) if data is not None else data
            if m.isfile() and new is None:
                continue
            if data is not None:
                m.size = len(new)
                b.addfile(m, io.BytesIO(new))
            else:
                b.addfile(m)
    return dst


def test_apply_corrupt_member_changes_nothing(env, tmp_path):
    cfg, net, md, home, root = env
    good = _bundle(cfg, net, md, tmp_path / "1", from_ref=None)
    bad = _rewrite(good, tmp_path / "bad.tar",
                   lambda m, d: d[:-1] + b"!" if m.name.endswith("a.deb") else d)
    with pytest.raises(ApplyError) as e:
        apply_bundle(home, str(bad), say=lambda *a: None)
    assert e.value.code == 2
    with SiteDB(home) as db:
        assert db.repo_states() == []
    assert _tree(root) == {}


def test_apply_truncated_changes_nothing(env, tmp_path):
    cfg, net, md, home, root = env
    good = _bundle(cfg, net, md, tmp_path / "1", from_ref=None)
    short = _rewrite(good, tmp_path / "short.tar", lambda m, d: None if m.name.endswith("b.deb") else d)
    with pytest.raises(ApplyError, match="truncated") as e:
        apply_bundle(home, str(short), say=lambda *a: None)
    assert e.value.code == 5 and _tree(root) == {}


def test_apply_missing_base_refuses_only_that_repo(env, tmp_path):
    cfg, net, md, home, root = env
    ka, kb = _key(cfg, "a"), _key(cfg, "b")
    bundle_network(net, cfg, md, out_path=tmp_path / "x", from_ref=None)     # the far side was "sent" v1 …
    other = tmp_path / "other-site"                                           # … but this site never got b's
    H.init_site(other, tmp_path / "srv2")
    apply_bundle(other, str(_bundle(cfg, net, md, tmp_path / "a-only", from_ref=None, repos=[ka])),
                 say=lambda *a: None)
    _change_a(md)
    _change_b(md)
    r = apply_bundle(other, str(_bundle(cfg, net, md, tmp_path / "2")), say=lambda *a: None)
    assert r.updated == [ka] and kb in r.refused and "--repo" in r.refused[kb]


def test_apply_held_repo_is_stored_not_switched(env, tmp_path):
    cfg, net, md, home, root = env
    ka = _key(cfg, "a")
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=lambda *a: None)
    with SiteDB(home) as db:
        db.set_held(ka, True)
    before = _tree(root)
    _change_a(md)
    r = apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "2")), say=lambda *a: None)
    assert r.stored_held == [ka] and r.updated == []
    assert _tree(root) == before
    with SiteDB(home) as db:
        st = db.repo_state(ka)
        assert st.serving.number == 1 and st.held and db.versions(ka)[0].number == 2


def test_apply_forward_switches_held(env, tmp_path):
    cfg, net, md, home, root = env
    ka = _key(cfg, "a")
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=lambda *a: None)
    with SiteDB(home) as db:
        db.set_held(ka, True)
    _change_a(md)
    r = apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "2")), forward={ka}, say=lambda *a: None)
    assert r.updated == [ka]
    with SiteDB(home) as db:
        st = db.repo_state(ka)
        assert st.serving.number == 2 and not st.held


def test_apply_reports_nginx_change(env, tmp_path):
    cfg, net, md, home, root = env
    r = apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=lambda *a: None)
    assert r.network_changed and any("recreate nginx" in a for a in r.actions)
    _change_a(md)
    r = apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "2")), say=lambda *a: None)
    assert not r.network_changed


def test_store_blob_survives_nginx_substitution(env, tmp_path):
    cfg, net, md, home, root = env
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=lambda *a: None)
    from mmw.site.store import Store
    with SiteDB(home) as db:
        cs = db.network_files()["nginx/mirror.conf"]
    import hashlib
    assert hashlib.sha256(Store(root).path(cs).read_bytes()).hexdigest() == cs   # never rewritten in place
    assert (root / "nginx/mirror.conf").stat().st_ino != Store(root).path(cs).stat().st_ino


def test_cli_apply(env, tmp_path):
    from typer.testing import CliRunner
    from mmw import cli
    cfg, net, md, home, root = env
    b = _bundle(cfg, net, md, tmp_path / "1", from_ref=None)
    r = CliRunner().invoke(cli.app, ["site", "apply", str(b)])
    assert r.exit_code == 1 and "--root" in r.output                # a new hostname needs a root
    r = CliRunner().invoke(cli.app, ["site", "apply", str(b), "--root", f"*={root}"])
    assert r.exit_code == 0, r.output
    assert "a  full → v1" in r.output and "start nginx in main" in r.output


def test_root_apply_takes_members_pushed_to_it(env, tmp_path):
    """RootApply, driven by hand, does what apply_bundle does."""
    from mmw.resources.apply import MANIFEST_MEMBER
    from mmw.site.apply import RootApply, members_of
    cfg, net, md, home, root = env
    b = _bundle(cfg, net, md, tmp_path / "1", from_ref=None)
    with RootApply(home, say=lambda *a: None) as ra:
        for m in members_of(str(b)):
            if m.name == MANIFEST_MEMBER:
                ra.begin(m.fh.read())
            else:
                ra.take(m)
        r = ra.finish()
    assert sorted(r.updated) == sorted([_key(cfg, "a"), _key(cfg, "b")])
    assert _tree(root) == _published(cfg, net, md, tmp_path, "p1")
