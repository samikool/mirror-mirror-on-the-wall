"""A network with the VS Code source publishes an offline Marketplace."""
import json

import pytest

from mmw.core import config
from mmw.core.publisher import publish_network
from mmw.providers.vscode import image
from test_gallery_image import _fake_docker
from test_vscode_review_fixes import _sync, _vs, up  # noqa: F401  (fixture)

APT = "https://archive.ubuntu.com/ubuntu"


@pytest.fixture
def docker(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    path, log = _fake_docker(bin_dir)
    monkeypatch.setattr(image, "DOCKER", path)
    return log


def _published(tmp_path, up, docker, **net):  # noqa: F811
    src = _vs(kinds=["extensions", "server"], platforms=["linux-x64"], qualities=["stable"])
    mirror_dir = tmp_path / "m"
    _sync(mirror_dir, src)
    cfg = config.Config(settings=config.Settings(mirror_dir=mirror_dir), sources=[src])
    root = tmp_path / "pub"
    result = publish_network(config.NetworkConfig(name="lab", root=root, **net), cfg, mirror_dir)
    return root, result, cfg


def test_payloads_land_at_microsofts_hosts(tmp_path, up, docker):  # noqa: F811
    root, result, _ = _published(tmp_path, up, docker)
    assert not [e for r in result.mirrors for e in r.errors]
    assert list(root.glob("*.gallerycdn.vsassets.io/**/*"))
    assert list((root / "vscode.download.prss.microsoft.com").rglob("*.tar.gz"))
    assert (root / "main.vscode-cdn.net/extensions/marketplace.json").exists()
    assert not list(root.rglob(".vscode-meta"))
    assert not (root / "marketplace.visualstudio.com").exists()


def test_generated_gallery_files(tmp_path, up, docker):  # noqa: F811
    root, _, _ = _published(tmp_path, up, docker, gallery_port=9100)
    g = root / "vscode-gallery"
    catalog = json.loads((g / "catalog.json").read_text())
    ids = {f"{e['publisher']['publisherName']}.{e['extensionName']}".lower() for e in catalog}
    assert "ms-python.python" in ids
    py = next(e for e in catalog if e["extensionName"] == "python")
    assert {v.get("targetPlatform") for v in py["versions"]} >= {"linux-x64"}
    updates = json.loads((g / "updates.json").read_text())
    assert updates["server-linux-x64"]["stable"]["latest"] in updates["server-linux-x64"]["stable"]["builds"]
    assert (g / "gallery.py").read_bytes().startswith(b"#!/usr/bin/env python3")
    compose = (g / "compose.yaml").read_text()
    assert "127.0.0.1:9100:8080" in compose and "python:3.13-slim" in compose
    # only the gallery's own directory — not the tree with .mmw/tls/ in it
    assert '".:/data:ro"' in compose and "..:" not in compose
    # every network's file sits in a `vscode-gallery/` dir: without a name
    # compose would call them all one project and replace one with the other
    assert "\nname: mmw-gallery-lab\n" in compose
    assert (g / image.TAR_NAME).exists()
    md = (g / "VSCODE.md").read_text()
    assert "*.gallerycdn.vsassets.io" in md and "docker load" in md


def test_compose_project_names_are_per_network():
    from mmw.providers.vscode.publish import compose_yaml
    names = {compose_yaml(8765, "img", n).decode().split("\nname: ", 1)[1].split("\n")[0]
             for n in ("lab", "Lab.Site 2", "_x")}
    assert names == {"mmw-gallery-lab", "mmw-gallery-lab-site-2", "mmw-gallery-x"}


def test_nginx_serves_static_hosts_and_proxies_the_rest(tmp_path, up, docker):  # noqa: F811
    root, _, _ = _published(tmp_path, up, docker)
    conf = (root / "nginx/mirror.conf").read_text()
    assert "server_name ~^[^.]+\\.gallerycdn\\.vsassets\\.io$;" in conf
    assert "location = /_apis/public/gallery/extensionquery {" in conf
    upd = conf.split("server_name update.code.visualstudio.com;")[1].split("server {")[0]
    assert "proxy_pass http://127.0.0.1:8765;" in upd
    assert "server_name vscode.download.prss.microsoft.com;" in conf


def test_certificate_names_include_the_wildcards(tmp_path, up, docker):  # noqa: F811
    from mmw.core.certs import collect_hostnames
    _, _, cfg = _published(tmp_path, up, docker)
    names = collect_hostnames(config.NetworkConfig(name="lab"), cfg)
    assert {"*.gallerycdn.vsassets.io", "*.gallery.vsassets.io", "marketplace.visualstudio.com",
            "update.code.visualstudio.com", "vscode.download.prss.microsoft.com",
            "main.vscode-cdn.net"} <= set(names)


def test_no_docker_reports_but_publishes_everything_else(tmp_path, up, monkeypatch):  # noqa: F811
    monkeypatch.setattr(image, "DOCKER", str(tmp_path / "missing-docker"))
    root, result, _ = _published(tmp_path, up, None)
    assert not [e for r in result.mirrors for e in r.errors]     # a warning, not a failure
    assert any("docker" in w.lower() for w in result.warnings)
    assert (root / "vscode-gallery/catalog.json").exists()


def test_a_mixed_network_leaves_apt_byte_identical(tmp_path, up, docker):  # noqa: F811
    from test_seam_golden import _build
    mirror_dir, apt_cfg = _build(tmp_path / "apt")
    only_apt = tmp_path / "only-apt"
    publish_network(config.NetworkConfig(name="lab", root=only_apt), apt_cfg, mirror_dir)
    src = _vs(kinds=["server"], platforms=["linux-x64"], qualities=["stable"])
    _sync(mirror_dir, src)
    mixed_cfg = apt_cfg.model_copy(update={"sources": [*apt_cfg.sources, src]})
    mixed = tmp_path / "mixed"
    publish_network(config.NetworkConfig(name="lab", root=mixed), mixed_cfg, mirror_dir)
    for f in only_apt.rglob("*"):
        rel = f.relative_to(only_apt)
        if f.is_file() and rel.parts[0] != "nginx":
            assert (mixed / rel).read_bytes() == f.read_bytes(), rel
    apt_conf = (only_apt / "nginx/mirror.conf").read_text().replace(str(only_apt), "R")
    mixed_conf = (mixed / "nginx/mirror.conf").read_text().replace(str(mixed), "R")
    for block in apt_conf.split("server {")[1:]:
        assert block in mixed_conf


def test_the_guide_checks_a_build_the_network_serves(tmp_path, up, docker):  # noqa: F811
    root, _, _ = _published(tmp_path, up, docker)
    md = (root / "vscode-gallery/VSCODE.md").read_text()
    assert "/api/update/server-linux-x64/stable/" in md
    assert "/api/update/linux-x64/" not in md


def test_an_extensions_only_guide_checks_the_query_instead():
    from mmw.core import config
    from mmw.providers.vscode.publish import vscode_md
    md = vscode_md(config.NetworkConfig(name="lab"), "t.tar", {}).decode()
    assert "/api/update/" not in md and "extensionquery" in md


def test_progress_names_every_group_and_never_restarts_its_count(tmp_path, up, docker):  # noqa: F811
    from test_seam_golden import _build
    mirror_dir, apt_cfg = _build(tmp_path / "apt")
    src = _vs(kinds=["server"], platforms=["linux-x64"], qualities=["stable"])
    _sync(mirror_dir, src)
    cfg = apt_cfg.model_copy(update={"sources": [*apt_cfg.sources, src]})
    calls = []
    publish_network(config.NetworkConfig(name="lab", root=tmp_path / "pub"), cfg, mirror_dir,
                    mirror_start_callback=lambda name, i, n: calls.append((name, i, n)))
    assert "vscode.download.prss.microsoft.com" in [c[0] for c in calls]
    assert [c[1] for c in calls] == list(range(1, len(calls) + 1))
    assert calls[-1][2] == len(calls)
