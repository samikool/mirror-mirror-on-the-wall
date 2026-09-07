"""Multi-arch: config expansion, name uniqueness, merging, and publish grouping.

Repository-first (see repository-first-redesign.md): sources are keyed on their
URI, there is no distro catalog, and architectures are learned by listing
`binary-*` directories.
"""
import pytest
from pydantic import ValidationError

from mmw.core import config, discover

ARCHIVE = "http://archive.example.com/ubuntu"
PORTS = "http://ports.example.com/ubuntu-ports"


# ── config expansion ─────────────────────────────────────────────────────────

def test_apt_expands_one_mirror_per_arch():
    src = config.SourceConfig(
        name="ubuntu", label="ubuntu", provider="apt", url=ARCHIVE,
        arches=["amd64", "arm64"], suites=["noble"], components=["main", "universe"],
    )
    mirrors = src.to_mirrors()
    assert [m.name for m in mirrors] == [
        "ubuntu-noble-main-amd64", "ubuntu-noble-main-arm64",
        "ubuntu-noble-universe-amd64", "ubuntu-noble-universe-arm64",
    ]
    assert {m.arch for m in mirrors} == {"amd64", "arm64"}


def test_apt_flat_ignores_extra_arches():
    """Flat repos carry the arch in the URL — expanding would duplicate the mirror."""
    src = config.SourceConfig(
        name="flat", label="custom", provider="apt-flat",
        url="https://example.com/repo/", arches=["amd64", "arm64"],
    )
    assert len(src.to_mirrors()) == 1


def test_mirror_base_is_the_repository_uri():
    """The published tree is derived from host + path alone — the identity."""
    src = config.SourceConfig(
        name="code", label="microsoft", provider="apt",
        url="https://packages.microsoft.com/repos/code",
        arches=["amd64"], suites=["stable"], components=["main"],
    )
    assert src.to_mirrors()[0].mirror_base == "packages.microsoft.com/repos/code"


# ── name collisions ──────────────────────────────────────────────────────────

def test_duplicate_mirror_names_rejected():
    """Two sources expanding to one mirror name would share a refs mirror_name:
    the second sync's reconcile silently deletes the first one's index."""
    # Names are joined with "-", so "a" + suite "b-c" and "a-b" + suite "c"
    # both expand to "a-b-c-main-amd64".
    common = dict(label="ubuntu", provider="apt", components=["main"], arches=["amd64"])
    with pytest.raises(ValidationError, match="duplicate mirror name"):
        config.Config(
            settings=config.Settings(mirror_dir="/tmp/x"),
            sources=[
                config.SourceConfig(name="a", url=ARCHIVE, suites=["b-c"], **common),
                config.SourceConfig(name="a-b", url=PORTS, suites=["c"], **common),
            ],
        )


def test_same_suite_different_arch_is_not_a_collision():
    common = dict(label="ubuntu", provider="apt", suites=["noble"], components=["main"])
    cfg = config.Config(
        settings=config.Settings(mirror_dir="/tmp/x"),
        sources=[
            config.SourceConfig(name="ubuntu-archive", url=ARCHIVE, arches=["amd64"], **common),
            config.SourceConfig(name="ubuntu-ports", url=PORTS, arches=["arm64"], **common),
        ],
    )
    assert [m.name for m in cfg.mirrors] == [
        "ubuntu-archive-noble-main-amd64", "ubuntu-ports-noble-main-arm64",
    ]


def test_duplicate_source_names_rejected():
    with pytest.raises(ValidationError, match="duplicate source name"):
        config.Config(
            settings=config.Settings(mirror_dir="/tmp/x"),
            sources=[
                config.SourceConfig(name="dup", label="d", provider="apt", url="https://a/"),
                config.SourceConfig(name="dup", label="d", provider="apt", url="https://b/"),
            ],
        )


# ── architecture discovery (offline: httpx stubbed) ──────────────────────────

class _Resp:
    def __init__(self, text="", status_code=200):
        self.text, self.status_code = text, status_code


def _listing(*names: str) -> str:
    return "".join(f'<a href="{n}/">{n}</a>' for n in names)


def _stub_client(monkeypatch, pages: dict[str, str]):
    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **k):
            for prefix, body in pages.items():
                if url.rstrip("/") == prefix.rstrip("/"):
                    return _Resp(body)
            return _Resp("", 404)

    monkeypatch.setattr(discover.httpx, "Client", _Client)


