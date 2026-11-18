"""A legacy site's conversion keeps its control state and tells the operator once."""
import asyncio
import sqlite3

import pytest
from typer.testing import CliRunner

from mmw import cli
from mmw.site import home as H, ops, recents
from mmw.site.db import SiteDB
from mmw.site.mset import open_set
from mmw.site.roots import Roots
from mmw.site.timeline import migrate
from mmw.tui.site.app import SiteApp

from test_site_timeline import REPO, _put, _snap
from waiting import until


@pytest.fixture
def legacy(tmp_path):
    root = tmp_path / "srv"
    home = H.init_root(root, name="main", set_id="legacy", network="lab")
    roots = Roots(H.load_site(home))
    with SiteDB(home) as db:
        for n in (1, 2):
            repo = _snap(n)
            _put(roots.store(roots.primary), repo)
            vid = db.add_version(REPO, "debian", n, f"digest{n}", f"2026110{n}T120000Z", None,
                                 [repo.base], [(p, cs, len(repo.blobs[cs])) for p, cs in repo.rows.items()])
        db.set_serving(REPO, vid, held=True)
    recents.remember(str(root))
    return root, home


def backup(home):
    return home.with_name("site.before-timelines")


def test_status_backs_up_old_database_and_settings_and_says_so_once(legacy):
    root, home = legacy
    settings = (home / "site.toml").read_bytes()
    with SiteDB(home) as db:
        versions = [(v.id, db.version_files(v.id)) for v in db.versions(REPO)]
    result = CliRunner().invoke(cli.app, ["site", "status", "--root", str(root)])
    assert result.exit_code == 0, result.output
    dest = backup(home)
    assert dest.is_dir()
    assert (dest / "site.toml").read_bytes() == settings
    with sqlite3.connect(dest / "site.db") as db:
        assert not db.execute("SELECT value FROM meta WHERE key='timeline'").fetchone()
        assert db.execute("SELECT count(*) FROM points").fetchone()[0] == 0
    with SiteDB(dest) as db:
        assert [(v.id, db.version_files(v.id)) for v in db.versions(REPO)] == versions
    assert "converted to timelines (one-way)" in result.output
    assert str(dest) in result.output and "remove it once you're satisfied" in result.output
    again = CliRunner().invoke(cli.app, ["site", "status", "--root", str(root)])
    assert again.exit_code == 0
    assert "converted to timelines" not in again.output
    assert not (dest / "store").exists() and not (dest / "lock").exists()


def test_existing_backup_is_not_overwritten(legacy):
    _, home = legacy
    dest = backup(home)
    dest.mkdir()
    (dest / "keep").write_bytes(b"original backup")
    ops.status(home)
    assert (dest / "keep").read_bytes() == b"original backup"
    assert sorted(p.name for p in dest.iterdir()) == ["keep"]


def test_backup_failure_does_not_convert_or_leave_a_partial_backup(legacy, monkeypatch):
    from pathlib import Path
    from mmw.core import disk_io
    _, home = legacy

    def fail(src, dst, **kwargs):
        Path(dst).mkdir()
        (Path(dst) / "partial").write_text("incomplete")
        raise OSError("disk full")
    monkeypatch.setattr(disk_io.ops.shutil, "copytree", fail)
    with pytest.raises(OSError, match="disk full"):
        ops.status(home)
    assert not backup(home).exists()
    assert not list(home.parent.glob(".site.before-timelines-*"))
    assert not (home / "lock").exists()
    with SiteDB(home) as db:
        assert not db.get_meta("timeline")
        assert not db.tracks(REPO)


def test_fresh_site_does_not_claim_to_have_converted(tmp_path):
    home = H.init_root(tmp_path / "new", name="new", set_id="new", network="lab")
    ops.status(home)
    assert ops.converted_notice(home) is None
    assert not backup(home).exists()


def test_tidy_lists_backup_as_removable_without_deleting_it(legacy):
    root, home = legacy
    result = CliRunner().invoke(cli.app, ["site", "tidy", "--root", str(root)])
    assert result.exit_code == 0, result.output
    assert str(backup(home)) in result.output
    assert "removable" in result.output
    assert backup(home).is_dir()


def test_tui_open_shows_conversion_notice_once_and_uses_shared_command(legacy, monkeypatch):
    from mmw.commands import site as command
    _, home = legacy
    seen = []
    real = command.conversion_notices

    def spy(*args, **kwargs):
        seen.append(True)
        return real(*args, **kwargs)
    monkeypatch.setattr(command, "conversion_notices", spy)

    async def run(first):
        app = SiteApp(open_set())
        async with app.run_test(size=(120, 40)) as pilot:
            if first:
                await until(pilot, lambda: type(app.screen).__name__ == "ConfirmDialog" and app.screen.query("#confirm-message"))
                assert "converted to timelines (one-way)" in str(app.screen.query_one("#confirm-message").render())
                assert str(backup(home)) in str(app.screen.query_one("#confirm-message").render())
                app.screen.query_one("#confirm").press()
            await until(pilot, lambda: type(app.screen).__name__ == "SiteHomePage" and app.screen.query("#site-repos"))
            await app.workers.wait_for_complete()
            assert type(app.screen).__name__ == "SiteHomePage"
    asyncio.run(run(True))
    asyncio.run(run(False))
    assert seen


