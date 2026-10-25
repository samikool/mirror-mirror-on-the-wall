"""mmw's installer: `mmw-<version>-<arch>.tar`, the runtime on its own.

One tar per architecture: `mmw-<version>/install.sh` and
`mmw-<version>/<arch>/` (uv, the standalone Python, the wheels,
`RUNTIME.json`) — what `runtime.build_runtime` makes, packed. It never
rides inside a bundle: installing mmw needs no mirror data.
"""
import io
import tarfile
from pathlib import Path

from mmw.core import disk_io
from mmw.core import runtime as R


def installer_name(version: str, arch: str) -> str:
    return f"mmw-{version}-{arch}.tar"


def _version(wheel: Path | None) -> str:
    if wheel is None:
        return R.mmw_version()
    from packaging.utils import parse_wheel_filename
    return str(parse_wheel_filename(wheel.name)[1])


def write_installers(out_dir: Path, *, arches: list[str], python: str, cache: Path, fetcher,
                     wheel: Path | None = None, progress=None, stop_event=None) -> list[Path]:
    """Write one installer per architecture into `out_dir`; their paths.
    Each is written to `.tmp` and renamed, so a failure leaves none."""
    from mmw.resources import INSTALL
    version = _version(wheel)
    top = f"mmw-{version}"
    disk_io.ops.mkdir(out_dir, parents=True, exist_ok=True)
    out = []
    for arch in arches:
        d = R.build_runtime(cache, arch=arch, python=python, mmw_version=version, fetcher=fetcher,
                            wheel=wheel, progress=progress, stop_event=stop_event)
        dest = Path(out_dir) / installer_name(version, arch)
        tmp = dest.with_name(dest.name + ".tmp")
        try:
            with disk_io.ops.open_file(tmp, "wb") as fh, tarfile.open(fileobj=fh, mode="w|") as tar:
                script = disk_io.ops.read_bytes(INSTALL)
                info = tarfile.TarInfo(f"{top}/install.sh")
                info.size, info.mode = len(script), 0o755
                tar.addfile(info, io.BytesIO(script))
                for p in sorted(disk_io.ops.rglob(d, "*")):
                    if not disk_io.ops.is_file(p):
                        continue
                    info = tarfile.TarInfo(f"{top}/{arch}/{p.relative_to(d).as_posix()}")
                    info.size = disk_io.ops.stat(p).st_size
                    info.mode = 0o755 if p.name == "uv" else 0o644
                    with disk_io.ops.open_file(p, "rb") as src:
                        tar.addfile(info, src)
            disk_io.ops.replace(tmp, dest)
        except BaseException:
            disk_io.ops.unlink(tmp, missing_ok=True)
            raise
        out.append(dest)
    return out
