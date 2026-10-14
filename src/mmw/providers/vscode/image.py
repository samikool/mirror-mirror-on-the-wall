"""The image the gallery runs in: stock python:3.13-slim for linux/amd64,
saved to a tar once per image id so bundles carry it only when it changes."""
import hashlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

DOCKER = "docker"
IMAGE = "python:3.13-slim"
PLATFORM = "linux/amd64"
TAR_NAME = "python-3.13-slim-linux-amd64.tar"


@dataclass
class ImageResult:
    path: Path | None
    checksum: str = ""
    notes: list[str] = field(default_factory=list)
    error: str = ""


def _run(*args: str, timeout: float = 900) -> str:
    out = subprocess.run([DOCKER, *args], check=True, capture_output=True, text=True,
                         timeout=timeout)
    return out.stdout.strip()


def _checksum(tar: Path) -> str:
    side = tar.with_name(tar.name + ".sha256")
    if side.exists():
        return side.read_text().strip()
    h = hashlib.sha256()
    with tar.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    side.write_text(h.hexdigest())
    return h.hexdigest()


def _newest(cache: Path) -> Path | None:
    tars = sorted(cache.glob("*.tar"), key=lambda p: p.stat().st_mtime, reverse=True) \
        if cache.is_dir() else []
    return tars[0] if tars else None


def _use_cache(cache: Path, notes: list[str], why: str, error: str) -> ImageResult:
    cached = _newest(cache)
    if cached is None:
        return ImageResult(None, error=error)
    notes.append(f"{why}; shipping the cached {cached.name}")
    return ImageResult(cached, _checksum(cached), notes)


def gallery_image(cache: Path) -> ImageResult:
    notes: list[str] = []
    try:
        _run("pull", "--platform", PLATFORM, IMAGE)
    except (OSError, subprocess.SubprocessError) as e:
        notes.append(f"docker pull {IMAGE} failed ({e}); using what is already here")
    try:
        image_id, _, platform = _run("image", "inspect", "--format",
                                     "{{.Id}} {{.Os}}/{{.Architecture}}", IMAGE,
                                     timeout=60).partition(" ")
    except (OSError, subprocess.SubprocessError) as e:
        return _use_cache(cache, notes, f"Docker unavailable ({e})",
                          f"cannot get {IMAGE} ({PLATFORM}) — is Docker installed and "
                          f"running here? ({e})")
    if platform != PLATFORM:
        # The pull failed and the local tag is another architecture's (an
        # arm64 builder): saved as …-linux-amd64.tar it would not run there.
        return _use_cache(cache, notes, f"the local {IMAGE} is {platform}, not {PLATFORM}",
                          f"the local {IMAGE} is {platform}, not {PLATFORM} — run "
                          f"`docker pull --platform {PLATFORM} {IMAGE}` here")
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"{image_id.replace(':', '-')}.tar"
    if not target.exists():
        tmp = target.with_name(target.name + ".part")
        try:
            _run("save", "-o", str(tmp), IMAGE)
        except (OSError, subprocess.SubprocessError) as e:
            tmp.unlink(missing_ok=True)
            return ImageResult(None, error=f"docker save {IMAGE} failed: {e}")
        tmp.rename(target)
    for old in cache.glob("*.tar"):
        if old != target:
            old.unlink(missing_ok=True)
            old.with_name(old.name + ".sha256").unlink(missing_ok=True)
    return ImageResult(target, _checksum(target), notes)
