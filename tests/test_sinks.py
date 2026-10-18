import hashlib
import json
import tarfile
from pathlib import Path

import pytest

from mmw.core import config
from mmw.core.pool import KIND_META, KIND_PKG, Pool
from mmw.core.publisher import sinks as sinks_mod
from mmw.core.publisher.manifest import ManifestHeader, delta
from mmw.core.publisher.nginx import ROOT_PLACEHOLDER
from mmw.core.publisher.sinks import BundleSink, TreeSink


@pytest.fixture
def pool(tmp_path):
    p = Pool(tmp_path / "mirror")
    yield p
    p.close()


def _seed(pool: Pool, content: bytes, name: str) -> tuple[str, Path]:
    """Returns (checksum, blob path). Pool.add returns the pooled Path."""
    checksum = hashlib.sha256(content).hexdigest()
    path = pool.add(content, checksum, "sha256", name, len(content))
    return checksum, path


def test_tree_sink_links_and_reports_status(pool, tmp_path):
    cs, src = _seed(pool, b"hello", "a.deb")
    sink = TreeSink(tmp_path / "pub")

    assert sink.link(src, "host/repo/pool/a.deb", 5, cs, KIND_PKG) == "linked"
    dst = tmp_path / "pub" / "host/repo/pool/a.deb"
    assert dst.read_bytes() == b"hello"
    # Second call to the same destination is a hardlink no-op, not a relink.
    assert sink.link(src, "host/repo/pool/a.deb", 5, cs, KIND_PKG) == "skipped"


def test_tree_sink_write_creates_parents(tmp_path):
    sink = TreeSink(tmp_path / "pub")
    sink.write("UPGRADE.md", b"# guide\n")
    assert (tmp_path / "pub" / "UPGRADE.md").read_bytes() == b"# guide\n"


def test_tree_sink_write_refuses_unsubstituted_placeholder(tmp_path):
    """A TreeSink destination is live-served; the nginx root placeholder must
    always already be substituted for a real path by the caller before it
    ever reaches write(). The spec says an unsubstituted placeholder reaching
    disk on either side is a bug -- nothing enforced that here."""
    sink = TreeSink(tmp_path / "pub")
    with pytest.raises(ValueError, match=ROOT_PLACEHOLDER):
        sink.write("nginx/sites/example.com.conf",
                  f"root {ROOT_PLACEHOLDER}/example.com;\n".encode())
    assert not (tmp_path / "pub" / "nginx").exists()


def test_tree_sink_finalize_group_prunes_unwanted(tmp_path):
    root = tmp_path / "pub"
    base = "host/repo"
    (root / base / "dists").mkdir(parents=True)
    (root / base / "dists" / "keep").write_bytes(b"k")
    (root / base / "dists" / "stale").write_bytes(b"s")

    sink = TreeSink(root)
    sink.finalize_group(base, keep={f"{base}/dists/keep"},
                        live_checksums=set(), exclude=set())

    assert (root / base / "dists" / "keep").exists()
    assert not (root / base / "dists" / "stale").exists()
    assert sink.pruned == 1


def test_tree_sink_finalize_group_honours_exclude(tmp_path):
    """A nested publish base must never be pruned by the outer group."""
    root = tmp_path / "pub"
    base = "host/repo"
    nested = f"{base}/sub"
    (root / nested).mkdir(parents=True)
    (root / nested / "theirs").write_bytes(b"x")

    sink = TreeSink(root)
    sink.finalize_group(base, keep=set(), live_checksums=set(), exclude={nested})

    assert (root / nested / "theirs").exists()
    assert sink.pruned == 0


def test_tree_sink_finalize_group_preserves_empty_base(tmp_path):
    """An empty selected APT repository keeps its published base directory."""
    root = tmp_path / "pub"
    sink = TreeSink(root)
    sink.write("host/repo/dists/stale", b"old")

    assert sink.finalize_group("host/repo", set(), set(), set()) == (1, 3)

    assert (root / "host/repo").is_dir()
    assert list((root / "host/repo").iterdir()) == []


def test_tree_sink_drop_if_empty_only_removes_empty_directory(tmp_path):
    root = tmp_path / "pub"
    sink = TreeSink(root)
    sink.write("vscode-gallery/catalog.json", b"{}")
    sink.drop_if_empty("vscode-gallery")
    assert (root / "vscode-gallery/catalog.json").read_bytes() == b"{}"

    sink.finalize_group("vscode-gallery", set(), set(), set())
    sink.drop_if_empty("vscode-gallery")
    assert not (root / "vscode-gallery").exists()
    sink.drop_if_empty("vscode-gallery")  # Already absent is harmless.


