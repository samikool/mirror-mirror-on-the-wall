"""Apply bundles from another network using the same path as the CLI and TUI."""

import hashlib
import io
import tarfile

import pytest

from mmw.core.publisher.manifest import ManifestHeader, write_manifest
from mmw.core.served_tree import MANIFEST_MEMBER
from mmw.core.snapshots import repo_digests
from mmw.site.apply import ApplyError, apply_bundle, apply_to_set
from mmw.site.db import SiteDB
from mmw.site.mset import open_set
from mmw.site.store import Store

from test_dry_run_totals import two_mirrors  # noqa: F401
from test_site_apply_timeline import _add_c, _bundle, _served_day, _track, env, set_a  # noqa: F401
from test_snapshot_repo_versions import _key


@pytest.fixture
def two_nets(env, tmp_path):  # noqa: F811
    cfg, net, md, _home = env
    elm = net.model_copy(update={"name": "elm", "root": tmp_path / "pub-elm"})
    return cfg, net, elm, md


def _set_apply(root, path, **kw):
    return apply_to_set(open_set([str(root)]), path, place={"*": str(root)}, say=lambda *a: None, **kw)


@pytest.fixture(autouse=True)
def _xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def _oak_site(cfg, net, md, tmp_path, days=(1, 2, 4)):
    root = tmp_path / "oak-srv"
    for i, day in enumerate(days):
        set_a(md, day)
        b = _bundle(cfg, net, md, tmp_path / f"x{day}", from_ref=None)
        if i == 0:
            apply_to_set(None, b, place={"*": str(root)}, say=lambda *a: None)
        else:
            _set_apply(root, b)
    return root


def _root_db(root):
    from mmw.site.home import read_root
    return SiteDB(read_root(str(root)).home)


def _nginx(root) -> str:
    return "".join(p.read_text() for p in sorted((root / "nginx").rglob("*.conf")))


def test_other_networks_history_merges(two_nets, tmp_path):
    cfg, oak, elm, md = two_nets
    root = _oak_site(cfg, oak, md, tmp_path)
    before = _nginx(root)
    set_a(md, 3)
    r = _set_apply(root, _bundle(cfg.model_copy(update={"networks": [oak, elm]}), elm, md,
                                 tmp_path / "y3", from_ref=None))
    assert r.foreign
    assert r.lines[0] == ("this bundle is for network 'elm' — this mirror holds 'oak': importing repository "
                          "history; its network files (nginx, settings, TLS) are not applied")
    with _root_db(root) as db:
        a = _key(cfg, "a")
        assert [p.date[:8] for p in db.points(_track(db, a).id)] == ["20261104", "20261103", "20261102",
                                                                      "20261101"]
        assert _served_day(db, a) == "20261104"
    assert _nginx(root) == before


def test_foreign_repo_skipped_by_default(two_nets, tmp_path):
    cfg, oak, elm, md = two_nets
    root = _oak_site(cfg, oak, md, tmp_path, days=(1,))
    cfg2 = _add_c(cfg, md).model_copy(update={"networks": [oak, elm]})
    r = _set_apply(root, _bundle(cfg2, elm, md, tmp_path / "y", from_ref=None))
    assert r.skipped == ["c"]
    assert any("c  not mirrored here — skipped (deploy it: mmw site apply BUNDLE --deploy 'c')" in ln
               for ln in r.lines)
    assert not (root / "example.net").exists()
    with _root_db(root) as db:
        assert "https://example.net/c/" not in {s.repo for s in db.repo_states()}


def test_foreign_repo_deployed_on_request(two_nets, tmp_path):
    cfg, oak, elm, md = two_nets
    root = _oak_site(cfg, oak, md, tmp_path, days=(1,))
    cfg2 = _add_c(cfg, md).model_copy(update={"networks": [oak, elm]})
    r = _set_apply(root, _bundle(cfg2, elm, md, tmp_path / "y", from_ref=None), deploy={"c"})
    assert r.skipped == []
    assert (root / "example.net/c/c.deb").read_bytes() == b"c" * 120
    assert "example.net" in _nginx(root)
    # The site's own network keeps c's server block on its next bundle.
    set_a(md, 2)
    _set_apply(root, _bundle(cfg, oak, md, tmp_path / "x2", from_ref=None))
    assert "example.net" in _nginx(root) and (root / "example.net/c/c.deb").exists()


