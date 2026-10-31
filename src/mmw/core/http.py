"""Bounded HTTP retries and process-wide, per-host server cooldowns.

All clients share cooldowns, including redirects and streaming downloads.
Existing provider/download retry loops may retry again after this budget;
even an exhausted request records Retry-After before returning its failure.
"""
import contextlib
import contextvars
import logging
import math
import random
import threading
import time
from email.utils import parsedate_to_datetime

import httpx

log = logging.getLogger(__name__)
ATTEMPTS = 4
BACKOFF = 1.0
RATE_LIMIT_WAIT = 60.0
_stop_event = contextvars.ContextVar("http_stop_event", default=None)


@contextlib.contextmanager
def request_scope(stop_event=None):
    token = _stop_event.set(stop_event)
    try:
        yield
    finally:
        _stop_event.reset(token)


def retry_after(headers) -> float | None:
    """Seconds remaining, with no cap that could shorten a server's delay."""
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        value = value.strip()
        if value.isascii() and value.isdigit():
            delay = float(value)
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return None
            delay = date.timestamp() - time.time()
        return max(0.0, delay) if math.isfinite(delay) else None
    except (ValueError, TypeError, OverflowError):
        return None


class HostCooldowns:
    def __init__(self):
        self._lock = threading.Lock()
        self._until: dict[str, float] = {}

    def defer(self, host: str, seconds: float) -> None:
        with self._lock:
            self._until[host] = max(self._until.get(host, 0), time.monotonic() + seconds)

    def wait(self, host: str) -> None:
        while True:
            stop = _stop_event.get()
            if stop is not None and stop.is_set():
                raise InterruptedError("sync stopped during HTTP cooldown")
            with self._lock:
                remaining = self._until.get(host, 0) - time.monotonic()
                if remaining <= 0:
                    self._until.pop(host, None)
                    return
            # Short slices allow cancellation and recheck extensions made by
            # other workers. Never hold the lock while sleeping.
            if stop is not None:
                stop.wait(min(remaining, 0.25))
            else:
                time.sleep(min(remaining, 0.25))


COOLDOWNS = HostCooldowns()


class RetryTransport(httpx.BaseTransport):
    def __init__(self, inner: httpx.BaseTransport, *, cooldowns=None):
        self.inner = inner
        self._cooldowns = cooldowns

    @property
    def cooldowns(self) -> HostCooldowns:
        """The process-wide table unless one was given — looked up per
        request, so a long-lived client follows a replaced table."""
        return self._cooldowns if self._cooldowns is not None else COOLDOWNS

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        # Only replay safe reads. Gallery POSTs keep their provider's retry
        # policy, but still participate in host cooldowns.
        attempts = ATTEMPTS if request.method in ("GET", "HEAD") else 1
        for attempt in range(attempts):
            self.cooldowns.wait(host)
            delay = BACKOFF * 2 ** attempt * random.uniform(1.0, 1.5)
            try:
                response = self.inner.handle_request(request)
            except httpx.TransportError:
                if attempt == attempts - 1:
                    raise
                self.cooldowns.defer(host, delay)
                continue
            transient = response.status_code in (408, 429) or 500 <= response.status_code < 600
            server_delay = retry_after(response.headers)
            if transient or server_delay is not None:
                if response.status_code == 429 and server_delay is None:
                    delay = max(delay, RATE_LIMIT_WAIT)
                delay = max(delay, server_delay or 0)
                self.cooldowns.defer(host, delay)
                log.warning("HTTP %s from %s; pausing host requests for %.1fs", response.status_code, host, delay)
            if not transient or attempt == attempts - 1:
                return response
            # Release the connection before waiting; no error body is pooled
            # or handed to the package parser.
            response.close()
        raise AssertionError("unreachable")

    def close(self) -> None:
        self.inner.close()


def retrying_client(**kwargs) -> httpx.Client:
    """Wrap every transport, including httpx's environment-proxy mounts.

    Constructing the normal Client first preserves proxies, TLS, timeouts,
    redirects, and test transports. httpx exposes transport selection through
    these internal attributes; keep the dependency isolated here.
    """
    client = httpx.Client(**kwargs)
    # Test doubles that do not implement actual HTTP need no transport policy.
    if hasattr(client, "_transport"):
        client._transport = RetryTransport(client._transport)
        client._mounts = {
            pattern: RetryTransport(transport) if transport is not None else None
            for pattern, transport in client._mounts.items()
        }
    return client
