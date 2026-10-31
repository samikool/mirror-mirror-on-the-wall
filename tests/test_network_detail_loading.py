"""The Networks detail pane's "Published" figures: one load per network shown,
superseded loads stop, the mirror list does not wait for the sizes, and a
revisit re-reads the published indexes only if one changed.

On the live pool reading oak's 88 published indexes takes ~23 s; opening
Networks started two such loads (list build + first highlight), every
cursor move another, none cancellable — "Loading…" for minutes."""
import asyncio
import os
import threading

from mmw.core.publisher import publish_network
from mmw.core.publisher import stats as stats_mod
from mmw.tui.sections import network_detail as ND
from mmw.tui.sections.network_detail import NetworkDetail
from mmw.tui.ui.base import ThemedApp

from test_dry_run_totals import two_mirrors  # noqa: F401 — the shared pool fixture
from waiting import shows, until


def _host(check):
    async def go():
        class _App(ThemedApp):
            def compose(self):
                yield NetworkDetail()
        app = _App()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await check(app, app.query_one(NetworkDetail), pilot)
    asyncio.run(go())


async def _until(pilot, cond, tries=None):
    """`tries` is ignored: see tests/waiting.py — wait for the thing, against a clock."""
    await until(pilot, cond)

def _counting(monkeypatch, gate=None):
    reads = []
    real = stats_mod.dst_packages

    def counted(root, mirror):
        reads.append(mirror.name)
        if gate is not None:
            gate.wait(5)
        return real(root, mirror)
    monkeypatch.setattr(stats_mod, "dst_packages", counted)
    return reads


def test_showing_the_same_network_again_starts_no_second_load(two_mirrors, monkeypatch):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    publish_network(net, cfg, mirror_dir)
    reads = _counting(monkeypatch)

    async def check(app, d, pilot):
        d.show(net, cfg)
        d.show(net, cfg)                    # list rebuild, then first highlight
        await _until(pilot, lambda: d._stats is not None)
        await pilot.pause(0.2)
        assert sorted(reads) == ["a", "b"], reads
        assert d._stats[:2] == (3, 1500)
    _host(check)


def test_the_mirror_list_does_not_wait_for_the_sizes(two_mirrors, monkeypatch):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    publish_network(net, cfg, mirror_dir)
    gate = threading.Event()
    _counting(monkeypatch, gate)

    async def check(app, d, pilot):
        d.show(net, cfg)
        await _until(pilot, lambda: d._selected is not None)
        assert d._stats is None
        # The line itself, not the list beside it: it can mount a moment later.
        await shows(pilot, lambda: "".join(str(w.render()) for w in d.query(".stats-line")),
                    "Reading published indexes")
        assert d.query("#mirror-list")
        gate.set()
        await _until(pilot, lambda: d._stats is not None)
    _host(check)


def test_a_superseded_load_stops_reading(two_mirrors, monkeypatch):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    publish_network(net, cfg, mirror_dir)
    gate = threading.Event()
    reads = _counting(monkeypatch, gate)
    other = net.model_copy(update={"name": "elm"})

    async def check(app, d, pilot):
        d.show(net, cfg)
        await _until(pilot, lambda: reads)          # the first index is being read
        d.show(other, cfg)                           # the user moved on
        gate.set()
        await _until(pilot, lambda: d._stats is not None)
        await pilot.pause(0.2)
        # oak stopped after its first index; elm read both.
        assert len(reads) == 3, reads
    _host(check)


def test_a_revisit_reuses_the_figures_until_an_index_changes(two_mirrors, monkeypatch):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    publish_network(net, cfg, mirror_dir)
    reads = _counting(monkeypatch)
    other = net.model_copy(update={"name": "elm", "root": None})

    async def check(app, d, pilot):
        d.show(net, cfg)
        await _until(pilot, lambda: d._stats is not None)
        d.show(other, cfg)
        await pilot.pause(0.1)
        d.show(net, cfg)
        await _until(pilot, lambda: d._stats is not None)
        assert len(reads) == 2, "an unchanged tree was read again"
        # A publish rewrote one index: that revisit reads again.
        index = net.root / "example.com" / "a" / "Packages"
        st = index.stat()
        os.utime(index, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        d.show(other, cfg)
        await pilot.pause(0.1)
        d.show(net, cfg)
        await _until(pilot, lambda: d._stats is not None)
        assert len(reads) == 4, reads
    _host(check)


def test_force_rechecks_the_same_network(two_mirrors, monkeypatch):  # noqa: F811
    """After a transfer finishes, Networks asks again for the same network:
    that must re-check its indexes (not be skipped as a duplicate) — and an
    unchanged tree is then served from the cache, not read again."""
    cfg, net, mirror_dir = two_mirrors
    publish_network(net, cfg, mirror_dir)
    checks = []
    real = ND._signature
    monkeypatch.setattr(ND, "_signature", lambda *a: (checks.append(1), real(*a))[1])
    reads = _counting(monkeypatch)

    async def check(app, d, pilot):
        d.show(net, cfg)
        await _until(pilot, lambda: d._stats is not None)
        d.show(net, cfg)                         # duplicate: nothing
        d.show(net, cfg, force=True)             # re-check
        await _until(pilot, lambda: d._stats is not None and len(checks) == 2)
        assert len(reads) == 2                   # …served from the cache
    _host(check)
