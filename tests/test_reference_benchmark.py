"""The benchmark runs both algorithms and verifies comparable final state."""
import json
from pathlib import Path
import subprocess
import sys


def test_reference_benchmark_reports_real_write_reductions():
    script = Path(__file__).resolve().parents[1] / "docs/tools/benchmark-reference-reconciliation.py"
    result = subprocess.run([sys.executable, str(script), "--rows", "200", "--repeats", "1"],
                            capture_output=True, text=True, check=True, timeout=30)
    report = json.loads(result.stdout)
    results = {(r["method"], r["workload"]): r for r in report["results"]}
    assert len(results) == 16
    assert results["rewrite", "unchanged"]["row_mutations"] == 400
    assert results["incremental", "unchanged"]["row_mutations"] == 0
    assert results["incremental", "one-percent"]["row_mutations"] == 4
    assert results["incremental", "ten-percent"]["row_mutations"] == 40
    assert results["incremental", "full-replacement"]["row_mutations"] == 400
    for workload in ("first-sync", "kind-only", "source-only", "empty"):
        assert results["incremental", workload]["row_mutations"] == 200
    for workload in {r["workload"] for r in report["results"]}:
        assert results["rewrite", workload]["final_state_sha256"] == results["incremental", workload]["final_state_sha256"]
