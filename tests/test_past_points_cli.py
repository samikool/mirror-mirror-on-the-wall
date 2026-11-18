"""`mmw sync --as-of DATE --repo NAME` and `--from-url URL`: the CLI's past points."""
import pytest
from typer.testing import CliRunner

from mmw import cli
from mmw.core import config
from mmw.core import past_points as pp
from mmw.core.pool import Pool

from test_past_points_record import lab  # noqa: F401 — the fixture

URL = "https://snapshot.ubuntu.com/ubuntu/20261015T013713Z/"


@pytest.fixture
def setup(lab, monkeypatch):  # noqa: F811
    """`lab`'s config saved, and the Ubuntu archive served by its upstream
    (holding the older tree)."""
    config.save(lab.cfg(), lab.tmp)
    monkeypatch.setattr(cli, "_CFG_DIR", cli._CFG_DIR)
    monkeypatch.setattr(pp.Archive, "base", lambda self, name, stamp: lab.up.url)
    lab.older(("hello", "2.9-1"))
    return lab


def _invoke(lb, *args):
    return CliRunner().invoke(cli.app, ["--config", str(lb.tmp), "sync", *args])


def _past_dates(lb):
    with Pool(lb.mirror_dir, read_only=True) as pool:
        return sorted(p.date[:8] for t in pool.network_tracks("lab") if not t.repo.startswith("network:")
                      for p in pool.network_points("lab", t.id))


def test_as_of_with_repo(setup):
    result = _invoke(setup, "--quiet", "--as-of", "2026-10-15", "--repo", setup.repo)
    assert result.exit_code == 0, result.output
    assert "Found: " in result.output and "snapshot.ubuntu.com/ubuntu as of 2026-10-15 23:59:59 UTC" in result.output
    assert "Added: " in result.output and "jammy 2022-04-01" in result.output
    assert "files fetched" in result.output
    assert "20220401" in _past_dates(setup)


def test_repo_by_name(setup):
    name = setup.src.name
    result = _invoke(setup, "--quiet", "--as-of", "2026-10-15", "--repo", name)
    assert result.exit_code == 0, result.output
    assert "Added: " in result.output


def test_from_url_autodetects(setup):
    result = _invoke(setup, "--quiet", "--from-url", URL)
    assert result.exit_code == 0, result.output
    assert "Found: " in result.output and "Added: " in result.output
    assert "20220401" in _past_dates(setup)


def test_as_of_needs_a_repo(setup):
    result = _invoke(setup, "--quiet", "--as-of", "2026-10-15")
    assert result.exit_code == 1
    assert "--repo" in result.output


def test_repo_alone_is_a_usage_error(setup):
    result = _invoke(setup, "--quiet", "--repo", setup.repo)
    assert result.exit_code == 2


def test_third_party_repo_names_the_archives(setup, monkeypatch):
    from mmw.commands import past_points as cmd
    monkeypatch.setattr(cmd, "_fetch", lambda url: b"Origin: Docker\nLabel: Docker CE\nCodename: jammy\n")
    result = _invoke(setup, "--quiet", "--as-of", "2026-10-15", "--repo", setup.repo)
    assert result.exit_code == 1
    assert "snapshot.debian.org" in result.output and "snapshot.ubuntu.com" in result.output


def test_before_the_archive(setup):
    result = _invoke(setup, "--quiet", "--as-of", "2022-04-01", "--repo", setup.repo)
    assert result.exit_code == 1
    assert "the archive starts 2023-03-01" in result.output


def test_without_quiet_opens_the_tui_page(setup, monkeypatch):
    apps = []
    monkeypatch.setattr(cli, "_run_tui", lambda app: apps.append(app))
    result = _invoke(setup, "--as-of", "2026-10-15", "--repo", "x")
    assert result.exit_code == 0, result.output
    assert apps[0]._initial_section == "repositories"
    assert apps[0]._start_past == ("2026-10-15", None, "x")
    assert apps[0]._start_sync_selection is None


def test_a_source_no_network_publishes(setup):
    other = config.SourceConfig(url="https://download.example.com/linux/repo", suites=["bookworm"],
                                components=["main"], arches=["amd64"], name="extra")
    cfg = setup.cfg()
    config.save(cfg.model_copy(update={"sources": [setup.src, other],
                                       "networks": [n.model_copy(update={
                                           "select": [config.NetworkSelect(source=setup.src.url)]})
                                           for n in cfg.networks]}), setup.tmp)
    result = _invoke(setup, "--quiet", "--as-of", "2026-10-15", "--repo", "extra")
    assert result.exit_code == 1
    assert "no network publishes extra" in result.output


def test_record_run_says_why_nothing_was_recorded(setup):
    from mmw.commands.past_points import look, plan_run, record_run
    cfg = setup.cfg()
    run = plan_run(cfg, setup.mirror_dir, look(cfg, setup.mirror_dir, as_of="2026-10-15", repo=setup.repo))
    text, ok = record_run(cfg, run, {}, [], [0, 0], stopped=True)
    assert not ok and text.startswith("Stopped — nothing was recorded")
    text, ok = record_run(cfg, run, {}, ["http://x: 429"], [0, 0], stopped=False)
    assert not ok and "Nothing recorded" in text and "http://x: 429" in text
    assert "20220401" not in _past_dates(setup)
