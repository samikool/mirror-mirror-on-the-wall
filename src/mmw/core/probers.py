"""Dispatching a pasted URL to whichever provider recognizes it.

A "Probe URL" paste is format-agnostic — mmw has to figure out *what* is at
the URL before it can figure out how to mirror it. That is different from the
config-paste box: a `.list`/`.sources` file's own syntax already says apt, so
there is nothing to dispatch over there.

Probers come from the provider registry: every provider defining `probe(url)`
takes part, in registry order — the seam a second provider plugs into without
`custom_repo.py` needing to change how it asks "what is this URL". Deliberately not generalized further than that: a
`Prober` returns a `DetectResult`, which today only apt populates
meaningfully, and outcomes that are inherently apt-specific (browsable
`candidates`, the `$(ARCH)` substitution — see `apt_sources.py` and
`discover.probe_arch_placeholder`) stay apt-specific rather than being forced
into a shape that fits a hypothetical second provider no one has built yet.
"""
from collections.abc import Callable

from mmw.core.discover import DetectResult

Prober = Callable[[str], DetectResult]


from mmw.providers import all_providers  # noqa: E402  (after the Prober alias)

# Ordered: the first prober to recognize the URL wins. Every provider that
# can recognise a URL (`probe`) takes part; ecosystem providers have none.
PROBERS: list[Prober] = [p.probe for p in all_providers() if callable(getattr(p, "probe", None))]


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
