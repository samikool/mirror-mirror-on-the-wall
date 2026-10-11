"""The Repositories logic, without a TUI."""
from pathlib import Path

import pytest

from mmw.core import config
from mmw.tui import repositories as R


def _cfg(*urls, **kw):
    return config.Config(
        settings=config.Settings(mirror_dir=Path("/nonexistent/pool")),
        sources=[config.SourceConfig(url=u, suites=["bookworm"], components=["main"],
                                     arches=["amd64"], **kw) for u in urls],
    )


def _model(*urls):
    m = R.RepositoryList(_cfg(*urls))
    m.refresh()              # no pool at that path: config expansion only
    return m


def test_rows_group_by_host_then_name():
    m = _model("https://b.example.com/x", "https://a.example.com/z", "https://a.example.com/y")
    kinds = [(r.kind, r.host if r.kind == "host" else r.source.url) for r in m.rows()]
    assert kinds == [
        ("host", "a.example.com"),
        ("source", "https://a.example.com/y"),
        ("source", "https://a.example.com/z"),
        ("host", "b.example.com"),
        ("source", "https://b.example.com/x"),
    ]
    assert len({r.key for r in m.rows()}) == len(m.rows())


def test_expanding_shows_mirrors_and_filter_narrows():
    m = _model("https://a.example.com/debian", "https://b.example.com/ubuntu")
    src = m.cfg.sources[0]
    m.toggle_expanded(src)
    assert [r.kind for r in m.rows()].count("mirror") == 1
    m.filter = "ubuntu"
    assert [r.source.url for r in m.rows() if r.kind == "source"] == ["https://b.example.com/ubuntu"]


def test_ticking_a_source_ticks_its_mirrors_and_untick_reverses():
    m = _model("https://a.example.com/debian")
    src = m.cfg.sources[0]
    m.toggle_source(src)
    assert m.is_selected(src) and m.selection == {x.name for x in m.mirrors_of(src)}
    m.toggle_source(src)
    assert not m.is_selected(src) and not m.selection


def test_ticking_every_mirror_ticks_the_source():
    m = _model("https://a.example.com/debian")
    src = m.cfg.sources[0]
    for x in m.mirrors_of(src):
        m.toggle_mirror(x)
    assert m.is_selected(src)


def test_removal_targets_prefer_ticked_then_highlighted():
    m = _model("https://a.example.com/one", "https://a.example.com/two", "https://a.example.com/three")
    one, two, three = m.cfg.sources
    assert m.removal_targets(two) == [two]
    m.toggle_source(one)
    m.toggle_source(three)
    assert m.removal_targets(two) == [one, three]
    assert m.removal_targets(None) == [one, three]


def test_forget_keeps_the_other_ticks():
    m = _model("https://a.example.com/one", "https://a.example.com/two")
    one, two = m.cfg.sources
    m.toggle_source(one)
    m.toggle_source(two)
    m.forget([one])
    assert m.selected_sources == {two.identity}


def test_without_and_replace_return_new_configs():
    cfg = _cfg("https://a.example.com/one", "https://a.example.com/two")
    one, two = cfg.sources
    smaller = R.without_sources(cfg, [one])
    assert [s.url for s in smaller.sources] == [two.url] and len(cfg.sources) == 2
    edited = two.model_copy(update={"name": "renamed"})
    swapped = R.replace_source(cfg, two.identity, edited)
    assert [s.name for s in swapped.sources] == [one.name, "renamed"]


def test_selection_summary_and_sync_request():
    m = _model("https://a.example.com/one", "https://a.example.com/two")
    one, two = m.cfg.sources
    assert m.selection_summary() == ""
    m.toggle_source(one)
    assert m.selection_summary() == "1 source"
    m.toggle_expanded(two)
    m.toggle_mirror(m.mirrors_of(two)[0])
    # one mirror of `two` is its whole set, so `two` counts as a source now
    assert m.selection_summary() == "2 sources"
    assert m.sync_request() == (sorted([one.identity, two.identity]), [])


