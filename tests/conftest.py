"""Suite-wide policy.

The suite had no conftest at all, so there was no single place to state things
that are true of every test — which is why the download backoff below was able
to add three real seconds to every failure-path test the moment it was
introduced.
"""
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
