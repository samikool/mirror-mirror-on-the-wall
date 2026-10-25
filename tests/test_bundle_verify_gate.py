"""CRITICAL 1 (whole-branch review, 2026-09-08) — `do_bundle` must run the
same completeness gate `do_publish` runs before it will publish.

`pool.mirror_files()` — the query BOTH `publish_group` (publish) and
`bundle_network` (bundle) enumerate from — is `refs ⋈ packages`. A ref the
pool has no blob for is invisible to that join, so it lands in neither a
published tree nor a bundle's tar/manifest. `do_publish` already refuses via
`_run_pre_publish` -> `_stopped_incomplete` when `verify_mirror` finds
`refs - packages`; `do_bundle` called `_execute_bundle` directly with no
verify phase at all, so an incomplete mirror bundled cleanly, `apply.py
--verify` passed on the far side, and the missing package only surfaced as a
404 during `apt install` -- on the far side of an airgap, where nobody can
debug it.
"""
import asyncio
import gzip
import hashlib


from mmw.core import config
from mmw.core.pool import KIND_META, KIND_PKG, Pool
from mmw.tui.app import MmwApp
from waiting import settle, until


def _cfg_with_incomplete_mirror(tmp_path) -> config.Config:
    """One apt-flat mirror whose Packages index names a package the pool
    never received the blob for -- exactly `refs - packages`."""
    mirror_dir = tmp_path / "mirror"
    pool = Pool(mirror_dir)
    src = "https://example.com/repo/"

    have = hashlib.sha256(b"present").hexdigest()
    missing = "f" * 64
    packages_text = (
        f"Package: a\nFilename: pool/a.deb\nSize: 7\nSHA256: {have}\n\n"
        f"Package: b\nFilename: pool/b.deb\nSize: 7\nSHA256: {missing}\n\n"
    )
    idx = gzip.compress(packages_text.encode())
    idx_cs = hashlib.sha256(idx).hexdigest()
    pool.add(b"present", have, "sha256", "a.deb", 7)
    pool.add(idx, idx_cs, "sha256", "Packages.gz", len(idx))
    pool.reconcile_refs("m", [
        (have, "pool/a.deb", KIND_PKG),
        (missing, "pool/b.deb", KIND_PKG),
        (idx_cs, "Packages.gz", KIND_META),
    ], source=src)
    pool.record_mirror(config.MirrorConfig(
        name="m", source=src, provider="apt-flat", source_url=src, label="custom",
    ))
    pool.close()

    return config.Config(
        settings=config.Settings(mirror_dir=mirror_dir),
        sources=[config.SourceConfig(url=src, provider="apt-flat")],
        networks=[config.NetworkConfig(name="lab", bundle_dir=tmp_path / "bundles")],
    )


class _RecordingApp(MmwApp):
    """The Networks section against a real (incomplete) pool; records
    `notify()` instead of rendering a toast."""

    def __init__(self, cfg: config.Config, cfg_dir):
        config.save(cfg, cfg_dir)
        super().__init__(cfg, cfg_dir, initial_section="networks")
        self.notifications: list[tuple[str, str]] = []

    def notify(self, message, *, title="", severity="information", timeout=None, markup=True) -> None:
        self.notifications.append((message, severity))


async def _settle(pilot, tries: int = 40):
    """Until no work is in flight — see tests/waiting.py. The count is ignored."""
    await settle(pilot)


def test_do_bundle_refuses_when_mirror_incomplete(tmp_path, monkeypatch):
    cfg = _cfg_with_incomplete_mirror(tmp_path)

    calls = []
    monkeypatch.setattr(
        "mmw.core.publisher.bundle.bundle_network",
        lambda *a, **k: calls.append((a, k)),
    )

    async def scenario():
        from textual.widgets import Button, ListView

        app = _RecordingApp(cfg, tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app.screen.query_one("#net-list", ListView).index = 0
            await _settle(pilot)

            app.screen.query_one("#btn-net-bundle", Button).press()
            await _settle(pilot)
            # The from/to picker is now on top -- take its defaults to reach
            # the verify gate this test is actually about.
            await until(pilot, lambda: app.screen.query("#btn-pick-go"))
            app.screen.query_one("#btn-pick-go", Button).press()
            await _settle(pilot)
            # The refusal is on the Publish panel, and a popup offers to
            # bundle anyway: cancel it.
            from mmw.tui.transfer import TransferView
            from mmw.tui.ui.dialog import ConfirmDialog
            await until(pilot, lambda: isinstance(app.screen, ConfirmDialog)
                        and bool(app.screen.query("#confirm")))  # mounted, not just pushed
            await pilot.press("escape")
            await until(pilot, lambda: app.screen.query(TransferView)
                        and app.screen.query(TransferView).first()._done_displayed)
            # Completion and the page-status render arrive in separate UI
            # messages; wait for the refusal text before reading it.
            await until(pilot, lambda: "incomplete" in str(app.screen.query_one("#page-status").render()))
            view = app.screen.query_one(TransferView)
            outcome = view.outcome
            status = str(app.screen.query_one("#page-status").render())
        return app.notifications, outcome, status

    notifications, outcome, status = asyncio.run(scenario())

    # bundle_network must never have been called for an incomplete mirror.
    assert calls == []
    # And the refusal must actually have been shown, not silently swallowed.
    assert not any("Transferred" in msg for msg, _ in notifications)
    assert outcome == "note" and "incomplete" in status
