"""Dispatching a pasted URL to whichever provider recognizes it.

A "Probe URL" paste is format-agnostic — mmw has to figure out *what* is at
the URL before it can figure out how to mirror it. That is different from the
config-paste box: a `.list`/`.sources` file's own syntax already says apt, so
there is nothing to dispatch over there.

Exactly one prober exists today (apt, via `discover.detect_repository`), so
this module is pure ceremony for now — but it is the seam a second provider
(pypi, say) plugs into later without `custom_repo.py` needing to change how it
asks "what is this URL". Deliberately not generalized further than that: a
`Prober` returns a `DetectResult`, which today only apt populates
meaningfully, and outcomes that are inherently apt-specific (browsable
`candidates`, the `$(ARCH)` substitution — see `apt_sources.py` and
`discover.probe_arch_placeholder`) stay apt-specific rather than being forced
into a shape that fits a hypothetical second provider no one has built yet.
"""
from collections.abc import Callable

from mmw.core import discover
from mmw.core.discover import DetectResult

Prober = Callable[[str], DetectResult]


def _probe_apt(url: str) -> DetectResult:
    # A thin wrapper, not `discover.detect_repository` itself: existing tests
    # (and any future caller) monkeypatch `discover.detect_repository` as a
    # module attribute, which only a fresh lookup through `discover` — not a
    # function reference captured once at import time — will pick up.
    return discover.detect_repository(url)


# Ordered: the first prober to recognize the URL wins. One entry today.
PROBERS: list[Prober] = [_probe_apt]


def probe_url(url: str) -> DetectResult:
    """Try each prober in turn; the first recognized result wins.

    When nothing recognizes it, returns the *last* prober's result, so its
    `candidates`/`error` still reach the caller — with one prober (today) this
    is exactly that prober's result, unchanged from calling it directly.
    """
    result = DetectResult(provider=None)
    for prober in PROBERS:
        result = prober(url)
        if result.provider is not None:
            return result
    return result


__all__ = ["Prober", "PROBERS", "probe_url"]
