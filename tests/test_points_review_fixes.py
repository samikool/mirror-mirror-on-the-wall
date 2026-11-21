"""Findings from the 1.5.6 review: each failed before its fix."""
import lzma

from typer.testing import CliRunner

from mmw import cli
from mmw.core import config
from mmw.core.pool import Pool
from mmw.site.db import SiteDB
from test_legacy_points_promoted import old  # noqa: F401
from test_repo_merge import NEW as C_NEW, OLD as C_OLD, two as connected_two  # noqa: F401
from test_repository_identity import _apply, bundle, release_tree, site  # noqa: F401
from test_site_merge import NEW, NEW_BASE, OLD, two  # noqa: F401

UK, UK_BASE = "https://ftp.uk.debian.org/debian", "ftp.uk.debian.org/debian"


def _days(home, repo, suite="bookworm"):
    with SiteDB(home) as db:
        return sorted(p.date[:8] for t in db.tracks(repo) if t.name.endswith(f"/dists/{suite}")
                      for p in db.points(t.id, stubs=True))


# C1 — a corrupt legacy index never stops the pool opening
def test_a_corrupt_legacy_index_never_stops_the_pool(old, monkeypatch):  # noqa: F811
    from mmw.site import tracks
    cfg, md, units, _snap, _a, _b = old

    def broken(_data):
        raise lzma.LZMAError("corrupt input data")
    monkeypatch.setitem(tracks._DECOMPRESS, "", broken)
    with Pool(md) as pool:                         # the upgrade runs here, and must not raise
        assert not pool.repository_points(units[0].source)
        assert pool._conn.execute("SELECT 1 FROM history_upgrades WHERE name = 'legacy-points'").fetchone()


# I2 — what stayed one network's is said where its dates are chosen
def test_kept_legacy_history_is_said_in_the_transfer_preview(old, monkeypatch):  # noqa: F811
    from mmw.core.transfer_ranges import preview_transfer
    from mmw.site import tracks
    cfg, md, _units, _snap, a, _b = old
    monkeypatch.setitem(tracks._DECOMPRESS, "", lambda _d: (_ for _ in ()).throw(EOFError("truncated")))
    with Pool(md):
        pass
    monkeypatch.undo()
    preview = preview_transfer(a, md, cfg=cfg, full=True)
    assert any("kept as a's only" in w for w in preview.warnings), preview.warnings


# C2 / I4 — another network's bundle never moves what this one serves
def test_a_foreign_bundle_is_rebased_onto_this_sites_base(site, tmp_path):  # noqa: F811
    home, root = site
    _apply(root, bundle(tmp_path / "o.tar", "oak", {NEW: ("new", NEW_BASE, [release_tree(3, base=NEW_BASE)])}))
    lines = _apply(root, bundle(tmp_path / "e.tar", "elm", {UK: ("uk", UK_BASE, [release_tree(3, base=UK_BASE),
                                                                                 release_tree(5, base=UK_BASE)])},
                                created="20261116T000000Z"))
    with SiteDB(home) as db:
        (st,) = db.repo_states()
        assert st.repo == NEW and st.bases == [NEW_BASE], lines
        assert all(t.name.startswith(NEW_BASE) for t in db.tracks(NEW) if t.name)
    assert _days(home, NEW) == ["20261103", "20261105"]
    assert (root / NEW_BASE / "pool/main/h/hello/hello_5_amd64.deb").exists()
    assert not (root / UK_BASE).exists()


# C3 — two arriving keys for one repository here never overwrite each other
def test_two_arriving_keys_for_one_repository_lose_nothing(site, tmp_path):  # noqa: F811
    home, root = site
    _apply(root, bundle(tmp_path / "o.tar", "oak", {NEW: ("new", NEW_BASE, [release_tree(3, base=NEW_BASE)])}))
    lines = _apply(root, bundle(tmp_path / "b.tar", "oak", {
        NEW: ("new", NEW_BASE, [release_tree(5, base=NEW_BASE)]),
        UK: ("uk", UK_BASE, [release_tree(3, base=UK_BASE)])}, created="20261116T000000Z"))
    assert "20261105" in _days(home, NEW), lines
    assert any("mmw site merge" in line for line in lines), lines


# I1 — a connected merge stopped before the config was saved can be finished
def test_an_interrupted_merge_is_finished(connected_two):  # noqa: F811
    from mmw.core.repo_merge import merge_repositories
    cfg, md, home = connected_two
    with Pool(md) as pool:
        merge_repositories(pool, cfg, C_OLD, C_NEW)          # the history moved; the config never saved
    assert len(config.load(home).sources) == 2
    r = CliRunner().invoke(cli.app, ["--config", str(home), "merge", "old", "new", "--yes"])
    assert r.exit_code == 0, r.output
    assert [s.identity for s in config.load(home).sources] == [C_NEW]


# I3 — site merge keeps what the older repository had retired or pinned
def test_site_merge_keeps_retired_suites_and_pins(two):  # noqa: F811
    from mmw.site.merge import merge_site_repositories
    home, root = two
    with SiteDB(home) as db:
        (t,) = [t for t in db.tracks(OLD) if t.name]
        first = min(db.points(t.id), key=lambda p: p.date)
        db.set_pin(t.id, first.id)
    _apply(root, bundle(tmp_path_of(home) / "x.tar", "oak", {OLD: ("old", "old.deb.debian.org/debian", [
        release_tree(4, base="old.deb.debian.org/debian", suite="trixie")])}, created="20261117T000000Z"))
    with SiteDB(home) as db:
        (trixie,) = [t for t in db.tracks(OLD) if t.name.endswith("/trixie")]
        db.set_present(OLD, {t.name for t in db.tracks(OLD) if not t.name.endswith("/trixie")}, network="oak")
        assert db.track(trixie.id).gone
    merge_site_repositories(home, "old", "new")
    with SiteDB(home) as db:
        tracks = {t.name.rsplit("/", 1)[-1]: t for t in db.tracks(NEW) if t.name}
        assert tracks["trixie"].gone
        assert tracks["bookworm"].pinned is not None
        assert db.point(tracks["bookworm"].pinned).date[:8] == "20261101"
    assert not (root / NEW_BASE / "dists/trixie").exists()


def tmp_path_of(home):
    return home.parent.parent.parent