def test_single_root_apply_skips_a_foreign_repository_not_held(env, tmp_path):  # noqa: F811
    cfg, oak, md, home = env
    elm = oak.model_copy(update={"name": "elm", "root": tmp_path / "pub-elm"})
    apply_bundle(home, _bundle(cfg, oak, md, tmp_path / "oak", from_ref=None), say=lambda *a: None)
    cfg = _add_c(cfg, md).model_copy(update={"networks": [oak, elm]})
    incoming = _bundle(cfg, elm, md, tmp_path / "elm", from_ref=None)

    result = apply_bundle(home, incoming, say=lambda *a: None)

    assert not (tmp_path / "srv/example.net/c/c.deb").exists()
    with SiteDB(home) as db:
        assert db.repo_state(_key(cfg, "c")) is None
    assert result.foreign and result.skipped == ["c"]
    assert not Store(tmp_path / "srv").has(hashlib.sha256(b"c" * 120).hexdigest())


def _format2_delta(tmp_path, a, *, bad_digest=False, staged=False, late_manifest=False):
    data = b"the foreign repository is the only sender of these bytes"
    checksum = hashlib.sha256(data).hexdigest()
    a_path, c_path = "example.com/a/new.deb", "example.net/c/new.deb"
    repos = {a: {"label": "a", "bases": ["example.com/a"], "from": 1, "to": 2},
             "c": {"label": "c", "bases": ["example.net/c"], "from": None, "to": 1}}
    digests = repo_digests([(a_path, checksum), (c_path, checksum)], {k: r["bases"] for k, r in repos.items()})
    for key, meta in repos.items():
        meta["digest"] = digests[key]
    if bad_digest:
        repos["c"]["digest"] = "0" * 64
    network_data = b"other network's top-level file"
    manifest = tmp_path / "manifest.jsonl"
    write_manifest(manifest, ManifestHeader(network="elm", created="20261102T120000Z", previous=None,
                                           full=False, file_count=2, format=2, repos=repos),
                   [(a_path, checksum, a, len(data)), (c_path, checksum, "c", len(data)),
                    ("network.txt", hashlib.sha256(network_data).hexdigest(), None, len(network_data))])
    entries = ((MANIFEST_MEMBER, manifest.read_bytes()), (c_path, data), ("network.txt", network_data))
    if staged:
        bundle = tmp_path / "elm-stage"
        for name, content in entries:
            path = bundle / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return str(bundle), checksum
    bundle = tmp_path / "elm-delta.tar"
    with tarfile.open(bundle, "w") as tar:
        if late_manifest:
            entries = (*entries[1:], entries[0])
        for name, content in entries:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            tar.addfile(member, io.BytesIO(content))
    return str(bundle), checksum


@pytest.mark.parametrize("staged", [False, True], ids=["tar", "stage"])
def test_format2_single_root_does_not_take_shared_bytes_from_a_skipped_member(env, tmp_path, staged):  # noqa: F811
    cfg, oak, md, home = env
    apply_bundle(home, _bundle(cfg, oak, md, tmp_path / "oak", from_ref=None), say=lambda *a: None)
    a = _key(cfg, "a")
    incoming, checksum = _format2_delta(tmp_path, a, staged=staged)

    planned = apply_bundle(home, incoming, dry_run=True, say=lambda *a: None)
    assert planned.foreign and planned.skipped == ["c"]
    assert not Store(tmp_path / "srv").has(checksum)
    result = apply_bundle(home, incoming, say=lambda *a: None)

    assert a in result.incomplete
    assert not Store(tmp_path / "srv").has(checksum)


def test_format2_single_root_validates_a_skipped_repository_digest(env, tmp_path):  # noqa: F811
    cfg, oak, md, home = env
    apply_bundle(home, _bundle(cfg, oak, md, tmp_path / "oak", from_ref=None), say=lambda *a: None)
    incoming, _ = _format2_delta(tmp_path, _key(cfg, "a"), bad_digest=True)

    with pytest.raises(ApplyError, match="c: its files do not match the version"):
        apply_bundle(home, incoming, say=lambda *a: None)


@pytest.mark.parametrize("stdin", [False, True], ids=["tar", "stdin"])
def test_single_root_classifies_members_before_a_late_manifest(env, tmp_path, monkeypatch, stdin):  # noqa: F811
    from pathlib import Path
    from types import SimpleNamespace

    cfg, oak, md, home = env
    apply_bundle(home, _bundle(cfg, oak, md, tmp_path / "oak", from_ref=None), say=lambda *a: None)
    a = _key(cfg, "a")
    incoming, checksum = _format2_delta(tmp_path, a, late_manifest=True)
    if stdin:
        monkeypatch.setattr("sys.stdin", SimpleNamespace(buffer=io.BytesIO(Path(incoming).read_bytes())))
        incoming = "-"

    result = apply_bundle(home, incoming, say=lambda *a: None)

    assert result.skipped == ["c"] and a in result.incomplete
    assert not Store(tmp_path / "srv").has(checksum)
