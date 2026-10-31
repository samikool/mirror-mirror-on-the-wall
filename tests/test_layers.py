"""Per-network layers: custom debs added to an upstream mirror a network publishes."""
import pytest
from pydantic import ValidationError

from mmw.core import config
from mmw.core.layers import layer_dir
from mmw.core.pool import KIND_META, Pool

UBUNTU = "http://archive.ubuntu.com/ubuntu"


def test_layer_config_round_trips_and_refuses_bad_entries(tmp_path):
    layer = config.NetworkLayer(source=UBUNTU, signing="resign", key="tools", contents=True)
    net = config.NetworkConfig(name="lab", layers=[layer])
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path), networks=[net])
    config.save(cfg, tmp_path)
    text = (tmp_path / "mmw.toml").read_text()
    assert "[[networks.layers]]" in text and "drop_valid_until" not in text
    assert config.load(tmp_path).networks[0].layers == [layer]
    with pytest.raises(ValidationError, match="key"):
        config.NetworkLayer(source=UBUNTU, signing="resign")
    with pytest.raises(ValidationError, match="one layer"):
        config.NetworkConfig(name="lab", layers=[config.NetworkLayer(source=UBUNTU, signing="unsigned"),
                                                 config.NetworkLayer(source=UBUNTU + "/", signing="unsigned")])
    assert config.NetworkConfig(name="lab").layer_for(UBUNTU) is None
    assert net.layer_for(UBUNTU + "/").key == "tools"


def test_layer_dir_is_per_network_and_repository(tmp_path):
    assert layer_dir(tmp_path, "lab", "ubuntu-archive") == tmp_path / "layers/lab/ubuntu-archive"


def test_pool_layer_overlay_round_trip_rename_and_forget(tmp_path):
    with Pool(tmp_path) as pool:
        assert pool.layer_overlay("lab", "archive.ubuntu.com/ubuntu") == {}
        assert pool.layer_digest("lab", "archive.ubuntu.com/ubuntu") is None
        blob = pool.add(b"Packages body", "a" * 64, "sha256", "Packages", 13)
        pool.replace_layer("lab", "archive.ubuntu.com/ubuntu", [
            ("dists/jammy/main/binary-amd64/Packages", "a" * 64, 13, KIND_META, "Packages"),
            ("dists/jammy/InRelease", "", 0, "", ""),
        ], "digest-1")
        overlay = pool.layer_overlay("lab", "archive.ubuntu.com/ubuntu")
        assert overlay["dists/jammy/InRelease"] is None
        row = overlay["dists/jammy/main/binary-amd64/Packages"]
        assert row == ("dists/jammy/main/binary-amd64/Packages", str(blob), 13, KIND_META, "a" * 64)
        assert pool.layer_digest("lab", "archive.ubuntu.com/ubuntu") == "digest-1"
        assert pool.rename_network("lab", "lab2") is None
        assert pool.layer_overlay("lab2", "archive.ubuntu.com/ubuntu")
        assert pool.layer_overlay("lab", "archive.ubuntu.com/ubuntu") == {}
        pool.forget_network("lab2")
        assert pool.layer_overlay("lab2", "archive.ubuntu.com/ubuntu") == {}
        assert pool.layer_digest("lab2", "archive.ubuntu.com/ubuntu") is None


def test_renaming_a_network_moves_its_layer_folder(tmp_path):
    (layer_dir(tmp_path, "lab", "u") / "jammy/main").mkdir(parents=True)
    problems = config.rename_network_state(tmp_path, "lab", "lab2")
    assert problems == [] and (layer_dir(tmp_path, "lab2", "u") / "jammy/main").is_dir()


# ── building a layer (Task 4) ────────────────────────────────────────────────

import shutil  # noqa: E402

from apt_upstream import UpstreamRepo, sync_upstream  # noqa: E402
from debs import make_deb  # noqa: E402
from mmw.core import layers  # noqa: E402
from mmw.core.apt_index import parse_packages, release_body  # noqa: E402
from mmw.core.signing import Keyring  # noqa: E402
from test_signing import gpgv_ok  # noqa: E402

