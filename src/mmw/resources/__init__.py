"""Files shipped verbatim to somewhere mmw is not installed."""
from pathlib import Path

_APPLY = Path(__file__).parent / "apply.py"
_SIGN = Path(__file__).parent / "sign.py"
_CA = Path(__file__).parent / "make-ca.py"
_GALLERY = Path(__file__).with_name("gallery.py")
_PYPI_RENDER = Path(__file__).with_name("pypi_render.py")


def apply_script_bytes() -> bytes:
    """The far-side apply script, read from disk each call (it is small)."""
    return _APPLY.read_bytes()


def sign_script_bytes() -> bytes:
    """The far-side signing script — signs mirror certificate requests with a
    CA that never leaves the airgapped network."""
    return _SIGN.read_bytes()


def ca_script_bytes() -> bytes:
    """The far-side CA maker — the CA sign.py signs with, made once."""
    return _CA.read_bytes()


def pypi_render_bytes() -> bytes:
    """The PyPI page renderer, shipped as `pypi-data/render.py` (stdlib only)."""
    return _PYPI_RENDER.read_bytes()


def gallery_script_bytes() -> bytes:
    """The far-side VS Code gallery (run by the stock python image)."""
    return _GALLERY.read_bytes()



INSTALL = Path(__file__).with_name("install.sh")
"""mmw's installer script (POSIX sh), the top of every
`mmw-<version>-<arch>.tar`."""
