"""Where every repository stands, a repository's history, and checking the
served tree against what the site says it serves."""
from typer.testing import CliRunner

from mmw import cli
from mmw.site import ops

from test_site_apply import env  # noqa: F401
from test_site_ops import _three
from test_snapshot_repo_versions import _key
from test_dry_run_totals import two_mirrors  # noqa: F401


def test_status_and_history(env, tmp_path):  # noqa: F811
    cfg, home, root, _ = _three(env, tmp_path)
    ops.serve(home, "a", 2)
    st = {s.repo: s for s in ops.status(home)}
    assert st[_key(cfg, "a")].serving.number == 2 and st[_key(cfg, "a")].held
    assert st[_key(cfg, "b")].serving.number == 1 and not st[_key(cfg, "b")].held
    hist = ops.history(home, "a")
    assert [(v.number, serving) for v, _total, _frees, serving in hist] == [(3, False), (2, True), (1, False)]
    run = lambda *a: CliRunner().invoke(cli.app, ["site", *a])  # noqa: E731
    r = run("status")
    assert r.exit_code == 0 and "held" in r.output and "current" in r.output and "v3" in r.output
    r = run("history", "a")
    assert r.exit_code == 0 and "▶ v2" in r.output


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
