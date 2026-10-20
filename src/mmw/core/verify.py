"""Mirror completeness verification against the pool.

Two independent questions, because they fail differently:

**refs − packages** — "is there anything this mirror's index wants that the
pool cannot fill?"  This is the one that matters before a publish or a bundle:
`pool.mirror_files()` is `refs ⋈ packages`, so a ref with no blob is *invisible*
to the enumeration — it lands in neither a published tree nor a bundle's tar or
manifest, `apply.py --verify` passes, and the gap only surfaces as a 404 during
`apt install` on the far side of an airgap. It is answered entirely from the
database (`pool.missing_for`), so it is cheap enough to gate every publish.

**the pooled index** — the provider's witness (apt: its own `Packages` file) is parsed
as an independent witness and every checksum it lists is checked. refs were
reconciled from that same index, so agreement is expected; disagreement means
the refs and the index have drifted.

Neither of those opens a single blob, so neither can see a file deleted or
truncated underneath the pool. `stat_blobs=True` adds that: it stats every blob and
optionally (`hash_blobs`) re-hashes it. It is off by default because it is O(blobs) of disk
I/O — minutes on a multi-TB pool on one spindle — and making it implicit would
have quietly turned every publish pre-flight into a full pool scan.
"""
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter

from mmw.core import disk_io
from mmw.core.config import MirrorConfig
from mmw.core.format import fmt_timings
from mmw.core.pool import Pool

_STAT_REPORT_EVERY = 5000


@dataclass
class VerifyResult:
    mirror_name: str
    provider: str
    total: int = 0
    present: int = 0
    missing_count: int = 0
    error: str = ""
    # refs the pool cannot fill — the publish/bundle gate. Sampled for display;
    # `missing_refs_count` is the true total.
    missing_refs: list[tuple[str, str]] = field(default_factory=list)
    missing_refs_count: int = 0
    # stat_blobs only: blobs whose file is gone, the wrong size, or (with
    # `hash_blobs`) whose bytes no longer hash to their own checksum.
    corrupt: list[str] = field(default_factory=list)
    # The checksums behind `corrupt` — what `Pool.forget_blobs` (`mmw verify
    # --repair`) drops so the next sync downloads them again.
    corrupt_checksums: list[str] = field(default_factory=list)
    checked_blobs: int = 0
    timings: dict[str, float] = field(default_factory=dict)

    def timing_summary(self) -> str:
        return fmt_timings(self.timings)

    def describe(self) -> str:
        """What is wrong with an incomplete mirror, in one line — shared by
        `mmw verify` and the publish/bundle pre-flight, which used to print
        "0 of N missing" for a mirror short only of refs or damaged blobs."""
        parts = []
        if self.missing_count:
            parts.append(f"{self.missing_count:,} of {self.total:,} missing")
        if self.missing_refs_count:
            parts.append(f"{self.missing_refs_count:,} wanted file(s) not in the pool")
        if self.corrupt:
            parts.append(f"{len(self.corrupt):,} damaged blob(s)")
        return ", ".join(parts) or "incomplete"

    @property
    def complete(self) -> bool:
        return (
            not self.error
            and self.missing_count == 0
            and self.missing_refs_count == 0
            and not self.corrupt
        )


def verify_mirror(
    mirror: MirrorConfig,
    mirror_dir: Path,
    pool: Pool,
    stat_blobs: bool = False,
    hash_blobs: bool = False,
    say=None,
    stop_event=None,
) -> VerifyResult:
    """Check that the pool can actually serve everything this mirror promises.

    `stat_blobs` stats every blob; `hash_blobs` (which implies it) also re-reads and
    hashes them. Both are off by default — see the module docstring. `say(text)`
    names each slow step as it starts. The blob stats run on a job's read lane;
    a set `stop_event` ends them with `error == "stopped"`.
    """
    from mmw.providers import get_provider
    try:
        provider = get_provider(mirror.provider)
    except ValueError:
        return VerifyResult(
            mirror.name, mirror.provider, error=f"unknown provider {mirror.provider!r}"
        )
    started = perf_counter()
    result = provider.verify_witness(mirror, pool, say=say, stop_event=stop_event) or VerifyResult(mirror.name, mirror.provider)
    if not result.timings:
        result.timings["witness"] = perf_counter() - started

    if say:
        say("checking every wanted file is pooled…")
    started = perf_counter()
    _check_refs(pool, mirror.name, result)
    result.timings["refs"] = perf_counter() - started
    if stat_blobs or hash_blobs:
        with disk_io.DiskJob("verify", stop_event=stop_event) as job:
            started = perf_counter()
            _check_blobs(pool, mirror.name, result, hash_blobs=hash_blobs, job=job, say=say)
            result.timings["blobs"] = perf_counter() - started
        for phase, seconds in job.stats.timings().items():     # the witness may have its own
            result.timings[phase] = result.timings.get(phase, 0.0) + seconds
        if job.stop_event is not None and job.stop_event.is_set():
            result.error = "stopped"
    return result


