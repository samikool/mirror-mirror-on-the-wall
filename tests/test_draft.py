"""`SourceDraft` — the one model behind every add flow.

Probing, the "empty means all" rule and the conversion to `SourceConfig` live
here rather than in a widget, so the single-repository step, the bulk review
pane and `mmw add` cannot drift apart.
"""
import pytest

from mmw.core import discover
from mmw.core.apt_sources import parse_apt_sources
from mmw.core.config import SourceConfig
from mmw.core.draft import DIMENSIONS, SourceDraft, drafts_from_entries, probe_all, resolve_placeholders


@pytest.fixture
def stub(monkeypatch):
    def apply(suites=("noble", "jammy"), components=("main", "universe"),
              arches=("amd64", "arm64"), release=([], [])):
        monkeypatch.setattr(discover, "list_suites", lambda url: list(suites))
        monkeypatch.setattr(discover, "list_components", lambda url, s: list(components))
        monkeypatch.setattr(discover, "list_arches", lambda url, s, c: list(arches))
        monkeypatch.setattr(discover, "release_dimensions", lambda url, s: release)
    return apply


# ── the empty-means-all rule ─────────────────────────────────────────────────

def test_everything_probed_and_ticked_is_stored_as_all(stub):
    """A frozen full list would stop following upstream — the bug
    `NetworkConfig.mirrors` had."""
    stub()
    d = SourceDraft(url="http://example.com/ubuntu")
    d.probe()

    assert d.available["arches"] == ["amd64", "arm64"]
    assert d.stored("arches") == [], "all ticked and probed means all"
    assert d.stored("suites") == []


def test_a_narrowed_selection_is_stored_literally(stub):
    stub()
    d = SourceDraft(url="http://example.com/ubuntu")
    d.probe()
    d.chosen["arches"] = ["arm64"]

    assert d.stored("arches") == ["arm64"]


def test_an_unprobed_selection_never_collapses_to_all(stub):
    """`available` is then merely what a pasted config named. Collapsing it
    would turn an explicit `Suites: noble` into every suite the repo has."""
    stub(suites=(), components=(), arches=())
    d = drafts_from_entries(parse_apt_sources(
        "deb [arch=arm64] http://example.com/ubuntu noble main\n"))[0]
    d.probe()

    assert d.enumerated["suites"] is False
    assert d.stored("suites") == ["noble"]
    assert d.stored("arches") == ["arm64"]


# ── probing widens the offer without moving the selection ────────────────────

def test_probing_offers_the_rest_but_keeps_the_config_selection(stub):
    """The case that motivated the redesign: paste an arm64-only config and
    every architecture the repository serves is shown, with only arm64 ticked."""
    stub(arches=("amd64", "arm64", "i386"))
    d = drafts_from_entries(parse_apt_sources(
        "deb [arch=arm64] http://example.com/ubuntu noble main\n"))[0]
    d.probe()

    assert d.available["arches"] == ["amd64", "arm64", "i386"]
    assert d.chosen["arches"] == ["arm64"]
    assert d.to_source().arches == ["arm64"]


def test_a_stated_value_the_listing_missed_stays_offered(stub):
    stub(suites=("jammy",))
    d = drafts_from_entries(parse_apt_sources(
        "deb http://example.com/ubuntu noble main\n"))[0]
    d.probe()

    assert d.available["suites"] == ["jammy", "noble"]
    assert d.chosen["suites"] == ["noble"]


def test_the_release_file_answers_when_nothing_lists(stub):
    """S3/CDN-backed vendor repos 404 every dists/ request while serving the
    files fine. Listing stays authoritative wherever it works."""
    stub(components=(), arches=(), release=(["main"], ["arm64"]))
    d = drafts_from_entries(parse_apt_sources(
        "deb http://example.com/repo noble main\n"))[0]
    d.probe()

    assert d.available["arches"] == ["arm64"]
    assert d.enumerated["arches"] is True


def test_probe_suite_refollows_a_different_suite(stub):
    stub()
    d = SourceDraft(url="http://example.com/ubuntu")
    d.probe()
    d.chosen["components"] = []
    d.probe_suite("jammy")

    assert d.chosen["components"] == ["main", "universe"]


def test_a_flat_repository_has_nothing_to_enumerate(stub):
    stub()
    d = SourceDraft(url="http://example.com/repo", provider="apt-flat")
    d.probe()

    assert d.available["suites"] == []
    assert d.to_source().provider == "apt-flat"


# ── grouping a bundle ────────────────────────────────────────────────────────