needs_gpg = pytest.mark.skipif(shutil.which("gpg") is None or shutil.which("gpgv") is None,
                               reason="needs gpg and gpgv")


@pytest.fixture
def lab(tmp_path):
    """An upstream (jammy main+universe, amd64+arm64) synced into a pool, a
    network publishing it, and mmw key `tools`."""
    up = UpstreamRepo(tmp_path, {("jammy", "main"): [make_deb(tmp_path / "u", "hello", "2.10-2")],
                                 ("jammy", "universe"): [make_deb(tmp_path / "u", "cowsay", "3.03")]},
                      arches=("amd64", "arm64"))
    src = up.source(["jammy"], ["main", "universe"])
    Keyring(tmp_path / "m").generate("tools")

    class Lab:
        pass
    lab = Lab()
    lab.tmp, lab.up, lab.src, lab.mirror_dir = tmp_path, up, src, tmp_path / "m"

    def cfg(layer=None, extra_networks=()):
        net = config.NetworkConfig(name="lab", root=tmp_path / "pub", layers=[layer] if layer else [])
        return config.Config(settings=config.Settings(mirror_dir=lab.mirror_dir), sources=[src],
                             networks=[net, *extra_networks])
    lab.cfg = cfg
    lab.units = sync_upstream(cfg(), src)
    lab.base = lab.units[0].mirror_base
    lab.folder = layer_dir(lab.mirror_dir, "lab", src.name)

    def build(layer, **kw):
        c = cfg(layer)
        with Pool(lab.mirror_dir) as pool:
            report = layers.build_network_layers(c.networks[0], c, lab.mirror_dir, pool, **kw)
            overlay = pool.layer_overlay("lab", lab.base)
        return report, overlay
    lab.build = build
    yield lab
    up.close()


def _body(row) -> bytes:
    return open(row[1], "rb").read()


RESIGN = dict(source="", signing="resign", key="tools")


def _layer(lab, **kw):
    return config.NetworkLayer(**{**RESIGN, "source": lab.src.url, **kw})


@needs_gpg
def test_a_resign_layer_adds_a_deb_and_re_signs(lab):
    make_deb(lab.folder / "jammy/main", "tools-ours", "1.0")
    report, overlay = lab.build(_layer(lab))
    assert report.errors == [] and report.warnings == []
    packages = parse_packages(_body(overlay["dists/jammy/main/binary-amd64/Packages"]).decode())
    assert [s["Package"] for s in packages] == ["hello", "tools-ours"]
    assert packages[1]["Filename"] == "pool/mmw/main/tools-ours_1.0_amd64.deb"
    assert "pool/mmw/main/tools-ours_1.0_amd64.deb" in overlay
    assert "dists/jammy/main/binary-arm64/Packages" not in overlay        # amd64 deb: arm64 untouched
    release = _body(overlay["dists/jammy/Release"])
    import hashlib
    xz = _body(overlay["dists/jammy/main/binary-amd64/Packages.xz"])
    assert f" {hashlib.sha256(xz).hexdigest()} {len(xz):>16} main/binary-amd64/Packages.xz".encode() in release
    assert b"Date: Thu, 21 Apr 2022 17:16:08 UTC" in release
    public = Keyring(lab.mirror_dir).public_armor("tools")
    assert gpgv_ok(lab.tmp, public, _body(overlay["dists/jammy/Release.gpg"]), release)
    assert release_body(_body(overlay["dists/jammy/InRelease"])) == release


@needs_gpg
def test_an_all_deb_lands_in_every_published_arch(lab):
    make_deb(lab.folder / "jammy/universe", "docs-ours", "1.0", "all")
    _, overlay = lab.build(_layer(lab))
    for arch in ("amd64", "arm64"):
        text = _body(overlay[f"dists/jammy/universe/binary-{arch}/Packages"]).decode()
        assert "Package: docs-ours" in text
        assert ("Package: cowsay" in text) == (arch == "amd64")      # upstream's own stanzas kept


