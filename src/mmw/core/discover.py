"""Probing an apt repository over HTTP.

Everything here is keyed on a **repository URI** (host + path), never on a
distro catalog. Nothing in this module
holds state between calls: what a repository publishes is re-read every time it
is asked for, so a suite or architecture appearing upstream simply shows up.
"""
import re
from dataclasses import dataclass, field

import httpx

from mmw.core.config import KNOWN_ARCHES

# `binary-all` is Debian's arch-independent index, not an architecture.
_APT_ARCH_DIR = re.compile(r"^binary-(?!all$)([a-z0-9][a-z0-9-]*)$")


@dataclass
class DetectResult:
    provider: str | None  # "apt" | "apt-flat"; None = unreachable or not a repo
    suite: str | None = None
    components: list[str] = field(default_factory=list)
    arches: list[str] = field(default_factory=list)
    suites: list[str] = field(default_factory=list)
    # Subdirectories to browse into when the URL is a host root rather than a
    # repository: archive.ubuntu.com/ holds ubuntu/, packages.microsoft.com/
    # holds a few dozen. Offered, never followed automatically.
    candidates: list[str] = field(default_factory=list)
    signal: str = ""  # human-readable description shown in the TUI
    error: str = ""   # why detection failed (provider is None)


def detect_repository(url: str) -> DetectResult:
    """Probe a URL and classify the repository.

    Only apt shapes are recognised. Anything else returns provider=None; if the
    URL turns out to be a host root or an intermediate directory, its listable
    subdirectories come back in `candidates` so the caller can offer them.
    Browsing is one level at a time and user-driven — an unrecognised URL is
    never followed automatically into a crawl.
    """
    base = url.rstrip("/")
    last_error = ""
    try:
        with httpx.Client(timeout=8, follow_redirects=True) as client:
            # Flat repository: a release file — or, unsigned, just the index —
            # at the root, no dists/ tree. InRelease alone used to be checked,
            # so a flat repository with only `Release`, or an unsigned one
            # (`deb [trusted=yes] … ./`), read as "not an apt repository".
            for name in ("InRelease", "Release", "Packages.xz", "Packages.gz", "Packages"):
                try:
                    r = client.get(f"{base}/{name}")
                except Exception as e:
                    last_error = str(e) or type(e).__name__
                    break               # unreachable — the next names are too
                if r.status_code == 200 and _looks_like(name, r.content):
                    result = DetectResult(provider="apt-flat",
                                          signal=f"APT flat  ({name} at root)")
                    if name in ("InRelease", "Release"):
                        _parse_release_headers(r.text, result)
                    return result

            # Standard repository: a dists/ directory.
            try:
                r = client.get(f"{base}/dists/")
                if r.status_code == 200:
                    suites = _listed_dirs(r.text)
                    if suites:
                        tail = ", ".join(suites[:4]) + ("…" if len(suites) > 4 else "")
                        result = DetectResult(
                            provider="apt",
                            suite=suites[0],
                            suites=suites,
                            signal=f"APT  (dists/ — {len(suites)} suite(s): {tail})",
                        )
                        try:
                            rel = client.get(f"{base}/dists/{suites[0]}/InRelease")
                            if rel.status_code == 200:
                                _parse_release_headers(rel.text, result)
                        except Exception:
                            pass
                        return result
                last_error = last_error or f"no dists/ (HTTP {r.status_code})"
            except Exception as e:
                last_error = str(e) or type(e).__name__
    except Exception as e:
        last_error = str(e) or type(e).__name__

    # Not a repository — but it may be the directory holding several.
    candidates = [d for d in _list_dirs(base + "/") if d not in ("dists", "pool")]
    return DetectResult(
        provider=None,
        candidates=candidates,
        error=last_error or "not an apt repository",
    )


def _looks_like(name: str, body: bytes) -> bool:
    """Is `body` plausibly the file `name` says? Some hosts answer 200 with an
    HTML page for any path (deb.debian.org's CDN landing page), which must not
    read as a flat repository."""
    if name.endswith(".xz"):
        return body.startswith(b"\xfd7zXZ\x00")
    if name.endswith(".gz"):
        return body.startswith(b"\x1f\x8b")
    head = body[:4096].lstrip()
    if name == "Packages":
        return head.startswith(b"Package:")
    return head.startswith(b"-----BEGIN PGP SIGNED MESSAGE-----") or any(
        f"\n{k}:".encode() in b"\n" + head
        for k in ("Origin", "Label", "Suite", "Codename", "SHA256", "Date"))


def _parse_release_headers(text: str, result: DetectResult) -> None:
    """Read Components/Codename out of a Release file.

    `Architectures:` is deliberately NOT trusted — Ubuntu ships a byte-identical
    InRelease on archive.ubuntu.com and ports.ubuntu.com advertising every arch,
    while each host serves only a subset. Architectures come from listing
    `binary-*` directories (`list_arches`); this is the fallback only.
    """
    for line in text.splitlines():
        if line.startswith("Components:"):
            result.components = line.split(":", 1)[1].strip().split()
        elif line.startswith("Architectures:") and not result.arches:
            result.arches = line.split(":", 1)[1].strip().split()
        elif line.startswith(("Codename:", "Suite:")) and not result.suite:
            result.suite = line.split(":", 1)[1].strip()


# ── Listing what a repository publishes ──────────────────────────────────────

