"""Files shipped verbatim to somewhere mmw is not installed."""
from pathlib import Path

_GALLERY = Path(__file__).with_name("gallery.py")
_PYPI_RENDER = Path(__file__).with_name("pypi_render.py")


def pypi_render_bytes() -> bytes:
    """The PyPI page renderer, shipped as `pypi-data/render.py` (stdlib only)."""
    return _PYPI_RENDER.read_bytes()


def gallery_script_bytes() -> bytes:
    """The far-side VS Code gallery (run by the stock python image)."""
    return _GALLERY.read_bytes()



INSTALL = Path(__file__).with_name("install.sh")
"""mmw's installer script (POSIX sh), the top of every
`mmw-<version>-<arch>.tar`."""