MULTI = """\
Types: deb
URIs: http://a.example.com/ubuntu
Suites: noble noble-updates
Components: main

Types: deb
URIs: http://b.example.com/ubuntu
Suites: noble
Components: main
Architectures: arm64
"""


def test_a_bundle_becomes_one_draft_per_repository():
    drafts = drafts_from_entries(parse_apt_sources(MULTI))

    assert [d.url for d in drafts] == [
        "http://a.example.com/ubuntu", "http://b.example.com/ubuntu"]
    assert drafts[0].chosen["suites"] == ["noble", "noble-updates"]
    assert drafts[1].chosen["arches"] == ["arm64"]
    assert len({d.name for d in drafts}) == 2, "names must not collide"


def test_probe_all_probes_every_draft(stub):
    stub()
    drafts = drafts_from_entries(parse_apt_sources(MULTI))
    probe_all(drafts)

    assert all(d.probed for d in drafts)
    # everything the repo lists, plus anything the config named that it missed
    assert drafts[0].available["suites"] == ["noble", "jammy", "noble-updates"]
    assert drafts[1].available["suites"] == ["noble", "jammy"]


def test_one_unreachable_repository_does_not_stop_the_rest(monkeypatch):
    def boom(url):
        if "b.example" in url:
            raise OSError("connection refused")
        return ["noble"]

    monkeypatch.setattr(discover, "list_suites", boom)
    monkeypatch.setattr(discover, "list_components", lambda url, s: ["main"])
    monkeypatch.setattr(discover, "list_arches", lambda url, s, c: ["amd64"])
    monkeypatch.setattr(discover, "release_dimensions", lambda url, s: ([], []))

    drafts = drafts_from_entries(parse_apt_sources(MULTI))
    probe_all(drafts)

    assert drafts[0].error == "" and drafts[0].available["arches"] == ["amd64"]
    assert "connection refused" in drafts[1].error


# ── keys ─────────────────────────────────────────────────────────────────────

def test_key_state_distinguishes_have_from_needs():
    a, b, c = (SourceDraft(url="http://x/1", gpg_key="ARMOUR"),
               SourceDraft(url="http://x/2", gpg_key_name="foo.gpg"),
               SourceDraft(url="http://x/3"))

    assert (a.key_state(), b.key_state(), c.key_state()) == ("have", "needs", "none")


def test_a_supplied_key_reaches_the_source():
    d = SourceDraft(url="http://x/1", gpg_key_name="foo.gpg")
    d.set_key("-----BEGIN PGP PUBLIC KEY BLOCK-----", source="dir")
    src = d.to_source()

    assert src.gpg_key and src.gpg_key_name == "foo.gpg"


# ── editing an existing source (`from_source`) ──────────────────────────────
# `from_source()` is `to_source()`'s mirror image: opening an existing config
# entry for editing must reproduce it exactly if the user probes nothing and
# changes nothing. The "empty means all" rule — the case that
# matters is the empty one, not the explicit one.

def test_an_explicit_selection_round_trips_unchanged():
    src = SourceConfig(url="http://example.com/ubuntu", suites=["noble"],
                        components=["main"], arches=["arm64"])
    d = SourceDraft.from_source(src)

    assert d.to_source().suites == ["noble"]
    assert d.to_source().components == ["main"]
    assert d.to_source().arches == ["arm64"]


def test_empty_dimensions_survive_the_round_trip_as_empty():
    """The important case: a source following upstream must reopen as still
    following upstream, not freeze at whatever `available` happens to hold."""
    src = SourceConfig(url="http://example.com/ubuntu")
    d = SourceDraft.from_source(src)

    for dim in DIMENSIONS:
        assert d.chosen[dim] == []
        assert d.available[dim] == []
    assert d.to_source().suites == []
    assert d.to_source().components == []
    assert d.to_source().arches == []


def test_every_dimension_starts_unenumerated():
    """Nothing has been listed yet — only a `probe()` may set this."""
    src = SourceConfig(url="http://example.com/ubuntu", arches=["amd64"])
    d = SourceDraft.from_source(src)

    assert all(d.enumerated[dim] is False for dim in DIMENSIONS)