@pytest.mark.parametrize("base", ["", "."])
def test_tree_sink_drop_if_empty_preserves_publish_root(tmp_path, base):
    root = tmp_path / "pub"
    root.mkdir()
    TreeSink(root).drop_if_empty(base)
    assert root.is_dir()


def test_tree_sink_finalize_group_returns_this_groups_count(tmp_path):
    """The return value is THIS call's own count, not the sink's running
    total — a shared sink across several groups must not hand one group
    another group's prune count."""
    root = tmp_path / "pub"
    base = "host/repo"
    (root / base).mkdir(parents=True)
    (root / base / "stale").write_bytes(b"12345")

    sink = TreeSink(root)
    files, size = sink.finalize_group(base, keep=set(), live_checksums=set(), exclude=set())

    assert (files, size) == (1, 5)
    assert sink.pruned == 1  # cumulative attribute still tracks the total


def test_tree_sink_finalize_group_progress_receives_rel_strings(tmp_path):
    """`progress` is the Sink contract used by the TUI's TransferView,
    which calls `dst.split("/")` — a Path here breaks that on the first
    event, not caught by anything that never drives this path."""
    root = tmp_path / "pub"
    base = "host/repo"
    (root / base / "dists").mkdir(parents=True)
    (root / base / "dists" / "stale").write_bytes(b"x")

    events = []
    sink = TreeSink(root, progress=lambda dst, status: events.append((dst, status)))
    sink.finalize_group(base, keep=set(), live_checksums=set(), exclude=set())

    assert events  # something fired
    for dst, _status in events:
        assert isinstance(dst, str), f"progress() received a {type(dst)}, not str: {dst!r}"
    statuses = [s for _, s in events]
    assert "prune-scan-start" in statuses
    assert any(s.startswith("prune-scan-done:") for s in statuses)
    pruned_events = [dst for dst, s in events if s == "pruned"]
    assert pruned_events == ["host/repo/dists/stale"]


def _members(path: Path) -> dict[str, bytes]:
    """Extract a bundle tar into {name: content} for assertions."""
    out: dict[str, bytes] = {}
    with tarfile.open(path, mode="r|") as tar:
        for ti in tar:
            if ti.isfile():
                out[ti.name] = tar.extractfile(ti).read()
            elif ti.islnk():
                out[ti.name] = b"->" + ti.linkname.encode()
    return out


def _manifest_header(path: Path) -> dict:
    """The header line of a produced bundle's own manifest member — the
    close()-computed `file_count` only ever exists in the header actually
    written into the tar, never on a leftover file (the temp manifest is
    unlinked in close()'s `finally`)."""
    return json.loads(_members(path)[".mmw/manifest.jsonl"].splitlines()[0])


def _hdr(previous=None, full=True):
    return ManifestHeader(network="lab", created="20260906T143000Z",
                          previous=previous, full=full)


def test_bundle_sink_writes_manifest_first(pool, tmp_path):
    cs, src = _seed(pool, b"hello", "a.deb")
    out = tmp_path / "lab.tar"
    sink = BundleSink(out, _hdr(), prev={}, apply_script=b"# apply\n")
    sink.link(src, "host/repo/pool/a.deb", 5, cs, KIND_PKG)
    sink.close()

    with tarfile.open(out, mode="r|") as tar:
        first = next(iter(tar))
    assert first.name == ".mmw/manifest.jsonl"


def test_bundle_sink_writes_a_plain_tar(pool, tmp_path):
    """No compression anywhere: no zstd magic, `tarfile` reads it with no
    mode, and the manifest member is plain JSONL — apply.py needs nothing
    but Python."""
    cs, src = _seed(pool, b"hello", "a.deb")
    out = tmp_path / "lab.tar"
    sink = BundleSink(out, _hdr(), prev={}, apply_script=b"# apply\n")
    sink.link(src, "host/repo/pool/a.deb", 5, cs, KIND_PKG)
    sink.close()

    assert out.read_bytes()[:4] != b"\x28\xb5\x2f\xfd"
    with tarfile.open(out, mode="r") as tar:
        assert "host/repo/pool/a.deb" in {ti.name for ti in tar}
        manifest = tar.extractfile(".mmw/manifest.jsonl").read()
    assert json.loads(manifest.splitlines()[0])["network"] == "lab"


