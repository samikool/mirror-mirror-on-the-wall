"""A network ships an offline nginx runtime through publish, snapshot and bundle."""
# ruff: noqa: F811 — pytest fixtures imported from the shared fixture module
import hashlib
import json
import shlex
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from mmw.core.pool import Pool
from mmw.core.publisher import publish_network, publish_snapshot
from mmw.core.publisher import nginx_container as nginx
from mmw.core.publisher.bundle import bundle_network
from mmw.core.publisher.nginx import ROOT_PLACEHOLDER
from mmw.core.snapshots import take_snapshot
from test_dry_run_totals import two_mirrors  # noqa: F401, F811
from test_gallery_image import _fake_docker


def _docker(tmp_path, monkeypatch, payload=b"nginx image", image_id="sha256:one"):
    bindir = tmp_path / "docker-bin"
    bindir.mkdir(exist_ok=True)
    binary, log = _fake_docker(bindir, image_id=image_id)
    data = bindir / "payload"
    data.write_bytes(payload)
    script = Path(binary)
    script.write_text(script.read_text().replace('echo "image-bytes" > "$out"',
                                                f'cat {shlex.quote(str(data))} > "$out"'))
    monkeypatch.setattr(nginx, "DOCKER", binary)
    return log


def _members(path):
    with tarfile.open(path) as archive:
        return {m.name: archive.extractfile(m).read() for m in archive if m.isfile() or m.islnk()}


def test_publish_ships_image_and_mounts_generated_config(two_mirrors, tmp_path, monkeypatch):
    cfg, net, mirror_dir = two_mirrors
    log = _docker(tmp_path, monkeypatch)
    phases = []
    result = publish_network(net, cfg, mirror_dir, progress=lambda rel, state: phases.append(state))
    assert not result.errors and not result.warnings
    assert (net.root / "nginx" / nginx.TAR_NAME).read_bytes() == b"nginx image"
    compose = (net.root / "nginx/compose.yaml").read_text()
    assert '"/path/to/oak:/srv/mmw:ro"' in compose
    assert '"/path/to/oak/nginx:/etc/nginx/conf.d:ro"' in compose
    assert 'type: bind' not in compose and 'MMW_ROOT' not in compose
    conf = (net.root / "nginx/mirror.conf").read_text()
    assert "root /srv/mmw/example.com;" in conf and str(net.root) not in conf
    assert "network_mode:" not in compose and "pull_policy: never" in compose
    assert '"80:80"' in compose and '"443:443"' in compose
    assert "name: mmw-nginx-oak" in compose
    assert "nginx-image:pulling" in phases and "nginx-image:done" in phases
    publish_network(net, cfg, mirror_dir)
    assert len([line for line in log.read_text().splitlines() if line.startswith("save ")]) == 1
    assert "pull --platform linux/amd64 nginx:latest" in log.read_text()


def test_snapshot_freezes_image_and_replay_does_not_contact_docker(two_mirrors, tmp_path, monkeypatch):
    cfg, net, mirror_dir = two_mirrors
    _docker(tmp_path, monkeypatch, b"original image")
    snap = take_snapshot(net, cfg, mirror_dir)
    with Pool(mirror_dir) as pool:
        assert dict(pool.snapshot_files(snap.id))[f"nginx/{nginx.TAR_NAME}"] == hashlib.sha256(b"original image").hexdigest()
    _docker(tmp_path, monkeypatch, b"new image", "sha256:two")
    publish_network(net, cfg, mirror_dir)
    monkeypatch.setattr(nginx, "nginx_image", lambda *a, **kw: pytest.fail("snapshot replay must not pull"))
    restored = tmp_path / "restored"
    publish_snapshot(net, cfg, mirror_dir, snap.stamp, root=restored)
    assert (restored / "nginx" / nginx.TAR_NAME).read_bytes() == b"original image"
    result = bundle_network(net, cfg, mirror_dir, to_ref=snap.stamp, full=True, out_path=tmp_path / "old.tar")
    assert _members(result.out_path)[f"nginx/{nginx.TAR_NAME}"] == b"original image"