def test_arches_come_from_binary_dirs_not_the_release_field(monkeypatch):
    """Ubuntu ships a byte-identical InRelease on archive and ports, both
    advertising every arch while each serves a subset. Trusting that field
    builds a mirror that 404s every index — so listing is the source of truth."""
    _stub_client(monkeypatch, {
        f"{ARCHIVE}/dists/noble/main": _listing("binary-amd64", "binary-i386", "binary-all"),
    })
    assert discover.list_arches(ARCHIVE, "noble", "main") == ["amd64", "i386"]


def test_binary_all_is_not_an_architecture(monkeypatch):
    _stub_client(monkeypatch, {
        f"{ARCHIVE}/dists/noble/main": _listing("binary-all"),
    })
    assert discover.list_arches(ARCHIVE, "noble", "main") == []


def test_suites_and_components_are_listed(monkeypatch):
    _stub_client(monkeypatch, {
        f"{ARCHIVE}/dists": _listing("noble", "noble-updates"),
        f"{ARCHIVE}/dists/noble": _listing("main", "universe", "binary-amd64", "by-hash"),
    })
    assert discover.list_suites(ARCHIVE) == ["noble", "noble-updates"]
    # arch dirs and by-hash are not components
    assert discover.list_components(ARCHIVE, "noble") == ["main", "universe"]


def test_listing_ignores_absolute_offsite_links(monkeypatch):
    """archive.kernel.org's index mixes real subdirectories with off-site links;
    offering `kernel.org` as a directory would be nonsense."""
    body = (
        '<a href="http://www.kernel.org/">kernel</a>'
        '<a href="archlinux-archive/">archlinux-archive</a>'
        '<a href="/absolute/">abs</a>'
        '<a href="../">up</a>'
    )
    _stub_client(monkeypatch, {"https://archive.kernel.org": body})
    assert discover.list_repositories("https://archive.kernel.org/") == ["archlinux-archive"]


# ── publish ──────────────────────────────────────────────────────────────────

def test_two_arches_of_one_base_publish_as_one_group(tmp_path):
    """Debian serves both arches from one host: the arch mirrors share a
    mirror_base, so the suite's InRelease is linked exactly once."""
    import hashlib

    from mmw.core.pool import KIND_META, KIND_PKG, KIND_RELEASE, Pool
    from mmw.core.publisher import publish_network

    mirror_dir = tmp_path / "mirror"
    with Pool(mirror_dir) as pool:
        rel = hashlib.sha256(b"release").hexdigest()
        pool.add(b"release", rel, "sha256", "InRelease", 7)
        for arch in ("amd64", "arm64"):
            deb = hashlib.sha256(f"deb-{arch}".encode()).hexdigest()
            pool.add(f"deb-{arch}".encode(), deb, "sha256", f"hello_{arch}.deb", 5)
            idx = hashlib.sha256(f"packages-{arch}".encode()).hexdigest()
            pool.add(f"packages-{arch}".encode(), idx, "sha256", "Packages.xz", 13)
            pool.reconcile_refs(f"debian-bookworm-main-{arch}", [
                (deb, f"pool/main/h/hello/hello_{arch}.deb", KIND_PKG),
                (idx, f"dists/bookworm/main/binary-{arch}/Packages.xz", KIND_META),
                (rel, "dists/bookworm/InRelease", KIND_RELEASE),
            ])

    net = config.NetworkConfig(name="n", root=tmp_path / "pub", prune=False)
    cfg = config.Config(
        settings=config.Settings(mirror_dir=mirror_dir),
        sources=[config.SourceConfig(
            name="debian", label="debian", provider="apt",
            url="https://deb.example.com/debian", arches=["amd64", "arm64"],
            suites=["bookworm"], components=["main"],
        )],
        networks=[net],
    )

    result = publish_network(net, cfg, mirror_dir)
    assert result.errors == []
    assert len(result.mirrors) == 1          # one group for the shared base
    assert result.mirrors[0].linked == 5     # 2 debs + 2 indexes + 1 shared InRelease

    dst = tmp_path / "pub" / "deb.example.com" / "debian"
    for arch in ("amd64", "arm64"):
        assert (dst / f"dists/bookworm/main/binary-{arch}/Packages.xz").exists()
        assert (dst / f"pool/main/h/hello/hello_{arch}.deb").exists()
    assert (dst / "dists/bookworm/InRelease").exists()


# ── merging sources (manage → Add) ───────────────────────────────────────────

def _ubuntu(url, arches, comps=("main",), suites=("noble",), name="ubuntu"):
    return [config.SourceConfig(
        name=name, label="ubuntu", provider="apt", url=url,
        arches=list(arches), suites=list(suites), components=list(comps),
    )]


