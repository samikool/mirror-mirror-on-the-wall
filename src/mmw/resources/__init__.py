"""Files shipped verbatim to somewhere mmw is not installed."""
from pathlib import Path

_APPLY = Path(__file__).parent / "apply.py"
_SIGN = Path(__file__).parent / "sign.py"


def apply_script_bytes() -> bytes:
    """The far-side apply script, read from disk each call (it is small)."""
    return _APPLY.read_bytes()


def sign_script_bytes() -> bytes:
    """The far-side signing script — signs mirror certificate requests with a
    CA that never leaves the airgapped network."""
    return _SIGN.read_bytes()