def test_bundle_sink_carries_payload_and_apply_script(pool, tmp_path):
    cs, src = _seed(pool, b"hello", "a.deb")
    out = tmp_path / "lab.tar"
    sink = BundleSink(out, _hdr(), prev={}, apply_script=b"# apply\n")
    sink.link(src, "host/repo/pool/a.deb", 5, cs, KIND_PKG)
    sink.write("UPGRADE.md", b"# guide\n")
    sink.close()

    m = _members(out)
    assert m["host/repo/pool/a.deb"] == b"hello"
    assert m["UPGRADE.md"] == b"# guide\n"
    assert m[".mmw/apply.py"] == b"# apply\n"


def test_bundle_sink_delta_sends_only_changed(pool, tmp_path):
    cs_a, src_a = _seed(pool, b"unchanged", "a.deb")
    cs_b, src_b = _seed(pool, b"brand new", "b.deb")
    prev = {"host/repo/pool/a.deb": cs_a}

    out = tmp_path / "lab.tar"
    sink = BundleSink(out, _hdr(previous="20260901T090000Z", full=False),
                      prev=prev, apply_script=b"")
    sink.link(src_a, "host/repo/pool/a.deb", 9, cs_a, KIND_PKG)
    sink.link(src_b, "host/repo/pool/b.deb", 9, cs_b, KIND_PKG)
    sink.close()

    m = _members(out)
    assert "host/repo/pool/b.deb" in m
    assert "host/repo/pool/a.deb" not in m      # unchanged — not re-sent
    assert sink.sent == 1


def test_bundle_sink_manifest_lists_the_whole_tree_not_the_delta(pool, tmp_path):
    cs_a, src_a = _seed(pool, b"unchanged", "a.deb")
    cs_b, src_b = _seed(pool, b"brand new", "b.deb")
    sink = BundleSink(tmp_path / "lab.tar",
                      _hdr(previous="20260901T090000Z", full=False),
                      prev={"host/repo/pool/a.deb": cs_a}, apply_script=b"")
    sink.link(src_a, "host/repo/pool/a.deb", 9, cs_a, KIND_PKG)
    sink.link(src_b, "host/repo/pool/b.deb", 9, cs_b, KIND_PKG)
    sink.close()

    assert dict(sink.manifest_rows) == {
        "host/repo/pool/a.deb": cs_a,
        "host/repo/pool/b.deb": cs_b,
    }


def test_bundle_sink_declares_its_own_file_count_not_the_manifests(pool, tmp_path):
    """`ManifestHeader.file_count` must reflect what THIS bundle's tar
    actually contains (1 — only b.deb changed), never the full manifest's
    file list (2 — a.deb is listed too, just carried forward unchanged).
    This is exactly the distinction whose absence broke a real delta bundle
    when a first attempt at apply.py's completeness check compared against
    the full manifest instead (see resources/apply.py)."""
    cs_a, src_a = _seed(pool, b"unchanged", "a.deb")
    cs_b, src_b = _seed(pool, b"brand new", "b.deb")
    out = tmp_path / "lab.tar"
    sink = BundleSink(out, _hdr(previous="20260901T090000Z", full=False),
                      prev={"host/repo/pool/a.deb": cs_a}, apply_script=b"")
    sink.link(src_a, "host/repo/pool/a.deb", 9, cs_a, KIND_PKG)
    sink.link(src_b, "host/repo/pool/b.deb", 9, cs_b, KIND_PKG)
    sink.close()

    header = _manifest_header(out)
    assert header["network"] == "lab"   # sanity: this is the header line, not a file row
    assert header["file_count"] == 1


def test_bundle_sink_dedups_by_checksum_as_tar_hardlinks(pool, tmp_path):
    """An apt index file appears canonically and under by-hash/ — one copy."""
    cs, src = _seed(pool, b"Packages content", "Packages.gz")
    out = tmp_path / "lab.tar"
    sink = BundleSink(out, _hdr(), prev={}, apply_script=b"")
    sink.link(src, "h/r/dists/n/main/binary-amd64/Packages.gz", 16, cs, KIND_META)
    sink.link(src, f"h/r/dists/n/main/binary-amd64/by-hash/SHA256/{cs}", 16, cs, KIND_META)
    sink.close()

    m = _members(out)
    assert m["h/r/dists/n/main/binary-amd64/Packages.gz"] == b"Packages content"
    assert m[f"h/r/dists/n/main/binary-amd64/by-hash/SHA256/{cs}"] == \
        b"->h/r/dists/n/main/binary-amd64/Packages.gz"