def test_metadata_and_inline_key_carry_across():
    src = SourceConfig(url="http://example.com/ubuntu", name="my-ubuntu", provider="apt-flat",
                        include_sources=True, gpg_key="-----BEGIN PGP PUBLIC KEY BLOCK-----",
                        gpg_key_name="ubuntu.asc")
    d = SourceDraft.from_source(src)

    assert d.name == "my-ubuntu"
    assert d.provider == "apt-flat"
    assert d.include_sources is True
    assert d.gpg_key == "-----BEGIN PGP PUBLIC KEY BLOCK-----"
    assert d.gpg_key_name == "ubuntu.asc"
    assert d.key_source == "inline"


def test_a_needs_only_key_name_is_not_marked_inline():
    """`key_state()` distinguishes `have` from `needs` — a source that only
    named a keyring (`Signed-By: /usr/share/...`) has no key material yet."""
    src = SourceConfig(url="http://example.com/ubuntu", gpg_key_name="ubuntu.asc")
    d = SourceDraft.from_source(src)

    assert d.gpg_key is None
    assert d.key_source == ""
    assert d.key_state() == "needs"


def test_probing_after_from_source_widens_available_without_moving_chosen(stub):
    """The behaviour the edit screen depends on: reopening an arm64-only source
    and probing shows every arch the repository serves, with only arm64 still
    ticked — exactly like a pasted config, because both go through `probe()`."""
    stub(suites=("noble",), components=("main",), arches=("amd64", "arm64", "i386"))
    src = SourceConfig(url="http://example.com/ubuntu", suites=["noble"],
                        components=["main"], arches=["arm64"])
    d = SourceDraft.from_source(src)
    d.probe()

    assert d.available["arches"] == ["amd64", "arm64", "i386"]
    assert d.chosen["arches"] == ["arm64"]
    assert d.enumerated["arches"] is True
    assert d.to_source().arches == ["arm64"]


# ── $(ARCH) placeholder resolution ───────────────────────────────────────────
# See core/apt_sources.py's arch_placeholder and the "never invent an
# architecture" — apt's own $(ARCH) substitution has no local machine to run
# on here, so this is resolved by probing each known architecture instead
# (discover.expand_arch_placeholder, monkeypatched below rather than hitting
# the network, matching this file's existing convention).

def test_resolve_placeholders_expands_into_one_entry_per_arch(monkeypatch):
    monkeypatch.setattr(discover, "expand_arch_placeholder",
                         lambda url, token: ["amd64", "arm64"])

    entries = parse_apt_sources("deb https://nvidia.example/deb/$(ARCH) /\n")
    resolved = resolve_placeholders(entries)

    assert [e.url for e in resolved] == [
        "https://nvidia.example/deb/amd64",
        "https://nvidia.example/deb/arm64",
    ]
    assert all(e.arch_placeholder is None for e in resolved)


def test_resolve_placeholders_drops_entries_that_resolve_to_nothing(monkeypatch):
    monkeypatch.setattr(discover, "expand_arch_placeholder", lambda url, token: [])

    entries = parse_apt_sources("deb https://x.example/deb/$(ARCH) /\n")
    assert resolve_placeholders(entries) == []


def test_resolve_placeholders_leaves_ordinary_entries_untouched(monkeypatch):
    monkeypatch.setattr(discover, "expand_arch_placeholder",
                         lambda url, token: pytest.fail("should not be called"))

    entries = parse_apt_sources("deb http://archive.ubuntu.com/ubuntu noble main\n")
    assert resolve_placeholders(entries) == entries


def test_placeholder_drafts_go_through_the_same_grouping_as_any_other(monkeypatch):
    """The point of splitting resolution out is that `drafts_from_entries`
    itself needs no special case — feeding it already-resolved entries is
    indistinguishable from a config that always named concrete arch URLs."""
    monkeypatch.setattr(discover, "expand_arch_placeholder",
                         lambda url, token: ["amd64", "arm64", "ppc64el"])

    entries = parse_apt_sources("deb https://nvidia.example/deb/$(ARCH) /\n")
    drafts = drafts_from_entries(resolve_placeholders(entries))

    assert len(drafts) == 3
    assert {d.url for d in drafts} == {
        "https://nvidia.example/deb/amd64",
        "https://nvidia.example/deb/arm64",
        "https://nvidia.example/deb/ppc64el",
    }
    assert all(d.provider == "apt-flat" for d in drafts)


def test_source_draft_takes_its_dimensions_from_the_provider():
    from mmw.core import draft
    from mmw.providers import get_provider
    assert draft.DIMENSIONS == tuple(d.name for d in get_provider("apt").dimensions)
    assert not hasattr(draft, "RepositoryDraft")
    assert draft.SourceDraft(url="http://x.example/ubuntu").provider == "apt"