@needs_gpg
def test_an_unchanged_rebuild_is_skipped_and_a_republish_rebuilds(lab, monkeypatch):
    make_deb(lab.folder / "jammy/main", "tools-ours", "1.0")
    _, first = lab.build(_layer(lab))
    with Pool(lab.mirror_dir) as pool:
        digest = pool.layer_digest("lab", lab.base)
    monkeypatch.setattr(layers, "_rebuild", lambda *a, **k: pytest.fail("rebuilt unchanged inputs"))
    _, again = lab.build(_layer(lab))
    assert again == first
    monkeypatch.undo()
    lab.up.republish({("jammy", "main"): [make_deb(lab.tmp / "u2", "hello", "2.10-3")],
                      ("jammy", "universe"): [make_deb(lab.tmp / "u", "cowsay", "3.03")]},
                     date="Fri, 22 Apr 2022 10:00:00 UTC")
    sync_upstream(lab.cfg(), lab.src)
    _, after = lab.build(_layer(lab))
    with Pool(lab.mirror_dir) as pool:
        assert pool.layer_digest("lab", lab.base) != digest
    assert b"2.10-3" in _body(after["dists/jammy/main/binary-amd64/Packages"])


@needs_gpg
def test_resign_with_no_debs_re_signs_only(lab):
    _, overlay = lab.build(_layer(lab, drop_valid_until=True))
    assert set(overlay) == {"dists/jammy/Release", "dists/jammy/InRelease", "dists/jammy/Release.gpg"}


@needs_gpg
def test_unsigned_drops_signatures_where_release_changed(lab):
    make_deb(lab.folder / "jammy/main", "tools-ours", "1.0")
    report, overlay = lab.build(_layer(lab, signing="unsigned", key=None))
    assert overlay["dists/jammy/InRelease"] is None and overlay["dists/jammy/Release.gpg"] is None
    assert any("trusted=yes" in w for w in report.warnings)


@needs_gpg
def test_errors_upstream_signing_and_unpublished_suite(lab):
    make_deb(lab.folder / "jammy/main", "tools-ours", "1.0")
    report, overlay = lab.build(None)
    assert overlay == {} and "resign or unsigned" in report.errors[0]
    make_deb(lab.folder / "noble/main", "x", "1")
    report, _ = lab.build(_layer(lab))
    assert any("noble/main" in e and "does not publish" in e for e in report.errors)


@needs_gpg
def test_clashes_are_reported_and_drop_excludes(lab):
    make_deb(lab.folder / "jammy/main", "hello", "2.10-2", extra={"Depends": "ours"})   # same version, other bytes
    make_deb(lab.folder / "jammy/main", "hello", "2.9", filename="hello_2.9_amd64.deb")   # older than upstream
    report, overlay = lab.build(_layer(lab))
    kinds = sorted((c.filename, c.kind) for c in report.clashes)
    assert kinds == [("hello_2.10-2_amd64.deb", "same-version"), ("hello_2.9_amd64.deb", "shadowed")]
    assert any("hello" in w for w in report.warnings)
    report, overlay = lab.build(_layer(lab), decisions={"hello_2.10-2_amd64.deb": "drop",
                                                         "hello_2.9_amd64.deb": "drop"})
    assert overlay.get("pool/mmw/main/hello_2.9_amd64.deb") is None
    assert report.clashes == []


@needs_gpg
def test_contents_for_apt_file_when_asked(lab):
    import gzip
    make_deb(lab.folder / "jammy/main", "tools-ours", "1.0")
    _, overlay = lab.build(_layer(lab))
    assert "dists/jammy/main/Contents-amd64.gz" not in overlay
    _, overlay = lab.build(_layer(lab, contents=True))
    text = gzip.decompress(_body(overlay["dists/jammy/main/Contents-amd64.gz"])).decode()
    assert "usr/share/doc/tools-ours/README  utils/tools-ours" in text
    assert b"main/Contents-amd64.gz" in _body(overlay["dists/jammy/Release"])


def test_contents_goes_where_upstream_keeps_it():
    rows = {"dists/jammy/Contents-amd64.gz": ("x",)}
    assert layers._contents_path(rows, "jammy", "main", "amd64") == "Contents-amd64"
    rows = {"dists/jammy/main/Contents-amd64.gz": ("x",), "dists/jammy/Contents-amd64.gz": ("x",)}
    assert layers._contents_path(rows, "jammy", "main", "amd64") == "main/Contents-amd64"
    assert layers._contents_path({}, "jammy", "main", "amd64") == "main/Contents-amd64"