def test_bundle_sink_full_resends_despite_matching_prev(pool, tmp_path):
    """`full=True` bypasses `prev` for the SEND decision -- "send everything"
    -- but `prev` itself is still supplied and still drives deletions (see
    the next test)."""
    cs, src = _seed(pool, b"unchanged", "a.deb")
    prev = {"host/repo/pool/a.deb": cs}   # identical to what's being linked

    out = tmp_path / "lab-full.tar"
    sink = BundleSink(out, _hdr(full=True), prev=dict(prev), apply_script=b"",
                      full=True)
    status = sink.link(src, "host/repo/pool/a.deb", 9, cs, KIND_PKG)
    sink.close()

    assert status == "linked"
    assert sink.sent == 1
    assert "host/repo/pool/a.deb" in _members(out)


def test_bundle_sink_full_still_computes_deletions_from_prev(pool, tmp_path):
    """IMPORTANT 4 -- a full bundle means "send everything", never "forget what
    the far side has". `finalize_group` must still read `prev` for the
    delete set even when `full=True` -- setting `prev={}` for a full bundle
    (the original implementation) computed zero deletions, permanently
    stranding files the far side no longer wants."""
    prev = {"host/repo/pool/gone.deb": "aa" * 32}
    sink = BundleSink(tmp_path / "lab-full.tar", _hdr(full=True),
                      prev=dict(prev), apply_script=b"", full=True)

    result = sink.finalize_group("host/repo", keep=set(), live_checksums=set(),
                                 exclude=set())

    assert result == (1, 0)
    assert sink.deleted == ["host/repo/pool/gone.deb"]


def test_bundle_sink_delete_list_only_when_pruning(pool, tmp_path):
    prev = {"host/repo/pool/gone.deb": "aa" * 32}
    for prune, expected in ((True, True), (False, False)):
        out = tmp_path / f"lab-{prune}.tar"
        sink = BundleSink(out, _hdr(full=False), prev=dict(prev),
                          apply_script=b"", prune=prune)
        result = sink.finalize_group("host/repo", keep=set(), live_checksums=set(),
                                     exclude=set())
        assert result == ((1, 0) if prune else (0, 0))
        sink.close()
        m = _members(out)
        assert (".mmw/delete.list" in m) is expected
        if expected:
            assert m[".mmw/delete.list"].decode().split() == ["host/repo/pool/gone.deb"]


def test_bundle_sink_refuses_to_write_anything_when_a_blob_is_damaged(
        pool, tmp_path):
    """The size recorded at link() time (from the pool DB) is checked against
    the blob actually on disk before anything is streamed. A mismatch means
    a damaged pool, and the bundle is refused whole — shipping the rest used
    to leave the far side permanently without the file, because the next
    delta (computed from the snapshot, which still names it) took it as sent."""
    cs, src = _seed(pool, b"hello", "a.deb")
    out = tmp_path / "lab.tar"
    sink = BundleSink(out, _hdr(), prev={}, apply_script=b"")
    sink.link(src, "host/repo/pool/a.deb", 999, cs, KIND_PKG)     # recorded size is wrong
    sink.link(src, "host/repo/pool/b.deb", 5, cs, KIND_PKG)

    with pytest.raises(ValueError, match="verify --repair"):
        sink.close()
    assert len(sink.errors) == 1 and "a.deb" in sink.errors[0] and "999" in sink.errors[0]
    assert not any(p.name.startswith("lab.tar") for p in tmp_path.iterdir()), (
        "a refused bundle left files behind")