def test_adding_a_second_arch_on_another_host_is_not_dropped():
    """Ports is a different repository URI, so it becomes its own source — the
    old name-match add flow silently discarded it."""
    existing = _ubuntu(ARCHIVE, ["amd64"])
    merged, added, updated = config.merge_sources(existing, _ubuntu(PORTS, ["arm64"]))
    assert (added, updated) == (1, 0)
    by_name = {s.name: s for s in merged}
    assert set(by_name) == {"ubuntu", "ubuntu-ports"}
    assert by_name["ubuntu-ports"].url == PORTS
    assert by_name["ubuntu-ports"].arches == ["arm64"]


def test_merge_unions_into_the_same_source_when_the_url_matches():
    merged, added, updated = config.merge_sources(
        _ubuntu(ARCHIVE, ["amd64"], comps=["main"]),
        _ubuntu(ARCHIVE, ["amd64"], comps=["main", "universe"]),
    )
    assert (added, updated) == (0, 1)
    assert merged[0].components == ["main", "universe"]


def test_merge_keeps_per_suite_components_intact():
    """Adding a suite with different components must not retro-add them to the
    suite already configured."""
    merged, _, _ = config.merge_sources(
        _ubuntu(ARCHIVE, ["amd64"], comps=["main"], suites=["noble"]),
        _ubuntu(ARCHIVE, ["amd64"], comps=["main", "universe"], suites=["jammy"]),
    )
    src = merged[0]
    assert sorted(src.suites) == ["jammy", "noble"]
    overrides = {o.suite: o.components for o in src.suite_overrides}
    effective = {s: overrides.get(s, src.components) for s in src.suites}
    assert effective == {"noble": ["main"], "jammy": ["main", "universe"]}


def test_merge_is_idempotent():
    merged, added, updated = config.merge_sources(
        _ubuntu(ARCHIVE, ["amd64"]), _ubuntu(ARCHIVE, ["amd64"]),
    )
    assert (added, updated) == (0, 0)
    assert len(merged) == 1


def test_merged_config_still_validates():
    """Merging must never produce the duplicate names the validator rejects."""
    sources = _ubuntu(ARCHIVE, ["amd64"])
    for url, arches in ((PORTS, ["arm64"]), (ARCHIVE, ["amd64"]), (PORTS, ["arm64"])):
        sources, _, _ = config.merge_sources(sources, _ubuntu(url, arches))
    cfg = config.Config(settings=config.Settings(mirror_dir="/tmp/x"), sources=sources)
    assert sorted(m.name for m in cfg.mirrors) == [
        "ubuntu-noble-main-amd64", "ubuntu-ports-noble-main-arm64",
    ]


def test_configured_units_are_per_arch():
    apt = config.SourceConfig(
        name="ubuntu", label="ubuntu", provider="apt", url=ARCHIVE,
        arches=["amd64", "arm64"], suites=["noble"], components=["main"],
    )
    assert apt.configured_units() == {
        ("ubuntu", "noble", "main", "amd64"), ("ubuntu", "noble", "main", "arm64"),
    }


# ── a flat repository is never arch-expanded ─────────────────────────────────

def test_a_flat_repo_takes_its_arch_from_the_url_not_from_amd64():
    """Measured on a real DGX pool: NVIDIA's sbsa and hpc-sdk repositories were
    labelled `amd64` while every package under them is `_arm64.deb`."""
    for url, expected in (
        ("https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/sbsa", "arm64"),
        ("https://developer.download.nvidia.com/hpc-sdk/ubuntu/arm64", "arm64"),
        ("https://example.com/repo/ubuntu/amd64", "amd64"),
    ):
        s = config.SourceConfig(name="x", provider="apt-flat", url=url)
        assert s.to_mirrors()[0].arch == expected, url


def test_a_flat_repo_whose_url_names_no_arch_stays_unknown():
    """Better an honest blank than an invented `amd64`."""
    s = config.SourceConfig(
        name="x", provider="apt-flat",
        url="https://workbench.download.nvidia.com/stable/linux/debian")
    assert s.to_mirrors()[0].arch == ""


def test_a_declared_arch_still_wins_for_a_flat_repo():
    s = config.SourceConfig(name="x", provider="apt-flat",
                            url="https://example.com/repo/ubuntu/arm64", arches=["armhf"])
    assert s.to_mirrors()[0].arch == "armhf"


