# Incremental reference commit measurements

Measured October 15, 2026, using the standalone benchmark in
`docs/tools/benchmark-reference-reconciliation.py`. Each number is the median
of three trials on a newly created disposable pool with identical indexes and
SQLite settings. No live mirror database or binary payload was used.

Checksums are monotonic synthetic values. OS caches were not flushed; these
are warm-cache filesystem measurements, not cold-drive or end-to-end download
predictions. Other tests and benchmarks ran concurrently on this machine.

## 100,000 references on the HDD

The scratch filesystem was ext4 on `/dev/sdc` (rotational drive). Times include
the direct reference transaction, not waiting in the deferred writer queue.

| Workload | Rewrite seconds | Incremental seconds | Rewrite row mutations | Incremental row mutations |
|---|---:|---:|---:|---:|
| Unchanged | 1.458 | 0.429 | 200,000 | 0 |
| 1% replaced | 1.623 | 0.507 | 200,000 | 2,000 |
| 10% replaced | 1.518 | 0.692 | 200,000 | 20,000 |
| Fully replaced | 1.557 | 2.046 | 200,000 | 200,000 |
| Empty target | 1.215 | 1.172 | 100,000 | 100,000 |
| First sync | 1.632 | 1.471 | 100,000 | 100,000 |
| Kind-only change | 1.801 | 2.302 | 200,000 | 100,000 |
| Source-only change | 1.498 | 2.016 | 200,000 | 100,000 |

Allocated WAL size for unchanged input fell from 32,057,752 bytes to zero;
for 1% replacement it fell from 32,061,872 bytes to 490,312 bytes. WAL allocation
is not an exact count of physical bytes written.

## 500,000 references on tmpfs

| Workload | Rewrite seconds | Incremental seconds | Rewrite row mutations | Incremental row mutations |
|---|---:|---:|---:|---:|
| Unchanged | 4.146 | 2.117 | 1,000,000 | 0 |
| 1% replaced | 4.040 | 2.128 | 1,000,000 | 10,000 |
| 10% replaced | 4.170 | 2.593 | 1,000,000 | 100,000 |
| Fully replaced | 4.322 | 5.723 | 1,000,000 | 1,000,000 |
| Empty target | 1.588 | 1.519 | 500,000 | 500,000 |
| First sync | 2.801 | 2.479 | 500,000 | 500,000 |
| Kind-only change | 3.520 | 4.189 | 1,000,000 | 500,000 |
| Source-only change | 3.580 | 4.226 | 1,000,000 | 500,000 |

Peak process RSS across the whole 500,000-row run was approximately 635 MiB.
This includes fixture creation, validation, and earlier trials; it does not
isolate reconciliation's memory use. The 100,000-row tmpfs run peaked near
145 MiB and reduced unchanged reconciliation from 0.729s to 0.332s.

## Queued sync check

A separate single-run sync with 20,000 pooled package paths and 2,000 metadata
files exercised the normal background writer. The displayed reference-commit
timer changed from 0.184s to 0.074s on the cached repeat. First-sync observations
were 0.129s and 0.177s. These are illustrative single runs, not medians, and
include writer waiting and scheduling overhead.

## Interpretation

The intended improvement is repeat syncs with few changes: persistent writes
scale with changed references, although reading and comparing still scales
with the full mirror. First-sync and empty-target fast paths avoid unnecessary
difference construction. When every old key is removed, one bulk deletion is
used without rewriting any retained row.

Full replacements and changes to every kind/source field can be slower despite
equal or fewer row mutations, because comparison and per-row SQL add CPU work.
The measurements above retain those costs rather than hiding them. No schema
fingerprint, staging-table writes, relaxed durability setting, or early success
acknowledgment was introduced to avoid that cost.

To reproduce:

```bash
python docs/tools/benchmark-reference-reconciliation.py --rows 100000 --repeats 3
python docs/tools/benchmark-reference-reconciliation.py --rows 500000 --repeats 3
python docs/tools/benchmark-reference-reconciliation.py --rows 100000 --repeats 3 --scratch-parent /path/on/hdd
```

The tool verifies every resulting reference field and requires both algorithms
to produce the same final-state digest for each workload.
