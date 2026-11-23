"""A repository taken out of a network: its next bundle names it as `left`,
and the far side stops serving it once no network brings it."""
import json
import tarfile

from mmw.core import config
from mmw.site.db import SiteDB

from site_apt_rows import Repo
from site_format3 import write_format3

from mmw.site.apply import apply_to_set
from mmw.site.home import init_root
from mmw.site.mset import open_set
from mmw.site.tracks import split_tracks


def _header(path):
    with tarfile.open(path) as tar:
        return json.loads(tar.extractfile(".mmw/manifest.jsonl").readline())


def _serves(db, repo) -> bool:
    state = db.repo_state(repo)
    return state is not None and state.serving is not None and bool(db.state_points(state.serving.id))


def _two_sources(tmp_path):
    """Two synced apt repositories with repository points (as a sync records them)."""
    import hashlib
    from mmw.core import points
    from mmw.core.pool import KIND_META, KIND_PKG, KIND_RELEASE, Pool
    md = tmp_path / "mirror"
    sources = [config.SourceConfig(url=f"https://apt.test/{name}", suites=["stable"], components=["main"],
                                   arches=["amd64"]) for name in ("one", "two")]
    release = b"Suite: stable\nDate: Sun, 01 Nov 2026 00:00:00 UTC\n"
    with Pool(md) as pool:
        for src in sources:
            for unit in src.to_mirrors():
                files = [(f"pool/{src.url[-3:]}.deb", src.url.encode(), KIND_PKG),
                         ("dists/stable/Release", release, KIND_RELEASE)]
                index = f"Package: x\nFilename: pool/{src.url[-3:]}.deb\n\n".encode()
                files.append(("dists/stable/main/binary-amd64/Packages", index, KIND_META))
                entries = []
                for name, body, kind in files:
                    cs = hashlib.sha256(body).hexdigest()
                    pool.add(body, cs, "sha256", name.rsplit("/", 1)[-1], len(body))
                    entries.append((cs, name, kind))
                pool.reconcile_refs(unit.name, entries, src.identity)
                pool.record_mirror(unit)
    net = config.NetworkConfig(name="oak", nginx=False, certs=False,
                               select=[config.NetworkSelect(source=s.identity) for s in sources])
    cfg = config.Config(settings=config.Settings(mirror_dir=md), sources=sources, networks=[net])
    points.record_points_after_sync(cfg, md, {s.identity for s in sources}, say=lambda _: None)
    return cfg, net, md, [s.identity for s in sources]


def test_a_repository_that_leaves_the_network_retires_on_the_far_side(tmp_path, monkeypatch):
    from mmw.core.publisher.bundle import bundle_network
    from mmw.site.apply import apply_bundle
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg, net, md, (one, two) = _two_sources(tmp_path)
    home = init_root(tmp_path / "srv", name="main", set_id="s1", network=net.name)
    first = bundle_network(net, cfg, md, out_path=tmp_path / "1.tar").out_path
    assert set(_header(first)["points"]) == {one, two} and "left" not in _header(first)
    apply_bundle(home, str(first), say=lambda *a: None)
    with SiteDB(home) as db:
        assert _serves(db, one) and _serves(db, two)

    net = net.model_copy(update={"select": [config.NetworkSelect(source=one)]})
    cfg = cfg.model_copy(update={"networks": [net]})
    second = bundle_network(net, cfg, md, out_path=tmp_path / "2.tar").out_path
    assert two not in _header(second)["points"] and _header(second)["left"] == [two]
    result = apply_bundle(home, str(second), say=lambda *a: None)

    with SiteDB(home) as db:
        assert _serves(db, one)
        assert all(t.gone for t in db.tracks(two))
        assert not _serves(db, two)
    assert any("no longer in oak" in line for line in result.lines)
    # Said once: a later bundle doesn't name it again.
    third = bundle_network(net, cfg, md, out_path=tmp_path / "3.tar", full=True).out_path
    assert "left" not in _header(third)


BASE = "ex.org/debian"
REPO = "debian"


def _site_apply(root, tmp_path, network, day, suites, left=None):
    repo = Repo(BASE)
    for suite in suites:
        repo.suite(suite, f"{day:02d} Nov 2026 00:00:00 UTC", [])
    stamp = f"202611{day:02d}T000000Z"
    points = [(d, stamp, None) for d in split_tracks(repo.rows, [BASE], repo.read, stamp)]
    path = write_format3(tmp_path / f"{network}-{day}.tar", network,
                         {REPO: ("Debian", [BASE], points)} if suites else {},
                         repo.blobs, created=stamp, selection=True, left=left)
    return apply_to_set(open_set([str(root)]), path, place={"*": str(root)}, say=lambda *a: None)


def test_a_repository_another_network_still_brings_stays_served(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "srv"
    home = init_root(root, name="main", set_id="s1", network="oak")
    release = root / BASE / "dists/bookworm/Release"
    _site_apply(root, tmp_path, "oak", 1, ["bookworm"])
    _site_apply(root, tmp_path, "elm", 2, ["bookworm"])

    _site_apply(root, tmp_path, "oak", 3, [], left=[REPO])
    assert release.is_file()
    with SiteDB(home) as db:
        assert _serves(db, REPO)

    _site_apply(root, tmp_path, "elm", 4, [], left=[REPO])
    assert not release.exists()
    with SiteDB(home) as db:
        assert not _serves(db, REPO)