def list_suites(url: str) -> list[str]:
    """Suite directories under dists/."""
    return _list_dirs(f"{url.rstrip('/')}/dists/")


def list_components(url: str, suite: str) -> list[str]:
    """Component directories under dists/{suite}/."""
    dirs = _list_dirs(f"{url.rstrip('/')}/dists/{suite}/")
    return [d for d in dirs if not _APT_ARCH_DIR.match(d) and d != "by-hash"]


def list_arches(url: str, suite: str, component: str) -> list[str]:
    """Architectures actually served, by listing binary-* directories.

    Never derived from the Release file's `Architectures:` field — see
    `_parse_release_headers`.
    """
    arches = []
    for d in _list_dirs(f"{url.rstrip('/')}/dists/{suite}/{component}/"):
        m = _APT_ARCH_DIR.match(d)
        if m:
            arches.append(m.group(1))
    return sorted(set(arches))


def release_dimensions(url: str, suite: str) -> tuple[list[str], list[str]]:
    """(components, arches) from a suite's Release file. Returns ([], []) if absent.

    The **fallback** for repositories that serve no directory listing at all —
    S3- and CDN-backed vendor repos answer every `dists/` request with 404 while
    serving the files themselves fine. Listing stays authoritative wherever it
    works, because Ubuntu's archive and ports hosts ship a byte-identical
    InRelease advertising architectures neither of them fully serves.
    """
    base = url.rstrip("/")
    for name in ("InRelease", "Release"):
        try:
            with httpx.Client(timeout=10, follow_redirects=True) as client:
                r = client.get(f"{base}/dists/{suite}/{name}")
                if r.status_code != 200:
                    continue
        except Exception:
            continue
        result = DetectResult(provider="apt")
        _parse_release_headers(r.text, result)
        if result.components or result.arches:
            return result.components, result.arches
    return [], []


def list_repositories(host_url: str) -> list[str]:
    """Directories under a host root, for browsing.

    Only a convenience: some hosts serve no listing at all (deb.debian.org
    returns a CDN landing page), so pasting a full repository URI must stay a
    first-class path.
    """
    return _list_dirs(host_url if host_url.endswith("/") else host_url + "/")


def _list_dirs(url: str) -> list[str]:
    try:
        with httpx.Client(timeout=10, follow_redirects=True) as client:
            r = client.get(url)
            if r.status_code != 200:
                return []
            return _listed_dirs(r.text)
    except Exception:
        return []


def _listed_dirs(html: str) -> list[str]:
    """Relative subdirectory names from an autoindex page.

    Absolute hrefs are dropped: archive.kernel.org's index mixes real
    subdirectories with off-site links, and offering `kernel.org` as a
    directory would be nonsense.

    Matches both quote styles: most autoindexes emit `href="..."`, but
    developer.download.nvidia.com's emits `href='...'`. A double-quote-only
    regex silently matches nothing there, so `candidates` came back empty at
    every directory above a flat repository's actual root — a dead end
    with no error, since an empty listing looks identical to "not a
    repository" (`detect_repository`'s `candidates` field, above).
    """
    names = []
    for href in re.findall(r'''href=["']([^"']+)["']''', html):
        if not href.endswith("/") or "://" in href or href.startswith(("/", "?", "#", "..")):
            continue
        name = href.rstrip("/")
        if name and name not in names:
            names.append(name)
    return names


def probe_arch_placeholder(url_template: str, placeholder: str) -> list[tuple[str, DetectResult]]:
    """Every (architecture, DetectResult) pair a `$(ARCH)`/`$ARCH` templated
    URL actually resolves to.

    apt fills this token in from the *local* machine's dpkg architecture at
    update time — mmw has no such machine, and the repository's own root
    typically has nothing at it to list (see `apt_sources._parse_one_line_body`
    for why guessing one arch here silently drops the others). There is
    usually no directory to crawl either (GitHub Pages and similar static
    hosts serve no index), so this checks each known architecture directly,
    reusing the same InRelease/dists probe every other repository goes
    through — no new HTTP logic, just more of it.

    Returns the full `DetectResult` per arch, not just the arch name, so a
    caller that also needs the repository's shape (suites, provider — the
    TUI's URL-probe path) never has to probe the same candidate twice.
    """
    # Concurrently: one probe per known arch, each up to a few requests with
    # 8 s timeouts, took minutes one after another against a slow host.
    from concurrent.futures import ThreadPoolExecutor
    arches = sorted(KNOWN_ARCHES)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda a: detect_repository(url_template.replace(placeholder, a)), arches))
    return [(a, r) for a, r in zip(arches, results) if r.provider is not None]


def expand_arch_placeholder(url_template: str, placeholder: str) -> list[str]:
    """Which architectures a `$(ARCH)`/`$ARCH` templated URL actually serves.

    A thin wrapper over `probe_arch_placeholder` for callers (headless `mmw
    add`, config-paste drafts) that only need the architecture names.
    """
    return [arch for arch, _ in probe_arch_placeholder(url_template, placeholder)]


__all__ = [
    "KNOWN_ARCHES",
    "DetectResult",
    "detect_repository",
    "expand_arch_placeholder",
    "probe_arch_placeholder",
    "list_arches",
    "list_components",
    "list_repositories",
    "list_suites",
    "release_dimensions",
]
