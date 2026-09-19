"""Dispatching a pasted URL to whichever provider recognizes it.

Exactly one prober exists today (apt); this module is the seam for adding a
second one later without the TUI needing to change how it asks "what is this
URL". These tests exercise the dispatch logic itself, independent of which
probers happen to be registered.
"""
from mmw.core import probers
from mmw.core.discover import DetectResult


def test_probe_url_returns_the_first_recognized_result(monkeypatch):
    recognized = DetectResult(provider="apt", signal="found it")
    monkeypatch.setattr(probers, "PROBERS", [lambda url: recognized])

    assert probers.probe_url("https://x.example/deb") is recognized


def test_probe_url_tries_the_next_prober_when_the_first_does_not_recognize_it(monkeypatch):
    not_apt = DetectResult(provider=None, error="not apt-shaped")
    other = DetectResult(provider="other", signal="found by the second prober")
    monkeypatch.setattr(probers, "PROBERS", [lambda url: not_apt, lambda url: other])

    assert probers.probe_url("https://x.example/deb") is other


def test_probe_url_returns_the_last_unrecognized_result_when_nothing_matches(monkeypatch):
    """With one prober (today's reality) this is just that prober's result,
    unchanged from calling it directly — candidates/error still reach the
    caller exactly as before this dispatcher existed."""
    last = DetectResult(provider=None, error="404", candidates=["ubuntu"])
    monkeypatch.setattr(probers, "PROBERS", [lambda url: DetectResult(provider=None), lambda url: last])

    assert probers.probe_url("https://x.example/deb") is last


def test_apt_is_the_only_registered_prober_today(monkeypatch):
    """PROBERS wraps `discover.detect_repository` rather than capturing it
    directly, so callers that monkeypatch it as a module attribute (the
    existing convention throughout this codebase's tests) are honored."""
    from mmw.core import discover

    assert len(probers.PROBERS) == 1
    sentinel = DetectResult(provider="apt", signal="via the real module lookup")
    monkeypatch.setattr(discover, "detect_repository", lambda url: sentinel)
    assert probers.PROBERS[0]("https://x.example/deb") is sentinel
