import hashlib
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

    def fetch_all(self, mirror_config, client: httpx.Client, status_callback=None):
        """One fetch of the index, handed to both halves.

        A flat repository has no Release checksum block, so there is no
        snapshot for the two views to disagree *about* — the consistency half
        of `AptProvider.fetch_all` does not apply here. What does apply is the
        second half: the index was GET twice per sync, once to enumerate and
        once for the metadata phase to pool.

        The fallback matters. `fetch_metadata` discovers variants with HEAD
        while `fetch_packages` uses GET, and some CDNs answer 405 to HEAD — so
        a repository that mirrors fine today would become a hard failure if
        enumeration were keyed off the HEAD result alone. When no usable
        metadata entry exists, this drops back to the GET loop unchanged.
        """
        base = mirror_config.source_url.rstrip("/")
        files = self.fetch_metadata(mirror_config, client)

        by_path = {f.repo_path: f for f in files}
        for suffix in (".xz", ".gz", ""):
            mf = by_path.get(f"Packages{suffix}")
            if mf is None:
                continue
            try:
                resp = client.get(mf.url)
            except Exception:
                continue
            if resp.status_code != 200:
                continue
            mf.content = resp.content
            text = _decompress(resp.content, mf.repo_path).decode("utf-8", errors="replace")
            return _parse_packages_flat(text, base), files

        # HEAD found nothing usable — enumerate the old way and let the
        # metadata phase fetch whatever it can find for itself.
        return self.fetch_packages(mirror_config, client, status_callback), files

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
                    files.append(MetadataFile(url=f"{base}/{name}", repo_path=name,
                                              is_release=True))
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


def _fetch_verified_index(mf: MetadataFile, client: httpx.Client) -> bytes:
    """Fetch one Release-listed index file and check it against its own hash.

    Tries the by-hash URL first — its path *is* the checksum, so it keeps
    serving the snapshot we read even after upstream republishes — and falls
    back to the canonical URL once. Deliberately not the five-retry backoff
    `downloader._fetch_by_hash` runs: that exists for the metadata phase, which
    happens hours later and may be racing a republish. Here the Release was
    read moments ago, so a by-hash miss means CDN propagation lag and the
    canonical answer settles it immediately.
    """
    attempts: list[str] = []
    for url in (mf.url, mf.fallback_url):
        if not url:
            continue
        try:
            resp = client.get(url)
        except Exception as e:
            attempts.append(f"{url}: {type(e).__name__}")
            continue
        if resp.status_code != 200:
            attempts.append(f"{url}: http {resp.status_code}")
            continue
        content = resp.content
        if mf.expected_sha256 and hashlib.sha256(content).hexdigest() != mf.expected_sha256:
            attempts.append(f"{url}: checksum mismatch")
            continue
        return content
    raise ValueError(f"could not fetch {mf.repo_path} — " + "; ".join(attempts))


def _pick_index(files: list[MetadataFile], suffix_path: str) -> MetadataFile | None:
    """The best compressed variant of one index, by repo_path.

    `.xz` then `.gz` then uncompressed: Ubuntu *lists* the uncompressed
    `Packages` in its Release block but does not serve it, so preferring it
    would 404 every time.
    """
    for ext in (".xz", ".gz", ""):
        for mf in files:
            if mf.repo_path.endswith(suffix_path + ext):
                return mf
    return None


