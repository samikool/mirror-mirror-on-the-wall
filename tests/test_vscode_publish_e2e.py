"""The published tree works as a Marketplace; bundles carry it; deltas stay small."""
import json
import tarfile
import threading

import httpx
import pytest

from mmw.core import config
from mmw.core.publisher import bundle_network, publish_network
from mmw.resources import gallery
from test_vscode_publish import _published, docker  # noqa: F401  (fixture)
from test_vscode_review_fixes import _sync, _vs, up  # noqa: F401  (fixture)


def _serve(root):
    store = gallery.Store(str(root / "vscode-gallery"))
    server = gallery.make_server(store, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}")


def test_the_published_gallery_answers_vscode(tmp_path, up, docker):  # noqa: F811
    root, _, _ = _published(tmp_path, up, docker)
    server, c = _serve(root)
    try:
        body = {"filters": [{"criteria": [{"filterType": 7, "value": "ms-python.python"}],
                             "pageNumber": 1, "pageSize": 10, "sortBy": 0, "sortOrder": 0}],
                "flags": 914}
        (ext,) = c.post("/_apis/public/gallery/extensionquery", json=body).json()["results"][0]["extensions"]
        source = ext["versions"][0]["files"][0]["source"]
        rel = source.split("://", 1)[1]
        assert (root / rel).exists()                        # nginx would serve it
        updates = json.loads((root / "vscode-gallery/updates.json").read_text())
        latest = updates["server-linux-x64"]["stable"]["latest"]
        assert c.get(f"/api/update/server-linux-x64/stable/{latest}").status_code == 204
        loc = c.get(f"/commit:{latest}/server-linux-x64/stable").headers["location"]
        assert (root / loc.split("://", 1)[1]).exists()
    finally:
        server.shutdown()


def _bundle(tmp_path, cfg, name="lab"):
    net = config.NetworkConfig(name=name, bundle_dir=tmp_path / "out")
    r = bundle_network(net, cfg, cfg.settings.mirror_dir)
    with tarfile.open(r.out_path) as tar:
        return r, {m.name for m in tar if m.isfile()}


def test_bundle_carries_it_and_a_delta_does_not_resend_it(tmp_path, up, docker):  # noqa: F811
    _, _, cfg = _published(tmp_path, up, docker)
    first, names = _bundle(tmp_path, cfg)
    assert "vscode-gallery/python-3.13-slim-linux-amd64.tar" in names
    assert "vscode-gallery/catalog.json" in names
    second, names2 = _bundle(tmp_path, cfg)
    # Nothing changed: no gallery file — least of all the image tar — is resent.
    assert not [n for n in names2 if n.startswith("vscode-gallery/")]


def test_apply_lays_out_the_same_tree(tmp_path, up, docker):  # noqa: F811
    import subprocess
    import sys
    root, _, cfg = _published(tmp_path, up, docker)
    r, _ = _bundle(tmp_path, cfg)
    far = tmp_path / "far"
    far.mkdir()
    subprocess.run([sys.executable, str(r.out_path.parent / "apply.py"), str(r.out_path),
                    "--root", str(far)], check=True)
    for f in (root / "vscode-gallery").iterdir():
        assert (far / "vscode-gallery" / f.name).read_bytes() == f.read_bytes(), f.name
    assert list(far.glob("*.gallerycdn.vsassets.io"))


def test_removing_the_source_prunes_its_tree(tmp_path, up, docker):  # noqa: F811
    root, _, cfg = _published(tmp_path, up, docker)
    apt_only = cfg.model_copy(update={"sources": []})
    publish_network(config.NetworkConfig(name="lab", root=root), apt_only, cfg.settings.mirror_dir)
    assert not (root / "vscode-gallery").exists()
    assert not list(root.glob("*.gallerycdn.vsassets.io"))
    assert not (root / "vscode.download.prss.microsoft.com").exists()


@pytest.mark.parametrize("replay", [False, True], ids=["live", "snapshot"])
@pytest.mark.parametrize("prune", [False, True])
def test_gallery_removal_respects_prune(tmp_path, up, docker, replay, prune):  # noqa: F811
    from mmw.core.publisher import publish_snapshot
    from mmw.core.snapshots import take_snapshot

    root, _, cfg = _published(tmp_path, up, docker)
    without_vscode = cfg.model_copy(update={"sources": []})
    net = config.NetworkConfig(name="lab", root=root, prune=prune)
    catalog = root / "vscode-gallery/catalog.json"
    before = catalog.read_bytes()
    if replay:
        snap = take_snapshot(net, without_vscode, cfg.settings.mirror_dir)
        result = publish_snapshot(net, without_vscode, cfg.settings.mirror_dir, snap.stamp)
    else:
        result = publish_network(net, without_vscode, cfg.settings.mirror_dir)
    assert not result.errors
    if prune:
        assert not (root / "vscode-gallery").exists()
    else:
        assert catalog.read_bytes() == before


def test_a_bundling_machine_without_docker_still_bundles(tmp_path, up, monkeypatch):  # noqa: F811
    """The airgap path is exactly a machine without Docker: the missing image
    is a warning on the bundle, never a reason to refuse it."""
    from mmw.providers.vscode import image
    from test_vscode_review_fixes import _vs as vs
    monkeypatch.setattr(image, "DOCKER", str(tmp_path / "missing-docker"))
    src = vs(kinds=["extensions"], platforms=["linux-x64"])
    mirror_dir = tmp_path / "m"
    _sync(mirror_dir, src)
    cfg = config.Config(settings=config.Settings(mirror_dir=mirror_dir), sources=[src])
    r, names = _bundle(tmp_path, cfg)
    assert "vscode-gallery/catalog.json" in names
    assert not [n for n in names if n.endswith(".tar")]
    assert any("python:3.13-slim" in w for w in r.warnings)
    assert not r.errors


def test_the_gallery_index_lands_after_the_files_it_names(tmp_path, up, docker):  # noqa: F811
    """`vscode-gallery/` sorts before `vscode.download…/`: in path order a far
    side mid-apply would advertise builds that are not there yet."""
    _, _, cfg = _published(tmp_path, up, docker)
    r, _ = _bundle(tmp_path, cfg)
    with tarfile.open(r.out_path) as tar:
        order = [m.name for m in tar if m.isfile()]
    last_payload = max(i for i, n in enumerate(order)
                       if n.startswith(("vscode.download.prss.microsoft.com/", "main.vscode-cdn.net/"))
                       or ".gallerycdn.vsassets.io/" in n)
    for name in ("vscode-gallery/catalog.json", "vscode-gallery/updates.json"):
        assert order.index(name) > last_payload, name


def test_snapshot_replay_places_the_gallery_index_last(tmp_path, up, docker):  # noqa: F811
    from mmw.core.publisher import publish_snapshot
    from mmw.core.snapshots import take_snapshot
    _, _, cfg = _published(tmp_path, up, docker)
    net = config.NetworkConfig(name="lab", root=tmp_path / "pub")
    snap = take_snapshot(net, cfg, cfg.settings.mirror_dir)
    seen = []
    publish_snapshot(net, cfg, cfg.settings.mirror_dir, snap.stamp, root=tmp_path / "again",
                     progress=lambda rel, status: seen.append(rel))
    seen = [n for n in seen if "/" in n]          # files; prune reports bases too
    last_payload = max(i for i, n in enumerate(seen) if n.startswith("vscode.download"))
    assert seen.index("vscode-gallery/updates.json") > last_payload
