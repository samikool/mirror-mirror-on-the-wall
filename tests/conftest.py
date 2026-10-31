"""Suite-wide policy.

The suite had no conftest at all, so there was no single place to state things
that are true of every test — which is why the download backoff below was able
to add three real seconds to every failure-path test the moment it was
introduced.
"""
import os
import socket
import tempfile
import threading

import pytest
from pathlib import Path

from mmw.core import downloader
from mmw.tui import custom_repo
# `up` — recorded PyPI served on 127.0.0.1 with real uv, offline — for every
# PyPI test module, without each importing (and shadowing) the fixture.
from test_pypi_req_e2e import up  # noqa: F401


@pytest.fixture(autouse=True)
def _isolate_cli_config(monkeypatch):
    """CLI invocations in different tests represent different processes.

    An explicit --config updates module state. Leaking it into a later test
    can publish the wrong fixture or make a sync contact a real upstream.
    """
    from mmw import cli
    monkeypatch.setattr(cli, '_CFG_DIR', Path('.'))


@pytest.fixture(autouse=True)
def _no_download_backoff(monkeypatch):
    """Neutralise the package-download retry backoff for the whole suite.

    Production waits 1s then 2s between the three attempts, because firing all
    three inside the same overloaded second is the moment least likely to
    succeed. In tests that delay is pure wall clock: a single
    `test_failed_download_still_wanted` went from milliseconds to 3.10s, and
    across every failure-path test it dominated the run.

    The backoff *behaviour* is still covered — `test_sync_safety.py` asserts
    that the delay grows and that a stop event interrupts it, setting its own
    values rather than depending on the production default.
    """
    monkeypatch.setattr(downloader, "_PKG_BACKOFF", 0.0, raising=True)
    monkeypatch.setattr(downloader, "_STOP_POLL", 0.0, raising=True)


@pytest.fixture(autouse=True)
def _no_http_backoff(monkeypatch):
    """The HTTP layer's own retries, neutralised — and its state per test.

    `mmw.core.http` replays a failed GET four times, 1s → 2s → 4s apart, and
    defers the host in a process-wide cooldown table. In tests that was ~8s
    per request to a fake host (`test_failed_download_still_wanted` took 27s),
    and because the table and the downloader's per-thread clients outlive a
    test, one test's cooldown stalled whichever test reached that host next on
    the same xdist worker — random multi-second stalls, by test order.

    `test_http_retries.py` sets its own values to test the policy itself.
    """
    from mmw.core import http
    monkeypatch.setattr(http, "BACKOFF", 0.0, raising=True)
    monkeypatch.setattr(http, "RATE_LIMIT_WAIT", 0.0, raising=True)
    monkeypatch.setattr(http, "COOLDOWNS", http.HostCooldowns(), raising=True)
    monkeypatch.setattr(downloader, "_thread_local", threading.local(), raising=True)


_LOOPBACK = ("localhost", "127.", "::1", "0.0.0.0")


@pytest.fixture(autouse=True)
def _no_internet(request, monkeypatch):
    """Ordinary tests never resolve a real host name. A test that "fails to
    reach" `http://x/` or `example.com` gets an instant, deterministic
    resolution error instead of whatever this machine's DNS does (a timeout
    of seconds, or a real answer). Opt-in network tests (`network` marker or
    an `MMW_*_E2E=1` run) keep real resolution."""
    if request.node.get_closest_marker("network") or any(
            k.startswith("MMW_") and k.endswith("_E2E") and v == "1"
            for k, v in os.environ.items()):
        return
    real = socket.getaddrinfo

    def guarded(host, *a, **kw):
        name = host.decode() if isinstance(host, bytes) else (host or "")
        if name and not name.startswith(_LOOPBACK) and not name.endswith(".localhost"):
            raise socket.gaierror(socket.EAI_NONAME, f"tests resolve no real hosts ({name})")
        return real(host, *a, **kw)
    monkeypatch.setattr(socket, "getaddrinfo", guarded)


@pytest.fixture(autouse=True)
def _serial_disk_io(monkeypatch):
    """Every disk lane runs inline at 1 / 1 / 0 — today's serial behaviour —
    so outcome tests stay deterministic. A test of concurrency sets its own
    with the `disk_limits` fixture; loading a config cannot unpin this
    (`disk_io.configure` is a no-op while pinned)."""
    from mmw.core import disk_io
    monkeypatch.setattr(disk_io, "_limits", disk_io.Limits(1, 1, 0))
    monkeypatch.setattr(disk_io, "_pinned", True)


