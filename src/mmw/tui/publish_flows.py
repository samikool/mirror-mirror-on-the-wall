"""The Networks verbs — publish, dry run, bundle, certs — as flows.

Publish, dry run and bundle run in the Publish section: each drives a
`TransferView` (preparing, then transferring) and never pushes a screen of its
own, so nothing lands on whichever section happens to be showing when it
finishes. Certs is a short page flow started from Networks.
"""
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from textual.markup import escape

from mmw.core import config
from mmw.core.format import fmt_size as _fmt_bytes
from mmw.core.resolve import network_mirrors
from mmw.tui.publish_modals import CertsPage

# Verify and dry-run are per-mirror, read-only, and independent. Run a few at a
# time: the expensive parts (lzma/gzip decompress, SQLite) release the GIL, so
# they genuinely overlap, and the modal advances as each lands instead of
# sitting still for the seconds one big index takes. Bounded low on purpose —
# every one of these hits the pool's single shared connection, so more workers
# would just queue on that lock.
_PREPARE_WORKERS = 4


async def _run_per_mirror(mirrors, fn, on_done, stop_event=None):
    """Run fn(mirror) off-thread, at most _PREPARE_WORKERS at once.

    Results come back in `mirrors` order; on_done(completed, mirror, result)
    fires as each finishes, in completion order, so progress keeps moving.
    Once `stop_event` is set no further mirror starts (its result stays None):
    a Stop while verifying a big network must not wait out every mirror.
    """
    sem = asyncio.Semaphore(_PREPARE_WORKERS)
    results: list = [None] * len(mirrors)
    done = 0

    async def _one(i, m):
        nonlocal done
        async with sem:
            if stop_event is not None and stop_event.is_set():
                return
            r = await asyncio.to_thread(fn, m)
        results[i] = r
        done += 1
        on_done(done, m, r)

    await asyncio.gather(*(_one(i, m) for i, m in enumerate(mirrors)))
    return results


@dataclass(frozen=True)
class PublishChoice:
    """What `p` publishes: the live state or a snapshot (stamp), and where —
    the network's root (None) or another directory."""
    snapshot: str | None = None
    root: Path | None = None


@dataclass(frozen=True)
class BundleChoice:
    """What `b` bundles: from where the last bundle went (`from_last`), a
    snapshot, or nothing (None: full); to a new snapshot of now or a snapshot."""
    from_last: bool = True
    from_ref: str | None = None
    to_ref: str = "now"