def test_migration_backs_up_each_local_root(legacy, tmp_path):
    _, home = legacy
    second_home = H.init_root(tmp_path / "second", name="second", set_id="legacy", network="lab")
    settings = H.load_site(home)
    settings.roots.append(H.load_site(second_home).primary)
    H.save_settings(home, settings)
    with SiteDB(home) as db:
        assert migrate(db, Roots(settings))
    assert backup(home).is_dir()
    assert (backup(second_home) / "site.toml").is_file()


def test_apply_also_backs_up_before_conversion(legacy):
    from mmw.site.apply import RootApply
    _, home = legacy
    with RootApply(home):
        assert backup(home).is_dir()
        with sqlite3.connect(backup(home) / "site.db") as old:
            assert old.execute("SELECT count(*) FROM points").fetchone()[0] == 0
    assert ops.converted_notice(home)


def test_read_only_apply_does_not_convert(legacy):
    from mmw.site.apply import RootApply
    _, home = legacy
    with RootApply(home, dry_run=True):
        pass
    assert not backup(home).exists()
    with SiteDB(home) as db:
        assert not db.get_meta("timeline")


def test_open_command_converts_and_reports_the_backup(legacy):
    root, home = legacy
    result = CliRunner().invoke(cli.app, ["site", "open", str(root)])
    assert result.exit_code == 0, result.output
    assert "converted to timelines (one-way)" in result.output
    assert backup(home).is_dir()


def test_failed_apply_backup_releases_its_lock(legacy, monkeypatch):
    from mmw.core import disk_io
    from mmw.site.apply import RootApply
    _, home = legacy

    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(disk_io.ops, "copy_tree_once", fail)
    with pytest.raises(OSError, match="disk full"):
        with RootApply(home):
            pass
    assert not (home / "lock").exists()


def test_tui_tidy_names_the_removable_backup(legacy):
    root, home = legacy
    CliRunner().invoke(cli.app, ["site", "status", "--root", str(root)])

    async def run():
        app = SiteApp(open_set())
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: type(app.screen).__name__ == "SiteHomePage" and app.screen.query("#btn-site-tidy"))
            await app.workers.wait_for_complete()
            app.screen.query_one("#btn-site-tidy").press()
            await until(pilot, lambda: type(app.screen).__name__ == "TidyRunPage" and app.screen.query("#run-done")
                        and "removable" in str(app.screen.query_one("#run-done").render()))
            assert str(backup(home)) in str(app.screen.query_one("#run-done").render())
    asyncio.run(run())
    assert backup(home).exists()


def test_remote_status_converts_there_and_returns_notice_once(legacy, tmp_path, monkeypatch):
    import os
    from pathlib import Path
    root, home = legacy
    monkeypatch.setenv("PATH", f"{Path(__file__).parent / 'fake_ssh'}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_REMOTE_HOME", str(tmp_path / "remote-home"))
    monkeypatch.setenv("FAKE_SSH_LOG", str(tmp_path / "ssh.log"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "remote-recents"))
    location = f"farhost:{root}"
    first = CliRunner().invoke(cli.app, ["site", "status", "--root", location])
    assert first.exit_code == 0, first.output
    assert "converted to timelines (one-way)" in first.output
    assert backup(home).is_dir()
    second = CliRunner().invoke(cli.app, ["site", "status", "--root", location])
    assert second.exit_code == 0, second.output
    assert "converted to timelines" not in second.output
    tidy = CliRunner().invoke(cli.app, ["site", "tidy", "--root", location])
    assert tidy.exit_code == 0, tidy.output
    assert str(backup(home)) in tidy.output and "removable" in tidy.output
    assert "farhost" in (tmp_path / "ssh.log").read_text()


def test_a_file_at_the_backup_path_refuses_conversion_without_overwriting(legacy):
    _, home = legacy
    backup(home).write_text("someone else's file")
    with pytest.raises(OSError, match="backup"):
        ops.status(home)
    assert backup(home).read_text() == "someone else's file"
    with SiteDB(home) as db:
        assert not db.get_meta("timeline")


def test_cli_reports_a_failed_backup_and_can_retry(legacy, monkeypatch):
    from mmw.core import disk_io
    root, home = legacy

    def fail(*args, **kwargs):
        raise OSError("disk full")
    with monkeypatch.context() as patch:
        patch.setattr(disk_io.ops, "copy_tree_once", fail)
        failed = CliRunner().invoke(cli.app, ["site", "status", "--root", str(root)])
        assert failed.exit_code == 1 and "disk full" in failed.output
    with SiteDB(home) as db:
        assert not db.get_meta("timeline")
    retried = CliRunner().invoke(cli.app, ["site", "status", "--root", str(root)])
    assert retried.exit_code == 0, retried.output
    assert "converted to timelines" in retried.output