def test_an_arch_filtered_network_still_publishes_a_flat_repo():
    """The consequence that made this worth fixing: `NetworkSelect` filters on
    arch, so a mislabelled — or legitimately unknown — flat repo was silently
    dropped from an arch-specific network's published tree."""
    url = "https://workbench.download.nvidia.com/stable/linux/debian"
    src = config.SourceConfig(name="wb", provider="apt-flat", url=url)
    mirror = src.to_mirrors()[0]
    sel = config.NetworkSelect(source=url, arches=["arm64"])

    assert sel.matches(mirror), "a non-arch-expanded mirror must survive an arch filter"


def test_an_arch_filter_still_excludes_a_genuinely_different_arch():
    """The fix must not turn the arch filter into a no-op."""
    url = "https://example.com/repo/ubuntu"
    src = config.SourceConfig(name="s", provider="apt", url=url,
                              suites=["noble"], components=["main"], arches=["amd64"])
    mirror = src.to_mirrors()[0]
    assert config.NetworkSelect(source=url, arches=["amd64"]).matches(mirror)
    assert not config.NetworkSelect(source=url, arches=["arm64"]).matches(mirror)


def test_resolve_and_to_mirrors_agree_on_a_flat_repo_arch():
    """Two implementations of the same rule is how this survived a full re-sync.

    `SourceConfig.effective_arches` was fixed while `resolve_source` kept its own
    `resolved.arches = ["amd64"]` override — and **resolve is the path a sync
    actually takes**, so the pool label never changed. Assert the two agree.
    """
    from mmw.core.resolve import resolve_source

    for url, expected in (
        ("https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/sbsa", "arm64"),
        ("https://developer.download.nvidia.com/hpc-sdk/ubuntu/arm64", "arm64"),
        ("https://workbench.download.nvidia.com/stable/linux/debian", ""),
    ):
        src = config.SourceConfig(name="x", provider="apt-flat", url=url)
        offline = src.to_mirrors()[0].arch
        resolved = resolve_source(src).mirrors[0].arch
        assert offline == expected, f"to_mirrors disagreed for {url}"
        assert resolved == expected, f"resolve_source disagreed for {url}"


def test_a_derived_flat_arch_is_reported_not_silent():
    """A dimension mmw fills in on the user's behalf must never be silent."""
    from mmw.core.resolve import resolve_source

    r = resolve_source(config.SourceConfig(
        name="hpc", provider="apt-flat",
        url="https://developer.download.nvidia.com/hpc-sdk/ubuntu/arm64"))
    assert any("arm64" in n for n in r.notes), f"nothing reported: {r.notes}"


def test_resolve_never_overrides_a_declared_flat_arch():
    from mmw.core.resolve import resolve_source

    src = config.SourceConfig(name="x", provider="apt-flat",
                              url="https://example.com/repo/ubuntu/arm64", arches=["armhf"])
    assert resolve_source(src).mirrors[0].arch == "armhf"


# ── keys must survive the trip to a published tree ───────────────────────────

def test_inline_keys_reach_the_published_tree(tmp_path):
    """The gap that left a DGX with packages it could not verify.

    `publish_keys` gated its per-mirror branch on `mirror.gpg_key`, but publish
    reads mirrors back from the **pool**, whose `mirrors` table has no key
    columns — so the field was always None, the branch never fired, and every
    inline NVIDIA key was written to `keys/` and silently never published.
    """
    from mmw.core.publisher.links import publish_keys
    from mmw.core.publisher.sinks import TreeSink

    mirror_dir = tmp_path / "pool"
    (mirror_dir / "keys" / "nvidia").mkdir(parents=True)
    (mirror_dir / "keys" / "nvidia" / "dgx_debian_prod.asc").write_text("ARMOUR")
    (mirror_dir / "keys" / "nvidia" / "dgx_debian_prod.gpg").write_bytes(b"\x99binary")
    (mirror_dir / "keys" / "ubuntu").mkdir()
    (mirror_dir / "keys" / "ubuntu" / "ubuntu-archive-keyring.asc").write_text("ARMOUR")

    net = config.NetworkConfig(name="lab", root=tmp_path / "lab")
    # Exactly what the pool hands back: no gpg_key / gpg_key_url fields set.
    pooled = [
        config.MirrorConfig(name="nvidia-repo-baseos-noble-dgx-arm64", provider="apt",
                            label="nvidia", source="https://repo.download.nvidia.com/baseos",
                            source_url="https://repo.download.nvidia.com/baseos",
                            arch="arm64", suite="noble", component="dgx"),
        config.MirrorConfig(name="ubuntu-ports-noble-main-arm64", provider="apt",
                            label="ubuntu", source="http://ports.ubuntu.com/ubuntu-ports",
                            source_url="http://ports.ubuntu.com/ubuntu-ports",
                            arch="arm64", suite="noble", component="main"),
    ]
    assert all(m.gpg_key is None for m in pooled), "fixture must match pool reality"

    publish_keys(TreeSink(net.root), mirror_dir, pooled)

    published = sorted(
        str(p.relative_to(net.root / "keys"))
        for p in (net.root / "keys").rglob("*") if p.is_file()
    )
    assert "nvidia/dgx_debian_prod.asc" in published, "inline key never published"
    assert "nvidia/dgx_debian_prod.gpg" in published
    assert "ubuntu/ubuntu-archive-keyring.asc" in published