# ── publish, snapshot, bundle (Task 5) ───────────────────────────────────────

def _publish(lab, layer, extra=()):
    from mmw.core.publisher import publish_network
    c = lab.cfg(layer, extra)
    results = [publish_network(n, c, lab.mirror_dir) for n in c.networks]
    return c, results


def _tree(root, lab):
    return root / lab.base


@needs_gpg
def test_publish_serves_the_layer_and_leaves_other_networks_pure(lab):
    make_deb(lab.folder / "jammy/main", "tools-ours", "1.0")
    plain = config.NetworkConfig(name="plain", root=lab.tmp / "plain")
    _, (res, res_plain) = _publish(lab, _layer(lab), [plain])
    assert not [r for r in res.mirrors if r.errors], [r.errors for r in res.mirrors]
    tree = _tree(lab.tmp / "pub", lab)
    assert (tree / "pool/mmw/main/tools-ours_1.0_amd64.deb").is_file()
    packages = (tree / "dists/jammy/main/binary-amd64/Packages").read_bytes()
    assert b"Package: tools-ours" in packages
    import hashlib
    xz = (tree / "dists/jammy/main/binary-amd64/Packages.xz").read_bytes()
    assert list(tree.glob(f"dists/jammy/**/by-hash/SHA256/{hashlib.sha256(xz).hexdigest()}"))
    pure = _tree(lab.tmp / "plain", lab)
    upstream = lab.up.root / "ubuntu"
    for rel in ("dists/jammy/InRelease", "dists/jammy/main/binary-amd64/Packages.xz"):
        assert (pure / rel).read_bytes() == (upstream / rel).read_bytes(), rel
    assert not (pure / "pool/mmw").exists()


@needs_gpg
def test_a_snapshot_records_the_served_layer(lab):
    from mmw.core.snapshots import take_snapshot
    make_deb(lab.folder / "jammy/main", "tools-ours", "1.0")
    c = lab.cfg(_layer(lab))
    snap = take_snapshot(c.networks[0], c, lab.mirror_dir, label="with-layer")
    with Pool(lab.mirror_dir, read_only=True) as pool:
        paths = {p for p, *_ in pool.snapshot_blob_rows(snap.id)}
    assert f"{lab.base}/pool/mmw/main/tools-ours_1.0_amd64.deb" in paths


@needs_gpg
def test_an_unsigned_publish_drops_signatures_and_warns(lab):
    make_deb(lab.folder / "jammy/main", "tools-ours", "1.0")
    _, (res,) = _publish(lab, _layer(lab, signing="unsigned", key=None))
    tree = _tree(lab.tmp / "pub", lab)
    assert (tree / "dists/jammy/Release").is_file()
    assert not (tree / "dists/jammy/InRelease").exists() and not (tree / "dists/jammy/Release.gpg").exists()
    assert any("trusted=yes" in w for w in res.warnings)


@needs_gpg
def test_a_broken_layer_is_a_publish_error(lab):
    make_deb(lab.folder / "jammy/main", "tools-ours", "1.0")
    _, (res,) = _publish(lab, None)
    assert any("resign or unsigned" in e for r in res.mirrors for e in r.errors)


# ── review fixes (plan 2) ────────────────────────────────────────────────────

@needs_gpg
def test_prune_keeps_live_layer_blobs(lab):
    make_deb(lab.folder / "jammy/main", "tools-ours", "1.0")
    lab.build(_layer(lab))
    with Pool(lab.mirror_dir) as pool:
        pool.prune_orphans([lab.src.identity])
        overlay = pool.layer_overlay("lab", lab.base)
    from pathlib import Path
    assert all(Path(row[1]).is_file() for row in overlay.values() if row is not None)


@needs_gpg
def test_an_overlay_with_a_missing_blob_is_rebuilt(lab):
    from pathlib import Path
    make_deb(lab.folder / "jammy/main", "tools-ours", "1.0")
    _, overlay = lab.build(_layer(lab))
    Path(overlay["dists/jammy/Release"][1]).unlink()
    _, overlay = lab.build(_layer(lab))
    assert Path(overlay["dists/jammy/Release"][1]).is_file()


