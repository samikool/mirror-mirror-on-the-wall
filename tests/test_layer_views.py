"""A layer is a view of a repository point (Task 20).

The oracle (`test_layered_bundle_bytes_unchanged`) was captured once, from the
code before views:
    MMW_UPDATE_GOLDEN=1 uv run pytest tests/test_layer_views.py -k bytes_unchanged
Never regenerate it to make a later change pass — a diff here IS the bug.
"""
import json
import os
import tarfile
from pathlib import Path

import shutil

import pytest
from apt_upstream import UpstreamRepo, sync_upstream
from debs import make_deb

from mmw.core import config, points
from mmw.core.layers import layer_dir, rebuild_current_views
from mmw.core.pool import Pool
from mmw.core.publisher import publish_as_of, publish_network
from mmw.core.publisher.bundle import bundle_network
from mmw.core.repository_points import current_repository_points, files_for_settings
from mmw.core.signing import Keyring

GOLDEN = Path(__file__).parent / "golden" / "layer_bundle.json"
FIRST, LATER = "Sun, 01 Nov 2026 06:00:00 UTC", "Mon, 02 Nov 2026 06:00:00 UTC"


def _sync(lab, stamp, monkeypatch):
    sync_upstream(lab.cfg, lab.src)
    with monkeypatch.context() as m:
        m.setattr("mmw.core.publisher.manifest.now_stamp", lambda: stamp)
        return points.record_points_after_sync(lab.cfg, lab.md, {lab.src.identity}, say=lambda *_: None)


def _make(tmp_path, monkeypatch, *, layer=None, networks=None, layered_from=1):
    """A real upstream (jammy main, amd64) synced on 1 and 2 Nov, each
    Release dated the day it syncs, and network `lab` serving it with a layer
    (unsigned unless `layer` says) of one deb from sync `layered_from` on."""
    hello = make_deb(tmp_path / "u", "hello", "2.10-2")
    up = UpstreamRepo(tmp_path, {("jammy", "main"): [hello]})
    up.republish({("jammy", "main"): [hello]}, date=FIRST)
    src = up.source(["jammy"])

    class Lab:
        pass
    lab = Lab()
    lab.up, lab.src, lab.md, lab.tmp = up, src, tmp_path / "m", tmp_path
    layer = layer or {"signing": "unsigned"}
    lab.net = config.NetworkConfig(name="lab", nginx=False, certs=False, root=tmp_path / "pub",
                                   layers=[config.NetworkLayer(source=src.url, **layer)])
    plain_lab = lab.net.model_copy(update={"layers": []})
    lab.cfg = config.Config(settings=config.Settings(mirror_dir=lab.md), sources=[src],
                            networks=[lab.net if layered_from == 1 else plain_lab, *(networks or [])])
    lab.folder = layer_dir(lab.md, "lab", src.name) / "jammy/main"
    if layer.get("signing") == "resign":
        Keyring(lab.md).generate(layer["key"])
    if layered_from == 1:
        make_deb(lab.folder, "tools-ours", "1.0")
    _sync(lab, "20261101T120000Z", monkeypatch)
    up.republish({("jammy", "main"): [hello, make_deb(tmp_path / "u", "cowsay", "3.03")]}, date=LATER)
    if layered_from == 2:
        make_deb(lab.folder, "tools-ours", "1.0")
        lab.cfg = lab.cfg.model_copy(update={"networks": [lab.net, *(networks or [])]})
    _sync(lab, "20261102T120000Z", monkeypatch)
    return lab


@pytest.fixture
def lab(tmp_path, monkeypatch):
    lab = _make(tmp_path, monkeypatch)
    yield lab
    lab.up.close()


def _current(lab) -> int:
    with Pool(lab.md, read_only=True) as pool:
        pid, = current_repository_points(pool, lab.src.identity)
    return pid


def _debs(files) -> set[str]:
    return {p.rsplit("/", 1)[1] for p in files if "/pool/mmw/" in p}


def _settings(lab, net=None):
    """A recording's settings for `net` (default `lab`) as the config has it now."""
    net = net or lab.net
    from mmw.core.resolve import network_mirrors
    return json.dumps({"network": net.model_dump(mode="json"),
                       "mirrors": [u.legacy_dict() for u in network_mirrors(net, lab.cfg, lab.md)]})


def _rows(path, port):
    """The bundle's served files and the points it names: no stamps, no
    port, and not the upstream's key (a new one each run)."""
    with tarfile.open(path) as tar:
        text = tar.extractfile(".mmw/manifest.jsonl").read().decode()
        members = sorted(n for n in tar.getnames() if n.startswith("blobs/"))
    lines = text.replace(f"127.0.0.1:{port}", "UP").replace(f"127.0.0.1_{port}", "UP").splitlines()
    header = json.loads(lines[0])
    rows = sorted((r["p"], r["c"], r["s"], r.get("r")) for r in map(json.loads, lines[1:])
                  if not r["p"].startswith("keys/"))
    pts = {repo: sorted((p["track"], p["ident"]) for p in entry["points"] if p["track"])
           for repo, entry in header["points"].items()}
    wanted = {r[1] for r in rows}
    return {"points": pts, "rows": rows, "full": header["full"],
            "blobs": [m for m in members if m.rsplit("/", 1)[1] in wanted]}


