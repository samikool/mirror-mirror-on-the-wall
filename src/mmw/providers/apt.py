import re
from pathlib import PurePosixPath

import httpx

from mmw.core.compress import decompress as _decompress
from mmw.providers.base import BaseProvider, MetadataFile, PackageInfo


def _parse_release_files(text: str) -> list[tuple[str, str, int]]:
    """
    Parse the SHA256 block from a Release/InRelease file.
    Returns list of (checksum, path, size).
    """
    results = []
    in_sha256 = False
    for line in text.splitlines():
        if line.startswith("SHA256:"):
            in_sha256 = True
            continue
        if in_sha256:
            if not line.startswith(" "):
                break
            parts = line.strip().split()
            if len(parts) == 3:
                checksum, size, path = parts
                results.append((checksum, path, int(size)))
    return results


def _parse_packages(text: str, base_url: str) -> list[PackageInfo]:
    packages = []
    block: dict[str, str] = {}

    def _flush():
        filename = block.get("Filename", "")
        sha256 = block.get("SHA256", "")
        size = int(block.get("Size", 0))
        name = block.get("Package", "")
        if filename and sha256:
            url = base_url.rstrip("/") + "/" + filename
            packages.append(PackageInfo(
                name=name,
                url=url,
                checksum=sha256,
                checksum_type="sha256",
                size=size,
                repo_path=filename,
                filename=PurePosixPath(filename).name,
            ))

    for line in text.splitlines():
        if line == "":
            _flush()
            block = {}
        elif line.startswith((" ", "\t")):
            continue  # continuation line — never a field, and may contain ": "
        elif ": " in line:
            key, _, value = line.partition(": ")
            block[key.strip()] = value.strip()
    _flush()
    return packages


def _parse_sources(text: str, base_url: str) -> list[PackageInfo]:
    """Parse a Sources file and return one PackageInfo per source file listed."""
    packages = []
    block: dict[str, str] = {}
    sha256_lines: list[str] = []
    in_sha256 = False

    def _flush():
        directory = block.get("Directory", "")
        name = block.get("Package", "")
        for line in sha256_lines:
            parts = line.strip().split()
            if len(parts) == 3:
                checksum, size_str, filename = parts
                repo_path = f"{directory}/{filename}"
                packages.append(PackageInfo(
                    name=name,
                    url=base_url.rstrip("/") + "/" + repo_path,
                    checksum=checksum,
                    checksum_type="sha256",
                    size=int(size_str),
                    repo_path=repo_path,
                    filename=filename,
                ))

    for line in text.splitlines():
        if line == "":
            _flush()
            block = {}
            sha256_lines = []
            in_sha256 = False
        elif line.startswith("Checksums-Sha256:"):
            in_sha256 = True
        elif in_sha256 and line.startswith(" "):
            sha256_lines.append(line)
        else:
            in_sha256 = False
            if line.startswith((" ", "\t")):
                continue  # continuation of some other multi-line field
            if ": " in line:
                key, _, value = line.partition(": ")
                block[key.strip()] = value.strip()
    _flush()
    return packages


def _fetch_sources_file(url: str, base_url: str, client: httpx.Client) -> list[PackageInfo]:
    for fmt in (".xz", ".gz", ""):
        try:
            resp = client.get(url + fmt)
            if resp.status_code == 200:
                text = _decompress(resp.content, url + fmt).decode("utf-8", errors="replace")
                return _parse_sources(text, base_url)
        except Exception:
            continue
    return []  # sources are optional — don't raise if missing


def _fetch_dist_upgrader_files(dist_base: str, component: str, suite: str, client: httpx.Client) -> list[MetadataFile]:
    """
    Walk dist-upgrader-all/current/ via HTTP directory listing and return MetadataFile
    entries for every file found there.  These files are NOT listed in the Release SHA256
    block so they can only be discovered this way.  Silently returns [] if absent.
    """
    dir_url = f"{dist_base}/{component}/dist-upgrader-all/current/"
    try:
        resp = client.get(dir_url)
        if resp.status_code != 200:
            return []
        files = []
        for match in re.finditer(r'href="([^"?#][^"]*)"', resp.text):
            name = match.group(1).rstrip("/")
            if "/" in name or name.startswith(".") or name.startswith("?"):
                continue
            url = f"{dir_url}{name}"
            repo_path = f"dists/{suite}/{component}/dist-upgrader-all/current/{name}"
            files.append(MetadataFile(url=url, repo_path=repo_path))
        return files
    except Exception:
        return []