def test_bundle_sink_delta_agrees_with_manifest_delta(pool, tmp_path):
    """`BundleSink` decides per-row in `link()` what to send and per-prefix in
    `finalize_group()` what to delete; `manifest.delta()` states the same
    rule as one set difference. Two implementations of one rule drift —
    this pins them to the same answer on a mixed fixture: unchanged, changed,
    brand new, and removed-since-the-last-bundle. Plain paths only: by-hash
    paths follow their own retention rule (live checksum in live_checksums)
    that delta() knows nothing about."""
    unchanged = b"same content"
    changed_old_content = b"stale payload"          # only its checksum matters
    changed_new_content = b"replacement payload"
    new_content = b"never bundled before"

    cs_unchanged, src_unchanged = _seed(pool, unchanged, "unchanged.deb")
    cs_changed_old = hashlib.sha256(changed_old_content).hexdigest()
    cs_changed_new, src_changed = _seed(pool, changed_new_content, "changed.deb")
    cs_new, src_new = _seed(pool, new_content, "new.deb")

    base = "host/repo"
    prev = {
        f"{base}/pool/unchanged.deb": cs_unchanged,
        f"{base}/pool/changed.deb": cs_changed_old,
        f"{base}/pool/removed.deb": "22" * 32,   # gone from the current listing
    }
    cur = {
        f"{base}/pool/unchanged.deb": cs_unchanged,
        f"{base}/pool/changed.deb": cs_changed_new,
        f"{base}/pool/new.deb": cs_new,
    }

    want_send, want_delete = delta(prev, cur)

    sink = BundleSink(tmp_path / "lab.tar", _hdr(previous="x", full=False),
                      prev=dict(prev), apply_script=b"")
    got_send = set()
    for rel, src, content, cs in (
        (f"{base}/pool/unchanged.deb", src_unchanged, unchanged, cs_unchanged),
        (f"{base}/pool/changed.deb", src_changed, changed_new_content, cs_changed_new),
        (f"{base}/pool/new.deb", src_new, new_content, cs_new),
    ):
        status = sink.link(src, rel, len(content), cs, KIND_PKG)
        if status == "linked":
            got_send.add(rel)
    sink.finalize_group(base, keep=set(cur), live_checksums=set(), exclude=set())
    sink.close()

    assert got_send == want_send
    assert set(sink.deleted) == want_delete


def test_bundle_sink_resends_empty_checksum_key_even_when_prev_matches(pool, tmp_path):
    """Pins the `and checksum` guard in `link()`: a published GPG key has no
    content hash (`sink.link(f, rel, size, "", KIND_META)` -- links.py's
    `publish_keys`), so `prev.get(rel) == ""` must never compare true against
    itself and skip a resend -- unlike a real checksum, `""` carries no
    trustworthy claim that the content is unchanged.
    `test_bundle_sink_delta_agrees_with_manifest_delta` explicitly excludes
    this case; this test is what actually pins it."""
    key_bytes = b"-----BEGIN PGP PUBLIC KEY BLOCK-----"
    _, src = _seed(pool, key_bytes, "cuda.asc")
    rel = "keys/nvidia/cuda.asc"
    # What bundle 1's own manifest recorded for this key -- no checksum, by
    # construction (publish_keys always calls link() with checksum="").
    prev = {rel: ""}

    out = tmp_path / "lab2.tar"
    sink = BundleSink(out, _hdr(previous="x", full=False), prev=dict(prev),
                      apply_script=b"")
    status = sink.link(src, rel, len(key_bytes), "", KIND_META)
    sink.close()

    assert sink.errors == []

    assert status == "linked"
    assert rel in _members(out)


def test_bundle_sink_by_hash_retention_differs_from_manifest_delta(pool, tmp_path):
    """By-hash paths diverge from the pure `manifest.delta()` rule. `delta()` sees
    only what's in cur vs prev; `BundleSink.finalize_group()` additionally retains
    a by-hash entry if its checksum is in `live_checksums`, even when not in `keep`."""
    cs, src = _seed(pool, b"index content", "Packages.gz")
    base = "host/repo"
    pkg_rel = f"{base}/dists/n/main/binary-amd64/Packages.gz"
    by_hash_rel = f"{base}/dists/n/main/binary-amd64/by-hash/SHA256/{cs}"

    # Both in prev, by-hash in cur (but links.py only puts the canonical path
    # in keep, not the by-hash path).
    prev = {
        pkg_rel: cs,
        by_hash_rel: cs,
    }
    keep = {pkg_rel}  # only the canonical path, not by-hash
    live_checksums = {cs}  # but the checksum is still live upstream

    sink = BundleSink(tmp_path / "lab.tar", _hdr(previous="x", full=False),
                      prev=dict(prev), apply_script=b"")
    sink.link(src, pkg_rel, 13, cs, KIND_META)
    sink.link(src, by_hash_rel, 13, cs, KIND_META)
    sink.finalize_group(base, keep=keep, live_checksums=live_checksums, exclude=set())
    sink.close()

    # delta() would say to delete by_hash_rel (not in cur). BundleSink doesn't,
    # because the checksum is in live_checksums.
    assert by_hash_rel not in sink.deleted
    # Plain path isn't deleted (it's in keep).
    assert pkg_rel not in sink.deleted


