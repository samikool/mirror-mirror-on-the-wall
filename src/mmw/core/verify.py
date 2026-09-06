"""Mirror completeness verification against the pool.

The mirror's own pooled index (Packages.xz) is parsed as the
independent source of truth, and every listed checksum is checked against the
packages table — "does the pool physically hold the bytes this index
promises?". refs were reconciled from the same index, so this also catches
blob corruption/loss after the fact.
"""
from dataclasses import dataclass
from pathlib import Path

from mmw.core.compress import decompress
from mmw.core.config import MirrorConfig
from mmw.core.pool import Pool


@dataclass
class VerifyResult:
    mirror_name: str
    provider: str
    total: int = 0
    present: int = 0
    missing_count: int = 0
    error: str = ""

    @property
    def complete(self) -> bool:
        return not self.error and self.missing_count == 0


def verify_mirror(mirror: MirrorConfig, mirror_dir: Path, pool: Pool) -> VerifyResult:
    if mirror.provider == "apt":
        return _verify_apt(mirror, pool)
    if mirror.provider == "apt-flat":
        return _verify_apt_flat(mirror, pool)
    return VerifyResult(mirror.name, mirror.provider, error=f"unknown provider {mirror.provider!r}")


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
    arch = mirror.arch or "amd64"
    base = f"dists/{suite}/{component}/binary-{arch}/Packages"

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
