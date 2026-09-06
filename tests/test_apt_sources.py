"""Importing apt config files verbatim — both deb formats, no network."""
from mmw.core.apt_sources import entries_to_sources, parse_apt_sources

# The real /etc/apt/sources.list.d/ubuntu.sources shipped on 24.04.
UBUNTU_DEB822 = """\
Types: deb
URIs: http://archive.ubuntu.com/ubuntu/
Suites: noble noble-updates noble-backports
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg

Types: deb
URIs: http://security.ubuntu.com/ubuntu/
Suites: noble-security
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
"""

CLASSIC = """\
# comment line
deb http://archive.ubuntu.com/ubuntu noble main restricted
deb http://archive.ubuntu.com/ubuntu noble-updates main restricted
deb http://security.ubuntu.com/ubuntu noble-security main
"""


def test_deb822_splits_stanzas():
    """Two stanzas must not collapse into one — the later used to overwrite
    the earlier, silently losing the main archive."""
    entries = parse_apt_sources(UBUNTU_DEB822)
    assert len(entries) == 4
    assert [e.suite for e in entries] == [
        "noble", "noble-updates", "noble-backports", "noble-security",
    ]
    assert entries[0].url == "http://archive.ubuntu.com/ubuntu"
    assert entries[-1].url == "http://security.ubuntu.com/ubuntu"


def test_deb822_expands_multi_valued_suites_and_uris():
    entries = parse_apt_sources(
        "Types: deb\n"
        "URIs: http://a.example/deb http://b.example/deb\n"
        "Suites: one two\n"
        "Components: main\n"
    )
    assert {(e.url, e.suite) for e in entries} == {
        ("http://a.example/deb", "one"),
        ("http://a.example/deb", "two"),
        ("http://b.example/deb", "one"),
        ("http://b.example/deb", "two"),
    }


def test_deb822_deb_src_sets_include_sources():
    entries = parse_apt_sources(
        "Types: deb deb-src\nURIs: http://x/deb\nSuites: s\nComponents: main\n"
    )
    assert len(entries) == 1
    assert entries[0].include_sources is True


def test_deb822_src_only_is_skipped():
    assert parse_apt_sources("Types: deb-src\nURIs: http://x\nSuites: s\n") == []


def test_classic_returns_every_line():
    entries = parse_apt_sources(CLASSIC)
    assert len(entries) == 3
    assert [e.suite for e in entries] == ["noble", "noble-updates", "noble-security"]


def test_classic_deb_src_marks_matching_entry():
    entries = parse_apt_sources(
        "deb http://x/deb bookworm main\n"
        "deb-src http://x/deb bookworm main\n"
        "deb http://x/deb trixie main\n"
    )
    assert len(entries) == 2
    by_suite = {e.suite: e for e in entries}
    assert by_suite["bookworm"].include_sources is True
    assert by_suite["trixie"].include_sources is False


def test_flat_repo_has_no_suite():
    entries = parse_apt_sources("deb [arch=arm64] http://developer.nvidia.com/compute/cuda /\n")
    assert len(entries) == 1
    assert entries[0].suite is None
    assert entries[0].arches == ["arm64"]


# ── entries → SourceConfig ────────────────────────────────────────────────────

def test_grouping_one_source_per_url():
    sources = entries_to_sources(parse_apt_sources(UBUNTU_DEB822))
    assert len(sources) == 2
    archive, security = sources
    assert archive.url == "http://archive.ubuntu.com/ubuntu"
    assert archive.suites == ["noble", "noble-updates", "noble-backports"]
    assert archive.components == ["main", "restricted", "universe", "multiverse"]
    assert security.suites == ["noble-security"]


def test_grouping_emits_suite_overrides_when_components_differ():
    sources = entries_to_sources(parse_apt_sources(CLASSIC))
    archive = next(s for s in sources if "archive" in s.url)
    assert archive.suites == ["noble", "noble-updates"]
    assert archive.components == ["main", "restricted"]
    # security has only 'main' but lives on its own URL — no override needed there
    security = next(s for s in sources if "security" in s.url)
    assert security.components == ["main"]


