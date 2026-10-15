"""Server cooldowns are shared by all HTTP paths, including streams."""
import threading
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate

import httpx
import pytest

from mmw.core import http
from mmw.core.config import MirrorConfig
from mmw.providers.apt import AptProvider


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = 0.0

        def sleep(self, seconds):
            self.now += seconds

    clock = Clock()
    monkeypatch.setattr(http.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(http.time, "time", lambda: 1_700_000_000 + clock.now)
    monkeypatch.setattr(http.time, "sleep", clock.sleep)
    monkeypatch.setattr(http.random, "uniform", lambda a, b: 1.0)
    monkeypatch.setattr(http, "COOLDOWNS", http.HostCooldowns())
    return clock


@pytest.mark.parametrize("value,expected", [
    ("600", 600), ("0", 0), ("garbage", None), ("-5", None), ("inf", None),
    (formatdate(1_700_000_120, usegmt=True), 120),
    (formatdate(1_699_999_900, usegmt=True), 0),
])
def test_retry_after_seconds_dates_and_invalid_values(clock, value, expected):
    assert http.retry_after(httpx.Headers({"Retry-After": value})) == expected


def test_initial_release_and_verified_index_retry_without_losing_snapshot(clock):
    import hashlib

    index = b""  # a valid empty Packages index
    checksum = hashlib.sha256(index).hexdigest()
    release = f"Suite: stable\nSHA256:\n {checksum} 0 main/binary-amd64/Packages\n".encode()
    calls = []
    counts = {}

    def respond(request):
        path = request.url.path
        calls.append((path, clock.now))
        counts[path] = counts.get(path, 0) + 1
        if path.endswith("/InRelease"):
            return httpx.Response(503) if counts[path] == 1 else httpx.Response(200, content=release)
        if "/by-hash/" in path:
            if counts[path] == 1:
                return httpx.Response(429, headers={"Retry-After": "120"})
            return httpx.Response(200, content=index)
        return httpx.Response(404)

    unit = MirrorConfig(name="m", provider="apt", source_url="https://repo.test", suite="stable",
                        component="main", arch="amd64")
    with http.retrying_client(transport=httpx.MockTransport(respond), trust_env=False) as client:
        packages, metadata = AptProvider().fetch_all(unit, client)
    assert packages == []
    assert calls[:2] == [("/dists/stable/InRelease", 0), ("/dists/stable/InRelease", 1)]
    assert clock.now == 121
    assert next(m for m in metadata if m.expected_sha256 == checksum).content == index


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_exhaustion_is_bounded_and_final_cooldown_survives_other_clients(clock, status):
    calls = []

    def failing(request):
        calls.append(clock.now)
        return httpx.Response(status, headers={"Retry-After": "10"})

    with http.retrying_client(transport=httpx.MockTransport(failing), trust_env=False) as first:
        assert first.get("https://repo.test/Release").status_code == status
    assert calls == [0, 10, 20, 30]
    with http.retrying_client(transport=httpx.MockTransport(lambda r: httpx.Response(200)),
                             trust_env=False) as second:
        second.get("https://other.test/file")
        assert clock.now == 30  # unrelated hosts continue
        with second.stream("GET", "https://repo.test/package.deb") as response:
            assert response.status_code == 200
        assert clock.now == 40  # package streams also respect another client's pause


@pytest.mark.parametrize("status", [200, 403, 404, 410])
def test_permanent_answers_are_not_retried(clock, status):
    calls = []

    def respond(request):
        calls.append(request.url)
        return httpx.Response(status)

    with http.retrying_client(transport=httpx.MockTransport(respond), trust_env=False) as client:
        assert client.get("https://repo.test/file").status_code == status
    assert len(calls) == 1
    assert clock.now == 0


def test_transport_failure_retries_and_closes_error_response_before_wait(clock):
    calls = []
    closed = []

    class ErrorStream(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError("error bodies should not be consumed")

        def close(self):
            closed.append(clock.now)

    def respond(request):
        calls.append(clock.now)
        if len(calls) == 1:
            raise httpx.ConnectError("temporary", request=request)
        if len(calls) == 2:
            return httpx.Response(503, stream=ErrorStream())
        return httpx.Response(200, content=b"package")

    with http.retrying_client(transport=httpx.MockTransport(respond), trust_env=False) as client:
        with client.stream("GET", "https://repo.test/a.deb") as response:
            assert response.read() == b"package"
    assert calls == [0, 1, 3]
    assert closed == [1]


def test_redirects_respect_retry_after(clock):
    def respond(request):
        if request.url.path == "/old":
            return httpx.Response(302, headers={"Location": "/new", "Retry-After": "600"})
        return httpx.Response(200)

    with http.retrying_client(transport=httpx.MockTransport(respond), trust_env=False,
                             follow_redirects=True) as client:
        assert client.get("https://repo.test/old").status_code == 200
    assert clock.now == 600


def test_no_header_rate_limit_uses_conservative_default(clock):
    responses = iter([httpx.Response(429), httpx.Response(200)])
    with http.retrying_client(transport=httpx.MockTransport(lambda r: next(responses)), trust_env=False) as client:
        assert client.get("https://repo.test/file").status_code == 200
    assert clock.now == http.RATE_LIMIT_WAIT


def test_retry_after_on_permanent_answer_still_pauses_next_request(clock):
    responses = iter([httpx.Response(403, headers={"Retry-After": "600"}), httpx.Response(200)])
    with http.retrying_client(transport=httpx.MockTransport(lambda r: next(responses)), trust_env=False) as client:
        assert client.get("https://repo.test/private").status_code == 403
        assert clock.now == 0  # don't retry a permanent refusal
        assert client.get("https://repo.test/public").status_code == 200
    assert clock.now == 600


def test_stop_interrupts_wait_and_does_not_clear_shared_cooldown(clock):
    stop = threading.Event()
    stop.set()
    http.COOLDOWNS.defer("repo.test", 600)
    with http.request_scope(stop), pytest.raises(InterruptedError):
        http.COOLDOWNS.wait("repo.test")
    assert clock.now == 0
    http.COOLDOWNS.wait("repo.test")
    assert clock.now == 600


def test_later_shorter_delay_cannot_shorten_existing_cooldown(clock):
    http.COOLDOWNS.defer("repo.test", 600)
    http.COOLDOWNS.defer("repo.test", 1)
    http.COOLDOWNS.wait("repo.test")
    assert clock.now == 600


def test_worker_client_waits_for_another_clients_limit_and_stop_interrupts_it(monkeypatch):
    monkeypatch.setattr(http, "COOLDOWNS", http.HostCooldowns())
    monkeypatch.setattr(http, "ATTEMPTS", 1)
    waiting = threading.Event()
    requests = []

    class Stop(threading.Event):
        def wait(self, timeout=None):
            waiting.set()
            return super().wait(timeout)

    stop = Stop()

    def worker():
        def respond(request):
            requests.append(request.url)
            return httpx.Response(200)

        with http.request_scope(stop), http.retrying_client(
            transport=httpx.MockTransport(respond), trust_env=False,
        ) as client:
            client.get("https://repo.test/Release")

    with http.retrying_client(transport=httpx.MockTransport(
        lambda r: httpx.Response(429, headers={"Retry-After": "3600"}),
    ), trust_env=False) as first:
        assert first.get("https://repo.test/package.deb").status_code == 429

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(worker)
        try:
            assert waiting.wait(2), "worker did not enter the shared cooldown"
            assert requests == []
            # An unrelated host remains usable while the worker is paused.
            with http.retrying_client(transport=httpx.MockTransport(lambda r: httpx.Response(200)),
                                     trust_env=False) as client:
                assert client.get("https://other.test/Release").status_code == 200
        finally:
            stop.set()
        with pytest.raises(InterruptedError):
            future.result(timeout=2)