@needs_gpg
def test_changing_the_selection_rebuilds(lab):
    make_deb(lab.folder / "jammy/main", "docs-ours", "1.0", "all")
    _, overlay = lab.build(_layer(lab))
    assert "dists/jammy/main/binary-arm64/Packages" in overlay
    narrowed = lab.cfg(_layer(lab))
    net = narrowed.networks[0].model_copy(update={"select": [config.NetworkSelect(
        source=lab.src.url, arches=["amd64"])]})
    with Pool(lab.mirror_dir) as pool:
        layers.build_network_layers(net, narrowed, lab.mirror_dir, pool)
        overlay = pool.layer_overlay("lab", lab.base)
    assert "dists/jammy/main/binary-arm64/Packages" not in overlay


@needs_gpg
def test_suite_level_contents_keeps_every_component(lab, monkeypatch):
    import gzip
    monkeypatch.setattr(layers, "_contents_path", lambda rows, suite, comp, arch: f"Contents-{arch}")
    make_deb(lab.folder / "jammy/main", "a-ours", "1.0")
    make_deb(lab.folder / "jammy/universe", "b-ours", "1.0")
    _, overlay = lab.build(_layer(lab, contents=True))
    text = gzip.decompress(_body(overlay["dists/jammy/Contents-amd64.gz"])).decode()
    assert "a-ours" in text and "b-ours" in text


def test_drop_valid_until_needs_resign():
    with pytest.raises(ValidationError, match="resign"):
        config.NetworkLayer(source=UBUNTU, drop_valid_until=True)
    with pytest.raises(ValidationError, match="resign"):
        config.NetworkLayer(source=UBUNTU, signing="unsigned", drop_valid_until=True)


def test_contents_merge_streams_and_stays_sorted(tmp_path):
    import gzip
    import tracemalloc
    upstream = tmp_path / "Contents-amd64.gz"
    with gzip.open(upstream, "wt") as f:
        for i in range(400_000):
            f.write(f"usr/share/doc/pkg{i:07d}/copyright  doc/pkg{i:07d}\n")
    tracemalloc.start()
    merged = layers._merge_contents(upstream, [("usr/share/doc/pkg0000005/extra", "utils/ours"),
                                              ("aaa/first", "utils/ours")])
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    lines = gzip.decompress(merged).decode().splitlines()
    assert lines[0] == "aaa/first  utils/ours" and len(lines) == 400_002
    assert lines == sorted(lines)
    assert peak < 8 * 1024 * 1024, f"{peak / 1e6:.0f} MB"


def test_index_forms_follow_what_upstream_lists():
    import bz2
    forms = layers._index_forms(b"SHA256:\n a 1 main/binary-amd64/Packages.gz\n"
                                b" b 2 main/binary-amd64/Packages.bz2\n", "main/binary-amd64/Packages")
    assert forms == ["", ".bz2", ".gz"]
    built = layers._compress_forms(b"text", forms)
    assert bz2.decompress(built["Packages.bz2"]) == b"text" and built["Packages"] == b"text"
    assert layers._index_forms(b"SHA256:\n", "x/Packages") == ["", ".gz", ".xz"]   # nothing listed: the usual three


@needs_gpg
def test_a_deb_for_an_unpublished_arch_is_reported(lab):
    make_deb(lab.folder / "jammy/main", "risc-ours", "1.0", "riscv64")
    report, _ = lab.build(_layer(lab))
    assert any("riscv64" in w for w in report.warnings)


@needs_gpg
def test_an_unchanged_build_does_not_reparse_upstream_for_clashes(lab, monkeypatch):
    make_deb(lab.folder / "jammy/main", "hello", "2.9")
    first, _ = lab.build(_layer(lab))
    assert [c.kind for c in first.clashes] == ["shadowed"]
    monkeypatch.setattr(layers, "_clashes", lambda *a, **k: pytest.fail("reparsed for clashes"))
    again, _ = lab.build(_layer(lab))
    assert again.clashes == first.clashes and again.warnings == first.warnings