class PublishFlows:
    def __init__(self, app, get_cfg: Callable[[], config.Config]):
        self.app = app
        self.cfg = get_cfg

    async def _run_pre_publish(
        self,
        net: "config.NetworkConfig",
        selected_mirrors: list,
        mirror_dir: "Path",
        view,
        include_dry_run: bool = False,
        include_file_counts: bool = True,
        include_verify: bool = True,
    ):
        """Run verify (+ optional dry run), reporting into `view`'s preparing stage.

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
            dry_run_mirror, NetworkPublishResult, dist_upgrade_info, network_totals,
        )

        verify_results = []
        file_counts: dict[str, int] = {}
        dry_result = None
        stop = getattr(view, "stop_event", None)

        def stopped() -> bool:
            return stop is not None and stop.is_set()

        pool = await asyncio.to_thread(lambda: Pool(mirror_dir, read_only=True))
        with pool:
            # ── Phase 1: verify ───────────────────────────────────────
            if include_verify:
                view.set_phase("Verifying mirrors", len(selected_mirrors))

                def _report_verify(n, m, vr):
                    if vr.error:
                        detail, ok = vr.error, False
                    elif vr.complete:
                        detail, ok = f"complete · {vr.total:,} files", True
                    else:
                        detail, ok = f"{vr.missing_count:,} of {vr.total:,} missing", False
                    view.mirror_done(n, m.name, detail, ok)

                verify_results = await _run_per_mirror(
                    selected_mirrors,
                    lambda m: verify_mirror(m, mirror_dir, pool),
                    _report_verify, stop,
                )
                if stopped():
                    return verify_results, file_counts, dry_result

            # ── Phase 2: unique package count per base group (DB query) ──
            # Skipped during dry-run — only needed when proceeding to publish.
            if include_file_counts:
                view.set_phase("Counting packages...", len(selected_mirrors))
                from collections import defaultdict
                base_to_names: dict[str, list[str]] = defaultdict(list)
                for m in selected_mirrors:
                    base_to_names[m.mirror_base].append(m.name)

                seen_assigned: set[str] = set()
                for m in selected_mirrors:
                    if stopped():
                        return verify_results, file_counts, dry_result
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
                view.set_phase("Calculating changes", len(selected_mirrors))

                def _report_dry(n, m, mr):
                    if mr.errors:
                        detail, ok = mr.errors[0], False
                    else:
                        detail = f"{mr.total_files:,} files · {_fmt_bytes(mr.total_bytes)}"
                        if mr.linked > 0:
                            detail += f"  →  {mr.linked:,} to add ({_fmt_bytes(mr.linked_bytes)})"
                        ok = True
                    view.mirror_done(n, m.name, detail, ok)

                # Filled from the worker threads: plain dict stores, each
                # atomic under the GIL, and a shared file is one key.
                published: dict[str, int] = {}
                dry_result.mirrors = await _run_per_mirror(
                    selected_mirrors,
                    lambda m: dry_run_mirror(m, net, mirror_dir, pool, published),
                    _report_dry, stop,
                )
                if stopped():
                    return verify_results, file_counts, dry_result
                dry_result.totals = await asyncio.to_thread(
                    network_totals, [m.name for m in selected_mirrors], pool, published)
                if net.dist_upgrade:
                    dry_result.dist_upgrade_paths, dry_result.dist_upgrade_warnings = \
                        await asyncio.to_thread(dist_upgrade_info, net, self.cfg(), pool)

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

    def _ends_early(self, verify_results: list, view) -> bool:
        """Stopped while preparing, or a mirror is incomplete: end the
        transfer here (in that order — a Stop is never reported as a refusal).
        True if it ended."""
        if view.stop_event.is_set():
            self._stopped_early(view)
            return True
        bad = self._incomplete_lines([r for r in verify_results if r is not None])
        if bad:
            view.finish_note(f"incomplete mirrors — nothing {view.VERB_PAST.lower()}",
                             ["  [$mmw-error]Cannot continue — incomplete mirrors:[/]",
                              *(f"[$mmw-error]{escape(line)}[/]" for line in bad)])
            return True
        return False

    async def _guarded(self, view, what: str, flow) -> None:
        """Any failure ends the transfer as an error on the panel — which the
        Publish section also toasts, wherever the user is. Never an app crash."""
        try:
            await flow()
        except Exception as e:  # noqa: BLE001 — reported, never fatal
            view.mark_error(f"{what} error: {str(e) or type(e).__name__}")

    async def publish(self, net: "config.NetworkConfig", view,
                      choice: PublishChoice | None = None) -> None:
        choice = choice or PublishChoice()
        if choice.root is not None:
            net = net.model_copy(update={"root": choice.root})
        if choice.snapshot is not None:
            # A snapshot's files are checked by publish_snapshot itself: the
            # verify below is about the live state's refs.
            await self._guarded(view, "Publish",
                                lambda: self._execute_snapshot_publish(net, choice.snapshot, view))
            return

        async def flow():
            # A pool query — off the loop, and inside the guard: a locked pool
            # is reported like any other publish error.
            selected_mirrors = await asyncio.to_thread(self._selected_mirrors_for, net)
            mirror_dir = self.cfg().settings.mirror_dir
            verify_results, per_mirror, _ = await self._run_pre_publish(
                net, selected_mirrors, mirror_dir, view)
            if self._ends_early(verify_results, view):
                return
            await self._execute_publish(net, per_mirror, view)
        await self._guarded(view, "Publish", flow)

    async def dry_run(self, net: "config.NetworkConfig", view) -> None:
        async def flow():
            selected_mirrors = await asyncio.to_thread(self._selected_mirrors_for, net)
            mirror_dir = self.cfg().settings.mirror_dir
            verify_results, _, dry_result = await self._run_pre_publish(
                net, selected_mirrors, mirror_dir, view,
                include_dry_run=True, include_file_counts=False,
            )
            if self._ends_early(verify_results, view):
                return
            if not await view.ask_publish(dry_result):
                if not view.stop_event.is_set():
                    view.finish_note("dry run only — nothing published")
                    return
                return self._stopped_early(view)
            # Confirmed — count files for the progress bar, but skip
            # re-verifying every mirror (that just happened).
            _, per_mirror, _ = await self._run_pre_publish(
                net, selected_mirrors, mirror_dir, view,
                include_file_counts=True, include_verify=False,
            )
            if view.stop_event.is_set():
                return self._stopped_early(view)
            await self._execute_publish(net, per_mirror, view)
        await self._guarded(view, "Publish", flow)

    async def bundle(self, net: "config.NetworkConfig", view,
                     choice: BundleChoice | None = None) -> None:
        # Same completeness gate publish runs: a ref the pool holds no blob
        # for is invisible to `pool.mirror_files()` — which both publish and
        # bundle enumerate from — so it would land in neither a published tree
        # NOR a bundle. Skipping this let a bundle ship an index naming a .deb
        # that isn't there: `apply.py --verify` passes, and the 404 only shows
        # as `apt install` failing on the far side, where nobody can debug it.
        # A snapshot destination was gated when it was taken; its files are
        # checked by bundle_network itself.
        choice = choice or BundleChoice()

        async def flow():
            if choice.to_ref == "now":
                selected_mirrors = await asyncio.to_thread(self._selected_mirrors_for, net)
                mirror_dir = self.cfg().settings.mirror_dir
                verify_results, _, _ = await self._run_pre_publish(
                    net, selected_mirrors, mirror_dir, view, include_file_counts=False,
                )
                if self._ends_early(verify_results, view):
                    return
            await self._execute_bundle(net, view, choice)
        await self._guarded(view, "Bundle", flow)

    @staticmethod
    def _stopped_early(view) -> None:
        """Stopped while preparing: nothing was transferred."""
        if view.running:
            view.mark_done(None)

    async def _execute_publish(self, net: "config.NetworkConfig", per_mirror: dict[str, int],
                               view) -> None:
        from mmw.core.publisher import publish_network

        view.begin(sum(per_mirror.values()), per_mirror)
        try:
            result = await asyncio.to_thread(
                publish_network, net, self.cfg(), self.cfg().settings.mirror_dir,
                view.record, view.stop_event, view.notify_mirror_start,
            )
        except Exception as exc:  # noqa: BLE001 — shown on the panel
            view.mark_error(str(exc) or type(exc).__name__)
            return
        stopped = view.stop_event.is_set()     # the publish's own outcome
        if net.certs and not stopped and not result.errors:
            await self._certs_after_publish(net, view)
        view.mark_done(result, stopped=stopped)

    async def _certs_after_publish(self, net: "config.NetworkConfig", view) -> None:
        """What headless `mmw publish` does after a clean publish of a certs
        network — refresh key/CSR — reported in the panel, not a page. A certs
        failure never turns a good publish into a failed one."""
        from mmw.core.certs import generate_certs

        try:
            hostnames, *paths = await asyncio.to_thread(generate_certs, net, self.cfg())
        except Exception as e:  # noqa: BLE001 — reported, publish stands
            view.write(f"  [$mmw-warn]⚠ certs: {escape(str(e) or type(e).__name__)}[/]")
            return
        view.write(f"  [dim]# certs for {escape(', '.join(hostnames))}[/]")
        for path in paths:
            view.write(f"  [dim]#   {escape(str(path))}[/]")

    async def _execute_snapshot_publish(self, net: "config.NetworkConfig", stamp: str,
                                        view) -> None:
        from mmw.core.pool import Pool
        from mmw.core.publisher import publish_snapshot

        def files() -> int:
            with Pool(self.cfg().settings.mirror_dir, read_only=True) as pool:
                return next((s.files for s in pool.snapshots(net.name) if s.stamp == stamp), 0)
        view.begin(await asyncio.to_thread(files))
        try:
            result = await asyncio.to_thread(
                lambda: publish_snapshot(
                    net, self.cfg(), self.cfg().settings.mirror_dir, stamp,
                    progress=view.record, stop_event=view.stop_event,
                    mirror_start_callback=view.notify_mirror_start))
        except Exception as exc:  # noqa: BLE001 — shown on the panel
            view.mark_error(str(exc) or type(exc).__name__)
            return
        stopped = view.stop_event.is_set()
        if net.certs and not stopped and not result.errors:
            await self._certs_after_publish(net, view)
        view.mark_done(result, stopped=stopped)

    async def _execute_bundle(self, net: "config.NetworkConfig", view,
                              choice: BundleChoice | None = None) -> None:
        from mmw.core.publisher.bundle import bundle_network

        choice = choice or BundleChoice()
        ends = {"to_ref": choice.to_ref}
        if not choice.from_last:
            ends["from_ref"] = choice.from_ref
        view.begin()
        try:
            result = await asyncio.to_thread(
                lambda: bundle_network(
                    net, self.cfg(), self.cfg().settings.mirror_dir,
                    progress=view.record, stop_event=view.stop_event,
                    mirror_start_callback=view.notify_mirror_start, **ends))
        except Exception as exc:  # noqa: BLE001 — shown on the panel
            view.mark_error(str(exc) or type(exc).__name__)
            return
        view.mark_done(result)

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
