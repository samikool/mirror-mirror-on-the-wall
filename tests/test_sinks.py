import hashlib
import tarfile
from pathlib import Path

import pytest
import zstandard

from mmw.core import config
from mmw.core.pool import KIND_META, KIND_PKG, Pool
from mmw.core.publisher.manifest import ManifestHeader, delta
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
    """`progress` is the Sink contract used by the TUI's PublishProgressModal,
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
    """Extract a .tar.zst into {name: content} for assertions."""
    dctx = zstandard.ZstdDecompressor()
    out: dict[str, bytes] = {}
    with open(path, "rb") as raw, dctx.stream_reader(raw) as zr:
        with tarfile.open(fileobj=zr, mode="r|") as tar:
            for ti in tar:
                if ti.isfile():
                    out[ti.name] = tar.extractfile(ti).read()
                elif ti.islnk():
                    out[ti.name] = b"->" + ti.linkname.encode()
    return out


def _hdr(previous=None, full=True):
    return ManifestHeader(network="lab", created="20260906T143000Z",
                          previous=previous, full=full)


def test_bundle_sink_writes_manifest_first(pool, tmp_path):
    cs, src = _seed(pool, b"hello", "a.deb")
    out = tmp_path / "lab.tar.zst"
    sink = BundleSink(out, _hdr(), prev={}, apply_script=b"# apply\n")
    sink.link(src, "host/repo/pool/a.deb", 5, cs, KIND_PKG)
    sink.close()

    dctx = zstandard.ZstdDecompressor()
    with open(out, "rb") as raw, dctx.stream_reader(raw) as zr:
        with tarfile.open(fileobj=zr, mode="r|") as tar:
            first = next(iter(tar))
    assert first.name == ".mmw/manifest.jsonl.zst"


def test_bundle_sink_carries_payload_and_apply_script(pool, tmp_path):
    cs, src = _seed(pool, b"hello", "a.deb")
    out = tmp_path / "lab.tar.zst"
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

    out = tmp_path / "lab.tar.zst"
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
    sink = BundleSink(tmp_path / "lab.tar.zst",
                      _hdr(previous="20260901T090000Z", full=False),
                      prev={"host/repo/pool/a.deb": cs_a}, apply_script=b"")
    sink.link(src_a, "host/repo/pool/a.deb", 9, cs_a, KIND_PKG)
    sink.link(src_b, "host/repo/pool/b.deb", 9, cs_b, KIND_PKG)
    sink.close()

    assert dict(sink.manifest_rows) == {
        "host/repo/pool/a.deb": cs_a,
        "host/repo/pool/b.deb": cs_b,
    }


def test_bundle_sink_dedups_by_checksum_as_tar_hardlinks(pool, tmp_path):
    """An apt index file appears canonically and under by-hash/ — one copy."""
    cs, src = _seed(pool, b"Packages content", "Packages.gz")
    out = tmp_path / "lab.tar.zst"
    sink = BundleSink(out, _hdr(), prev={}, apply_script=b"")
    sink.link(src, "h/r/dists/n/main/binary-amd64/Packages.gz", 16, cs, KIND_META)
    sink.link(src, f"h/r/dists/n/main/binary-amd64/by-hash/SHA256/{cs}", 16, cs, KIND_META)
    sink.close()

    m = _members(out)
    assert m["h/r/dists/n/main/binary-amd64/Packages.gz"] == b"Packages content"
    assert m[f"h/r/dists/n/main/binary-amd64/by-hash/SHA256/{cs}"] == \
        b"->h/r/dists/n/main/binary-amd64/Packages.gz"


def test_bundle_sink_delete_list_only_when_pruning(pool, tmp_path):
    prev = {"host/repo/pool/gone.deb": "aa" * 32}
    for prune, expected in ((True, True), (False, False)):
        out = tmp_path / f"lab-{prune}.tar.zst"
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


def test_bundle_sink_reports_error_on_size_mismatch_without_corrupting_archive(
        pool, tmp_path):
    """The size recorded at link() time (from the pool DB) is checked against
    the blob actually on disk before anything is streamed. A stale/wrong size
    must be reported and skipped, never silently written as a short tar
    entry that would misalign every entry after it."""
    cs, src = _seed(pool, b"hello", "a.deb")
    out = tmp_path / "lab.tar.zst"
    sink = BundleSink(out, _hdr(), prev={}, apply_script=b"")
    # Claim a size larger than the actual 5-byte blob.
    sink.link(src, "host/repo/pool/a.deb", 999, cs, KIND_PKG)
    sink.link(src, "host/repo/pool/b.deb", 5, cs, KIND_PKG)  # dup checksum, real size
    sink.close()

    assert len(sink.errors) == 1
    assert "a.deb" in sink.errors[0]
    assert "999" in sink.errors[0]
    # The archive itself must still be well-formed and readable to the end —
    # a bad entry does not corrupt what comes after it, because nothing for
    # it was ever written to the tar stream.
    m = _members(out)
    assert "host/repo/pool/a.deb" not in m
    assert m["host/repo/pool/b.deb"] == b"hello"


def test_bundle_sink_delta_agrees_with_manifest_delta(pool, tmp_path):
    """`BundleSink` decides per-row in `link()` what to send and per-prefix in
    `finalize_group()` what to delete; `manifest.delta()` states the same
    rule as one set difference. Two implementations of one rule drift —
    this pins them to the same answer on a mixed fixture: unchanged, changed,
    brand new, and removed-since-the-last-bundle."""
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

    sink = BundleSink(tmp_path / "lab.tar.zst", _hdr(previous="x", full=False),
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
    assert "archive.ubuntu.com" not in body   # never hardcoded — design-notes.md
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
    assert "None" not in mirror_root_line
