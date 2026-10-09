"""Mirror completeness verification against the pool.

Two independent questions, because they fail differently:

**refs − packages** — "is there anything this mirror's index wants that the
pool cannot fill?"  This is the one that matters before a publish or a bundle:
`pool.mirror_files()` is `refs ⋈ packages`, so a ref with no blob is *invisible*
to the enumeration — it lands in neither a published tree nor a bundle's tar or
manifest, `apply.py --verify` passes, and the gap only surfaces as a 404 during
`apt install` on the far side of an airgap. It is answered entirely from the
database (`pool.missing_for`), so it is cheap enough to gate every publish.

**the pooled index** — the mirror's own `Packages` file is parsed as an
independent witness and every checksum it lists is checked. refs were
reconciled from that same index, so agreement is expected; disagreement means
the refs and the index have drifted.

Neither of those opens a single blob, so neither can see a file deleted or
truncated underneath the pool. `deep=True` adds that: it stats every blob and
optionally re-hashes it. It is off by default because it is O(blobs) of disk
I/O — minutes on a multi-TB pool on one spindle — and making it implicit would
have quietly turned every publish pre-flight into a full pool scan.
"""
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from mmw.core.compress import decompress
from mmw.core.config import MirrorConfig
from mmw.core.pool import Pool

_HASH_CHUNK = 1 << 20


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
    # deep mode only: blobs whose file is gone, the wrong size, or (with
    # `rehash`) whose bytes no longer hash to their own checksum.
    corrupt: list[str] = field(default_factory=list)
    checked_blobs: int = 0

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
    deep: bool = False,
    rehash: bool = False,
) -> VerifyResult:
    """Check that the pool can actually serve everything this mirror promises.

    `deep` stats every blob; `rehash` (which implies `deep`) also re-reads and
    hashes them. Both are off by default — see the module docstring.
    """
    if mirror.provider == "apt":
        result = _verify_apt(mirror, pool)
    elif mirror.provider == "apt-flat":
        result = _verify_apt_flat(mirror, pool)
    else:
        return VerifyResult(
            mirror.name, mirror.provider, error=f"unknown provider {mirror.provider!r}"
        )

    _check_refs(pool, mirror.name, result)
    if deep or rehash:
        _check_blobs(pool, mirror.name, result, rehash=rehash)
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


def _check_blobs(pool: Pool, mirror_name: str, result: VerifyResult, rehash: bool) -> None:
    """Does the pool physically hold these bytes, and are they still right?

    The database says a blob exists; only the filesystem knows whether it
    still does. A blob deleted out from under the pool, or truncated by a full
    disk, is invisible to every other check here.
    """
    for repo_path, pool_path, size, _kind, checksum in pool.mirror_files([mirror_name]):
        p = Path(pool_path)
        try:
            st = p.stat()
        except OSError:
            result.corrupt.append(f"{repo_path}: blob missing from pool ({pool_path})")
            continue
        result.checked_blobs += 1
        if size and st.st_size != size:
            result.corrupt.append(
                f"{repo_path}: blob is {st.st_size} bytes, index says {size}"
            )
            continue
        if rehash and checksum:
            h = hashlib.sha256()
            try:
                with p.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(_HASH_CHUNK), b""):
                        h.update(chunk)
            except OSError as e:
                result.corrupt.append(f"{repo_path}: unreadable ({e})")
                continue
            if h.hexdigest() != checksum:
                result.corrupt.append(f"{repo_path}: content no longer matches its checksum")


def _resolve_first(pool: Pool, mirror_name: str, candidates: list[str]) -> Path | None:
    for repo_path in candidates:
        p = pool.resolve(mirror_name, repo_path)
        if p is not None and p.exists():
            return p
    return None


def _check_checksums(pool: Pool, result: VerifyResult, checksums: list[str]) -> VerifyResult:
    result.total = len(checksums)
    present = pool.batch_contains(checksums)
    result.present = sum(1 for c in checksums if c in present)
    result.missing_count = result.total - result.present
    return result


# ── APT ───────────────────────────────────────────────────────────────────────

def _verify_apt(mirror: MirrorConfig, pool: Pool) -> VerifyResult:
    result = VerifyResult(mirror.name, mirror.provider)
    suite = mirror.suite or ""
    component = mirror.component or ""
    # No `or "amd64"`. An arch-less `apt` mirror is a bug upstream of here, and
    # inventing one sent verify at `binary-amd64/Packages`, which does not
    # exist — reporting "has this mirror been synced?" for what is really a
    # wrong assumption. design-notes.md's rule is "never invent an architecture";
    # this was the last place in core/ that still did.
    if not mirror.arch:
        result.error = (
            "mirror has no architecture — an apt mirror is one arch per sync unit, "
            "so this row cannot be verified (re-sync to repopulate it)"
        )
        return result
    base = f"dists/{suite}/{component}/binary-{mirror.arch}/Packages"

    pkg_file = _resolve_first(pool, mirror.name, [base + ".xz", base + ".gz", base])
    if pkg_file is None:
        result.error = "Packages index not in pool — has this mirror been synced?"
        return result

    try:
        checksums = _parse_apt_sha256(pkg_file)
    except Exception as e:
        result.error = f"failed to parse Packages file: {e}"
        return result

    return _check_checksums(pool, result, checksums)


def _verify_apt_flat(mirror: MirrorConfig, pool: Pool) -> VerifyResult:
    result = VerifyResult(mirror.name, mirror.provider)
    pkg_file = _resolve_first(pool, mirror.name, ["Packages.xz", "Packages.gz", "Packages"])
    if pkg_file is None:
        result.error = "Packages index not in pool — has this mirror been synced?"
        return result

    try:
        checksums = _parse_apt_sha256(pkg_file)
    except Exception as e:
        result.error = f"failed to parse Packages file: {e}"
        return result

    return _check_checksums(pool, result, checksums)


def _parse_apt_sha256(path: Path) -> list[str]:
    text = decompress(path.read_bytes(), path.name).decode("utf-8", errors="replace")
    return [line[8:].strip() for line in text.splitlines() if line.startswith("SHA256: ")]
