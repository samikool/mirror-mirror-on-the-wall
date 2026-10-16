# Incremental Reference Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for native execution or superpowers:subagent-driven-development if the user selects delegation. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce end-of-sync database writes by retaining unchanged mirror references while preserving an atomic, exact transition to the new desired state.

**Architecture:** Keep `Pool.reconcile_refs()` and its callers unchanged. Under the existing writer lock and an explicit SQLite transaction, read one mirror's references, compute the difference in memory, and write only removals, additions, and changed non-key fields. Both deferred and inline writers continue through this one method.

**Tech Stack:** Python >=3.11, standard-library sqlite3, SQLite WAL, pytest, existing background pool writer. No new dependency or database migration.

**Spec:** The agreed problem and proposed design are captured in [Design brief](#design-brief) below; this is a planning deliverable, not authorization to implement.

## Global Constraints

- Publish and bundle must produce the same deployable file tree unless the user explicitly requests a difference.
- Publishing must not automatically create snapshots. Publish snapshots remain manual; bundling current content automatically creates its snapshot.
- Preserve `reconcile_refs(mirror_name: str, entries: list[tuple[str, str, str]], source: str = "") -> None`.
- Preserve `submit_reconcile(mirror_name, entries, source, done) -> bool`, including its completion/error callback.
- Reconciliation remains a blocker to sync completion; acknowledge success only after the transaction commits.
- Readers see the complete old state or the complete new state, never a partial transition.
- References represent desired state, including checksums for packages not yet downloaded.
- Preserve the existing retry/backoff, metadata verification, and cancellation gates.
- Do not run benchmarks or migrations against the live mirror pool.

## Design brief

The user wants the final sync reference commit to avoid rewriting everything when most of a mirror is unchanged. Current `src/mmw/core/pool.py:reconcile_refs` flushes queued blob writes, deletes every reference for one mirror, reinserts the incoming list, and commits. The current end-to-end timer also includes time waiting for the deferred writer; a slow displayed commit does not prove that all of its duration is reference SQL.

For a fixed mirror, the actual key is `(checksum, repo_path)`. `kind` and `source` are mutable fields. Do not key solely by path: multiple checksums at one path, or one checksum at multiple paths, are supported by the current schema. Duplicate incoming keys use the first entry, matching the current `INSERT OR IGNORE` order. The `source` argument applies to every desired row, including when the argument is an empty string.

Recommended algorithm: one O(N) indexed read and in-memory comparison, followed by O(changed rows) persistent row mutations. An unchanged repeat sync performs no `INSERT`, `UPDATE`, or `DELETE` against `refs`. It still reads and compares the reference set, waits for earlier writer work, and completes a transaction; it is not constant-time or guaranteed to be disk-free.

Alternatives considered:

1. **In-memory difference (recommended):** uses existing indexes and avoids staging every row on the HDD. Costs additional Python memory proportional to one mirror's reference set.
2. **Temporary SQL staging table:** makes set operations natural, but staging every incoming row adds O(N) writes and can spill to the HDD. Reconsider only if measured memory use makes the recommended approach unsuitable.
3. **Incremental updates during downloads:** changes visibility and recovery semantics and risks publishing a partially updated mirror. Outside this change.

Expected benefit is greatest for unchanged and small-update syncs. First syncs still insert everything; full replacements still remove and insert everything. Keep those cases in the benchmark instead of promising a speedup for every workload.

## Review Focus

1. Missing package rows: keep desired references so verification still reports failed downloads (Task 1).
2. Aliases and conflicting duplicate keys: preserve composite-key identity and first-entry-wins behavior (Task 1).
3. Kind/source-only changes, including clearing source: update fields without silently retaining old metadata (Task 1).
4. A failure after some mutations: roll back the whole transaction and leave the connection usable (Task 2).
5. Concurrent readers and queued blob writes: preserve old-or-new visibility and writer completion ordering (Task 2).

## Files and responsibilities

- Modify `src/mmw/core/pool.py`: incremental reconciliation, explicit rollback, and current docstrings describing full replacement.
- Create `tests/test_reference_reconciliation.py`: exact-state, mutation-count, rollback, and concurrency tests.
- Retain `tests/test_pool_v2.py`: existing public behavior and cross-mirror isolation tests.
- Retain `tests/test_sync_safety.py` and `tests/test_interrupted_sync_resume.py`: metadata gates and resumability.
- Create `docs/tools/benchmark-reference-reconciliation.py`: reproducible temporary-pool comparison against the previous full-rewrite algorithm.
- Modify `docs/cli.md`: explain incremental reference commits and what the displayed time includes.
- Retain `src/mmw/core/downloader.py`'s existing reference commit timer and callback contract; no new public result type.

### Task 1: Differential writes with exact existing semantics

**Files:** Modify `src/mmw/core/pool.py`; create `tests/test_reference_reconciliation.py`.

**Interfaces:** Consumes the existing `Pool` connection, `_lock`, `flush_writes()`, and `(checksum, path, kind)` input. Produces the same `reconcile_refs(...)->None` behavior, with zero reference mutations for unchanged input.

- [ ] **Step 1: Add the failing unchanged-sync regression.** Use inline writes so unrelated queued package inserts cannot affect the mutation count:

```python
from mmw.core.pool import KIND_META, KIND_PKG, Pool

def test_unchanged_reconcile_does_not_rewrite_refs(tmp_path):
    with Pool(tmp_path, defer_writes=False) as pool:
        entries = [("a" * 64, "pool/a.deb", KIND_PKG),
                   ("b" * 64, "dists/Packages.xz", KIND_META)]
        pool.reconcile_refs("unit", entries, "https://example.test/repo")
        before = pool._conn.total_changes
        pool.reconcile_refs("unit", list(reversed(entries)), "https://example.test/repo")
        assert pool._conn.total_changes - before == 0
        assert pool.ref_map("unit") == {path: cs for cs, path, _ in entries}
```

- [ ] **Step 2: Run the new test.** `.venv/bin/pytest -q tests/test_reference_reconciliation.py`. Expect failure because the old code deletes and reinserts both references.

- [ ] **Step 3: Implement the in-memory difference.** Normalize incoming entries with `desired.setdefault((checksum, path), (kind, source))`. Do not silently discard missing-package references. Inside `_lock`, begin the transaction before reading:

```python
conn.execute("BEGIN IMMEDIATE")
old = {
    (cs, path): (kind, old_source)
    for cs, path, kind, old_source in conn.execute(
        "SELECT checksum, repo_path, kind, source FROM refs WHERE mirror_name = ?",
        (mirror_name,),
    )
}
removed = old.keys() - desired.keys()
added = desired.keys() - old.keys()
changed = [key for key in old.keys() & desired.keys() if old[key] != desired[key]]
```

Use `executemany` with these exact predicates and argument layouts; do not issue row mutations for unchanged keys:

```python
conn.executemany(
    "DELETE FROM refs WHERE checksum = ? AND mirror_name = ? AND repo_path = ?",
    ((cs, mirror_name, path) for cs, path in sorted(removed)),
)
conn.executemany(
    "UPDATE refs SET kind = ?, source = ? "
    "WHERE checksum = ? AND mirror_name = ? AND repo_path = ?",
    ((desired[key][0], desired[key][1], key[0], mirror_name, key[1])
     for key in sorted(changed)),
)
conn.executemany(
    "INSERT INTO refs (checksum, mirror_name, repo_path, kind, source) VALUES (?, ?, ?, ?, ?)",
    ((cs, mirror_name, path, desired[(cs, path)][0], desired[(cs, path)][1])
     for cs, path in sorted(added)),
)
```

Retain blob-writer draining before the transaction. Commit on success and explicitly roll back/re-raise on failure. Normalize the map once per reconcile; release temporary maps when the call ends. Do not add a schema index or enable foreign-key enforcement as part of this change.

- [ ] **Step 4: Expand the regression matrix.** Parameterize inline/deferred pools. Seed missing checksums directly as desired references, as existing tests do. Query full `(checksum, path, kind, source)` rows for assertions instead of using `ref_map()` where multiple checksums share a path. Required cases and exact expectations:

| Case | Expected result |
|---|---|
| First sync with two rows | Two inserted rows |
| Repeat with reordered input | Zero row mutations; identical state |
| Retain one, remove one, add one | Exactly two row mutations; retained row untouched |
| Same key, change kind | Exactly one update; new kind stored |
| Same keys, change source, then clear it | One update per affected row for each change; empty string stored |
| One checksum at two paths | Both rows retained independently |
| One path with two checksums | Both rows retained independently |
| Repeated identical key with conflicting kinds | First kind wins, matching current INSERT OR IGNORE |
| Missing package checksum | Reference retained and `missing_for()` reports it |
| Empty desired set | Only this mirror's rows removed; second mirror untouched |

For mutation-count assertions in deferred mode, call `pool.flush_writes()` before taking the baseline. Use `conn.total_changes` deltas, not elapsed-time assertions.

- [ ] **Step 5: Run** `.venv/bin/pytest -q tests/test_reference_reconciliation.py tests/test_pool_v2.py tests/test_sync_phase_performance.py` with normal localhost/asyncio access. Expect all tests to pass; investigate any changed existing semantics before committing.
- [ ] **Step 6: Commit** `perf(pool): reconcile only changed mirror references` with the pool change and its regression tests.

### Task 2: Prove atomic failure and writer ordering

**Files:** Extend `tests/test_reference_reconciliation.py`; modify rollback handling in `src/mmw/core/pool.py` if the tests expose a failure.

**Interfaces:** Consumes Task 1's unchanged public method and existing `submit_reconcile(..., done)` callback. Produces evidence that no reader sees a partial update and no callback reports success before commit.

- [ ] **Step 1: Add a failure-injection test.** Seed an old mirror with a row that will be removed; make insertion of its replacement abort with a connection-local trigger:

```sql
CREATE TEMP TRIGGER reject_new_ref BEFORE INSERT ON main.refs
WHEN NEW.repo_path = 'new.deb'
BEGIN
  SELECT RAISE(ABORT, 'injected reconcile failure');
END;
```

Call reconcile with the replacement. Assert an exception, the complete old row set, `not pool._conn.in_transaction`, and success on a subsequent reconcile after dropping the trigger. This catches the dangerous failure after deletion but before insertion.

- [ ] **Step 2: Add a deterministic concurrent-reader test.** Register a SQLite function on the writer connection that signals a `threading.Event` and waits on another event, with a five-second timeout. Invoke it through a temporary trigger during a changed reconcile. While the transaction is paused, read through a separate connection and assert the exact old row set; release the writer and assert the exact new set. Use `try/finally` to release the event even if an assertion fails. Reuse the existing concurrent reconcile test as additional coverage.
- [ ] **Step 3: Add a deferred-writer ordering test.** Queue `pool.add()` calls, then `submit_reconcile()`. Wait for its `done(error)` callback with a bounded event wait. Inside/after completion, open a separate read-only connection and assert both package rows and the complete desired ref set are visible. Repeat with the failure trigger: callback receives an error and old refs remain intact. Test `defer_writes=False` through the direct method as well.
- [ ] **Step 4: Run** `.venv/bin/pytest -q tests/test_reference_reconciliation.py tests/test_pool_v2.py tests/test_sync_safety.py tests/test_interrupted_sync_resume.py tests/test_pool_first_sync.py tests/test_sync_engine.py`. Expect atomic rollback, missing-ref detection, stop behavior, and resume behavior to remain correct.
- [ ] **Step 5: Commit** `test(pool): cover incremental reconcile atomicity and writer ordering` with any necessary rollback fix.

### Task 3: Benchmark representative changes and document the limits

**Files:** Create `docs/tools/benchmark-reference-reconciliation.py`; modify `docs/cli.md`.

**Interfaces:** Consumes the unchanged `Pool.reconcile_refs()` API. Produces a standalone benchmark with `--rows`, `--repeats`, and optional `--scratch-parent` arguments; it owns a `TemporaryDirectory` under the selected parent, never opens an existing pool, and prints machine-readable JSON.

- [ ] **Step 1: Implement both benchmark paths in the tool.** The baseline function must run the previous DELETE-all/INSERT-all transaction against a temporary pool using the same indexes and durability settings. The candidate calls the actual `Pool.reconcile_refs()`. Use `Pool(..., defer_writes=False)` for direct transaction measurements, restore the same initial state outside each timed trial, and close each pool after collecting results. Use synthetic checksum/path rows without allocating binary payloads; missing desired packages are a supported pool state.

```python
def full_rewrite(pool, mirror_name, entries, source):
    pool.flush_writes()
    with pool._lock:
        try:
            pool._conn.execute("DELETE FROM refs WHERE mirror_name = ?", (mirror_name,))
            pool._conn.executemany(
                "INSERT OR IGNORE INTO refs "
                "(checksum, mirror_name, repo_path, kind, source) VALUES (?, ?, ?, ?, ?)",
                [(cs, mirror_name, path, kind, source) for cs, path, kind in entries],
            )
            pool._conn.commit()
        except BaseException:
            pool._conn.rollback()
            raise

def generated_rows(count, offset=0):
    return [(f"{i:064x}", f"pool/{i}.deb", "pkg") for i in range(offset, offset + count)]

def replacement_target(initial, fraction):
    changed = int(len(initial) * fraction)
    return initial[changed:] + generated_rows(changed, offset=len(initial))
```

Seed each trial through `full_rewrite`, then checkpoint/truncate the WAL outside the timer so setup writes do not inflate the measured WAL size. For first-sync trials seed no references. Preserve the existing synchronous/WAL settings for both variants.
- [ ] **Step 2: Add workload generation with exact final-state verification.** For N initial rows generate: unchanged; 1% replacements; 10% replacements; full replacement; empty target; first sync; kind-only change; source-only change. A replacement removes one old composite key and adds one new key, keeping row count constant. Record elapsed time, persistent row mutation count via `total_changes`, allocated WAL bytes before the final checkpoint, row count, and peak process RSS where the platform supports it. Label WAL file size as an allocation measurement, not an exact physical-write counter. Assert baseline and candidate final rows are equal for every trial. Emit medians over at least three trials; do not enforce unstable timing assertions in pytest.
- [ ] **Step 3: Run** `.venv/bin/python docs/tools/benchmark-reference-reconciliation.py --rows 100000 --repeats 3`, then repeat with `--rows 500000`. If a disposable directory on the HDD is available, repeat with `--scratch-parent` pointing there; identify warm-cache and HDD results separately. Never pass the live mirror root as a database path.
- [ ] **Step 4: Measure the queued path separately.** Run the existing sync benchmark or a temporary synthetic sync with queued blob writes and record its existing `reference commit` duration. Explain that this includes queue/drain time, whereas the direct transaction benchmark isolates reconciliation. Investigate any material full-replacement or first-sync regression before shipping. Review peak memory at 500,000 rows before considering larger inputs; no universal speedup promise.
- [ ] **Step 5: Document** that unchanged refs are retained; completion still waits for successful transaction commit; initial/full-replacement syncs do substantial work; and existing stage timings include deferred-writer waiting. Preserve publish/bundle parity and snapshot semantics in all examples.
- [ ] **Step 6: Run final verification:**

```bash
env -u NO_COLOR .venv/bin/pytest -q -n 4 --dist worksteal
.venv/bin/ruff check src tests docs/tools/benchmark-reference-reconciliation.py
git diff --check
```

The suite needs normal localhost networking and asyncio worker wakeups; use the established test execution permissions. Existing deployment-parity and snapshot replay tests must pass. Do not refresh golden outputs simply to hide unrelated failures.

- [ ] **Step 7: Commit** `docs(perf): benchmark and explain incremental reference commits`. Report workload size, warm-cache/HDD conditions, before/after medians, write counts, memory, and any remaining queue bottleneck. Follow the user's chosen implementation workflow for branch review and publication.

## Acceptance criteria

- An unchanged mirror causes zero persistent ref-row mutations.
- A changed mirror ends with exactly the same references as the previous full-rewrite algorithm.
- Changed kind/source fields and composite-key aliases retain their current semantics.
- Missing blobs remain visible to completeness verification.
- Failure rolls back all ref mutations; concurrent readers observe old or new complete states.
- Both deferred and inline paths wait for successful commit before reporting sync completion.
- Benchmarks demonstrate reduced writes for unchanged/small-update syncs and disclose first-sync/full-replacement costs and memory use.
- Publish/bundle deployment parity and the manual/automatic snapshot distinction remain unchanged.

## Self-review

The three tasks cover the complete design brief and each Review Focus item. Public method signatures and callback types remain unchanged across tasks. No product code has been implemented by writing this plan. Benchmark output and regression tests are required evidence; the earlier VS Code completeness-verifier speedup is not evidence for this commit-stage change.
