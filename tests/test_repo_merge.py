"""`mmw merge OLDER NEWER`: one archive configured under two URLs becomes one
repository with one history — the older URL's points moved under the newer
one's, an identical Release kept once."""
import hashlib

import pytest
from typer.testing import CliRunner

from mmw.cli import app
from mmw.core import config, points
from mmw.core.pool import KIND_META, KIND_PKG, KIND_RELEASE, Pool

OLD = "http://old.test/debian"
NEW = "https://apt.test/debian"


def state(md, src, day):
    """`src`'s units as upstream published them on `day` (one Release a day)."""
    release = f"Suite: stable\nCodename: bookworm\nDate: Sun, {day:02d} Nov 2026 00:00:00 UTC\n".encode()
    with Pool(md) as pool:
        for unit in src.to_mirrors():
            files = [(f"pool/{unit.arch}-{day}.deb", f"{unit.arch}{day}".encode(), KIND_PKG),
                     ("dists/stable/Release", release, KIND_RELEASE)]
            index = "".join(f"Package: p\nFilename: {n}\n\n" for n, _b, k in files if k == KIND_PKG).encode()
            files.append((f"dists/stable/main/binary-{unit.arch}/Packages", index, KIND_META))
            entries = []
            for name, body, kind in files:
                cs = hashlib.sha256(body).hexdigest()
                pool.add(body, cs, "sha256", name.rsplit("/", 1)[-1], len(body))
                entries.append((cs, name, kind))
            pool.reconcile_refs(unit.name, entries, src.identity)
            pool.record_mirror(unit)


def synced(cfg, md, src, *days):
    for day in days:
        state(md, src, day)
        points.record_points_after_sync(cfg, md, {src.identity}, say=lambda _: None)


@pytest.fixture
def two(tmp_path):
    old = config.SourceConfig(url=OLD, name="old", suites=["stable"], components=["main"], arches=["amd64"])
    new = config.SourceConfig(url=NEW, name="new", suites=["stable"], components=["main"], arches=["amd64"])
    md = tmp_path / "mirror"
    net = config.NetworkConfig(name="work", nginx=False, certs=False,
                               select=[config.NetworkSelect(source=OLD)])
    cfg = config.Config(settings=config.Settings(mirror_dir=md), sources=[old, new], networks=[net])
    synced(cfg, md, old, 1, 2)          # the old URL: the 1st and the 2nd
    synced(cfg, md, new, 2, 3)          # the new one: the 2nd (the same Release) and the 3rd
    config.save(cfg, tmp_path)
    return cfg, md, tmp_path


def _points(md, repo):
    with Pool(md, read_only=True) as pool:
        return sorted(p.date[:8] for p in pool.repository_points(repo, stubs=True))


def test_merge_moves_history_and_dedupes(two):
    from mmw.core.repo_merge import merge_repositories, plan_merge
    cfg, md, home = two
    with Pool(md) as pool:
        plan = plan_merge(pool, cfg, OLD, NEW)
        assert plan.proof == "release" and plan.points == 2
        result = merge_repositories(pool, cfg, OLD, NEW, config_dir=home)
    assert result.moved == 1 and result.same == 1
    assert _points(md, NEW) == ["20261101", "20261102", "20261103"]
    assert _points(md, OLD) == []
    saved = config.load(home)
    assert [s.identity for s in saved.sources] == [NEW]
    assert saved.networks[0].select[0].source == NEW          # the network follows the repository


def test_merge_dry_run_changes_nothing(two):
    cfg, md, home = two
    before = (_points(md, OLD), _points(md, NEW))
    r = CliRunner().invoke(app, ["--config", str(home), "merge", "old", "new", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "2 point(s)" in r.output and "identical Release" in r.output
    assert (_points(md, OLD), _points(md, NEW)) == before
    assert len(config.load(home).sources) == 2


def test_merge_twice_is_already_one(two):
    cfg, md, home = two
    r = CliRunner().invoke(app, ["--config", str(home), "merge", "old", "new", "--yes"])
    assert r.exit_code == 0, r.output
    r = CliRunner().invoke(app, ["--config", str(home), "merge", OLD, NEW, "--yes"])
    assert r.exit_code == 0, r.output
    assert "already one repository" in r.output


def test_unrelated_repositories_are_refused(tmp_path):
    from mmw.core.repo_merge import merge_repositories
    a = config.SourceConfig(url=OLD, name="old", suites=["stable"], components=["main"], arches=["amd64"])
    b = config.SourceConfig(url=NEW, name="new", suites=["testing"], components=["main"], arches=["amd64"])
    md = tmp_path / "mirror"
    cfg = config.Config(settings=config.Settings(mirror_dir=md), sources=[a, b])
    synced(cfg, md, a, 1)
    with Pool(md) as pool:
        with pytest.raises(ValueError, match="nothing shows they are one repository"):
            merge_repositories(pool, cfg, OLD, NEW, config_dir=tmp_path)
    config.save(cfg, tmp_path)
    r = CliRunner().invoke(app, ["--config", str(tmp_path), "merge", "old", "new", "--yes"])
    assert r.exit_code == 1 and "nothing shows they are one repository" in r.output


def test_tui_merge_action_calls_the_same_function(two):
    import asyncio

    from mmw.tui.app import MmwApp
    from mmw.tui.pick import PickPage
    from mmw.tui.ui.dialog import ConfirmDialog
    from waiting import settle, until
    cfg, md, home = two

    async def go():
        tui = MmwApp(config.load(home), home, initial_section="repositories")
        async with tui.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            section = tui.screen
            await until(pilot, lambda: section._highlighted_source() is not None)
            while section._highlighted_source().identity != OLD:
                await pilot.press("down")
            await pilot.press("m")
            await until(pilot, lambda: isinstance(tui.screen, PickPage))
            tui.screen.query_one("#pick-list").select(f"new ({NEW})")
            await pilot.click("#btn-done")
            await until(pilot, lambda: isinstance(tui.screen, ConfirmDialog) and tui.screen.query("#confirm"))
            await pilot.click("#confirm")
            await until(pilot, lambda: _points(md, OLD) == [])
            await settle(pilot)
    asyncio.run(go())
    assert _points(md, NEW) == ["20261101", "20261102", "20261103"]
    assert [s.identity for s in config.load(home).sources] == [NEW]