def test_grouping_splits_by_arch():
    entries = parse_apt_sources(
        "deb [arch=amd64] http://x/deb noble main\n"
        "deb [arch=arm64] http://x/deb noble main\n"
    )
    sources = entries_to_sources(entries)
    assert len(sources) == 1
    assert sources[0].arches == ["amd64", "arm64"]


def test_default_arch_when_file_is_silent():
    sources = entries_to_sources(parse_apt_sources(CLASSIC), default_arch="arm64")
    assert all(s.arches == ["arm64"] for s in sources)


def test_flat_repo_becomes_apt_flat_provider():
    sources = entries_to_sources(parse_apt_sources("deb http://x/cuda /\n"))
    assert sources[0].provider == "apt-flat"
    assert sources[0].suites == []


def test_label_derived_from_the_domain():
    """There is no distro catalog any more — the label is the host's domain
    name, so a country mirror's keys land beside the primary's."""
    sources = entries_to_sources(parse_apt_sources(UBUNTU_DEB822))
    assert all(s.label == "ubuntu" for s in sources)

    country = entries_to_sources(
        parse_apt_sources("deb http://us.archive.ubuntu.com/ubuntu noble main\n")
    )
    assert country[0].label == "ubuntu"


def test_label_is_only_a_label_never_identity():
    """A lookalike host gets its own label and its own source — identity is the
    URI, so nothing about the label can make two repositories collide."""
    sources = entries_to_sources(parse_apt_sources(
        "deb http://repo.internal/deb noble main\n"
        "deb http://notarchive.ubuntu.com.evil.test/ubuntu noble main\n"
    ))
    assert [s.label for s in sources] == ["repo", "evil"]
    assert len({s.url for s in sources}) == 2


def test_names_are_unique_and_stable():
    sources = entries_to_sources(parse_apt_sources(UBUNTU_DEB822))
    names = [s.name for s in sources]
    assert len(set(names)) == len(names)
    assert names == ["ubuntu-archive", "ubuntu-security"]


def test_result_round_trips_through_config_validation():
    """Imported sources must be loadable — expansion to mirrors must not
    produce duplicate mirror names."""
    from mmw.core.config import Config, Settings
    sources = entries_to_sources(parse_apt_sources(UBUNTU_DEB822))
    cfg = Config(settings=Settings(mirror_dir="/tmp/x"), sources=sources)
    names = [m.name for m in cfg.mirrors]
    assert len(set(names)) == len(names)


# ── real-world files ──────────────────────────────────────────────────────────

def test_disabled_stanza_is_skipped():
    """deb822 `Enabled: no` turns a source off — importing it would mirror a
    repo the machine itself is not using."""
    assert parse_apt_sources(
        "Enabled: no\n"
        "Types: deb\n"
        "URIs: http://x/deb\n"
        "Suites: focal\n"
        "Components: main\n"
    ) == []


def test_non_http_uris_are_rejected():
    """cdrom:/file: sources are not mirrorable, and a cdrom label containing
    spaces would otherwise split into a URI per word."""
    entries = parse_apt_sources(
        "Types: deb\n"
        "URIs: cdrom:[Ubuntu 20.04.4 LTS _Focal Fossa_ - Release amd64 (20220223)]/\n"
        "Suites: focal\n"
        "Components: main restricted\n"
    )
    assert entries == []


def test_non_http_one_line_is_rejected():
    assert parse_apt_sources("deb file:/srv/local focal main\n") == []




# ── deb822 continuation lines and inline keys ────────────────────────────────

PPA = """\
Types: deb
URIs: https://ppa.example.net/canonical-nvidia/nvidia-desktop-edge/ubuntu/
Suites: noble
Components: main
Signed-By:
 -----BEGIN PGP PUBLIC KEY BLOCK-----
 .
 mQINBGXfTyoBEADL2xlBXPUKAvU4j0KZ+v5uFmMk8U7VeG7vhfEM7M4R
 =6QQD
 -----END PGP PUBLIC KEY BLOCK-----
"""