def test_bundle_sink_by_hash_deletion_on_stale_checksum(pool, tmp_path):
    """A by-hash path is deleted only when its checksum stops appearing upstream."""
    cs_stale = "aa" * 32
    cs_live = "bb" * 32
    base = "host/repo"
    live_rel = f"{base}/dists/n/main/binary-amd64/by-hash/SHA256/{cs_live}"
    stale_rel = f"{base}/dists/n/main/binary-amd64/by-hash/SHA256/{cs_stale}"

    prev = {
        live_rel: cs_live,
        stale_rel: cs_stale,
    }

    sink = BundleSink(tmp_path / "lab.tar", _hdr(full=False), prev=dict(prev),
                      apply_script=b"", prune=True)
    # Only link the live checksum; don't link stale at all.
    sink.finalize_group(base, keep=set(), live_checksums={cs_live}, exclude=set())

    assert stale_rel in sink.deleted
    assert live_rel not in sink.deleted


def test_bundle_sink_write_unchanged_does_not_increment_sent(pool, tmp_path):
    """If write() produces a payload whose sha256 is already in prev, the
    manifest still lists it (manifest is the whole tree, not the delta), but
    sent must not increment and the data must not be queued for the archive."""
    data = b"UPGRADE guide"
    checksum = hashlib.sha256(data).hexdigest()
    prev = {"UPGRADE.md": checksum}

    out = tmp_path / "lab.tar"
    sink = BundleSink(out, _hdr(previous="x", full=False), prev=prev,
                      apply_script=b"")
    sink.write("UPGRADE.md", data)
    sink.close()

    # Manifest lists it (even though unchanged).
    assert dict(sink.manifest_rows) == {"UPGRADE.md": checksum}
    # But it was not sent.
    assert sink.sent == 0
    assert sink.sent_bytes == 0
    # And it's not in the archive payload.
    m = _members(out)
    assert "UPGRADE.md" not in m


def test_bundle_sink_finalize_group_exclude_parameter(tmp_path):
    """A nested publish base in exclude must never be scheduled for deletion,
    even when not in keep."""
    base = "host/repo"
    nested = f"{base}/sub"
    prev = {
        f"{nested}/keep": "aa" * 32,
        f"{nested}/stale": "bb" * 32,
    }

    sink = BundleSink(tmp_path / "lab.tar", _hdr(full=False), prev=dict(prev),
                      apply_script=b"", prune=True)
    # Nested base is excluded; even though it's not in keep, nothing under it
    # is added to deleted.
    sink.finalize_group(base, keep=set(), live_checksums=set(), exclude={nested})

    assert len(sink.deleted) == 0


class RecordingSink:
    """Captures sink calls so a writer can be tested without a filesystem."""

    def __init__(self):
        self.errors: list[str] = []
        self.linked: dict[str, Path] = {}
        self.written: dict[str, bytes] = {}
        self.finalized: list[tuple[str, set[str]]] = []

    def link(self, src, rel, size, checksum, kind):
        self.linked[rel] = src
        return "linked"

    def write(self, rel, data):
        self.written[rel] = data

    def finalize_group(self, base, keep, live_checksums, exclude):
        self.finalized.append((base, keep))
        return (0, 0)

    def close(self):
        pass


def test_publish_keys_writes_label_relative_paths(tmp_path):
    from mmw.core.publisher.links import publish_keys

    keys = tmp_path / "mirror" / "keys" / "nvidia"
    keys.mkdir(parents=True)
    (keys / "cuda.asc").write_bytes(b"-----BEGIN PGP PUBLIC KEY BLOCK-----\n")

    m = config.MirrorConfig(source_url="https://repo.nvidia.com/cuda/",
                            name="cuda", provider="apt", label="nvidia")
    sink = RecordingSink()
    publish_keys(sink, tmp_path / "mirror", [m])

    assert "keys/nvidia/cuda.asc" in sink.linked


def test_write_upgrade_md_goes_to_sink_root(tmp_path):
    from mmw.core.publisher.dist_upgrade import write_upgrade_md

    net = config.NetworkConfig(name="lab", root=tmp_path / "pub")
    m = config.MirrorConfig(source_url="https://ports.ubuntu.com/",
                            name="p", provider="apt", label="ubuntu")
    sink = RecordingSink()
    pool = Pool(tmp_path / "mirror")
    try:
        write_upgrade_md(sink, net, pool, [], [], mirrors=[m])
    finally:
        pool.close()

    body = sink.written["UPGRADE.md"].decode()
    assert "ports.ubuntu.com" in body
    assert "archive.ubuntu.com" not in body   # never hardcoded
    assert "changelogs.ubuntu.com" in body    # always appended


