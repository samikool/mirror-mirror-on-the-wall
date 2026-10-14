"""Getting python:3.13-slim (amd64) into a tar, once per image id."""
import stat


from mmw.providers.vscode import image


def _fake_docker(tmp_path, image_id="sha256:abc", pull_ok=True, platform="linux/amd64"):
    log = tmp_path / "docker.log"
    script = tmp_path / "docker"
    script.write_text(f"""#!/bin/sh
echo "$@" >> {log}
case "$1" in
  pull) {"exit 0" if pull_ok else "echo offline >&2; exit 1"} ;;
  image) echo "{image_id} {platform}" ;;
  save) out=""; while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done; echo "image-bytes" > "$out" ;;
esac
""")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script), log


def test_pull_inspect_save_once(tmp_path, monkeypatch):
    docker, log = _fake_docker(tmp_path)
    monkeypatch.setattr(image, "DOCKER", docker)
    cache = tmp_path / "cache"
    first = image.gallery_image(cache)
    assert first.path.exists() and not first.error and len(first.checksum) == 64
    again = image.gallery_image(cache)
    assert again.path == first.path
    saves = [line for line in log.read_text().splitlines() if line.startswith("save ")]
    assert len(saves) == 1                                    # cached by image id
    assert "--platform linux/amd64" in log.read_text()


def test_a_new_image_id_replaces_the_cached_tar(tmp_path, monkeypatch):
    docker, _ = _fake_docker(tmp_path, "sha256:one")
    monkeypatch.setattr(image, "DOCKER", docker)
    old = image.gallery_image(tmp_path / "cache").path
    docker, _ = _fake_docker(tmp_path, "sha256:two")
    monkeypatch.setattr(image, "DOCKER", docker)
    new = image.gallery_image(tmp_path / "cache").path
    assert new != old and not old.exists()


def test_offline_uses_the_cache_with_a_note(tmp_path, monkeypatch):
    docker, _ = _fake_docker(tmp_path)
    monkeypatch.setattr(image, "DOCKER", docker)
    cached = image.gallery_image(tmp_path / "cache").path
    docker, _ = _fake_docker(tmp_path, pull_ok=False)
    monkeypatch.setattr(image, "DOCKER", docker)
    r = image.gallery_image(tmp_path / "cache")
    assert r.path == cached and any("pull" in n for n in r.notes)


def test_no_docker_and_no_cache_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(image, "DOCKER", str(tmp_path / "no-such-docker"))
    r = image.gallery_image(tmp_path / "cache")
    assert r.path is None and "docker" in r.error.lower()


def test_a_local_image_of_another_architecture_is_never_shipped_as_amd64(tmp_path, monkeypatch):
    """`pull --platform` failed on an arm64 builder: the local tag is arm64 and
    saving it under `…-linux-amd64.tar` would ship an image the far side can't run."""
    docker, log = _fake_docker(tmp_path, pull_ok=False, platform="linux/arm64")
    monkeypatch.setattr(image, "DOCKER", docker)
    r = image.gallery_image(tmp_path / "cache")
    assert r.path is None and "linux/arm64" in r.error and "linux/amd64" in r.error
    assert not [line for line in log.read_text().splitlines() if line.startswith("save ")]


def test_another_architecture_falls_back_to_the_cached_tar(tmp_path, monkeypatch):
    docker, _ = _fake_docker(tmp_path)
    monkeypatch.setattr(image, "DOCKER", docker)
    cached = image.gallery_image(tmp_path / "cache").path
    docker, _ = _fake_docker(tmp_path, image_id="sha256:other", pull_ok=False, platform="linux/arm64")
    monkeypatch.setattr(image, "DOCKER", docker)
    r = image.gallery_image(tmp_path / "cache")
    assert r.path == cached and any("linux/arm64" in n for n in r.notes)