def test_publishing_keys_twice_is_idempotent(tmp_path):
    from mmw.core.publisher.links import publish_keys
    from mmw.core.publisher.sinks import TreeSink

    mirror_dir = tmp_path / "pool"
    (mirror_dir / "keys" / "nvidia").mkdir(parents=True)
    (mirror_dir / "keys" / "nvidia" / "k.asc").write_text("ARMOUR")
    net = config.NetworkConfig(name="lab", root=tmp_path / "lab")
    m = config.MirrorConfig(name="m", provider="apt", label="nvidia",
                            source="https://x/y", source_url="https://x/y", arch="arm64")

    sink = TreeSink(net.root)
    publish_keys(sink, mirror_dir, [m])
    publish_keys(sink, mirror_dir, [m])
    assert (net.root / "keys" / "nvidia" / "k.asc").read_text() == "ARMOUR"


def test_a_network_only_publishes_its_own_repositories_keys(tmp_path):
    from mmw.core.publisher.links import publish_keys
    from mmw.core.publisher.sinks import TreeSink

    mirror_dir = tmp_path / "pool"
    for group in ("nvidia", "ubuntu"):
        (mirror_dir / "keys" / group).mkdir(parents=True)
        (mirror_dir / "keys" / group / "k.asc").write_text(group)

    net = config.NetworkConfig(name="lab", root=tmp_path / "lab")
    publish_keys(TreeSink(net.root), mirror_dir, [config.MirrorConfig(
        name="m", provider="apt", label="nvidia",
        source="https://x/y", source_url="https://x/y", arch="arm64")])

    assert (net.root / "keys" / "nvidia" / "k.asc").exists()
    assert not (net.root / "keys" / "ubuntu").exists(), "published an unrelated key"


# ── the dist-upgrade guide must describe *this* network ──────────────────────

def test_the_dns_table_names_the_hosts_this_network_actually_mirrors():
    """It was hardcoded to archive/security.ubuntu.com. An arm64 network
    mirrors ports.ubuntu.com and neither of those — so the guide told the user
    to point DNS at two hostnames they had no copy of, and omitted the one
    they did."""
    from mmw.core.publisher.dist_upgrade import upgrade_hostnames

    mirrors = [
        config.MirrorConfig(name="a", provider="apt", label="ubuntu",
                            source="http://ports.ubuntu.com/ubuntu-ports",
                            source_url="http://ports.ubuntu.com/ubuntu-ports",
                            arch="arm64", suite="noble", component="main"),
        config.MirrorConfig(name="b", provider="apt", label="nvidia",
                            source="https://repo.download.nvidia.com/baseos/ubuntu/noble/arm64",
                            source_url="https://repo.download.nvidia.com/baseos/ubuntu/noble/arm64",
                            arch="arm64", suite="noble", component="dgx"),
    ]
    hosts = [h for h, _ in upgrade_hostnames(mirrors)]

    assert "ports.ubuntu.com" in hosts
    assert "repo.download.nvidia.com" in hosts
    assert "archive.ubuntu.com" not in hosts, "named a host this network has no copy of"
    assert "security.ubuntu.com" not in hosts
    assert hosts[-1] == "changelogs.ubuntu.com", "discovery host must always be listed"


def test_the_dns_table_lists_each_host_once():
    from mmw.core.publisher.dist_upgrade import upgrade_hostnames

    mirrors = [
        config.MirrorConfig(name=f"m{i}", provider="apt", label="ubuntu",
                            source="http://ports.ubuntu.com/ubuntu-ports",
                            source_url="http://ports.ubuntu.com/ubuntu-ports",
                            arch="arm64", suite="noble", component=c)
        for i, c in enumerate(("main", "universe", "restricted"))
    ]
    hosts = [h for h, _ in upgrade_hostnames(mirrors)]
    assert hosts.count("ports.ubuntu.com") == 1


def test_the_guide_still_works_with_no_mirrors():
    from mmw.core.publisher.dist_upgrade import upgrade_hostnames

    assert [h for h, _ in upgrade_hostnames(None)] == ["changelogs.ubuntu.com"]
