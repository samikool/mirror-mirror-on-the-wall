"""APT consistency checks shared by recording, verification and previews."""
import lzma
import posixpath
from dataclasses import dataclass

from mmw.core.apt_index import release_body
from mmw.core.compress import decompress
from mmw.site.tracks import _indexes, _referenced, release_date


@dataclass(frozen=True)
class PointProblem:
    repo: str
    suite: str
    date: str
    problems: list[str]


def newest_release(variants: dict[str, list[str]], read) -> str:
    """Choose a trust-root checksum by its UTC Date, then by checksum.

    `read(checksum)` reads a blob: a conflicting path has several contents.
    Missing/invalid dates sort before valid ones, deterministically.
    """
    choices = {cs for path, checksums in variants.items()
               if path.rsplit('/', 1)[-1] in ('Release', 'InRelease') for cs in checksums}
    def order(cs):
        text = release_body(read(cs) or b'').decode('utf-8', 'replace')
        return release_date(text, '')[0], cs
    return max(choices, key=order)


def release_mismatches(files: dict[str, str], track: str, read) -> list[str]:
    """The cheap half of `check_suite`: present indexes against the Release
    alone (one read), for listings that mark every point."""
    from mmw.providers.apt import _parse_release_files
    release = next((data for name in ('Release', 'InRelease')
                    if (path := f'{track}/{name}') in files and (data := read(path))), None)
    if release is None:
        return ['Release is missing or unreadable']
    problems = []
    for checksum, path, _ in _parse_release_files(release.decode('utf-8', 'replace')):
        canonical = f'{track}/{path}'
        by_hash = posixpath.join(posixpath.dirname(canonical), 'by-hash/SHA256', checksum)
        if canonical in files and files[canonical] != checksum and files.get(by_hash) != checksum:
            problems.append(f'{path} does not match its Release')
    return problems


def check_suite(files: dict[str, str], track: str, read) -> list[str]:
    """Check present indexes against the Release and their package references.

    A narrowed suite may omit indexes. A canonical index with a different
    digest is safe only when this Release's by-hash copy is also retained.
    `read(path)` reads a path within this immutable point.
    """
    if not track:
        return []
    problems = release_mismatches(files, track, read)
    if problems == ['Release is missing or unreadable']:
        return problems
    base = track.split('/dists/', 1)[0] if '/dists/' in track else track
    for stem, variants in _indexes(files, track).items():
        for path in variants:
            data = read(path)
            if data is None:
                continue
            try:
                lines = decompress(data, path).decode('utf-8', 'replace').splitlines()
                names = _referenced(lines, stem.rsplit('/', 1)[-1], base)
            except (OSError, EOFError, lzma.LZMAError, ValueError):
                continue
            problems.extend(f'{p[len(base) + 1:]} is missing' for p in sorted(names) if p not in files)
            break
        else:
            problems.append(f'{variants[0][len(track) + 1:]} is unreadable')
    return problems
