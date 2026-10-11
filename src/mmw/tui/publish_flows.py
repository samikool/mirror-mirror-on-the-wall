"""The Networks verbs — publish, dry run, bundle, certs — as flows.

Run from the Networks section. A flow needs only the app (to push its
screens and notify) and the current config.
"""
import asyncio
from collections.abc import Callable
from pathlib import Path

from mmw.core import config
from mmw.core.format import fmt_size as _fmt_bytes
from mmw.core.resolve import network_mirrors
from mmw.tui.modals import InfoModal
from mmw.tui.publish_modals import (
    BundleModeModal, BundleProgressModal, CertsPage, DryRunResultPage, PreparePublishModal,
    PublishProgressModal,
)

# Verify and dry-run are per-mirror, read-only, and independent. Run a few at a
# time: the expensive parts (lzma/gzip decompress, SQLite) release the GIL, so
# they genuinely overlap, and the modal advances as each lands instead of
# sitting still for the seconds one big index takes. Bounded low on purpose —
# every one of these hits the pool's single shared connection, so more workers
# would just queue on that lock.
_PREPARE_WORKERS = 4


async def _run_per_mirror(mirrors, fn, on_done):
    """Run fn(mirror) off-thread, at most _PREPARE_WORKERS at once.

    Results come back in `mirrors` order; on_done(completed, mirror, result)
    fires as each finishes, in completion order, so progress keeps moving.
    """
    sem = asyncio.Semaphore(_PREPARE_WORKERS)
    results: list = [None] * len(mirrors)
    done = 0

    async def _one(i, m):
        nonlocal done
        async with sem:
            r = await asyncio.to_thread(fn, m)
        results[i] = r
        done += 1
        on_done(done, m, r)

    await asyncio.gather(*(_one(i, m) for i, m in enumerate(mirrors)))
    return results


