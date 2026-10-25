"""Spec §9's acceptance test: a real bundle made before v2 (a v1 manifest
with no sizes, mmw's runtime inside) applies with v2, untouched.

Opt-in: MMW_ACCEPTANCE_TAR=the .tar, MMW_ACCEPTANCE_DIR=a folder with room
for it. Ordinary pytest never runs it."""
import os
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

TAR = os.environ.get("MMW_ACCEPTANCE_TAR")
DIR = os.environ.get("MMW_ACCEPTANCE_DIR")
pytestmark = pytest.mark.skipif(not (TAR and DIR), reason="opt-in: MMW_ACCEPTANCE_TAR and MMW_ACCEPTANCE_DIR")


def test_real_v1_bundle_applies(monkeypatch):
    from mmw import cli
    from mmw.site.apply import plan_set_apply
    work = Path(DIR)
    monkeypatch.setenv("XDG_STATE_HOME", str(work / "state"))
    root = work / "root"
    run = lambda *a: CliRunner().invoke(cli.app, ["site", *a])  # noqa: E731

    started = time.monotonic()
    plan = plan_set_apply(None, TAR)
    planned = time.monotonic() - started
    print(f"\nplan: {len(plan.hosts)} hostname(s) {sorted(plan.hosts)} in {planned:.1f}s")
    assert plan.hosts and all(plan.sizes[h] > 0 for h in plan.hosts) and plan.network_bytes > 0
    assert planned < 120

    r = run("apply", TAR, "--dry-run", "--root", f"*={root}")
    print(r.output)
    assert r.exit_code == 0, r.output
    assert "free ✓" in r.output and not root.exists()

    started = time.monotonic()
    r = run("apply", TAR, "--root", f"*={root}")
    took = time.monotonic() - started
    print(r.output[-3000:])
    print(f"apply: {took:.0f}s, {Path(TAR).stat().st_size / took / 1e6:.0f} MB/s")
    assert r.exit_code == 0, r.output
    assert r.output.count("full → v1") == sum(len(v) for v in plan.hosts.values())
    assert r.output.count("carries an mmw runtime — ignored") == 1
    assert "start nginx in root" in r.output and "docker compose up -d" in r.output

    r = run("verify")
    assert r.exit_code == 0, r.output
    r = run("status")
    assert r.exit_code == 0 and "held" not in r.output, r.output

    store = root / ".mmw" / "store"
    blobs = sum(1 for _ in store.rglob("*"))
    started = time.monotonic()
    r = run("apply", TAR)
    print(f"second apply: {time.monotonic() - started:.0f}s")
    assert r.exit_code == 0, r.output
    assert r.output.count("already at v1") == sum(len(v) for v in plan.hosts.values())
    assert sum(1 for _ in store.rglob("*")) == blobs