def test_layered_bundle_bytes_unchanged(lab):
    port = lab.up.server.server_address[1]
    cfg, net, md, out = lab.cfg, lab.net, lab.md, lab.tmp

    def rows(name, **kw):
        return _rows(bundle_network(net, cfg, md, out_path=out / name, **kw).out_path, port)
    actual = {"current": rows("a.tar", full=True)}
    make_deb(lab.folder, "new-tool", "1.0")
    actual["incremental"] = rows("b.tar")
    actual["edited"] = rows("c.tar", full=True)
    actual["dated"] = rows("d.tar", full=True, until="2026-11-01")
    if os.environ.get("MMW_UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(json.dumps(actual, indent=1, sort_keys=True) + "\n")
    expected = json.loads(GOLDEN.read_text())
    assert _layout(json.loads(json.dumps(actual))) == _layout(expected)


_COMPRESSED = (".gz", ".xz", ".deb")


def _layout(bundles: dict) -> dict:
    """The bundles' layout, not their bytes: each checksum becomes a token by
    first appearance in path order, and compressed files lose their sizes. A
    Python's zlib and liblzma builds differ (3.11 and 3.12 make other bytes
    than 3.10 and 3.13), so what a bundle carries is pinned, not how its
    indexes compress."""
    import re
    hexes = re.compile(r"[0-9a-f]{64}")
    tokens: dict[str, str] = {}

    def tok(text: str) -> str:
        return hexes.sub(lambda m: tokens.setdefault(m.group(0), f"#{len(tokens) + 1}"), text)
    out = {}
    for name in sorted(bundles):            # rows first: path order fixes every token
        rows = bundles[name]["rows"]
        named = {(p.rsplit("/", 1)[0], c): p.rsplit("/", 1)[1] for p, c, _s, _r in rows if "/by-hash/" not in p}

        def path(p: str, c: str) -> str:
            # A by-hash copy is named for the index it duplicates — its own
            # name is a hash, and would order the rows by bytes.
            if "/by-hash/SHA256/" not in p:
                return p
            parent = p.split("/by-hash/", 1)[0]
            return f"{parent}/by-hash/SHA256/<{named.get((parent, c), 'older index')}>"
        out[name] = {"rows": [[tok(p2), tok(c), None if p.endswith(_COMPRESSED) or "/by-hash/" in p else s, r]
                              for p2, p, c, s, r in sorted((path(p, c), p, c, s, r) for p, c, s, r in rows)]}
    for name in sorted(bundles):
        b = bundles[name]
        out[name]["points"] = {repo: [[tok(t), tok(i)] for t, i in pts] for repo, pts in b["points"].items()}
        out[name]["full"] = b["full"]
        out[name]["blobs"] = sorted(tok(m.rsplit("/", 1)[1]) for m in b["blobs"])
    return out


def test_sync_builds_the_view_of_each_layered_network(tmp_path, monkeypatch):
    lab = _make(tmp_path, monkeypatch)
    try:
        other = config.NetworkConfig(name="other", nginx=False, certs=False,
                                     layers=[config.NetworkLayer(source=lab.src.url, signing="unsigned")])
        plain = config.NetworkConfig(name="plain", nginx=False, certs=False)
        lab.cfg = lab.cfg.model_copy(update={"networks": [lab.net, other, plain]})
        make_deb(layer_dir(lab.md, "other", lab.src.name) / "jammy/main", "other-tool", "1.0")
        lab.up.republish({("jammy", "main"): [make_deb(tmp_path / "u", "hello", "2.10-3")]},
                         date="Tue, 03 Nov 2026 06:00:00 UTC")
        _sync(lab, "20261103T120000Z", monkeypatch)
        pid = _current(lab)
        with Pool(lab.md, read_only=True) as pool:
            point = pool.points_of([pid])[pid]
            assert [v[3] for v in pool.layer_views("lab", pid)] == [point.date]      # from the point's date
            assert [v[3] for v in pool.layer_views("other", pid)] == [point.date]
            assert pool.layer_views("plain", pid) == []
            ours = files_for_settings(pool, pid, _settings(lab))
            theirs = files_for_settings(pool, pid, _settings(lab, other))
            pure = files_for_settings(pool, pid, _settings(lab, plain))
            for name in ("lab", "other", "plain"):
                assert pool.latest_recordings(name)[lab.src.identity] == [pid]
        assert _debs(ours) == {"tools-ours_1.0_amd64.deb"}
        assert _debs(theirs) == {"other-tool_1.0_amd64.deb"}
        assert _debs(pure) == set() and any(p.endswith("/InRelease") for p in pure)
    finally:
        lab.up.close()


def test_a_package_edit_makes_a_new_view_of_the_current_point(lab):
    from typer.testing import CliRunner

    from mmw.cli import app
    conf = lab.tmp / "conf"
    conf.mkdir()
    config.save(lab.cfg, conf)
    deb = make_deb(lab.tmp / "new", "new-tool", "1.0")
    added = CliRunner().invoke(app, ["--config", str(conf), "packages", "add", str(deb),
                                     "--into", lab.src.name, "--network", "lab"])
    assert added.exit_code == 0, added.output
    pid = _current(lab)
    with Pool(lab.md, read_only=True) as pool:
        (_, _, _, first), (_, _, _, edited) = pool.layer_views("lab", pid)
        assert first == pool.points_of([pid])[pid].date and edited > first
        before = files_for_settings(pool, pid, _settings(lab), when=first)
        now = files_for_settings(pool, pid, _settings(lab))
    assert _debs(before) == {"tools-ours_1.0_amd64.deb"}
    assert _debs(now) == {"tools-ours_1.0_amd64.deb", "new-tool_1.0_amd64.deb"}
    history = CliRunner().invoke(app, ["--config", str(conf), "history"])
    assert history.exit_code == 0, history.output
    assert "2026-11-02" in history.output


def _served(root, lab):
    from mmw.core.resolve import network_mirrors
    base = network_mirrors(lab.net, lab.cfg, lab.md)[0].mirror_base
    return root / base


def test_an_older_point_published_as_of_its_date_carries_its_view(lab):
    make_deb(lab.folder, "new-tool", "1.0")                  # an edit after both syncs
    assert not rebuild_current_views(lab.cfg, lab.md, "lab").errors
    for day, root in (("2026-11-01", "first"), ("2026-11-02", "second")):
        res = publish_as_of(lab.net, lab.cfg, lab.md, day, root=lab.tmp / root)
        assert not [r.errors for r in res.mirrors if r.errors]
    first, second = _served(lab.tmp / "first", lab), _served(lab.tmp / "second", lab)
    assert (first / "pool/mmw/main/tools-ours_1.0_amd64.deb").is_file()
    packages = (first / "dists/jammy/main/binary-amd64/Packages").read_text()
    assert "Package: tools-ours" in packages and "Package: cowsay" not in packages
    assert not (first / "dists/jammy/InRelease").exists()                  # unsigned, as then
    assert "Package: new-tool" not in (second / "dists/jammy/main/binary-amd64/Packages").read_text()
    assert not publish_network(lab.net, lab.cfg, lab.md).errors
    assert "Package: new-tool" in (_served(lab.net.root, lab) / "dists/jammy/main/binary-amd64/Packages").read_text()


def test_a_point_from_before_the_layer_serves_plain(tmp_path, monkeypatch):
    lab = _make(tmp_path, monkeypatch, layered_from=2)
    try:
        for day, root in (("2026-11-01", "first"), ("2026-11-02", "second")):
            res = publish_as_of(lab.net, lab.cfg, lab.md, day, root=lab.tmp / root)
            assert not [r.errors for r in res.mirrors if r.errors]
        first, second = _served(lab.tmp / "first", lab), _served(lab.tmp / "second", lab)
        assert not (first / "pool/mmw").exists()
        assert (first / "dists/jammy/InRelease").is_file()                  # upstream's, as served then
        assert (second / "pool/mmw/main/tools-ours_1.0_amd64.deb").is_file()
        assert not (second / "dists/jammy/InRelease").exists()
    finally:
        lab.up.close()


needs_gpg = pytest.mark.skipif(shutil.which("gpg") is None, reason="needs gpg")


@needs_gpg
def test_resigning_one_release_twice_gives_the_same_bytes(tmp_path, monkeypatch):
    resign = {"signing": "resign", "key": "tools"}
    twin = config.NetworkConfig(name="twin", nginx=False, certs=False, layers=[])
    lab = _make(tmp_path, monkeypatch, layer=resign)
    try:
        # Another network, another work folder: only the pool's kept signature can match.
        twin = twin.model_copy(update={"layers": [config.NetworkLayer(source=lab.src.url, **resign)]})
        lab.cfg = lab.cfg.model_copy(update={"networks": [lab.net, twin]})
        shutil.copytree(lab.folder, layer_dir(lab.md, "twin", lab.src.name) / "jammy/main")
        assert not rebuild_current_views(lab.cfg, lab.md, "twin").errors
        pid = _current(lab)
        with Pool(lab.md, read_only=True) as pool:
            ours = files_for_settings(pool, pid, _settings(lab))
            theirs = files_for_settings(pool, pid, _settings(lab, twin))
        for name in ("Release", "InRelease", "Release.gpg"):
            path = next(p for p in ours if p.endswith(f"/dists/jammy/{name}"))
            assert ours[path] == theirs[path], name
    finally:
        lab.up.close()