def _fetch_packages_file(url: str, base_url: str, client: httpx.Client) -> list[PackageInfo]:
    for fmt in (".xz", ".gz", ""):
        try:
            resp = client.get(url + fmt)
            if resp.status_code == 200:
                text = _decompress(resp.content, url + fmt).decode("utf-8", errors="replace")
                return _parse_packages(text, base_url)
        except Exception:
            continue
    raise ValueError(f"Could not fetch Packages file from {url}")


def _parse_packages_flat(text: str, base_url: str) -> list[PackageInfo]:
    """Like _parse_packages but strips leading ./ from Filename fields (flat repo convention)."""
    packages = []
    block: dict[str, str] = {}

    def _flush():
        filename = block.get("Filename", "").lstrip("./")
        sha256 = block.get("SHA256", "")
        size = int(block.get("Size", 0))
        name = block.get("Package", "")
        if filename and sha256:
            url = base_url.rstrip("/") + "/" + filename
            packages.append(PackageInfo(
                name=name,
                url=url,
                checksum=sha256,
                checksum_type="sha256",
                size=size,
                repo_path=filename,
                filename=PurePosixPath(filename).name,
            ))

    for line in text.splitlines():
        if line == "":
            _flush()
            block = {}
        elif line.startswith((" ", "\t")):
            continue  # continuation line — never a field, and may contain ": "
        elif ": " in line:
            key, _, value = line.partition(": ")
            block[key.strip()] = value.strip()
    _flush()
    return packages


class AptFlatProvider(BaseProvider):
    """APT flat/trivial repository: Packages file at the root, no dists/ hierarchy.

    Used for repos whose sources.list entry has suite '/' — e.g.:
      deb https://nvidia.github.io/libnvidia-container/stable/deb/amd64 /
    Set source_url to the full per-arch URL including arch component.
    """

    def fetch_packages(self, mirror_config, client: httpx.Client, status_callback=None) -> list[PackageInfo]:
        base = mirror_config.source_url.rstrip("/")
        for suffix in (".xz", ".gz", ""):
            try:
                url = f"{base}/Packages{suffix}"
                resp = client.get(url)
                if resp.status_code == 200:
                    text = _decompress(resp.content, url).decode("utf-8", errors="replace")
                    return _parse_packages_flat(text, base)
            except Exception:
                continue
        raise ValueError(f"Could not fetch Packages from {base}/Packages[.xz/.gz]")

    def fetch_metadata(self, mirror_config, client: httpx.Client) -> list[MetadataFile]:
        base = mirror_config.source_url.rstrip("/")
        files: list[MetadataFile] = []
        for name in ("InRelease", "Release", "Release.gpg"):
            try:
                resp = client.head(f"{base}/{name}")
                if resp.status_code == 200:
                    files.append(MetadataFile(url=f"{base}/{name}", repo_path=name))
            except Exception:
                pass
        for suffix in (".xz", ".gz", ""):
            name = f"Packages{suffix}"
            try:
                resp = client.head(f"{base}/{name}")
                if resp.status_code == 200:
                    files.append(MetadataFile(url=f"{base}/{name}", repo_path=name))
            except Exception:
                pass
        return files


# Every arch Debian/Ubuntu have ever shipped — used to exclude other-arch index
# variants by token so e.g. arch="mips" can never substring-match "mipsel".
_KNOWN_ARCHES = {
    "amd64", "arm64", "armhf", "armel", "i386", "ppc64el", "s390x", "riscv64",
    "mipsel", "mips64el", "mips", "powerpc", "ppc64", "sparc", "sparc64",
    "ia64", "hppa", "alpha", "m68k", "sh4", "x32", "hurd-i386", "hurd-amd64",
    "kfreebsd-i386", "kfreebsd-amd64",
}


