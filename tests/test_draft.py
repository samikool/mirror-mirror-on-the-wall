"""`RepositoryDraft` — the one model behind every add flow.

Probing, the "empty means all" rule and the conversion to `SourceConfig` live
here rather than in a widget, so the single-repository step, the bulk review
pane and `mmw add` cannot drift apart.
"""
import pytest

from mmw.core import discover
from mmw.core.apt_sources import parse_apt_sources
from mmw.core.draft import RepositoryDraft, drafts_from_entries, probe_all


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
    d = RepositoryDraft(url="http://example.com/ubuntu")
    d.probe()

    assert d.available["arches"] == ["amd64", "arm64"]
    assert d.stored("arches") == [], "all ticked and probed means all"
    assert d.stored("suites") == []


def test_a_narrowed_selection_is_stored_literally(stub):
    stub()
    d = RepositoryDraft(url="http://example.com/ubuntu")
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
    d = RepositoryDraft(url="http://example.com/ubuntu")
    d.probe()
    d.chosen["components"] = []
    d.probe_suite("jammy")

    assert d.chosen["components"] == ["main", "universe"]


def test_a_flat_repository_has_nothing_to_enumerate(stub):
    stub()
    d = RepositoryDraft(url="http://example.com/repo", provider="apt-flat")
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
    a, b, c = (RepositoryDraft(url="http://x/1", gpg_key="ARMOUR"),
               RepositoryDraft(url="http://x/2", gpg_key_name="foo.gpg"),
               RepositoryDraft(url="http://x/3"))

    assert (a.key_state(), b.key_state(), c.key_state()) == ("have", "needs", "none")


def test_a_supplied_key_reaches_the_source():
    d = RepositoryDraft(url="http://x/1", gpg_key_name="foo.gpg")
    d.set_key("-----BEGIN PGP PUBLIC KEY BLOCK-----", source="dir")
    src = d.to_source()

    assert src.gpg_key and src.gpg_key_name == "foo.gpg"
