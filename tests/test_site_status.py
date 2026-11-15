"""Where every repository stands, a repository's history, and checking the
served tree against what the site says it serves."""
from typer.testing import CliRunner

from mmw import cli
from mmw.site import ops

from test_site_apply import env  # noqa: F401
from site_points import nth
from test_site_ops import _three
from test_dry_run_totals import two_mirrors  # noqa: F401


def test_status_and_history(env, tmp_path):  # noqa: F811
    cfg, home, root, _ = _three(env, tmp_path)
    second = nth(home, "a", 2)
    ops.serve(home, "a", second)
    ((_t, rows),) = ops.history(home, "a")
    assert [mark for _p, _total, _frees, mark in rows] == ["previous", "serving", ""]
    run = lambda *a: CliRunner().invoke(cli.app, ["site", *a])  # noqa: E731
    r = run("status")
    assert r.exit_code == 0 and "pinned" in r.output and "current" in r.output and "files 20" in r.output
    r = run("history", "a")
    assert r.exit_code == 0 and "files" in r.output
    assert any(line.startswith("▶") and second in line for line in r.output.splitlines())


def test_verify_clean_then_detects_tamper(env, tmp_path):  # noqa: F811
    cfg, home, root, _ = _three(env, tmp_path)
    assert ops.verify(home) == []
    victim = next((root / "example.com/a").rglob("*.deb"))
    victim.unlink()
    victim.write_bytes(b"tampered")
    (root / "example.com/b/b.deb").unlink()
    problems = ops.verify(home)
    assert any(victim.name in p and "differs" in p for p in problems)
    assert any("b.deb" in p and "missing" in p for p in problems)
    r = CliRunner().invoke(cli.app, ["site", "verify"])
    assert r.exit_code == 4
