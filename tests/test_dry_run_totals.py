"""Dry-run totals count each file once.

Mirrors share files — an `Architecture: all` package is in every arch's index,
a `-security` package is in `-updates` too — so summing per-mirror sizes
counted the oak network (4.1 TB of distinct content) as 7.2 TB."""
import hashlib

import pytest

from mmw.core import config
from mmw.core.pool import KIND_META, KIND_PKG, Pool
from mmw.core.publisher import dry_run_network, publish_network

SHARED = b"x" * 1000            # in both mirrors
ONLY_A = b"a" * 300
ONLY_B = b"b" * 200


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _index(files: dict[str, bytes]) -> bytes:
    return "".join(f"Package: {n.split('.')[0]}\nFilename: ./{n}\nSize: {len(c)}\n"
                   f"SHA256: {_sha(c)}\n\n" for n, c in files.items()).encode()


@pytest.fixture
def two_mirrors(tmp_path):
    mirror_dir = tmp_path / "mirror"
    sources, contents = [], {"a": {"shared.deb": SHARED, "a.deb": ONLY_A},
                             "b": {"shared.deb": SHARED, "b.deb": ONLY_B}}
    with Pool(mirror_dir) as pool:
        for name, files in contents.items():
            refs = []
            for fname, data in files.items():
                pool.add(data, _sha(data), "sha256", fname, len(data))
                refs.append((_sha(data), fname, KIND_PKG))
            idx = _index(files)
            pool.add(idx, _sha(idx), "sha256", "Packages", len(idx))
            refs.append((_sha(idx), "Packages", KIND_META))
            pool.reconcile_refs(name, refs)
            sources.append(config.SourceConfig(name=name, label="custom", provider="apt-flat",
                                               url=f"https://example.com/{name}/"))
    net = config.NetworkConfig(name="oak", root=tmp_path / "pub")
    cfg = config.Config(settings=config.Settings(mirror_dir=mirror_dir), sources=sources,
                        networks=[net])
    return cfg, net, mirror_dir


def test_pool_unique_stats_counts_a_shared_blob_once(two_mirrors):
    _, _, mirror_dir = two_mirrors
    with Pool(mirror_dir, read_only=True) as pool:
        assert pool.unique_stats(["a", "b"]) == (3, 1500)
        assert pool.unique_stats([]) == (0, 0)
        assert pool.group_unique_count(["a", "b"]) == 3


def test_dry_run_totals_count_shared_files_once(two_mirrors):
    cfg, net, mirror_dir = two_mirrors
    r = dry_run_network(net, cfg, mirror_dir)
    assert not r.errors
    assert sorted(m.total_bytes for m in r.mirrors) == [1200, 1300]   # rows: per mirror
    assert (r.total_files, r.total_bytes) == (3, 1500)                # not 4 / 2500
    assert (r.skipped_bytes, r.linked_bytes) == (0, 1500)


def test_already_published_is_counted_once_too(two_mirrors):
    cfg, net, mirror_dir = two_mirrors
    assert not publish_network(net, cfg, mirror_dir).errors
    r = dry_run_network(net, cfg, mirror_dir)
    assert (r.total_bytes, r.skipped_bytes, r.linked_bytes) == (1500, 1500, 0)
    assert (r.skipped, r.linked) == (3, 0)


def test_the_tui_dry_run_counts_shared_files_once(two_mirrors):
    """The Networks section builds its own dry-run result (per-mirror progress
    in the prepare modal) — its totals must match `dry_run_network`'s."""
    import asyncio

    from mmw.core.resolve import network_mirrors
    from mmw.tui.app import MmwApp
    from mmw.tui.publish_flows import PublishFlows

    cfg, net, mirror_dir = two_mirrors
    config.save(cfg, mirror_dir.parent)
    got = {}

    async def go():
        app = MmwApp(cfg, mirror_dir.parent, initial_section="networks")
        async with app.run_test(size=(120, 40)) as pilot:
            flows = PublishFlows(app, lambda: app.cfg)
            mirrors = network_mirrors(net, cfg, mirror_dir)

            async def run():
                from types import SimpleNamespace
                quiet = SimpleNamespace(set_phase=lambda *a: None, mirror_done=lambda *a: None)
                _, _, got["r"] = await flows._run_pre_publish(
                    net, mirrors, mirror_dir, quiet, include_dry_run=True,
                    include_file_counts=False, include_verify=False)
            await app.run_worker(run(), exit_on_error=False).wait()
            await pilot.pause()
    asyncio.run(go())
    r = got["r"]
    assert (r.total_files, r.total_bytes, r.linked_bytes) == (3, 1500, 1500)


def test_the_network_detail_published_size_counts_shared_files_once(two_mirrors):
    from mmw.core.resolve import network_mirrors
    from mmw.tui.sections.network_detail import stat_tree

    cfg, net, mirror_dir = two_mirrors
    assert not publish_network(net, cfg, mirror_dir).errors
    mirror_map = {m.name: m for m in network_mirrors(net, cfg, mirror_dir)}
    total_files, total_bytes, _, per_mirror = stat_tree(net, mirror_map)
    assert (total_files, total_bytes) == (3, 1500)
    assert sorted(size for _, _, size, _, _ in per_mirror) == [1200, 1300]