class PublishFlows:
    def __init__(self, app, get_cfg: Callable[[], config.Config]):
        self.app = app
        self.cfg = get_cfg

    async def _run_pre_publish(
        self,
        net: "config.NetworkConfig",
        selected_mirrors: list,
        mirror_dir: "Path",
        include_dry_run: bool = False,
        include_file_counts: bool = True,
        include_verify: bool = True,
    ):
        """Run verify (+ optional dry run) with a PreparePublishModal.

        Returns (verify_results, file_counts, dry_run_result).
        file_counts: dict[mirror_name, int] — actual package file count for the first
        mirror sharing each mirror_base; 0 for subsequent mirrors in the same base
        (they skip instantly since the first one did the work).
        dry_run_result is None when include_dry_run=False.
        include_verify=False skips phase 1 — used when the caller just verified
        (dry-run → confirm → publish) to avoid re-parsing every index.
        """
        from mmw.core.verify import verify_mirror
        from mmw.core.pool import Pool
        from mmw.core.publisher import (
            dry_run_mirror, NetworkPublishResult, dist_upgrade_info,
        )

        modal = PreparePublishModal(net.name)
        await self.app.push_screen(modal)
        verify_results = []
        file_counts: dict[str, int] = {}
        dry_result = None

        try:
            pool = await asyncio.to_thread(
                lambda: Pool(mirror_dir, read_only=True))
            with pool:
                # ── Phase 1: verify ───────────────────────────────────────
                if include_verify:
                    modal.set_phase("Verifying mirrors", len(selected_mirrors))

                    def _report_verify(n, m, vr):
                        if vr.error:
                            detail, ok = vr.error, False
                        elif vr.complete:
                            detail, ok = f"complete · {vr.total:,} files", True
                        else:
                            detail, ok = f"{vr.missing_count:,} of {vr.total:,} missing", False
                        modal.mirror_done(n, m.name, detail, ok)

                    verify_results = await _run_per_mirror(
                        selected_mirrors,
                        lambda m: verify_mirror(m, mirror_dir, pool),
                        _report_verify,
                    )

                # ── Phase 2: unique package count per base group (DB query) ──
                # Skipped during dry-run — only needed when proceeding to publish.
                if include_file_counts:
                    modal.set_phase("Counting packages...", len(selected_mirrors))
                    from collections import defaultdict
                    base_to_names: dict[str, list[str]] = defaultdict(list)
                    for m in selected_mirrors:
                        base_to_names[m.mirror_base].append(m.name)

                    seen_assigned: set[str] = set()
                    for m in selected_mirrors:
                        if m.mirror_base not in seen_assigned:
                            seen_assigned.add(m.mirror_base)
                            names = base_to_names[m.mirror_base]
                            count = await asyncio.to_thread(pool.group_unique_count, names)
                            file_counts[m.name] = count
                        else:
                            file_counts[m.name] = 0

                # ── Phase 3: dry run (optional) ───────────────────────────
                if include_dry_run:
                    dry_result = NetworkPublishResult(network_name=net.name, dry_run=True)
                    modal.set_phase("Calculating changes", len(selected_mirrors))

                    def _report_dry(n, m, mr):
                        if mr.errors:
                            detail, ok = mr.errors[0], False
                        else:
                            detail = f"{mr.total_files:,} files · {_fmt_bytes(mr.total_bytes)}"
                            if mr.linked > 0:
                                detail += f"  →  {mr.linked:,} to add ({_fmt_bytes(mr.linked_bytes)})"
                            ok = True
                        modal.mirror_done(n, m.name, detail, ok)

                    dry_result.mirrors = await _run_per_mirror(
                        selected_mirrors,
                        lambda m: dry_run_mirror(m, net, mirror_dir, pool),
                        _report_dry,
                    )
                    if net.dist_upgrade:
                        dry_result.dist_upgrade_paths, dry_result.dist_upgrade_warnings = \
                            await asyncio.to_thread(dist_upgrade_info, net, self.cfg(), pool)
        finally:
            try:
                self.app.pop_screen()
            except Exception:
                pass
            await asyncio.sleep(0)  # let Textual process the pop before caller pushes next screen

        return verify_results, file_counts, dry_result

    def _incomplete_lines(self, verify_results: list) -> list[str]:
        return [
            f"  • {r.mirror_name}: {r.error}" if r.error
            else f"  • {r.mirror_name}: {r.missing_count:,} of {r.total:,} files missing"
            for r in verify_results if not r.complete
        ]

    def _selected_mirrors_for(self, net: "config.NetworkConfig") -> list:
        mirror_map = {
            m.name: m
            for m in network_mirrors(net, self.cfg(), self.cfg().settings.mirror_dir)
        }
        selected_names = list(mirror_map)
        return [mirror_map[n] for n in selected_names if n in mirror_map]

    async def _show_incomplete(self, verify_results: list) -> bool:
        """Show the incomplete-mirrors modal if needed; True if publish must abort."""
        bad = self._incomplete_lines(verify_results)
        if bad:
            await self.app.push_screen_wait(
                InfoModal("Incomplete Mirrors", "Cannot publish — incomplete mirrors:\n" + "\n".join(bad))
            )
            return True
        return False

    async def _execute_publish(self, net: "config.NetworkConfig", per_mirror: dict[str, int]) -> None:
        """Run publish_network behind a PublishProgressModal and notify the outcome."""
        from mmw.core.publisher import publish_network

        mirror_dir = self.cfg().settings.mirror_dir
        prog_modal = PublishProgressModal(
            net.name, total=sum(per_mirror.values()), per_mirror_counts=per_mirror
        )

        async def _run_publish():
            try:
                r = await asyncio.to_thread(
                    publish_network, net, self.cfg(), mirror_dir,
                    prog_modal.record, prog_modal.stop_event, prog_modal.notify_mirror_start,
                )
                prog_modal.mark_done(r)
            except Exception as exc:
                prog_modal.mark_error(str(exc) or type(exc).__name__)

        # run_worker, not ensure_future: the publish task is then owned by the
        # widget and cancelled with it, and a crash surfaces rather than being
        # swallowed by an unreferenced future.
        self.app.run_worker(_run_publish(), name="publish", exit_on_error=False)
        result = await self.app.push_screen_wait(prog_modal)

        if result is None:
            # Stopped by user or error — notification already shown in modal
            return

        pruned_str = f", {result.pruned:,} pruned" if result.pruned else ""
        du_warn_str = (
            f", {len(result.dist_upgrade_warnings)} dist-upgrade warning(s)"
            if result.dist_upgrade_warnings else ""
        )
        if prog_modal.stop_event.is_set():
            self.app.notify(
                f"Publish stopped: {result.linked:,} linked, {result.skipped:,} already current{pruned_str}.",
                severity="warning",
            )
        elif result.errors:
            self.app.notify(
                f"Publish complete with {len(result.errors)} error(s). "
                f"{result.linked:,} linked, {result.skipped:,} already current{pruned_str}{du_warn_str}.",
                severity="warning",
            )
        else:
            sev = "warning" if result.dist_upgrade_warnings else "information"
            self.app.notify(
                f"Published {net.name}: {result.linked:,} linked, {result.skipped:,} already current{pruned_str}{du_warn_str}.",
                severity=sev,
            )
            if net.certs:
                await self.certs(net)

    async def publish(self, net: "config.NetworkConfig") -> None:

        try:
            # A pool query — off the loop, and inside the try: a locked pool
            # is reported like any other publish error.
            selected_mirrors = await asyncio.to_thread(self._selected_mirrors_for, net)
            mirror_dir = self.cfg().settings.mirror_dir
            verify_results, per_mirror, _ = await self._run_pre_publish(net, selected_mirrors, mirror_dir)
            if await self._show_incomplete(verify_results):
                return
            await self._execute_publish(net, per_mirror)
        except Exception as e:
            self.app.notify(f"Publish error: {e}", severity="error", timeout=15)

    async def dry_run(self, net: "config.NetworkConfig") -> None:

        try:
            # A pool query — off the loop, and inside the try: a locked pool
            # is reported like any other publish error.
            selected_mirrors = await asyncio.to_thread(self._selected_mirrors_for, net)
            mirror_dir = self.cfg().settings.mirror_dir
            verify_results, _, dry_result = await self._run_pre_publish(
                net, selected_mirrors, mirror_dir, include_dry_run=True, include_file_counts=False
            )
            if await self._show_incomplete(verify_results):
                return

            proceed = await self.app.push_screen_wait(DryRunResultPage(net.name, dry_result))
            if not proceed:
                return

            # User confirmed — compute file counts for the progress bar, but
            # skip re-verifying every mirror (we just did).
            _, per_mirror, _ = await self._run_pre_publish(
                net, selected_mirrors, mirror_dir,
                include_file_counts=True, include_verify=False,
            )
            await self._execute_publish(net, per_mirror)
        except Exception as e:
            self.app.notify(f"Publish error: {e}", severity="error", timeout=15)

    async def bundle(self, net: "config.NetworkConfig") -> None:
        if net.bundle_dir is None:
            self.app.notify(
                f"Network '{net.name}' has no bundle directory — set one in Edit.",
                severity="warning",
            )
            return

        full = await self.app.push_screen_wait(BundleModeModal(net.name))
        if full is None:
            return   # cancelled — never falls back to delta silently

        # Same completeness gate `do_publish` runs before it will publish: a
        # ref the pool doesn't hold a blob for (refs - packages) is invisible
        # to `pool.mirror_files()` — the query both publish and bundle
        # enumerate from — so it lands in neither a published tree NOR a
        # bundle's tar or manifest. Skipping this let a bundle ship an apt
        # index referencing a .deb that isn't there: `apply.py --verify`
        # passes, and the 404 only surfaces as `apt install` failing on the
        # far side of the airgap, where nobody can debug it.
        try:
            # A pool query — off the loop, and inside the try: a locked pool
            # is reported like any other publish error.
            selected_mirrors = await asyncio.to_thread(self._selected_mirrors_for, net)
            mirror_dir = self.cfg().settings.mirror_dir
            verify_results, _, _ = await self._run_pre_publish(
                net, selected_mirrors, mirror_dir, include_file_counts=False,
            )
            if await self._show_incomplete(verify_results):
                return
            await self._execute_bundle(net, full=full)
        except Exception as e:
            self.app.notify(f"Bundle error: {e}", severity="error", timeout=15)

    async def _execute_bundle(self, net: "config.NetworkConfig", full: bool = False) -> None:
        """Run bundle_network behind a BundleProgressModal and notify the outcome."""
        from mmw.core.publisher.bundle import bundle_network

        mirror_dir = self.cfg().settings.mirror_dir
        prog = BundleProgressModal(net.name)

        async def _run():
            try:
                r = await asyncio.to_thread(
                    bundle_network, net, self.cfg(), mirror_dir,
                    None, full, 3, prog.record, prog.stop_event,
                    prog.notify_mirror_start, compress=net.bundle_compress,
                )
                prog.mark_done(r)
            except Exception as exc:
                prog.mark_error(str(exc) or type(exc).__name__)

        # run_worker, not ensure_future: the task is owned by the widget and
        # cancelled with it, and a crash surfaces rather than being swallowed.
        self.app.run_worker(_run(), name="bundle", exit_on_error=False)
        result = await self.app.push_screen_wait(prog)

        if result is None:
            # Stopped by user or error — notification already shown in modal
            return

        # Mirrors `_execute_publish`'s branching: a non-None result can still
        # be a stopped or partially-failed run (dist-upgrade failures populate
        # `BundleResult.errors` directly, per its own dataclass docstring), and
        # reporting either of those as unqualified success is exactly what
        # this project's "nothing is silent" outcome discipline forbids — the
        # modal's own title bar (⚠ Stopped / ✗ Error / ✓ Bundled) already knows
        # better, so the notification must agree with it.
        deleted_str = f", {result.deleted:,} to delete" if result.deleted else ""
        du_warn_str = (
            f", {len(result.dist_upgrade_warnings)} dist-upgrade warning(s)"
            if result.dist_upgrade_warnings else ""
        )
        if prog.stop_event.is_set():
            self.app.notify(
                f"Bundle stopped: {result.sent:,} sent{deleted_str}.",
                severity="warning",
            )
        elif result.errors:
            self.app.notify(
                f"Bundle complete with {len(result.errors)} error(s). "
                f"{result.sent:,} sent{deleted_str}{du_warn_str}.",
                severity="warning",
            )
        else:
            sev = "warning" if result.dist_upgrade_warnings else "information"
            self.app.notify(
                f"Bundled {net.name}: {result.sent:,} sent{deleted_str}{du_warn_str} → {result.out_path}.",
                severity=sev,
            )

    async def certs(self, net: "config.NetworkConfig") -> None:
        """Generate key+CSR for net, showing a preview modal then a results modal."""
        from mmw.core.certs import collect_hostnames, generate_certs

        try:
            hostnames = await asyncio.to_thread(collect_hostnames, net, self.cfg())
        except Exception as e:
            self.app.notify(f"Certs error: {e}", severity="error", timeout=15)
            return
        if not hostnames:
            self.app.notify("No mirrors configured for this network.", severity="warning")
            return

        if not await self.app.push_screen_wait(CertsPage(net.name, hostnames)):
            return

        try:
            hostnames, key_path, cnf_path, csr_path, md_path = await asyncio.to_thread(
                generate_certs, net, self.cfg()
            )
            await self.app.push_screen_wait(
                CertsPage(net.name, hostnames, key_path=key_path, cnf_path=cnf_path, csr_path=csr_path, md_path=md_path)
            )
        except Exception as e:
            await self.app.push_screen_wait(CertsPage(net.name, hostnames, error=str(e)))
