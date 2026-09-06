"""Empty means all: resolving open dimensions by listing the repository."""
import pytest

from mmw.core import discover, resolve
from mmw.core.config import SourceConfig

URL = "http://archive.example.com/ubuntu"


class _Resp:
    def __init__(self, text="", status_code=200):
        self.text, self.status_code = text, status_code


def _listing(*names: str) -> str:
    return "".join(f'<a href="{n}/">{n}</a>' for n in names)


@pytest.fixture
def upstream(monkeypatch):
    """A small repository: 2 suites x 2 components, amd64 + i386."""
    pages = {
        f"{URL}/dists": _listing("noble", "noble-updates"),
        f"{URL}/dists/noble": _listing("main", "universe", "binary-amd64"),
        f"{URL}/dists/noble-updates": _listing("main", "universe"),
        f"{URL}/dists/noble/main": _listing("binary-amd64", "binary-i386", "binary-all"),
        f"{URL}/dists/noble/universe": _listing("binary-amd64", "binary-i386"),
        f"{URL}/dists/noble-updates/main": _listing("binary-amd64", "binary-i386"),
        f"{URL}/dists/noble-updates/universe": _listing("binary-amd64", "binary-i386"),
    }
    requested: list[str] = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **k):
            requested.append(url)
            for prefix, body in pages.items():
                if url.rstrip("/") == prefix.rstrip("/"):
                    return _Resp(body)
            return _Resp("", 404)

    monkeypatch.setattr(discover.httpx, "Client", _Client)
    return requested


def test_everything_open_resolves_to_the_whole_repository(upstream):
    r = resolve.resolve_source(SourceConfig(url=URL))
    assert r.errors == []
    assert r.source.suites == ["noble", "noble-updates"]
    assert r.source.components == ["main", "universe"]
    assert r.source.arches == ["amd64", "i386"]
    # 2 suites x 2 components x 2 arches
    assert len(r.mirrors) == 8


def test_what_all_expanded_to_is_reported(upstream):
    """A blank field must never quietly become a hundred mirrors."""
    r = resolve.resolve_source(SourceConfig(url=URL))
    joined = "\n".join(r.notes)
    assert "all suites → 2" in joined
    assert "all components → main, universe" in joined
    assert "all arches → amd64, i386" in joined


def test_a_stated_selection_is_never_widened(upstream):
    """The config states a selection; resolving only fills what was left open."""
    r = resolve.resolve_source(
        SourceConfig(url=URL, suites=["noble"], components=["main"], arches=["amd64"])
    )
    assert r.source.suites == ["noble"]
    assert r.source.components == ["main"]
    assert r.source.arches == ["amd64"]
    assert r.notes == []
    assert len(r.mirrors) == 1


def test_only_the_open_dimension_is_listed(upstream):
    r = resolve.resolve_source(SourceConfig(url=URL, suites=["noble"], components=["main"]))
    assert r.source.suites == ["noble"]
    assert r.source.arches == ["amd64", "i386"]
    assert len(r.mirrors) == 2


def test_arches_come_from_listing_not_the_release_file(upstream):
    """binary-all is excluded, and the Release file is never consulted."""
    r = resolve.resolve_source(SourceConfig(url=URL))
    assert "all" not in r.source.arches
    assert not any("InRelease" in u or "/Release" in u for u in upstream)


def test_unreachable_repository_reports_and_yields_nothing(monkeypatch):
    monkeypatch.setattr(discover, "list_suites", lambda url: [])
    r = resolve.resolve_source(SourceConfig(url=URL))
    assert r.mirrors == []
    assert r.errors and "no suites found" in r.errors[0]


