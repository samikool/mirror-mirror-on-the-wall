"""Public snapshot metadata: access failures are never missing suites."""
import asyncio

import httpx
import pytest
from textual.widgets import Static
from typer.testing import CliRunner

from mmw import cli
from mmw.core import config, http, past_points as pp

from test_past_points_record import lab  # noqa: F401 — the shared fixture
from waiting import shows

BASE = "https://snapshot.ubuntu.com/ubuntu/20261015T235959Z"
RELEASE = b"Origin: Ubuntu\nLabel: Ubuntu\nCodename: jammy\n"


@pytest.fixture
def archive(lab, monkeypatch):  # noqa: F811
    config.save(lab.cfg(), lab.tmp)
    seen = []
    state = {"code": 401}

    def respond(request):
        seen.append(str(request.url))
        code = state["code"] if request.url.host == "snapshot.ubuntu.com" else 200
        return httpx.Response(code, content=RELEASE if code == 200 else b"access denied")

    monkeypatch.setattr(http, "retrying_client", lambda **kw: httpx.Client(
        transport=httpx.MockTransport(respond), **kw))
    return lab, state, seen


@pytest.mark.parametrize("code", [401, 403, 429, 503])
def test_metadata_failure_is_not_absence(archive, code):
    _, state, seen = archive
    state["code"] = code
    with pytest.raises(pp.ArchiveUnavailable, match=f"HTTP {code}"):
        pp.release_fields(pp.fetch_release, BASE, "jammy")
    assert seen == [f"{BASE}/dists/jammy/Release"]


def test_only_404_means_missing_metadata(archive):
    _, state, seen = archive
    state["code"] = 404
    assert pp.release_fields(pp.fetch_release, BASE, "jammy") is None
    assert seen == [f"{BASE}/dists/jammy/Release", f"{BASE}/dists/jammy/InRelease"]


@pytest.mark.parametrize("mode", ["date", "url", "url-repo"])
def test_cli_reports_snapshot_access_failure_before_import(archive, mode):
    lb, _, _ = archive
    args = (["--as-of", "2026-10-15", "--repo", lb.repo] if mode == "date"
            else ["--from-url", BASE])
    if mode == "url-repo":
        args += ["--repo", lb.repo]
    result = CliRunner().invoke(cli.app, ["--config", str(lb.tmp), "sync", "--quiet", *args])
    assert result.exit_code == 1, result.output
    assert "HTTP 401" in result.output and "require no credentials" in result.output
    assert f"{BASE}/dists/jammy/Release" in result.output
    assert "Found:" not in result.output and "Added:" not in result.output


@pytest.mark.parametrize("when", ["2026-10-15", BASE])
def test_tui_keeps_add_disabled_and_can_retry(archive, when):
    from mmw.tui.app import MmwApp
    from mmw.tui.ui.widgets import Button

    lb, state, _ = archive

    async def scenario():
        app = MmwApp(lb.cfg(), lb.tmp, initial_section="repositories",
                     start_past=(when if when != BASE else None, BASE if when == BASE else None, lb.repo))
        async with app.run_test(size=(120, 40)) as pilot:
            found = lambda: str(app.screen.query_one("#past-found", Static).render())  # noqa: E731
            await shows(pilot, found, "HTTP 401", "require no credentials")
            assert app.screen.query_one("#btn-past-run", Button).disabled
            state["code"] = 200
            app.screen.query_one("#btn-past-look", Button).press()
            await shows(pilot, found, "Found:", "snapshot.ubuntu.com/ubuntu")
            assert not app.screen.query_one("#btn-past-run", Button).disabled
            state["code"] = 401
            app.screen.query_one("#btn-past-look", Button).press()
            await shows(pilot, found, "HTTP 401")
            assert app.screen.query_one("#btn-past-run", Button).disabled
            assert app.screen.targets == []

    asyncio.run(scenario())