def test_write_upgrade_md_with_no_root_never_prints_none(tmp_path):
    """A bundle-only network has no local publish root — the guide is read on
    the far side after extraction, so the literal string `None` must never
    appear where the mirror root would otherwise be named."""
    from mmw.core.publisher.dist_upgrade import write_upgrade_md

    net = config.NetworkConfig(name="lab", root=None)
    m = config.MirrorConfig(source_url="https://ports.ubuntu.com/",
                            name="p", provider="apt", label="ubuntu")
    sink = RecordingSink()
    pool = Pool(tmp_path / "mirror")
    try:
        write_upgrade_md(sink, net, pool, [], [], mirrors=[m])
    finally:
        pool.close()

    body = sink.written["UPGRADE.md"].decode()
    mirror_root_line = next(line for line in body.splitlines() if line.startswith("Mirror root:"))
    assert mirror_root_line == "Mirror root: the directory this bundle was applied to"


def test_bundle_sink_close_reports_archive_progress(pool, tmp_path):
    """close() does all the real I/O (reading pool blobs, streaming them
    into the tar) — link()/write() only ever append to in-memory
    lists. Without a progress hook inside close() itself, a bundle's progress
    UI races to a "done"-looking count almost instantly and then sits silent
    for however long the archive actually takes to write — see the docstring
    this test is locking in behaviour for (TransferView's archive
    phase)."""
    cs, src = _seed(pool, b"hello-package-bytes", "a.deb")
    out = tmp_path / "lab.tar"
    events: list[tuple[str, str]] = []
    sink = BundleSink(out, _hdr(), prev={}, apply_script=b"# apply\n",
                      progress=lambda rel, status: events.append((rel, status)))
    sink.link(src, "host/repo/pool/a.deb", len(b"hello-package-bytes"), cs, KIND_PKG)
    sink.write("UPGRADE.md", b"# guide\n")
    sink.close()

    starts = [s for _, s in events if s.startswith("archive-start:")]
    dones = [s for _, s in events if s.startswith("archive-done:")]
    assert len(starts) == 1
    assert len(dones) == 1

    # archive-start carries the totals known BEFORE any archive I/O runs —
    # `sent`/`sent_bytes` are final the moment link()/write() stop being
    # called, so the progress UI can show a determinate bar from the first
    # tick instead of an indeterminate spinner.
    _, total_files, total_bytes = starts[0].split(":")
    assert int(total_files) == sink.sent == 2
    assert int(total_bytes) == sink.sent_bytes == len(b"hello-package-bytes") + len(b"# guide\n")

    # archive-done carries files written and the file bytes fed into the tar
    # — the same unit as archive-start's total, so the bar reaches 100%.
    _, files_written, bytes_written = dones[0].split(":")
    assert int(files_written) >= 2   # payload + generated, plus .mmw/ control files
    assert int(bytes_written) >= sink.sent_bytes  # plus the .mmw/ control files' own bytes
    assert int(bytes_written) <= out.stat().st_size   # tar headers and padding on top


def test_bundle_sink_archive_progress_names_the_file_and_counts_bytes(pool, tmp_path,
                                                                    monkeypatch):
    monkeypatch.setattr(sinks_mod, "_ARCHIVE_REPORT_INTERVAL", 0.0)  # report every file

    payload = b"x" * 100_000
    cs, src = _seed(pool, payload, "a.deb")
    out = tmp_path / "lab.tar"
    events: list[tuple[str, str]] = []
    sink = BundleSink(out, _hdr(), prev={}, apply_script=b"# apply\n",
                      progress=lambda rel, status: events.append((rel, status)))
    sink.link(src, "host/repo/pool/a.deb", len(payload), cs, KIND_PKG)
    sink.close()

    progressed = [(rel, s) for rel, s in events if s.startswith("archive-progress:")]
    assert progressed, "at least one mid-archive report expected with the interval zeroed"
    rel, status = progressed[-1]
    _, files, written = status.split(":")
    assert int(written) >= len(payload)
    # The rel names the file that completed at the moment this (throttled)
    # report fired — a viewer wanting to show "currently archiving: X" needs
    # this, not an empty placeholder.
    assert rel == "host/repo/pool/a.deb"