def test_flat_repositories_need_no_listing(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("apt-flat must not list dists/")

    monkeypatch.setattr(discover, "list_suites", boom)
    r = resolve.resolve_source(
        SourceConfig(url="https://example.com/cuda/", provider="apt-flat")
    )
    assert r.errors == []
    assert len(r.mirrors) == 1
    # This used to assert `["amd64"]`, encoding the bug: a flat repository whose
    # URL names no architecture has an *unknown* one, not an amd64 one. The
    # invented label made `NetworkSelect` drop real arm64 repositories from
    # arm64 networks.
    assert r.source.arches == []
    assert r.mirrors[0].arch == ""

    named = resolve.resolve_source(
        SourceConfig(url="https://example.com/hpc-sdk/ubuntu/arm64", provider="apt-flat")
    )
    assert named.source.arches == ["arm64"], "the URL names it — use it"


def test_resolve_sources_reports_each_failure(monkeypatch, upstream):
    monkeypatch.setattr(
        discover, "list_suites",
        lambda url: [] if "broken" in url else ["noble"],
    )
    monkeypatch.setattr(discover, "list_components", lambda url, s: ["main"])
    monkeypatch.setattr(discover, "list_arches", lambda url, s, c: ["amd64"])

    mirrors, notes, errors = resolve.resolve_sources([
        SourceConfig(url=URL),
        SourceConfig(url="http://broken.example.com/repo"),
    ])
    assert len(mirrors) == 1               # only the good one contributed
    assert len(errors) == 1 and "broken" in errors[0]


def test_needs_resolving():
    assert resolve.needs_resolving([SourceConfig(url=URL)])
    assert not resolve.needs_resolving([
        SourceConfig(url=URL, suites=["a"], components=["b"], arches=["amd64"]),
    ])
    # a flat repository never needs listing
    assert not resolve.needs_resolving([
        SourceConfig(url=URL, provider="apt-flat"),
    ])


# ── headless exit codes (cron-facing) ────────────────────────────────────────

def test_resolve_and_report_echoes_what_all_expanded_to(upstream):
    from mmw.commands.mirror import resolve_and_report
    from mmw.core.config import Config, Settings

    cfg = Config(settings=Settings(mirror_dir="/tmp/x"), sources=[SourceConfig(url=URL)])
    lines: list[str] = []
    mirrors, errors = resolve_and_report(cfg, echo=lines.append)

    assert len(mirrors) == 8
    assert errors == []
    joined = "\n".join(lines)
    assert "all suites" in joined and "all arches" in joined


def test_resolve_and_report_surfaces_failures(monkeypatch):
    """A repository that cannot be listed must be reported, and the caller must
    be able to exit non-zero — a cron sync that silently did nothing is worse
    than one that fails."""
    from mmw.commands.mirror import resolve_and_report
    from mmw.core.config import Config, Settings

    monkeypatch.setattr(discover, "list_suites", lambda url: [])
    cfg = Config(
        settings=Settings(mirror_dir="/tmp/x"),
        sources=[SourceConfig(url="http://nowhere.invalid/repo")],
    )
    lines: list[str] = []
    mirrors, errors = resolve_and_report(cfg, echo=lines.append)

    assert mirrors == []
    assert len(errors) == 1
    assert any("⚠" in ln for ln in lines)


# ── what actually exists comes from the pool ─────────────────────────────────

def _seeded_pool(tmp_path, source, arches):
    import hashlib

    from mmw.core.pool import KIND_PKG, Pool
    from mmw.core.config import MirrorConfig

    with Pool(tmp_path) as pool:
        for arch in arches:
            cs = hashlib.sha256(arch.encode()).hexdigest()
            pool.add(arch.encode(), cs, "sha256", f"{arch}.deb", len(arch))
            m = MirrorConfig(
                name=f"example-archive-noble-main-{arch}", provider="apt",
                source_url="http://archive.example.com/ubuntu", source=source,
                arch=arch, suite="noble", component="main", label="ubuntu",
            )
            pool.record_mirror(m)
            pool.reconcile_refs(m.name, [(cs, f"pool/{arch}.deb", KIND_PKG)], source)
    return tmp_path


def test_available_mirrors_reports_every_synced_arch(tmp_path):
    """The gap this closes: config expansion lists only the arches spelled out,
    so publish would silently skip every other arch that was actually synced."""
    from mmw.core.config import Config, Settings

    cfg = Config(
        settings=Settings(mirror_dir=tmp_path),
        sources=[SourceConfig(url="http://archive.example.com/ubuntu",
                              suites=["noble"], components=["main"])],
    )
    _seeded_pool(tmp_path, cfg.sources[0].identity, ["amd64", "i386", "arm64"])

    assert [m.name for m in cfg.mirrors] == ["example-archive-noble-main-amd64"]
    assert sorted(m.arch for m in resolve.available_mirrors(cfg, tmp_path)) == [
        "amd64", "arm64", "i386",
    ]


def test_available_mirrors_reconstructs_usable_configs(tmp_path):
    """Publish needs mirror_base and the apt fields, not just names."""
    from mmw.core.config import Config, Settings

    cfg = Config(
        settings=Settings(mirror_dir=tmp_path),
        sources=[SourceConfig(url="http://archive.example.com/ubuntu")],
    )
    _seeded_pool(tmp_path, cfg.sources[0].identity, ["amd64"])
    m = resolve.available_mirrors(cfg, tmp_path)[0]
    assert m.mirror_base == "archive.example.com/ubuntu"
    assert (m.suite, m.component, m.arch) == ("noble", "main", "amd64")


def test_available_mirrors_drops_repositories_removed_from_the_config(tmp_path):
    from mmw.core.config import Config, Settings

    _seeded_pool(tmp_path, "http://archive.example.com/ubuntu", ["amd64"])
    cfg = Config(
        settings=Settings(mirror_dir=tmp_path),
        sources=[SourceConfig(url="http://other.example.com/debian", suites=["bookworm"],
                              components=["main"], arches=["amd64"])],
    )
    names = [m.name for m in resolve.available_mirrors(cfg, tmp_path)]
    assert not any("example-archive" in n for n in names)


def test_available_mirrors_falls_back_before_the_first_sync(tmp_path):
    """An empty pool must not make the network editor look empty."""
    from mmw.core.config import Config, Settings

    cfg = Config(
        settings=Settings(mirror_dir=tmp_path / "never-synced"),
        sources=[SourceConfig(url="http://archive.example.com/ubuntu",
                              suites=["noble"], components=["main"])],
    )
    assert [m.name for m in resolve.available_mirrors(cfg, tmp_path / "never-synced")] == [
        "example-archive-noble-main-amd64",
    ]


# ── repositories that serve no directory listing ─────────────────────────────

NVIDIA = "https://repo.example.com/spark/ubuntu/arm64"

_RELEASE = (
    "Origin: NVIDIA Spark Repository\n"
    "Suite: noble-updates\n"
    "Architectures: arm64\n"
    "Components: common\n"
)


@pytest.fixture
def unlistable(monkeypatch):
    """S3/CDN-backed repo: every dists/ request 404s, the files themselves serve."""
    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **k):
            if url.rstrip("/").endswith(("/InRelease", "/Release")):
                return _Resp(_RELEASE)
            return _Resp("", 404)          # no autoindex anywhere

    monkeypatch.setattr(discover.httpx, "Client", _Client)