def test_inline_signed_by_key_is_captured():
    """Launchpad PPAs ship the whole armoured key inline. On an airgapped
    network that beats a URL, because the URL is what cannot be reached."""
    e = parse_apt_sources(PPA)[0]
    assert e.gpg_key is not None
    assert e.gpg_key.startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----")
    assert e.gpg_key.rstrip().endswith("-----END PGP PUBLIC KEY BLOCK-----")
    assert "mQINBGXfTyoBEADL" in e.gpg_key


def test_the_key_does_not_disturb_the_other_fields():
    e = parse_apt_sources(PPA)[0]
    assert e.suite == "noble"
    assert e.components == ["main"]
    assert e.url == "https://ppa.example.net/canonical-nvidia/nvidia-desktop-edge/ubuntu"


def test_armour_headers_cannot_override_stanza_fields():
    """Armoured keys legitimately carry `Version:`/`Comment:` headers. Parsing
    lines independently let those — and anything else inside the block — be read
    as stanza fields, so a pasted key could rewrite `Suites:`."""
    e = parse_apt_sources("""\
Types: deb
URIs: https://example.com/repo
Suites: noble
Components: main
Signed-By:
 -----BEGIN PGP PUBLIC KEY BLOCK-----
 Version: GnuPG v2
 Suites: evil-suite
 Components: evil-component
 .
 mQINBGXfTyoB
 -----END PGP PUBLIC KEY BLOCK-----
""")[0]
    assert e.suite == "noble"
    assert e.components == ["main"]


def test_a_keyring_path_is_not_treated_as_key_material():
    """The common `Signed-By: /usr/share/keyrings/....gpg` names a file already
    on the machine — there is nothing to mirror."""
    e = parse_apt_sources("""\
Types: deb
URIs: https://example.com/repo
Suites: noble
Components: main
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
""")[0]
    assert e.gpg_key is None


def test_inline_key_survives_into_the_source_and_config(tmp_path):
    from mmw.core import config

    src = entries_to_sources(parse_apt_sources(PPA))[0]
    assert src.gpg_key and "BEGIN PGP" in src.gpg_key

    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path), sources=[src])
    config.save(cfg, tmp_path)
    reloaded = config.load(tmp_path).sources[0]
    assert reloaded.gpg_key == src.gpg_key
    assert reloaded.to_mirrors()[0].gpg_key == src.gpg_key


# ── names pick the distinguishing path segment ───────────────────────────────

def test_generic_path_tails_are_skipped():
    """A URL commonly ends in a distro or format word, so the *last* segment is
    often the least informative. Launchpad PPAs end in `/ubuntu`, Cloudsmith in
    `/deb/ubuntu` — the segment before is the one that names the repository."""
    from mmw.core.config import derive_name

    assert derive_name(
        "https://ppa.launchpadcontent.net/canonical-nvidia/nvidia-desktop-edge/ubuntu"
    ).endswith("nvidia-desktop-edge")
    assert derive_name(
        "https://dl.cloudsmith.io/public/myorg/myrepo/deb/ubuntu"
    ).endswith("myrepo")


def test_a_wholly_generic_path_falls_back_to_the_host():
    """archive.ubuntu.com/ubuntu has nothing distinguishing below the host."""
    from mmw.core.config import derive_name

    assert derive_name("http://archive.ubuntu.com/ubuntu") == "ubuntu-archive"
    assert derive_name("http://security.ubuntu.com/ubuntu") == "ubuntu-security"
    assert derive_name("http://ports.ubuntu.com/ubuntu-ports") == "ubuntu-ports"


def test_one_host_many_repositories_still_gets_distinct_names():
    from mmw.core.config import derive_name

    a = derive_name("https://packages.microsoft.com/repos/code")
    b = derive_name("https://packages.microsoft.com/repos/edge")
    assert a != b and a.endswith("code") and b.endswith("edge")