def _check_refs(pool: Pool, mirror_name: str, result: VerifyResult) -> None:
    """refs − packages: what the index wants that the pool does not hold.

    This is the accessor the completeness gate is built on, and it covers
    every ref kind — source packages from `include_sources` and all metadata
    refs included — where parsing the binary `Packages` index alone sees only
    binary packages.
    """
    missing = pool.missing_for(mirror_name)
    result.missing_refs_count = len(missing)
    result.missing_refs = missing[:20]


def blob_stats(rows: Iterable, job: "disk_io.DiskJob", *, path_of: Callable,
               on_progress: Callable[[int], None] | None = None,
               every: int | None = None) -> Iterator[tuple]:
    """(row, os.stat_result | OSError) for each row's blob, in row order, the
    stats on `job`'s read lane — many in flight let a spinning disk order its
    inode reads (2.2x at 16, measured). Shared by `mmw verify --stat` and the
    bundle pre-flight. `on_progress(done)` fires every `every` rows and once
    at the end."""
    every = every or _STAT_REPORT_EVERY
    done = 0
    for row, st in job.read_map(lambda r: disk_io.ops.stat(path_of(r)), rows):
        done += 1
        if on_progress and done % every == 0:
            on_progress(done)
        if isinstance(st, Exception) and not isinstance(st, OSError):
            raise st
        yield row, st
    if on_progress and done % every:
        on_progress(done)


def _check_blobs(pool: Pool, mirror_name: str, result: VerifyResult,
                 hash_blobs: bool, job: "disk_io.DiskJob", say=None) -> None:
    """Does the pool physically hold these bytes, and are they still right?

    The database says a blob exists; only the filesystem knows whether it
    still does. A blob deleted out from under the pool, or truncated by a full
    disk, is invisible to every other check here.
    """
    rows = pool.mirror_files([mirror_name])
    total = len(rows)
    progress = (lambda done: say(f"stat {done:,}/{total:,} blobs…")) if say else None
    to_hash: list[tuple[str, str, str]] = []
    for (repo_path, pool_path, size, _kind, checksum), st in blob_stats(
            rows, job, path_of=lambda r: r[1], on_progress=progress):
        if isinstance(st, OSError):
            result.corrupt.append(f"{repo_path}: blob missing from pool ({pool_path})")
            result.corrupt_checksums.append(checksum)
            continue
        result.checked_blobs += 1
        if size and st.st_size != size:
            result.corrupt.append(
                f"{repo_path}: blob is {st.st_size} bytes, index says {size}"
            )
            result.corrupt_checksums.append(checksum)
            continue
        if hash_blobs and checksum:
            to_hash.append((repo_path, pool_path, checksum))
    if not to_hash:
        return
    # The re-hash streams the blobs in order on this thread, the next ones
    # opened ahead (`disk_readahead`) — never several read at once.
    if say:
        say(f"hashing {len(to_hash):,} blobs…")
    done = 0
    for (repo_path, _pool_path, checksum), (_p, digest) in zip(
            to_hash, job.hash_files(Path(r[1]) for r in to_hash)):
        done += 1
        if say and (done % _STAT_REPORT_EVERY == 0 or done == len(to_hash)):
            say(f"hash {done:,}/{len(to_hash):,} blobs…")
        if isinstance(digest, Exception):
            result.corrupt.append(f"{repo_path}: unreadable ({digest})")
            result.corrupt_checksums.append(checksum)
        elif digest != checksum:
            result.corrupt.append(f"{repo_path}: content no longer matches its checksum")
            result.corrupt_checksums.append(checksum)


def _resolve_first(pool: Pool, mirror_name: str, candidates: list[str]) -> Path | None:
    for repo_path in candidates:
        p = pool.resolve(mirror_name, repo_path)
        if p is not None and disk_io.ops.exists(p):
            return p
    return None


def _check_checksums(pool: Pool, result: VerifyResult, checksums: list[str]) -> VerifyResult:
    result.total = len(checksums)
    present = pool.batch_contains(checksums)
    result.present = sum(1 for c in checksums if c in present)
    result.missing_count = result.total - result.present
    return result
