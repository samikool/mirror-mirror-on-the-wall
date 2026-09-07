"""Files shipped verbatim to somewhere mmw is not installed."""
from pathlib import Path

_APPLY = Path(__file__).parent / "apply.py"


def apply_script_bytes() -> bytes:
    """The far-side apply script, read from disk each call (it is small)."""
    return _APPLY.read_bytes()
