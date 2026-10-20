"""Pull and cache a Docker runtime image, exporting once per image id."""
import hashlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from mmw.core import disk_io


@dataclass
class ImageResult:
    path: Path | None
    checksum: str = ""
    notes: list[str] = field(default_factory=list)
    error: str = ""


def _run(docker: str, *args: str, timeout: float = 900) -> str:
    out = subprocess.run([docker, *args], check=True, capture_output=True, text=True,
                         timeout=timeout)
    return out.stdout.strip()


def _checksum(tar: Path) -> str:
    side = tar.with_name(tar.name + ".sha256")
    if disk_io.ops.exists(side):
        return disk_io.ops.read_text(side).strip()
    h = hashlib.sha256()
    with disk_io.ops.open_file(tar, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    disk_io.ops.write_text(side, h.hexdigest())
    return h.hexdigest()


def _newest(cache: Path) -> Path | None:
    tars = sorted(disk_io.ops.glob(cache, "*.tar"), key=lambda p: disk_io.ops.stat(p).st_mtime, reverse=True) \
        if disk_io.ops.is_dir(cache) else []
    return tars[0] if tars else None


def _use_cache(cache: Path, notes: list[str], why: str, error: str, say) -> ImageResult:
    cached = _newest(cache)
    if cached is None:
        return ImageResult(None, error=error)
    say("cached")
    notes.append(f"{why}; shipping the cached {cached.name}")
    return ImageResult(cached, _checksum(cached), notes)


class _Stopped(Exception):
    pass


def container_image(cache: Path, *, image: str, platform: str, docker: str = "docker",
                    label: str = "Container", progress=None, stop_event=None) -> ImageResult:
    def say(phase):
        if stop_event is not None and stop_event.is_set():
            raise _Stopped
        if phase and progress:
            progress(phase)
    try:
        result = _container_image(cache, say, image, platform, docker)
        say("done")
        return result
    except _Stopped:
        return ImageResult(None, error=f"{label} image preparation stopped")


def _container_image(cache: Path, say, image: str, platform: str, docker: str) -> ImageResult:
    notes: list[str] = []
    say("pulling")
    try:
        _run(docker, "pull", "--platform", platform, image)
    except (OSError, subprocess.SubprocessError) as e:
        notes.append(f"docker pull {image} failed ({e}); using what is already here")
    say("inspecting")
    try:
        image_id, _, actual_platform = _run(docker, "image", "inspect", "--format",
                                     "{{.Id}} {{.Os}}/{{.Architecture}}", image,
                                     timeout=60).partition(" ")
    except (OSError, subprocess.SubprocessError) as e:
        return _use_cache(cache, notes, f"Docker unavailable ({e})",
                          f"cannot get {image} ({platform}) — is Docker installed and "
                          f"running here? ({e})", say)
    if actual_platform != platform:
        # The pull failed and the local tag is another architecture's (an
        # arm64 builder): saved as …-linux-amd64.tar it would not run there.
        return _use_cache(cache, notes, f"the local {image} is {actual_platform}, not {platform}",
                          f"the local {image} is {actual_platform}, not {platform} — run "
                          f"`docker pull --platform {platform} {image}` here", say)
    disk_io.ops.mkdir(cache, parents=True, exist_ok=True)
    target = cache / f"{image_id.replace(':', '-')}.tar"
    if not disk_io.ops.exists(target):
        say("saving")
        tmp = target.with_name(target.name + ".part")
        try:
            _run(docker, "save", "-o", str(tmp), image)
            say(None)  # Stop during Docker save must not replace the cached image.
        except _Stopped:
            disk_io.ops.unlink(tmp, missing_ok=True)
            raise
        except (OSError, subprocess.SubprocessError) as e:
            disk_io.ops.unlink(tmp, missing_ok=True)
            return ImageResult(None, error=f"docker save {image} failed: {e}")
        disk_io.ops.rename(tmp, target)
    else:
        say("cached")
    for old in disk_io.ops.glob(cache, "*.tar"):
        if old != target:
            disk_io.ops.unlink(old, missing_ok=True)
            disk_io.ops.unlink(old.with_name(old.name + ".sha256"), missing_ok=True)
    return ImageResult(target, _checksum(target), notes)