def test_release_file_is_the_fallback_when_nothing_lists(unlistable):
    """An arm64-only vendor repo must resolve, not fail with "no binary-*"."""
    r = resolve.resolve_source(
        SourceConfig(url=NVIDIA, suites=["noble-updates"], components=["common"])
    )
    assert r.errors == []
    assert r.source.arches == ["arm64"]
    assert any("Release file" in n for n in r.notes), "the source must be named"


def test_release_fallback_also_supplies_components(unlistable):
    r = resolve.resolve_source(SourceConfig(url=NVIDIA, suites=["noble-updates"]))
    assert r.source.components == ["common"]
    assert r.source.arches == ["arm64"]


def test_listing_still_wins_when_it_exists(upstream):
    """The Ubuntu trap: archive and ports ship a byte-identical InRelease
    advertising every arch. Where a listing exists it must be authoritative,
    so the fallback may only fire when listing produced nothing."""
    r = resolve.resolve_source(SourceConfig(url=URL, suites=["noble"], components=["main"]))
    assert r.source.arches == ["amd64", "i386"]
    assert any("[listing]" in n for n in r.notes)


def test_an_unresolvable_repository_says_where_it_looked(monkeypatch):
    monkeypatch.setattr(discover, "list_arches", lambda u, s, c: [])
    monkeypatch.setattr(discover, "release_dimensions", lambda u, s: ([], []))
    r = resolve.resolve_source(
        SourceConfig(url=NVIDIA, suites=["noble-updates"], components=["common"])
    )
    assert r.mirrors == []
    assert "Release" in r.errors[0] and "noble-updates" in r.errors[0]
