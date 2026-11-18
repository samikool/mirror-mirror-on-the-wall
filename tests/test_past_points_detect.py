"""Which snapshot archive, which time, which repository — no network."""
import pytest

from mmw.core.config import MirrorConfig, normalize_url
from mmw.core.past_points import (
    ARCHIVES, ArchiveUnavailable, BeforeArchive, NoArchive, NoMatch,
    archive_for, match_repos, parse_archive_url,
)

DEBIAN, UBUNTU = ARCHIVES["debian"], ARCHIVES["ubuntu"]


def _release(origin: str, codename: str, suite: str | None = None, label: str | None = None) -> bytes:
    return (f"Origin: {origin}\nLabel: {label or origin}\nSuite: {suite or codename}\n"
            f"Codename: {codename}\nDate: Wed, 14 Oct 2026 20:05:11 UTC\n"
            "Architectures: amd64\nComponents: main\nSHA256:\n").encode()


def _units(url: str, *suites: str, provider: str = "apt") -> list[MirrorConfig]:
    return [MirrorConfig(name=f"{url}-{s}", provider=provider, source_url=url, source=normalize_url(url),
                         dims={"suite": s, "component": "main", "arch": "amd64"}) for s in suites]


def _fetcher(served: dict[str, bytes], seen: list[str] | None = None):
    def fetch(url: str) -> bytes | None:
        if seen is not None:
            seen.append(url)
        return served.get(url)
    return fetch


# ── parse_archive_url ─────────────────────────────────────────────────────

def test_parse_debian_url():
    archive, name, stamp = parse_archive_url("https://snapshot.debian.org/archive/debian-security/20261015T013713Z/")
    assert (archive, name, stamp) == (DEBIAN, "debian-security", "20261015T013713Z")
    assert archive.base(name, stamp) == "https://snapshot.debian.org/archive/debian-security/20261015T013713Z"


def test_parse_ubuntu_url_with_a_path_below_it():
    archive, name, stamp = parse_archive_url(
        "https://snapshot.ubuntu.com/ubuntu/20261015T013713Z/dists/noble/")
    assert (archive, name, stamp) == (UBUNTU, "ubuntu", "20261015T013713Z")
    assert archive.base(name, stamp) == "https://snapshot.ubuntu.com/ubuntu/20261015T013713Z"


@pytest.mark.parametrize("day", ["20261015", "2026-10-15"])
def test_a_bare_day_is_its_end(day):
    _, _, stamp = parse_archive_url(f"https://snapshot.ubuntu.com/ubuntu/{day}/")
    assert stamp == "20261015T235959Z"


def test_ubuntu_before_its_start():
    with pytest.raises(BeforeArchive, match="the archive starts 2023-03-01"):
        parse_archive_url("https://snapshot.ubuntu.com/ubuntu/20230228T120000Z/")


def test_ubuntu_ports_refused_up_front():
    with pytest.raises(ArchiveUnavailable, match="ports archive .* isn't public on snapshot.ubuntu.com"):
        parse_archive_url("https://snapshot.ubuntu.com/ubuntu-ports/20261015T000000Z/")


@pytest.mark.parametrize("url", ["https://example.com/debian/20261015T000000Z/",
                                 "https://snapshot.debian.org/archive/debian/",
                                 "https://snapshot.debian.org/archive/debian/yesterday-ish/"])
def test_not_an_archive_url(url):
    with pytest.raises(ValueError):
        parse_archive_url(url)


# ── archive_for (--as-of) ────────────────────────────────────────────────

def test_archive_for_debian_and_security():
    assert archive_for("http://deb.debian.org/debian", "Debian") == (DEBIAN, "debian")
    assert archive_for("http://security.debian.org/debian-security", "Debian") == (DEBIAN, "debian-security")


def test_archive_for_ubuntu_mirrors():
    assert archive_for("http://gb.archive.ubuntu.com/ubuntu", "Ubuntu") == (UBUNTU, "ubuntu")
    assert archive_for("http://security.ubuntu.com/ubuntu", "Ubuntu") == (UBUNTU, "ubuntu")


def test_archive_for_ubuntu_ports():
    with pytest.raises(ArchiveUnavailable):
        archive_for("http://ports.ubuntu.com/ubuntu-ports", "Ubuntu")


def test_third_party_repo_has_no_archive():
    with pytest.raises(NoArchive) as e:
        archive_for("https://download.docker.com/linux/debian", "Docker")
    assert "snapshot.debian.org" in str(e.value) and "snapshot.ubuntu.com" in str(e.value)
    assert "download.docker.com/linux/debian" in str(e.value)