def test_delta_only_resends_image_when_it_changes_and_apply_leaves_binary_untouched(two_mirrors, tmp_path, monkeypatch):
    cfg, net, mirror_dir = two_mirrors
    destination = tmp_path / "far side"
    # Both the placeholder and the real root must survive in binary payloads.
    payload = b"\x00" + ROOT_PLACEHOLDER.encode() + str(destination).encode() + b"\xff"
    _docker(tmp_path, monkeypatch, payload)
    first = bundle_network(net, cfg, mirror_dir, full=True, out_path=tmp_path / "first.tar")
    assert not first.errors
    assert _members(first.out_path)[f"nginx/{nginx.TAR_NAME}"] == payload
    same = bundle_network(net, cfg, mirror_dir, out_path=tmp_path / "same.tar")
    assert f"nginx/{nginx.TAR_NAME}" not in _members(same.out_path)
    _docker(tmp_path, monkeypatch, payload + b"updated", "sha256:two")
    changed = bundle_network(net, cfg, mirror_dir, out_path=tmp_path / "changed.tar")
    assert _members(changed.out_path)[f"nginx/{nginx.TAR_NAME}"] == payload + b"updated"

    apply = tmp_path / "apply.py"
    for result in (first, same, changed):
        run = subprocess.run([sys.executable, str(apply), str(result.out_path), "--root", str(destination), "--verify"],
                             capture_output=True, text=True)
        assert run.returncode == 0, run.stdout + run.stderr
    assert (destination / "nginx" / nginx.TAR_NAME).read_bytes() == payload + b"updated"
    compose = (destination / "nginx/compose.yaml").read_text()
    assert ROOT_PLACEHOLDER not in compose
    assert 'type: bind' not in compose and 'MMW_ROOT' not in compose
    assert '"/path/to/oak:/srv/mmw:ro"' in compose
    assert "root /srv/mmw/example.com;" in (destination / "nginx/mirror.conf").read_text()


def test_nginx_off_omits_and_prunes_runtime(two_mirrors, tmp_path, monkeypatch):
    cfg, net, mirror_dir = two_mirrors
    _docker(tmp_path, monkeypatch)
    publish_network(net, cfg, mirror_dir)
    off = net.model_copy(update={"nginx": False})
    monkeypatch.setattr(nginx, "nginx_image", lambda *a, **kw: pytest.fail("nginx is off"))
    publish_network(off, cfg, mirror_dir)
    assert not list((net.root / "nginx").rglob("*"))
    result = bundle_network(off, cfg, mirror_dir, full=True, out_path=tmp_path / "off.tar")
    assert not any(name.startswith("nginx/") for name in _members(result.out_path))


def test_no_docker_warns_and_still_writes_setup(two_mirrors):
    cfg, net, mirror_dir = two_mirrors
    result = publish_network(net, cfg, mirror_dir)
    assert not result.errors
    assert any("nginx image is not included" in w for w in result.warnings)
    assert (net.root / "nginx/compose.yaml").is_file()


def test_tls_uses_the_portable_tree_without_external_certificate_mounts():
    files = nginx.deployment_files("oak")
    compose = files["nginx/compose.yaml"].decode()
    assert "-certs/" not in compose
    assert '"/path/to/oak:/srv/mmw:ro"' in compose
    assert ".mmw/tls" in files["nginx/README.md"].decode()


@pytest.mark.parametrize("with_gallery", [False, True])
def test_compose_parses_with_the_docker_compose_cli(tmp_path, with_gallery):
    import shutil
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose CLI unavailable")
    compose = tmp_path / "compose.yaml"
    compose.write_bytes(nginx.deployment_files("oak", with_gallery=with_gallery)["nginx/compose.yaml"])
    run = subprocess.run(["docker", "compose", "-f", str(compose), "config", "--format", "json"],
                         capture_output=True, text=True)
    if "is not a docker command" in run.stderr:
        pytest.skip("Docker Compose CLI unavailable")
    assert run.returncode == 0, run.stderr
    services = json.loads(run.stdout)["services"]
    service = services["nginx"]
    assert service["volumes"][0]["read_only"]
    assert service["pull_policy"] == "never"
    assert "network_mode" not in service
    assert {(str(p["published"]), p["target"]) for p in service["ports"]} == {("80", 80), ("443", 443)}
    if with_gallery:
        assert "gallery" in service["depends_on"]
        assert "ports" not in services["gallery"]
        assert services["gallery"]["volumes"][0]["target"] == "/data"
    else:
        assert "gallery" not in services


def test_upgrade_delta_compares_against_old_host_root_config():
    from mmw.core.publisher.bundle import _far_nginx

    settings = {"network": {"name": "oak", "nginx": True}, "mirrors": [],
                "upstream_hosts": ["example.test"]}
    old = _far_nginx(json.dumps(settings), False, previous=True)
    new = _far_nginx(json.dumps(settings | {"nginx_container": True}), False)
    assert ROOT_PLACEHOLDER.encode() in old["nginx/mirror.conf"]
    assert b"root /srv/mmw/example.test;" in new["nginx/mirror.conf"]
    assert old["nginx/mirror.conf"] != new["nginx/mirror.conf"]
    assert "nginx/compose.yaml" not in old and "nginx/compose.yaml" in new
