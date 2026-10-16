# Execution record: incremental reference reconciliation

Native implementation with one fresh final review. All work was verified in an isolated checkout.

[Measurements and limitations](../../internals/reference-reconciliation-benchmarks.md).

# SDD ledger — plan: docs/superpowers/plans/2026-10-15-incremental-reference-reconciliation.md
Execution: native; isolated /tmp/mmw-incremental-refs; baseline 9e4c6d0.
Spec: design brief embedded in approved plan.
Pre-flight Task 1 -> Task 2: reconcile_refs and submit_reconcile signatures unchanged; compatible.
Pre-flight Task 1 -> Task 3: same Pool API and SQLite settings; compatible.
Tasks: 1 complete; 2 complete; 3 complete.
Task 1: complete (commits 9e4c6d0..2f37c57, tests: env -u NO_COLOR PYTHONPATH=/tmp/mmw-incremental-refs/src /12-tb/rpm-mirror/.venv/bin/pytest -q tests/test_reference_reconciliation.py tests/test_pool_v2.py tests/test_sync_phase_performance.py → 41 passed in 10.49s)
Task 2: complete (commits 2f37c57..e1edba8, tests: env -u NO_COLOR PYTHONPATH=/tmp/mmw-incremental-refs/src /12-tb/rpm-mirror/.venv/bin/pytest -q tests/test_reference_reconciliation.py tests/test_pool_v2.py tests/test_sync_safety.py tests/test_interrupted_sync_resume.py tests/test_pool_first_sync.py tests/test_sync_engine.py → 94 passed in 38.39s)
Task 3: Ruling: include the benchmark script in source distributions — its new CLI test is shipped with tests/** and the build proved the script was absent — cost if wrong: one small extra source-archive file.
Task 3: Ruling: use bulk deletion only when every old reference is removed, and bypass difference construction for empty/first-sync targets — the first 100k benchmark showed avoidable whole-replacement overhead — cost if wrong: an incorrect all-removed predicate could rewrite retained rows; mutation-count and isolation tests cover it.
Task 3: Ruling: replace the prior commit's monolithic checksum sort with small sorted runs plus a Python merge — both unchanged dev and this branch failed the existing 120ms UI guard; a 200k global sort alone measured 125ms holding the GIL — cost if wrong: extra Python merge CPU, guarded by membership, deduplication, and responsiveness tests. No timing thresholds changed.
Final review: fresh gpt-6-astra reviewer, range 9e4c6d0..07e9845. No Critical/Important/Minor findings. Independently passed 200 randomized state/mutation/isolation transitions and rollback after update+deletion before failed insertion.
Final: Ruling: performance claims remain limited to the documented warm-cache scratch measurements through 500k refs — live/cold-cache behavior was not measured — cost if wrong: an inaccurate prediction of user-side speed; no universal speed promise is made.
Final: Ruling: preserve existing deferred-writer timeout/error policies — normal sync's FIFO writer ordering is tested; revising legacy timeout policy is a separate behavior change — cost if wrong: existing long-wait limitations remain.
Final: Ruling: keep the declared string/list API without adding coercion for unsupported inputs — matches current callers and approved scope — cost if wrong: untyped external callers still receive errors.
Final: Ruling: parent owns full deployment/TLS/snapshot verification — reviewer inspected unchanged integration boundaries; full regression suite must pass before integration — cost if wrong: an integration regression could escape, so publication remains gated on the suite.
Task 3: complete (commits e1edba8..07e9845, tests: env -u NO_COLOR PYTHONPATH=/tmp/mmw-incremental-refs/src /12-tb/rpm-mirror/.venv/bin/pytest -q -n 4 --dist worksteal → 1920 passed, 1 skipped in 242.16s (0:04:02))

Final verification: 1920 passed, 1 skipped; 31 snapshots passed; source-archive benchmark smoke 1 passed; ruff and git diff --check clean.
Regression evidence: initial write-count tests RED (12 failed/2 passed), then Task 1 GREEN 41; all-removed SQL tests RED 4, then GREEN 43; benchmark CLI RED missing tool then GREEN; source archive RED missing script then GREEN; existing responsiveness guards RED on dev and feature branch, then GREEN 62 without threshold changes.
Final review: no findings to fix; no deferred minors.
