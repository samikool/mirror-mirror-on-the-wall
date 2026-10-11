"""A bundle's toast (`TransferView.summary` / `.severity`, shown by the Publish
section) must branch on the outcome
exactly like `_execute_publish` does — a stopped or partially-failed bundle
reported as unqualified success is exactly what this project's "nothing is
silent" outcome discipline forbids. `BundleResult.errors` is populated
directly (dist-upgrade / key failures, per its own dataclass docstring), and
`stop_event` can be set even though `mark_done` still receives a (partial)
result — same as the publish path.

`bundle_network` is monkeypatched here rather than run for real: these tests
are only about the notification text/severity `_execute_bundle` chooses for a
given `BundleResult` + stop state, not about the bundling pipeline itself
(covered by `tests/test_bundle.py`).
"""
import asyncio
from pathlib import Path


from mmw.core import config
from mmw.core.publisher.results import BundleResult
from mmw.tui.app import MmwApp
from mmw.tui.publish_flows import PublishFlows
from mmw.tui.transfer import BUNDLE, TransferView


def _cfg_with_bundle_network(tmp_path: Path) -> config.Config:
    return config.Config(
        settings=config.Settings(mirror_dir=tmp_path / "mirror"),
        networks=[config.NetworkConfig(name="lab", bundle_dir=tmp_path / "bundles")],
    )


class _RecordingApp(MmwApp):
    """The Networks section, recording every `notify()` call instead of
    rendering a toast — the thing under test is what `_execute_bundle`
    chooses to say, not Textual's notification widget."""

    def __init__(self, cfg: config.Config, cfg_dir: Path):
        config.save(cfg, cfg_dir)
        super().__init__(cfg, cfg_dir, initial_section="networks")
        self.notifications: list[tuple[str, str]] = []

    def notify(self, message, *, title="", severity="information", timeout=None, markup=True) -> None:
        self.notifications.append((message, severity))


def _run_bundle_scenario(tmp_path: Path, fake_bundle_network, monkeypatch):
    """Run `_execute_bundle` into a mounted `TransferView` against a faked
    `bundle_network`, and return the notifications the run produced."""
    monkeypatch.setattr(
        "mmw.core.publisher.bundle.bundle_network", fake_bundle_network,
    )

    cfg = _cfg_with_bundle_network(tmp_path)

    async def scenario():
        app = _RecordingApp(cfg, tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            flows = PublishFlows(app, lambda: app.cfg)
            net = cfg.networks[0]
            view = TransferView(BUNDLE, net.name)
            await app.screen.mount(view)
            worker = app.run_worker(flows._execute_bundle(net, view), exit_on_error=False)
            await worker.wait()
            await pilot.pause()
            # The Publish section toasts exactly this pair once `Completed`
            # lands (tests/test_tui_publish_section.py covers that hop).
            return [(view.summary(), view.severity)]

    return asyncio.run(scenario())


def test_execute_bundle_reports_success(tmp_path, monkeypatch):
    out_path = tmp_path / "bundles" / "lab-x.tar"

    def fake(network, cfg, mirror_dir, out_path_arg=None, full=False, progress=None, stop_event=None, mirror_start_callback=None, **_ends):
        return BundleResult(network_name=network.name, out_path=out_path, sent=5, deleted=1)

    notifications = _run_bundle_scenario(tmp_path, fake, monkeypatch)

    assert len(notifications) == 1
    message, severity = notifications[0]
    assert severity == "information"
    assert "Bundled lab" in message
    assert "5 sent" in message
    assert str(out_path) in message


def test_execute_bundle_reports_stopped_not_success(tmp_path, monkeypatch):
    """A user-stopped bundle must never read as unqualified success."""
    out_path = tmp_path / "bundles" / "lab-x.tar"

    def fake(network, cfg, mirror_dir, out_path_arg=None, full=False, progress=None, stop_event=None, mirror_start_callback=None, **_ends):
        stop_event.set()
        return BundleResult(
            network_name=network.name, out_path=out_path, sent=2,
            errors=["stopped before the bundle was written"],
        )

    notifications = _run_bundle_scenario(tmp_path, fake, monkeypatch)

    assert len(notifications) == 1
    message, severity = notifications[0]
    assert severity == "warning"
    assert "stopped" in message.lower()
    assert "2 sent" in message
    assert "Bundled" not in message  # must not be phrased as a clean success


def test_execute_bundle_reports_errors_not_success(tmp_path, monkeypatch):
    """A partially-failed bundle (e.g. a dist-upgrade failure) must not read
    as unqualified success even though the run was not stopped."""
    out_path = tmp_path / "bundles" / "lab-x.tar"

    def fake(network, cfg, mirror_dir, out_path_arg=None, full=False, progress=None, stop_event=None, mirror_start_callback=None, **_ends):
        return BundleResult(
            network_name=network.name, out_path=out_path, sent=3,
            errors=["could not write UPGRADE.md: disk full"],
        )

    notifications = _run_bundle_scenario(tmp_path, fake, monkeypatch)

    assert len(notifications) == 1
    message, severity = notifications[0]
    assert severity == "warning"
    assert "1 error" in message
    assert "3 sent" in message
    assert "Bundled" not in message  # must not be phrased as a clean success