class AptProvider(BaseProvider):

    def fetch_all(self, mirror_config, client: httpx.Client, status_callback=None):
        """Packages and metadata from ONE upstream snapshot, and one fetch.

        The inherited default calls `fetch_packages()` then `fetch_metadata()`,
        which is wrong here in two separate ways:

        **Two snapshots.** `fetch_packages` GETs the canonical
        `…/binary-{arch}/Packages.xz` directly while `fetch_metadata` reads
        `InRelease` and verifies indexes against *that*. Ubuntu republishes
        `-updates`/`-security` several times a day and `-proposed` constantly,
        so the two GETs can straddle a republish — and then the index we
        *publish* (verified against InRelease) lists packages that were never
        enumerated into refs, never downloaded, and 404 for every client. The
        stale-snapshot re-fetch in `download_mirror` does not save this: it
        re-verifies *metadata* against the current snapshot, while package refs
        still come from the original enumeration.

        **Two downloads of the same file.** The Packages index is the largest
        metadata file in a mirror, and it was fetched once to enumerate and
        again to pool.

        Both go away by reading the Release once, fetching the index it
        *names* (by hash), and handing the same bytes to both halves.

        The bytes are held on `MetadataFile.content` until the metadata phase,
        which runs after the package downloads — so for a long sync they stay
        resident. That is a few MB per mirror (Ubuntu noble `main` is ~1.6 MB
        compressed, `universe` ~9 MB), against a package list for the same
        mirror that is already far larger, so it is the cheaper half of what a
        sync holds.
        """
        base = mirror_config.source_url.rstrip("/")
        component = mirror_config.component or "main"
        arch = mirror_config.arch

        # Raises RuntimeError naming the reason when the Release is unreadable.
        # Called first so that failure is what the caller sees, rather than a
        # generic "Could not fetch Packages file" from a package fetch that was
        # only ever going to fail for the same underlying reason.
        files = self.fetch_metadata(mirror_config, client)

        packages: list[PackageInfo] = []
        wanted = [(f"{component}/binary-{arch}/Packages", _parse_packages)]
        if getattr(mirror_config, "include_sources", False):
            wanted.append((f"{component}/source/Sources", _parse_sources))

        for suffix, parse in wanted:
            mf = _pick_index(files, suffix)
            if mf is None:
                if parse is _parse_sources:
                    continue        # deb-src is optional; absence is not an error
                raise ValueError(
                    f"{base}: the Release file for suite '{mirror_config.suite}' "
                    f"lists no {suffix} — is component '{component}' or arch "
                    f"'{arch}' served by this repository?"
                )
            if status_callback:
                status_callback(f"Reading {mf.repo_path}...")
            content = _fetch_verified_index(mf, client)
            # Handed to the metadata phase as-is: `_fetch_metadata_content`
            # takes pre-fetched content and re-verifies it against
            # `expected_sha256`, so this saves the GET without skipping a check.
            mf.content = content
            text = _decompress(content, mf.repo_path).decode("utf-8", errors="replace")
            packages += parse(text, base)

        return packages, files

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
        # Why every failure is recorded rather than `continue`d: this loop is
        # the only thing that produces `release_text`, and the caller reports a
        # per-file problem purely by *omission*. So a 503 on both InRelease and
        # Release — which Ubuntu really does return under load — used to yield
        # an empty metadata list that `download_mirror` treated as a clean
        # index, reconciling the mirror's refs down to packages alone and
        # deleting every index and release ref it had, at exit 0. A missing
        # Release file is a hard failure for an apt repository, and the reason
        # has to travel with it.
        release_failures: list[str] = []
        for name in ("InRelease", "Release", "Release.gpg"):
            url = f"{dist_base}/{name}"
            try:
                r = client.get(url)
            except Exception as e:
                release_failures.append(f"{name}: {type(e).__name__}")
                continue
            if r.status_code != 200:
                release_failures.append(f"{name}: http {r.status_code}")
                continue
            files.append(MetadataFile(url=url, repo_path=f"dists/{suite}/{name}",
                                      content=r.content, is_release=True))
            if name in ("InRelease", "Release") and not release_text:
                release_text = r.text
                release_checksums = {path: cs for cs, path, _ in _parse_release_files(release_text)}

        if not release_text:
            # Raising, not returning a short list: an apt repository always has
            # a Release file, so "I could not read it" means either the
            # repository is unreachable or the suite is wrong — never "this
            # mirror legitimately has no index". The sync frontends treat a
            # provider exception as a failed mirror and report it; an empty
            # return was indistinguishable from success.
            raise RuntimeError(
                f"no usable Release file at {dist_base} — "
                + ("; ".join(release_failures) if release_failures else "nothing served")
            )

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
