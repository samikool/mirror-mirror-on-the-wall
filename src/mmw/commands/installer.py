"""Headless installer — `mmw installer --out DIR`: mmw's installer on its
own, one `mmw-<version>-<arch>.tar` per architecture."""
import platform
from pathlib import Path

from mmw.commands.common import eprint, load_config_or_none
from mmw.core import config
from mmw.core import runtime as rt
from mmw.core.installer import write_installers
from mmw.core.runtime import default_fetcher


def run(cfg_dir: Path, out: Path, network: str | None = None, runtime_wheel: Path | None = None) -> int:
    cfg = load_config_or_none(cfg_dir)
    if cfg is None:
        return 1
    arch = {"arm64": "aarch64"}.get(platform.machine(), platform.machine())
    arches, python = [arch], config.DEFAULT_RUNTIME_PYTHON
    if network is not None:
        net = next((n for n in cfg.networks if n.name == network), None)
        if net is None:
            eprint(f"no network {network!r}")
            return 1
        arches, python = sorted(net.runtime_arches), net.runtime_python
    cache = Path(cfg.settings.mirror_dir) / "runtime"
    try:
        paths = write_installers(out, arches=arches, python=python, cache=cache, fetcher=default_fetcher(),
                                 wheel=rt.resolve_wheel(runtime_wheel, cache / "build"),
                                 progress=lambda m: m != "done" and eprint(f"  {m}"))
    except (rt.RuntimeUnavailable, ValueError, OSError) as e:
        eprint(f"mmw installer not written: {e}")
        return 1
    for p in paths:
        print(p)
    return 0