@pytest.fixture
def disk_limits(monkeypatch):
    """`disk_limits(write_threads=8, …)`: the lanes every job made from now on
    in this test get — how a concurrency test leaves the 1 / 1 / 0 pin."""
    from mmw.core import disk_io

    def set_limits(**kw):
        monkeypatch.setattr(disk_io, "_limits", disk_io.Limits(**kw))
    return set_limits


@pytest.fixture(autouse=True)
def _no_paste_debounce(monkeypatch):
    """Collapse the paste-box debounce for the whole suite.

    The box waits half a second after the last keystroke before probing, so a
    typed config costs one round of network work instead of one per character.
    In tests that is dead wall clock on every wizard test that pastes anything.
    Kept small but non-zero: the timer still goes through the event loop, so
    the coalescing and the ordering it guarantees are unchanged — only the
    wait goes. (An interval of exactly 0 does not schedule reliably and broke
    fourteen wizard tests.)

    `test_wizard_repository_flow.py` covers the coalescing behaviour itself.
    """
    monkeypatch.setattr(custom_repo, "_PASTE_DEBOUNCE", 0.01, raising=True)


@pytest.fixture(autouse=True)
def _no_real_docker(monkeypatch):
    """Ordinary tests never run the real Docker CLI (it pulls from the network and
    writes to the host's image store). Publishing nginx or a VS Code network
    without a fake reports "no Docker" instead.
    The explicit MMW_VSCODE_E2E opt-in restores Docker in its own fixture and
    permits only cached-image inspect/save/load; its test network is internal.
    """
    from mmw.providers.vscode import image
    from mmw.core.publisher import nginx_container
    monkeypatch.setattr(image, "DOCKER", "mmw-test-has-no-docker", raising=True)
    monkeypatch.setattr(nginx_container, "DOCKER", "mmw-test-has-no-docker", raising=True)
    # …and for mmw run as a subprocess (`mmw bundle --out -`), which these
    # patches never reach: it used the host's real Docker and put a 167 MB
    # nginx image in the test's pool twice.
    monkeypatch.setenv("MMW_DOCKER", "mmw-test-has-no-docker")


@pytest.fixture(autouse=True)
def _no_runtime_downloads(monkeypatch):
    """mmw's runtime rides mmw networks' bundles; building it downloads uv,
    an interpreter and wheels. Every test builds from a fake instead —
    `test_runtime_e2e.py` is the one that fetches for real."""
    from mmw.core import runtime
    from test_runtime import FakeFetcher
    monkeypatch.setattr(runtime, "default_fetcher", FakeFetcher)


@pytest.fixture(autouse=True)
def _temp_files_stay_in_the_test(tmp_path_factory, monkeypatch):
    """`tempfile` (mkdtemp, TemporaryDirectory, …) writes under this test's
    pytest temp tree, which pytest prunes — never loose in /tmp, where a
    `mkdtemp()` nobody removes (the layout sweep made 16 per run) piled up
    run after run. In-process only: subprocesses keep the real TMPDIR, since
    a deep pytest path can overflow the Unix socket path limit (gpg, ssh)."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path_factory.mktemp("tmp")))


@pytest.fixture(autouse=True)
def _isolated_mmw_state(tmp_path_factory, monkeypatch):
    """This machine's mmw state (recent roots, the CA's location) per test —
    never the real ~/.local/state/mmw."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path_factory.mktemp("state")))
    # Never a real PyPI check from a test; the update tests turn it back on.
    monkeypatch.setenv("MMW_NO_UPDATE_CHECK", "1")


@pytest.fixture(autouse=True)
def _stop_gpg_agents(tmp_path):
    """gpg starts an agent per keyring directory and leaves it running; a
    test's keyrings live under its tmp_path, so stop their agents after it."""
    yield
    import subprocess
    for home in {p.parent for p in tmp_path.glob("**/pubring.kbx")}:
        try:
            subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "all"], capture_output=True)
        except FileNotFoundError:          # no gpgconf: no agent was started either
            return