# ── match_repos ──────────────────────────────────────────────────────────

SNAP = "https://snapshot.ubuntu.com/ubuntu/20261015T000000Z"


def test_one_match_among_candidates():
    debian = "http://deb.debian.org/debian"
    docker = "https://download.docker.com/linux/debian"
    base = "https://snapshot.debian.org/archive/debian/20261015T000000Z"
    candidates = {normalize_url(debian): _units(debian, "bookworm"),
                  normalize_url(docker): _units(docker, "bookworm")}
    served = {f"{base}/dists/bookworm/Release": _release("Debian", "bookworm"),
              f"{debian}/dists/bookworm/Release": _release("Debian", "bookworm"),
              f"{docker}/dists/bookworm/Release": _release("Docker CE", "bookworm")}
    assert match_repos(DEBIAN, "debian", "20261015T000000Z", candidates, _fetcher(served)) == [
        normalize_url(debian)]


def test_two_matches_archive_and_security():
    archive, security = "http://archive.ubuntu.com/ubuntu", "http://security.ubuntu.com/ubuntu"
    candidates = {normalize_url(archive): _units(archive, "noble", "noble-updates"),
                  normalize_url(security): _units(security, "noble-security")}
    served = {}
    for url in (SNAP, archive, security):
        served[f"{url}/dists/noble/Release"] = _release("Ubuntu", "noble")
        served[f"{url}/dists/noble-updates/Release"] = _release("Ubuntu", "noble", "noble-updates")
        served[f"{url}/dists/noble-security/Release"] = _release("Ubuntu", "noble", "noble-security")
    found = match_repos(UBUNTU, "ubuntu", "20261015T000000Z", candidates, _fetcher(served))
    assert found == sorted([normalize_url(archive), normalize_url(security)])


def test_candidates_narrowed_by_path_before_any_fetch():
    debian_sec = "http://security.debian.org/debian-security"
    ubuntu = "http://archive.ubuntu.com/ubuntu"
    flat = "http://archive.ubuntu.com/ubuntu-flat"
    seen: list[str] = []
    candidates = {normalize_url(debian_sec): _units(debian_sec, "bookworm-security"),
                  normalize_url(ubuntu): _units(ubuntu, "noble", provider="apt-flat"),
                  normalize_url(flat): _units(flat, "noble")}
    with pytest.raises(NoMatch):
        match_repos(UBUNTU, "ubuntu", "20261015T000000Z", candidates, _fetcher({}, seen))
    assert seen == []


def test_no_match_names_what_was_compared():
    archive = "http://archive.ubuntu.com/ubuntu"
    candidates = {normalize_url(archive): _units(archive, "noble")}
    served = {f"{SNAP}/dists/noble/Release": _release("Ubuntu", "noble"),
              f"{archive}/dists/noble/Release": _release("Ubuntu", "plucky", "noble")}
    with pytest.raises(NoMatch) as e:
        match_repos(UBUNTU, "ubuntu", "20261015T000000Z", candidates, _fetcher(served))
    text = str(e.value)
    assert "snapshot.ubuntu.com" in text and "noble" in text
    assert "Codename" in text and "plucky" in text


def test_a_suite_missing_then_does_not_refuse_the_others():
    archive = "http://archive.ubuntu.com/ubuntu"
    candidates = {normalize_url(archive): _units(archive, "noble", "noble-backports")}
    served = {f"{SNAP}/dists/noble/Release": _release("Ubuntu", "noble"),
              f"{archive}/dists/noble/Release": _release("Ubuntu", "noble"),
              f"{archive}/dists/noble-backports/Release": _release("Ubuntu", "noble", "noble-backports")}
    assert match_repos(UBUNTU, "ubuntu", "20261015T000000Z", candidates, _fetcher(served)) == [
        normalize_url(archive)]


def test_own_release_can_come_from_elsewhere():
    archive = "http://archive.ubuntu.com/ubuntu"
    candidates = {normalize_url(archive): _units(archive, "noble")}
    seen: list[str] = []
    served = {f"{SNAP}/dists/noble/Release": _release("Ubuntu", "noble")}
    found = match_repos(UBUNTU, "ubuntu", "20261015T000000Z", candidates, _fetcher(served, seen),
                        own=lambda unit: _release("Ubuntu", "noble"))
    assert found == [normalize_url(archive)]
    assert seen == [f"{SNAP}/dists/noble/Release"]
