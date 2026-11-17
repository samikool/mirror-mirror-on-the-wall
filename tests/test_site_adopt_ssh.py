"""Adopting over ssh: a `HOST:/PATH` folder, or a root over ssh, runs `mmw
site adopt` on that host, its lines streamed back — adopting links the files
into the root's store, so it runs where both are. The fake ssh plays the
remote here, so its paths are this machine's."""
import os
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mmw import cli
from mmw.site import home as H
from mmw.site import recents, ssh

from apt_tree import make_tree
from test_site_adopt import upstream  # noqa: F401

FAKE = Path(__file__).parent / "fake_ssh"
DEB = "deb.debian.org/debian/pool/main/h/hello/hello_1.0_amd64.deb"


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setattr(recents, "_FILE", tmp_path / "recents.json", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("PATH", f"{FAKE}:{Path(sys.executable).parent}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_REMOTE_HOME", str(tmp_path / "remote-home"))   # no ~/.local/bin/mmw there
    monkeypatch.setenv("FAKE_SSH_LOG", str(tmp_path / "ssh.log"))
    monkeypatch.delenv("FAKE_SSH_MODE", raising=False)
    ssh.forget_checked()
    root = tmp_path / "srv"
    H.init_root(root, name="main", set_id="s1", network="oak")
    return root.resolve()


def run(*args):
    return CliRunner().invoke(cli.app, ["site", *args])


def _ssh_log(tmp_path) -> str:
    return (tmp_path / "ssh.log").read_text(errors="replace")


def test_a_host_path_is_adopted_on_that_host(site, tmp_path):
    make_tree(tmp_path / "www" / "debian")
    r = run("adopt", f"farhost:{tmp_path / 'www'}", "--root", f"farhost:{site}",
            "--as", "deb.debian.org/debian", "--unverified", "--yes")
    assert r.exit_code == 0, r.output
    assert "On farhost:" in r.output and "Found 1 tree of Debian bookworm" in r.output
    assert "adopted " in r.output
    assert (site / DEB).exists()
    assert "site adopt" in _ssh_log(tmp_path)


def test_a_plain_folder_with_a_root_over_ssh_is_on_that_host(site, tmp_path):
    make_tree(tmp_path / "www" / "debian")
    r = run("adopt", str(tmp_path / "www"), "--root", f"farhost:{site}",
            "--as", "deb.debian.org/debian", "--unverified", "--yes")
    assert r.exit_code == 0, r.output
    assert "On farhost:" in r.output and (site / DEB).exists()


def test_dry_run_over_ssh_changes_nothing(site, tmp_path):
    make_tree(tmp_path / "www" / "debian")
    r = run("adopt", f"farhost:{tmp_path / 'www'}", "--root", f"farhost:{site}",
            "--as", "deb.debian.org/debian", "--unverified", "--dry-run")
    assert r.exit_code == 0, r.output
    assert "Found 1 tree" in r.output and not (site / DEB).exists()


def test_without_yes_and_no_terminal_it_asks_for_yes(site, tmp_path):
    make_tree(tmp_path / "www" / "debian")
    r = run("adopt", f"farhost:{tmp_path / 'www'}", "--root", f"farhost:{site}",
            "--as", "deb.debian.org/debian", "--unverified")
    assert r.exit_code == 1 and "--yes" in r.output and not (site / DEB).exists()


def test_a_key_file_here_goes_over_ssh(site, tmp_path, upstream):  # noqa: F811
    make_tree(tmp_path / "www" / "debian", sign_with=(upstream, "up"))
    key = tmp_path / "up.asc"
    key.write_text(upstream.public_armor("up"))
    r = run("adopt", f"farhost:{tmp_path / 'www'}", "--root", f"farhost:{site}",
            "--as", "deb.debian.org/debian", "--key", str(key), "--yes")
    assert r.exit_code == 0, r.output
    assert "verified" in r.output and "signature not checked" not in r.output
    assert "--key -" in _ssh_log(tmp_path).replace("', '", " ")   # the contents went, not the path


def test_a_host_path_into_a_root_on_another_machine_is_refused(site, tmp_path):
    r = run("adopt", f"farhost:{tmp_path / 'www'}", "--root", str(site), "--unverified", "--yes")
    assert r.exit_code == 1 and "both must be on one machine" in r.output
    assert not (tmp_path / "ssh.log").exists()


def test_a_missing_folder_says_so(site, tmp_path):
    r = run("adopt", str(tmp_path / "nowhere"), "--root", str(site), "--unverified", "--yes")
    assert r.exit_code == 1 and "doesn't exist on this machine" in r.output
    assert "no APT trees" not in r.output


def test_a_host_without_mmw_says_how_to_install(site, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", f"{FAKE}:/usr/bin:/bin")           # the remote has no mmw of its own
    ssh.forget_checked()
    r = run("adopt", f"farhost:{tmp_path / 'www'}", "--unverified", "--yes")
    assert r.exit_code == 1 and "isn't installed on farhost" in r.output and "--install" in r.output


def test_the_tui_adopts_into_a_root_over_ssh(site, tmp_path):
    from mmw.tui.site.adopt_flow import AdoptChoice, _run_adopt
    make_tree(tmp_path / "www" / "debian")
    c = AdoptChoice(str(tmp_path / "www"), "deb.debian.org/debian", None, True)
    code, lines = _run_adopt(f"farhost:{site}", c, dry_run=True)
    assert code == 0, lines
    assert "On farhost:" in lines and any("Found 1 tree" in x for x in lines)
    code, lines = _run_adopt(f"farhost:{site}", c, dry_run=False)
    assert code == 0, lines
    assert (site / DEB).exists()