def _wanted_index_file(path: str, component: str, other_arches: set[str], include_sources: bool) -> bool:
    """Decide whether a Release-listed index file belongs to this mirror.

    Includes everything in our component subtree (Packages, Translation-*,
    cnf/Commands-*, dep11/*, Contents-*, debian-installer, …) plus suite
    top-level files, excluding other-arch variants and — unless include_sources
    — the source/ index. Matching is segment/token based: substring matching
    would let component "free" match "non-free" and arch "mips" match "mipsel".
    """
    segs = path.split("/")
    in_component = segs[0] == component
    is_top_level = len(segs) == 1
    if not (is_top_level or in_component):
        return False
    if in_component and len(segs) > 1 and segs[1] == "source" and not include_sources:
        return False
    tokens = set(re.split(r"[/.\-]", path))
    return not (tokens & other_arches)


class AptProvider(BaseProvider):

    def fetch_packages(self, mirror_config, client: httpx.Client, status_callback=None) -> list[PackageInfo]:
        base = mirror_config.source_url.rstrip("/")
        suite = mirror_config.suite
        component = mirror_config.component or "main"
        arch = mirror_config.arch

        packages_base = f"{base}/dists/{suite}/{component}/binary-{arch}/Packages"
        packages = _fetch_packages_file(packages_base, base, client)

        if getattr(mirror_config, "include_sources", False):
            sources_base = f"{base}/dists/{suite}/{component}/source/Sources"
            packages += _fetch_sources_file(sources_base, base, client)

        return packages

    def fetch_metadata(self, mirror_config, client: httpx.Client) -> list[MetadataFile]:
        base = mirror_config.source_url.rstrip("/")
        suite = mirror_config.suite
        component = mirror_config.component or "main"
        arch = mirror_config.arch
        dist_base = f"{base}/dists/{suite}"

        files: list[MetadataFile] = []

        # Fetch InRelease, Release, AND Release.gpg with content= in one pass so
        # all three describe the same upstream snapshot.  Fetching Release at
        # write time (hours later) could capture a newer publish whose hashes
        # don't match the indexes we verified against InRelease.
        release_text = ""
        release_checksums: dict[str, str] = {}  # path → sha256
        for name in ("InRelease", "Release", "Release.gpg"):
            url = f"{dist_base}/{name}"
            try:
                r = client.get(url)
            except Exception:
                continue
            if r.status_code != 200:
                continue
            files.append(MetadataFile(url=url, repo_path=f"dists/{suite}/{name}", content=r.content))
            if name in ("InRelease", "Release") and not release_text:
                release_text = r.text
                release_checksums = {path: cs for cs, path, _ in _parse_release_files(release_text)}

        other_arches = _KNOWN_ARCHES - {arch}
        include_sources = getattr(mirror_config, "include_sources", False)
        if release_text:
            for _checksum, path, _size in _parse_release_files(release_text):
                if not _wanted_index_file(path, component, other_arches, include_sources):
                    continue
                sha256 = release_checksums.get(path)
                canonical_url = f"{dist_base}/{path}"
                # Fetch via by-hash so CDN propagation lag can't cause a mismatch.
                # by-hash dir sits alongside the file: parent/by-hash/SHA256/<hash>
                file_parent = PurePosixPath(path).parent
                hash_base = f"{dist_base}/{file_parent}" if str(file_parent) != "." else dist_base
                by_hash_url = f"{hash_base}/by-hash/SHA256/{sha256}"
                files.append(MetadataFile(
                    url=by_hash_url,
                    repo_path=f"dists/{suite}/{path}",
                    expected_sha256=sha256,
                    fallback_url=canonical_url,
                ))

        # dist-upgrader-all/ is not in the Release SHA256 block; walk it directly.
        # Only present under the "main" component — skip silently for others.
        if component == "main":
            files += _fetch_dist_upgrader_files(dist_base, component, suite, client)

        # Mirror changelogs.ubuntu.com for dist-upgrade support — declared on the
        # source (`dist_upgrade = true`), not inferred from a distro name. Both
        # files are identical regardless of suite, so last-write-wins across
        # multiple mirrors.
        if component == "main" and getattr(mirror_config, "dist_upgrade", False):
            for name in ("meta-release", "meta-release-lts"):
                # repo_path prefix marks these as network-root-relative for publish
                files.append(MetadataFile(
                    url=f"https://changelogs.ubuntu.com/{name}",
                    repo_path=f"changelogs.ubuntu.com/{name}",
                ))

        return files