def test_a_suite_in_the_path_is_not_the_repository_name():
    """`…/baseos/ubuntu/noble/arm64` is the baseos repository for noble, not a
    repository called noble — so a known suite is skipped."""
    from mmw.core.config import derive_name

    url = "https://repo.download.nvidia.com/baseos/ubuntu/noble/arm64"
    assert derive_name(url, suites=["noble", "noble-updates"]).endswith("baseos")


def test_the_whole_dgx_bundle_gets_unique_readable_names():
    """Nine cat'd files, eight repositories, two of them on one PPA host."""
    bundle = "\n\n".join([
        "Types: deb\nURIs: https://repo.download.nvidia.com/baseos/ubuntu/noble/arm64/\n"
        "Suites: noble noble-updates\nComponents: common dgx\n",
        "Types: deb\nURIs: https://repo.download.nvidia.com/spark/ubuntu/arm64/\n"
        "Suites: noble-updates\nComponents: common\n",
        "Types: deb\nURIs: https://ppa.launchpadcontent.net/canonical-nvidia/"
        "nvidia-desktop-edge/ubuntu/\nSuites: noble\nComponents: main\n",
        "Types: deb\nURIs: https://ppa.launchpadcontent.net/canonical-nvidia/"
        "vulkan-packages-nv-desktop/ubuntu/\nSuites: noble\nComponents: main\n",
        "Types: deb\nURIs: http://ports.ubuntu.com/ubuntu-ports/\n"
        "Suites: noble\nComponents: main\n",
    ])
    names = [s.name for s in entries_to_sources(parse_apt_sources(bundle))]
    assert len(set(names)) == len(names), f"collision in {names}"
    assert any(n.endswith("baseos") for n in names)
    assert any(n.endswith("spark") for n in names)
    assert any(n.endswith("nvidia-desktop-edge") for n in names)
    assert any(n.endswith("vulkan-packages-nv-desktop") for n in names)
    assert "ubuntu-ports" in names


# ── a whole machine's apt configuration in one paste ─────────────────────────

def test_a_repeated_types_field_starts_a_new_stanza():
    """`cat /etc/apt/sources.list.d/*.sources` is how a machine's whole apt
    configuration arrives, and those files end with a newline rather than a
    blank line — so concatenation glues the last record of one onto the first
    of the next and the later `URIs:` silently wins. Measured on a real DGX
    Spark: ten repositories parsed as one.

    deb822 forbids a duplicate field within a record, so a second unindented
    `Types:` can only be a new record.
    """
    glued = (
        "Types: deb\n"
        "URIs: http://a.example.com/ubuntu\n"
        "Suites: noble\n"
        "Components: main\n"
        "Types: deb\n"
        "URIs: http://b.example.com/ubuntu\n"
        "Suites: jammy\n"
        "Components: universe\n"
    )
    entries = parse_apt_sources(glued)

    assert [(e.url, e.suite) for e in entries] == [
        ("http://a.example.com/ubuntu", "noble"),
        ("http://b.example.com/ubuntu", "jammy"),
    ]


def test_an_indented_types_inside_an_armour_block_is_not_a_new_stanza():
    """Continuation lines belong to the field above them. A `Types:` line
    appearing inside a pasted key must not split the record."""
    stanza = (
        "Types: deb\n"
        "URIs: http://a.example.com/ubuntu\n"
        "Suites: noble\n"
        "Components: main\n"
        "Signed-By:\n"
        " -----BEGIN PGP PUBLIC KEY BLOCK-----\n"
        " Types: deb\n"
        " mQINBGXfTyoB\n"
        " -----END PGP PUBLIC KEY BLOCK-----\n"
    )
    entries = parse_apt_sources(stanza)

    assert len(entries) == 1
    assert entries[0].url == "http://a.example.com/ubuntu"
    assert "BEGIN PGP PUBLIC KEY BLOCK" in entries[0].gpg_key