def test_large_files_get_an_immediate_unthrottled_announcement(pool, tmp_path, monkeypatch):
    """The throttled per-file sampling only ever reports a file AFTER
    tar.addfile() returns, so one large file can leave the log silent for as
    long as it takes to compress — reading as a stall rather than real,
    in-progress work. A file at or above the threshold gets its own
    immediate event the instant it starts, regardless of the throttle."""
    monkeypatch.setattr(sinks_mod, "_ANNOUNCE_LARGE_FILE_BYTES", 10)

    small_cs, small_src = _seed(pool, b"tiny", "small.deb")
    big_payload = b"y" * 100
    big_cs, big_src = _seed(pool, big_payload, "big.deb")
    out = tmp_path / "lab.tar"
    events: list[tuple[str, str]] = []
    sink = BundleSink(out, _hdr(), prev={}, apply_script=b"# apply\n",
                      progress=lambda rel, status: events.append((rel, status)))
    sink.link(small_src, "host/repo/pool/small.deb", 4, small_cs, KIND_PKG)
    sink.link(big_src, "host/repo/pool/big.deb", len(big_payload), big_cs, KIND_PKG)
    sink.close()

    large_events = [(rel, s) for rel, s in events if s.startswith("archive-large-file:")]
    assert large_events == [("host/repo/pool/big.deb", f"archive-large-file:{len(big_payload)}")]


def test_large_file_reports_incremental_progress_while_compressing(pool, tmp_path, monkeypatch):
    """A file large enough to get the immediate "starting" announcement also
    reports percentage-through progress as tarfile streams it — the whole
    point being a multi-GB file doesn't just sit at "compressing…" with no
    sense of how far through it is."""
    monkeypatch.setattr(sinks_mod, "_ANNOUNCE_LARGE_FILE_BYTES", 10)
    monkeypatch.setattr(sinks_mod, "_ARCHIVE_REPORT_INTERVAL", 0.0)  # report every chunk

    payload = b"z" * 50_000  # large enough for tarfile's internal copy loop
    # to call .read() several times, so "incremental" is actually exercised.
    cs, src = _seed(pool, payload, "big.deb")
    out = tmp_path / "lab.tar"
    events: list[tuple[str, str]] = []
    sink = BundleSink(out, _hdr(), prev={}, apply_script=b"# apply\n",
                      progress=lambda rel, status: events.append((rel, status)))
    sink.link(src, "host/repo/pool/big.deb", len(payload), cs, KIND_PKG)
    sink.close()

    progress_events = [
        (rel, s) for rel, s in events if s.startswith("archive-large-file-progress:")
    ]
    assert len(progress_events) >= 2, "expected multiple incremental reads, not just one"
    assert all(rel == "host/repo/pool/big.deb" for rel, _ in progress_events)

    dones = [int(s.split(":")[1]) for _, s in progress_events]
    sizes = [int(s.split(":")[2]) for _, s in progress_events]
    assert dones == sorted(dones), "done count must only ever increase"
    assert all(s == len(payload) for s in sizes)
    assert dones[-1] == len(payload)   # the last read reaches the full size


def test_bundle_sink_close_progress_is_optional(pool, tmp_path):
    """No `progress=` passed must behave exactly as before — close() cannot
    assume every caller wants archive events."""
    cs, src = _seed(pool, b"hello", "a.deb")
    out = tmp_path / "lab.tar"
    sink = BundleSink(out, _hdr(), prev={}, apply_script=b"# apply\n")
    sink.link(src, "host/repo/pool/a.deb", 5, cs, KIND_PKG)
    sink.close()   # must not raise
    assert out.exists()


def test_bundle_sink_sent_bytes_counts_duplicate_content_once(pool, tmp_path):
    """A repeated checksum ships as a tar hardlink with no payload, so the
    archive-start total must count its bytes once — otherwise the total is
    the logical tree size and the progress bar stalls short of 100% (oak:
    5.11 TB announced against a 4.65 TB tar)."""
    content = b"arch-all-package-bytes"
    cs, src = _seed(pool, content, "all.deb")
    out = tmp_path / "lab.tar"
    events: list[str] = []
    sink = BundleSink(out, _hdr(), prev={}, apply_script=b"# apply\n",
                      progress=lambda rel, status: events.append(status))
    sink.link(src, "archive.example/ubuntu/pool/all.deb", len(content), cs, KIND_PKG)
    sink.link(src, "ports.example/ubuntu/pool/all.deb", len(content), cs, KIND_PKG)
    assert sink.sent == 2
    assert sink.sent_bytes == len(content)

    sink.close()
    start = next(s for s in events if s.startswith("archive-start:"))
    assert start == f"archive-start:2:{len(content)}"
    m = _members(out)
    assert m["archive.example/ubuntu/pool/all.deb"] == content
    assert m["ports.example/ubuntu/pool/all.deb"] == b"->archive.example/ubuntu/pool/all.deb"
