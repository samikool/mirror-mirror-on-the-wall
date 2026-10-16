#!/usr/bin/env python3
"""Compare full rewrites with incremental reference commits on disposable pools.

Run with the project's Python environment. --scratch-parent selects a filesystem,
never an existing database. stdout is JSON; progress goes to stderr. RSS includes
setup, validation, and earlier trials. SQLite/OS caches are not flushed.
"""
import argparse
import hashlib
from itertools import zip_longest
import json
from pathlib import Path
import statistics
import sys
import tempfile
from time import perf_counter

from mmw.core.pool import Pool


WORKLOADS = ("unchanged", "one-percent", "ten-percent", "full-replacement",
             "empty", "first-sync", "kind-only", "source-only")


def full_rewrite(pool, mirror_name, entries, source):
    """The previous algorithm, with the same connection and durability settings."""
    pool.flush_writes()
    with pool._lock:
        try:
            pool._conn.execute("DELETE FROM refs WHERE mirror_name = ?", (mirror_name,))
            pool._conn.executemany(
                "INSERT OR IGNORE INTO refs (checksum, mirror_name, repo_path, kind, source) VALUES (?, ?, ?, ?, ?)",
                [(cs, mirror_name, path, kind, source) for cs, path, kind in entries],
            )
            pool._conn.commit()
        except BaseException:
            pool._conn.rollback()
            raise


def generated_rows(count, offset=0):
    return [(f"{i:064x}", f"pool/{i}.deb", "pkg") for i in range(offset, offset + count)]


def workload(initial, name):
    if name == "empty":
        return initial, [], "repo"
    if name == "first-sync":
        return [], initial, "repo"
    if name == "kind-only":
        return initial, [(cs, path, "meta") for cs, path, _ in initial], "repo"
    if name == "source-only":
        return initial, initial, "new-repo"
    fraction = {"unchanged": 0, "one-percent": .01, "ten-percent": .1, "full-replacement": 1}[name]
    changed = int(len(initial) * fraction)
    return initial, initial[changed:] + generated_rows(changed, len(initial)), "repo"


def peak_rss_bytes():
    try:
        import resource
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return rss if sys.platform == "darwin" else rss * 1024
    except ImportError:
        return None


def trial(initial, target, source, method, scratch_parent):
    with tempfile.TemporaryDirectory(prefix="mmw-ref-bench-", dir=scratch_parent) as directory:
        with Pool(Path(directory), defer_writes=False) as pool:
            full_rewrite(pool, "bench", initial, "repo")
            pool._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            before = pool._conn.total_changes
            started = perf_counter()
            if method == "rewrite":
                full_rewrite(pool, "bench", target, source)
            else:
                pool.reconcile_refs("bench", target, source)
            elapsed = perf_counter() - started
            mutations = pool._conn.total_changes - before
            wal = pool.pool_dir / "mmw.db-wal"
            wal_bytes = wal.stat().st_size if wal.exists() else 0
            # Validate every field independently of the reconciliation's maps.
            expected = sorted((cs, path, kind, source) for cs, path, kind in target)
            actual = pool._conn.execute(
                "SELECT checksum, repo_path, kind, source FROM refs "
                "WHERE mirror_name = 'bench' ORDER BY checksum, repo_path")
            fingerprint = hashlib.sha256()
            for actual_row, expected_row in zip_longest(actual, expected):
                if actual_row != expected_row:
                    raise AssertionError((method, actual_row, expected_row))
                fingerprint.update(json.dumps(actual_row).encode())
                fingerprint.update(b"\n")
            return {"seconds": elapsed, "row_mutations": mutations, "wal_allocated_bytes": wal_bytes,
                    "final_rows": len(target), "final_state_sha256": fingerprint.hexdigest()}


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=positive_int, default=100000)
    parser.add_argument("--repeats", type=positive_int, default=3)
    parser.add_argument("--scratch-parent", type=Path)
    parser.add_argument("--workloads", nargs="+", choices=WORKLOADS, default=WORKLOADS)
    args = parser.parse_args()
    initial = generated_rows(args.rows)
    results = []
    for name in args.workloads:
        before, target, source = workload(initial, name)
        signatures = set()
        for method in ("rewrite", "incremental"):
            samples = []
            for repeat in range(args.repeats):
                print(f"{name}: {method} {repeat + 1}/{args.repeats}", file=sys.stderr, flush=True)
                sample = trial(before, target, source, method, args.scratch_parent)
                signatures.add(sample["final_state_sha256"])
                samples.append(sample)
            results.append({"method": method, "workload": name,
                            "median_seconds": statistics.median(s["seconds"] for s in samples),
                            "row_mutations": samples[0]["row_mutations"],
                            "median_wal_allocated_bytes": statistics.median(s["wal_allocated_bytes"] for s in samples),
                            "final_rows": samples[0]["final_rows"],
                            "final_state_sha256": samples[0]["final_state_sha256"],
                            "process_peak_rss_bytes": peak_rss_bytes()})
        if len(signatures) != 1:
            raise AssertionError(f"algorithms disagree for {name}")
    print(json.dumps({"rows": args.rows, "repeats": args.repeats,
                      "scratch_parent": str(args.scratch_parent or tempfile.gettempdir()),
                      "cache_state": "OS/SQLite caches not flushed; monotonic synthetic checksums",
                      "rss_scope": "process high-water mark includes setup, validation, and earlier trials",
                      "results": results}, indent=2))


if __name__ == "__main__":
    main()
