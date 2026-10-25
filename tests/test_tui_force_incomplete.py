"""Incomplete content requires an explicit TUI action, for both deployments."""
import asyncio
import subprocess
import sys

import pytest

from mmw.core.pool import Pool
from mmw.core import verify
from mmw.tui.app import MmwApp
from mmw.tui.publish_flows import BundleChoice, PublishChoice
from mmw.tui.ui.dialog import ConfirmDialog
from mmw.tui.ui.widgets import Button
from test_bundle_verify_gate import _cfg_with_incomplete_mirror
from waiting import settle, until


@pytest.mark.parametrize("verb", ["publish", "bundle"])
def test_anyway_button_continues_after_refusal_with_warning(tmp_path, monkeypatch, verb):
    cfg = _cfg_with_incomplete_mirror(tmp_path)
    # The older gate fixture stores a legacy name and an unnormalized URI.
    # Record a current sync unit so this also exercises real file deployment.
    unit = cfg.mirrors[0]
    with Pool(cfg.settings.mirror_dir) as pool:
        entries = pool._conn.execute(
            "SELECT checksum, repo_path, kind FROM refs WHERE mirror_name = 'm'"
        ).fetchall()
        pool.reconcile_refs(unit.name, entries, unit.source)
        pool.record_mirror(unit)
    net = cfg.networks[0].model_copy(update={"root": tmp_path / "pub"})
    cfg = cfg.model_copy(update={"networks": [net]})
    calls = []
    actual_verify = verify.verify_mirror

    def counted(*args, **kwargs):
        calls.append(args[0].name)
        return actual_verify(*args, **kwargs)

    monkeypatch.setattr(verify, "verify_mirror", counted)
    alternate = tmp_path / "alternate"
    choice = PublishChoice(root=alternate) if verb == "publish" else BundleChoice(from_last=False)

    async def scenario():
        app = MmwApp(cfg, tmp_path, initial_section="publish")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await app.start_transfer(verb, net, choice=choice)
            first = app.screen.view
            await until(pilot, lambda: isinstance(app.screen, ConfirmDialog)
                        and bool(app.screen.query("#confirm")))  # mounted, not just pushed
            assert first.outcome == "note" and first.incomplete
            assert not alternate.exists()
            assert not list(net.bundle_dir.glob("*.tar*"))
            dialog = app.screen
            listed = str(dialog.query_one("#confirm-details Label").render())
            assert f"✗ {unit.name}:" in listed
            confirm = dialog.query_one("#confirm", Button)
            assert str(confirm.label) == ("Publish anyway" if verb == "publish" else "Transfer anyway")
            confirm.press()
            await until(pilot, lambda: app.active_transfer is not None
                        and app.active_transfer is not first)
            second = app.active_transfer
            await until(pilot, lambda: second._done_displayed)
            assert not isinstance(app.screen, ConfirmDialog)
            assert second.outcome == "ok", second.summary()
            assert second.severity == "warning"
            assert "incomplete content allowed" in second.summary()
            assert app.screen._last_request[2].allow_incomplete
            if verb == "publish":
                assert app.screen._last_request[2].root == alternate
            else:
                assert app.screen._last_request[2].from_last is False
            return second._result

    deployed = asyncio.run(scenario())
    assert calls == [unit.name]  # force does not repeat the expensive scan
    if verb == "publish":
        assert (alternate / "example.com/repo/pool/a.deb").read_bytes() == b"present"
        assert not (alternate / "example.com/repo/pool/b.deb").exists()
        assert not net.root.exists()
    else:
        assert deployed.out_path.is_file()
        applied = tmp_path / "applied"
        result = subprocess.run(
            [sys.executable, str(net.bundle_dir / "apply.py"), str(deployed.out_path),
             "--root", str(applied), "--verify"], capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        assert (applied / "example.com/repo/pool/a.deb").read_bytes() == b"present"
        assert not (applied / "example.com/repo/pool/b.deb").exists()
        from mmw.core.publisher import publish_network
        from test_deployment_parity import _files
        assert not publish_network(net, cfg, cfg.settings.mirror_dir).errors
        assert _files(applied) == _files(net.root)
    with Pool(cfg.settings.mirror_dir, read_only=True) as pool:
        assert len(pool.snapshots(net.name)) == (1 if verb == "bundle" else 0)


def test_cancel_in_the_incomplete_dialog_publishes_nothing(tmp_path):
    cfg = _cfg_with_incomplete_mirror(tmp_path)
    net = cfg.networks[0].model_copy(update={"root": tmp_path / "pub"})
    cfg = cfg.model_copy(update={"networks": [net]})

    async def scenario():
        app = MmwApp(cfg, tmp_path, initial_section="publish")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await app.start_transfer("publish", net)
            first = app.screen.view
            await until(pilot, lambda: isinstance(app.screen, ConfirmDialog)
                        and bool(app.screen.query("#confirm")))  # mounted, not just pushed
            await pilot.press("escape")
            await settle(pilot)
            assert not isinstance(app.screen, ConfirmDialog)
            assert app.active_transfer in (None, first)
            assert first.outcome == "note"

    asyncio.run(scenario())
    assert not net.root.exists()
