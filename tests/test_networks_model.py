"""The Networks logic, without a TUI."""
from pathlib import Path

import pytest

from mmw.core import config
from mmw.tui import networks as N


def _sel(url="https://deb.example.com/debian"):
    return config.NetworkSelect(source=url)


def _cfg(*names):
    return config.Config(
        settings=config.Settings(mirror_dir=Path("/nonexistent/pool")),
        networks=[config.NetworkConfig(name=n, select=[_sel()]) for n in names],
    )


@pytest.mark.parametrize("kw,needle", [
    (dict(selects=[]), "nothing selected publishes nothing"),
    (dict(name="a/b"), "letters, digits"),
    (dict(name=".."), "letters, digits"),
    (dict(name=""), "letters, digits"),
    (dict(name="lab", existing_names={"lab"}), "already exists"),
    (dict(bundle_tls=True, certs=False), "needs TLS certs"),
])
def test_form_problems(kw, needle):
    args = dict(name="lab", existing_names=set(), selects=[_sel()], certs=False, bundle_tls=False)
    args.update(kw)
    assert needle in N.form_problems(**args)


def test_form_problems_none_when_fine():
    assert N.form_problems(name="lab-2", existing_names={"lab"}, selects=[_sel()],
                           certs=True, bundle_tls=True) is None


def test_with_without_and_replace():
    cfg = _cfg("a", "b")
    added = N.with_network(cfg, config.NetworkConfig(name="c", select=[_sel()]))
    assert [n.name for n in added.networks] == ["a", "b", "c"] and len(cfg.networks) == 2
    assert [n.name for n in N.without_network(cfg, "a").networks] == ["b"]
    renamed = config.NetworkConfig(name="z", select=[_sel()], prune=False)
    out = N.replace_network(cfg, "b", renamed)
    assert [(n.name, n.prune) for n in out.networks] == [("a", True), ("z", False)]


def test_clear_ledger_tolerates_a_missing_directory(tmp_path):
    assert N.clear_ledger(tmp_path, "lab") is None


def test_clear_ledger_removes_it(tmp_path):
    from mmw.core.publisher.manifest import ledger_dir
    d = ledger_dir(tmp_path, "lab")
    d.mkdir(parents=True)
    (d / "x.jsonl").write_text("{}")
    assert N.clear_ledger(tmp_path, "lab") is None
    assert not d.exists()


def _mm(name, base_url):
    return config.MirrorConfig(name=name, provider="apt", source=base_url, source_url=base_url,
                               arch="amd64", suite="noble", component="main")


def test_stale_trees_use_the_old_root(tmp_path):
    a = _mm("a", "https://a.example.com/u")
    b = _mm("b", "https://b.example.com/d")
    old = config.NetworkConfig(name="lab", root=tmp_path / "old", select=[])
    (tmp_path / "old" / b.mirror_base).mkdir(parents=True)
    dirs, shared, note = N.stale_trees(old, old.model_copy(update={"select": []}),
                                       {"a": a, "b": b}, removed={"b"}, kept={"a"})
    assert dirs == [tmp_path / "old" / b.mirror_base] and note is None


def test_stale_trees_offer_nothing_when_the_root_moved(tmp_path):
    """Narrowing and re-rooting in one Save used to offer the *new* root's
    directory — possibly another network's tree — for deletion."""
    a = _mm("a", "https://a.example.com/u")
    b = _mm("b", "https://b.example.com/d")
    old = config.NetworkConfig(name="lab", root=tmp_path / "old", select=[])
    new = old.model_copy(update={"root": tmp_path / "other"})
    (tmp_path / "other" / b.mirror_base).mkdir(parents=True)
    dirs, shared, note = N.stale_trees(old, new, {"a": a, "b": b}, removed={"b"}, kept={"a"})
    assert dirs == [] and shared == []
    assert str(tmp_path / "old") in note