def test_pool_helpers_cope_with_no_pool(tmp_path):
    assert R.pool_scale(tmp_path / "missing") is None
    assert R.pool_busy(tmp_path / "missing") is False
    assert not (tmp_path / "missing").exists()


def _edit(src, **over):
    from mmw.core.draft import RepositoryDraft
    args = dict(original=src, url=src.url, name=src.name, label=src.label, key_url="",
                include_sources=False, dist_upgrade=False, extra_files=[], overrides_text="",
                other_names=set(), other_urls=set(), suites_started_empty=not src.suites)
    args.update(over)
    return R.edited_source(RepositoryDraft.from_source(src), **args)


def test_edited_source_round_trips_unchanged():
    src = _cfg("https://a.example.com/debian").sources[0]
    out, err, _ = _edit(src)
    assert err is None and out.url == src.url and out.suites == src.suites


@pytest.mark.parametrize("over,needle,focus", [
    ({"name": "taken"}, "already the name", "edit-source-name"),
    ({"url": "https://b.example.com/other"}, "already has that URL", "edit-source-url"),
    ({"url": "ftp://a.example.com/debian"}, "must start with http", "edit-source-url"),
    ({"key_url": "file:///k.gpg"}, "key URL must start", "edit-source-key-url"),
    ({"overrides_text": "bookworm"}, "suite override line 1", "edit-source-overrides"),
    ({"extra_files": ["https://x.example.com/a?b=1"]}, "cannot be served", "edit-source-extra-files"),
])
def test_edited_source_refuses_with_a_reason_and_a_field(over, needle, focus):
    src = _cfg("https://a.example.com/debian").sources[0]
    over.setdefault("other_names", {"taken"})
    over.setdefault("other_urls", {config.normalize_url("https://b.example.com/other")})
    out, err, field = _edit(src, **over)
    assert out is None and needle in err and field == focus


def test_merge_new_sources_counts_and_leaves_an_unchanged_config_alone():
    cfg = _cfg("https://a.example.com/one")
    new = [config.SourceConfig(url="https://b.example.com/two", suites=["bookworm"],
                               components=["main"], arches=["amd64"])]
    merged, added, updated = R.merge_new_sources(cfg, new)
    assert (added, updated) == (1, 0) and len(merged.sources) == 2 and len(cfg.sources) == 1
    same, added, updated = R.merge_new_sources(cfg, [cfg.sources[0]])
    assert same is cfg and (added, updated) == (0, 0)
    assert R.added_summary(2, 1) == "added 2, updated 1"
    assert R.added_summary(0, 3) == "updated 3"


def _rm(name, source):
    return config.MirrorConfig(name=name, provider="apt", source=source, source_url=source,
                               arch="arm64", suite="noble", component="main")


def test_narrow_to_selection_keeps_identities_and_never_widens():
    resolved = [_rm("spark-noble-main-arm64", "https://a.example.com/spark"),
                _rm("other-noble-main-arm64", "https://b.example.com/other")]
    got, problem, _ = R.narrow_to_selection(resolved, [], (["https://a.example.com/spark"], []))
    assert [m.name for m in got] == ["spark-noble-main-arm64"] and problem is None
    got, problem, is_error = R.narrow_to_selection(resolved, [], (["https://gone.example.com/x"], []))
    assert got == [] and "Nothing selected could be resolved" in problem and is_error
    got, problem, _ = R.narrow_to_selection(resolved, [], ((), ()))
    assert len(got) == 2
    got, problem, is_error = R.narrow_to_selection([], ["boom"], ((), ()))
    assert got == [] and problem == "No mirrors could be resolved." and is_error


def test_resolve_selection_is_quiet_and_returns_messages(monkeypatch):
    import mmw.tui.repositories as repos_mod
    resolved = [_rm("a-noble-main-arm64", "https://a.example.com/a")]
    monkeypatch.setattr(repos_mod, "resolve_sources",
                        lambda sources, progress=None: (resolved, ["a: 1 arch"], []))
    mirrors, messages = R.resolve_selection(_cfg("https://a.example.com/a"), ((), ()))
    assert [m.name for m in mirrors] == ["a-noble-main-arm64"]
    assert messages == ["a: 1 arch"]
