"""`mmw site adopt PATH`: report what is found, grouped by suite; ask where
each group goes and how to check it; adopt; `site status` says what wasn't
checked."""
import pytest
from typer.testing import CliRunner

from mmw import cli
from mmw.site import home as H
from mmw.site import recents

from apt_tree import make_tree
from test_site_adopt import _ring, upstream  # noqa: F401


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setattr(recents, "_FILE", tmp_path / "recents.json", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "srv"
    H.init_root(root, name="main", set_id="s1", network="oak")
    return root.resolve()


def run(*args):
    return CliRunner().invoke(cli.app, ["site", *args])


def test_cli_adopt_reports_and_adopts(site, tmp_path):
    make_tree(tmp_path / "www" / "debian", day=1)
    make_tree(tmp_path / "www" / "debian-new", day=2, packages={"hello_2.0_amd64.deb": b"two"})
    r = run("adopt", str(tmp_path / "www"), "--root", str(site), "--as", "deb.debian.org/debian",
            "--unverified", "--yes")
    assert r.exit_code == 0, r.output
    assert "Found 2 trees of Debian bookworm" in r.output
    assert "2026-11-01" in r.output and "2026-11-02" in r.output and "signature not checked" in r.output
    assert (site / "deb.debian.org/debian/pool/main/h/hello/hello_2.0_amd64.deb").exists()
    st = run("status", "--root", str(site))
    assert "signature not checked" in st.output


def test_cli_needs_as_for_a_new_repository(site, tmp_path):
    make_tree(tmp_path / "t")
    r = run("adopt", str(tmp_path / "t"), "--root", str(site), "--unverified", "--yes")
    assert r.exit_code == 1 and "--as HOST/PATH" in r.output


def test_cli_no_key_needs_key_or_unverified(site, tmp_path):
    make_tree(tmp_path / "t")
    r = run("adopt", str(tmp_path / "t"), "--root", str(site), "--as", "deb.debian.org/debian", "--yes")
    assert r.exit_code == 1 and "--key" in r.output and "--unverified" in r.output


def test_cli_with_a_key_verifies(site, tmp_path, upstream):  # noqa: F811
    make_tree(tmp_path / "t", sign_with=(upstream, "up"))
    key = tmp_path / "up.asc"
    key.write_text(upstream.public_armor("up"))
    r = run("adopt", str(tmp_path / "t"), "--root", str(site), "--as", "deb.debian.org/debian",
            "--key", str(key), "--yes")
    assert r.exit_code == 0, r.output
    assert "verified" in r.output and "signature not checked" not in run("status", "--root", str(site)).output


def test_cli_dry_run_changes_nothing(site, tmp_path):
    make_tree(tmp_path / "t")
    r = run("adopt", str(tmp_path / "t"), "--root", str(site), "--as", "deb.debian.org/debian",
            "--unverified", "--dry-run")
    assert r.exit_code == 0, r.output
    assert not (site / "deb.debian.org").exists()


def test_cli_strays_are_listed(site, tmp_path):
    make_tree(tmp_path / "t", extra={"pool/main/o/old/old_1_amd64.deb": b"o", "project/trace/x": b"t"})
    r = run("adopt", str(tmp_path / "t"), "--root", str(site), "--as", "deb.debian.org/debian",
            "--unverified", "--yes")
    assert r.exit_code == 0, r.output
    assert "not adopted: 1 pool file(s) no index names, 1 other file(s)" in r.output


def test_cli_nothing_found(site, tmp_path):
    (tmp_path / "empty").mkdir()
    r = run("adopt", str(tmp_path / "empty"), "--root", str(site))
    assert r.exit_code == 1 and "no APT trees" in r.output
