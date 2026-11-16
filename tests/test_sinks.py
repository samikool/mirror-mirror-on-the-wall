import hashlib
import json
import tarfile
import threading
from pathlib import Path

import pytest

from mmw.core import config
from mmw.core.pool import KIND_PKG, Pool
from mmw.core.publisher import sinks as sinks_mod
from mmw.core.publisher.manifest import ManifestHeader
from mmw.core.publisher.nginx import ROOT_PLACEHOLDER
from mmw.core.publisher.sinks import PointsSink, TreeSink
from mmw.core.disk_io import DiskJob, Limits


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


def test_tree_sink_link_many_settles_like_link_in_item_order(pool, tmp_path):
    seeded = [_seed(pool, f"blob {i}".encode(), f"{i}.whl") for i in range(40)]
    items = [(src, f"host/p/{i}.whl", 6, cs, KIND_PKG) for i, (cs, src) in enumerate(seeded)]
    items[7] = (tmp_path / "missing", "host/p/7.whl", 6, seeded[7][0], KIND_PKG)
    items[30] = (tmp_path / "gone", "host/p/30.whl", 6, seeded[30][0], KIND_PKG)
    sink = TreeSink(tmp_path / "pub", prev={"host/p/0.whl": seeded[0][0]})

    results = list(sink.link_many(items))
    statuses = [status for status, _ in results]

    assert statuses[0] == "skipped"                      # the record's word, no disk
    assert statuses[7] == statuses[30] == "error"
    assert statuses.count("linked") == 37
    assert [e.split(":")[0] for e in sink.errors] == ["host/p/7.whl", "host/p/30.whl"]
    assert [error for _, error in results if error] == sink.errors   # each with its own row
    assert (tmp_path / "pub" / "host/p/39.whl").read_bytes() == b"blob 39"
    assert [rel for rel, _ in sink.manifest_rows] == [it[1] for i, it in enumerate(items) if i not in (7, 30)]
    sink.close()


def test_tree_sink_link_many_yields_each_row_before_the_run_is_done(pool, tmp_path, monkeypatch, disk_limits):
    disk_limits(write_threads=8)
    seeded = [_seed(pool, f"blob {i}".encode(), f"{i}.whl") for i in range(20)]
    sink = TreeSink(tmp_path / "pub")
    gate, place, timed_out = threading.Event(), sink._place, []

    def held(src, rel):
        if rel.endswith("/19.whl"):
            timed_out.append(not gate.wait(5))           # the run's last row is stuck
        return place(src, rel)

    monkeypatch.setattr(sink, "_place", held)
    results = sink.link_many([(src, f"host/p/{i}.whl", 6, cs, KIND_PKG) for i, (cs, src) in enumerate(seeded)])

    assert next(results) == ("linked", None)             # reported while row 19 still waits
    gate.set()
    assert [status for status, _ in results] == ["linked"] * 19
    assert timed_out == [False]                          # nobody waited for row 19 to report row 0
    sink.close()


def test_link_many_runs_concurrently_at_8(pool, tmp_path, monkeypatch, disk_limits):
    """Serial-fails: row 0's placement waits on something only row 5's sets."""
    disk_limits(write_threads=8)
    seeded = [_seed(pool, f"blob {i}".encode(), f"{i}.whl") for i in range(10)]
    sink = TreeSink(tmp_path / "pub")
    row5, place, timed_out = threading.Event(), sink._place, []

    def held(src, rel):
        if rel.endswith("/0.whl"):
            timed_out.append(not row5.wait(5))
        if rel.endswith("/5.whl"):
            row5.set()
        return place(src, rel)

    monkeypatch.setattr(sink, "_place", held)
    items = [(src, f"host/p/{i}.whl", 6, cs, KIND_PKG) for i, (cs, src) in enumerate(seeded)]
    assert [status for status, _ in sink.link_many(items)] == ["linked"] * 10
    assert timed_out == [False]
    sink.close()


def test_link_many_at_one_thread_is_inline(pool, tmp_path, monkeypatch, disk_limits):
    disk_limits(write_threads=1)
    seeded = [_seed(pool, f"blob {i}".encode(), f"{i}.whl") for i in range(5)]
    sink = TreeSink(tmp_path / "pub")
    place, threads = sink._place, []
    monkeypatch.setattr(sink, "_place",
                        lambda src, rel: (threads.append(threading.current_thread()), place(src, rel))[1])
    items = [(src, f"host/p/{i}.whl", 6, cs, KIND_PKG) for i, (cs, src) in enumerate(seeded)]
    assert [status for status, _ in sink.link_many(items)] == ["linked"] * 5
    assert threads == [threading.main_thread()] * 5
    sink.close()


def test_link_many_stop_records_only_settled_rows(pool, tmp_path, disk_limits):
    disk_limits(write_threads=4)
    seeded = [_seed(pool, f"blob {i}".encode(), f"{i}.whl") for i in range(40)]
    stop = threading.Event()
    sink = TreeSink(tmp_path / "pub", stop_event=stop)
    items = [(src, f"host/p/{i}.whl", 6, cs, KIND_PKG) for i, (cs, src) in enumerate(seeded)]
    yielded = []
    for n, (status, _error) in enumerate(sink.link_many(items)):
        yielded.append(items[n][1])
        if n == 2:
            stop.set()
    assert 3 <= len(yielded) < 40
    assert [rel for rel, _ in sink.manifest_rows] == yielded
    # What the ruling is for: every file a stopped run put in the tree is in
    # its record — nothing placed is left outside it.
    on_disk = {p.relative_to(tmp_path / "pub").as_posix()
               for p in (tmp_path / "pub" / "host").rglob("*") if p.is_file()}
    assert on_disk == set(yielded)
    sink.close()


def test_tree_sink_owns_a_job_it_made(tmp_path):
    sink = TreeSink(tmp_path / "pub")
    assert sink.job.name == "publish"
    sink.close()


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
    sink = PointsSink(out, _hdr(), sent=set(),
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
    sink = PointsSink(out, _hdr(), sent=set(),
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
    sink = PointsSink(out, _hdr(), sent=set(),
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

    payload = b"z" * 3_000_000  # several of the tar writer's 1 MiB copy reads,
    # so "incremental" is actually exercised.
    cs, src = _seed(pool, payload, "big.deb")
    out = tmp_path / "lab.tar"
    events: list[tuple[str, str]] = []
    sink = PointsSink(out, _hdr(), sent=set(),
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
    sink = PointsSink(out, _hdr(), sent=set())
    sink.link(src, "host/repo/pool/a.deb", 5, cs, KIND_PKG)
    sink.close()   # must not raise
    assert out.exists()


def test_bundle_sink_sent_bytes_counts_duplicate_content_once(pool, tmp_path):
    """A repeated checksum is carried once (one member, named by it), so the
    archive-start total counts its bytes once — otherwise the total is the
    logical tree size and the progress bar stalls short of 100% (oak: 5.11 TB
    announced against a 4.65 TB tar)."""
    from mmw.core.publisher.manifest import blob_member
    content = b"arch-all-package-bytes"
    cs, src = _seed(pool, content, "all.deb")
    out = tmp_path / "lab.tar"
    events: list[str] = []
    sink = PointsSink(out, _hdr(), sent=set(),
                      progress=lambda rel, status: events.append(status))
    sink.link(src, "archive.example/ubuntu/pool/all.deb", len(content), cs, KIND_PKG)
    sink.link(src, "ports.example/ubuntu/pool/all.deb", len(content), cs, KIND_PKG)
    assert sink.sent == 1
    assert sink.sent_bytes == len(content)

    sink.close()
    start = next(s for s in events if s.startswith("archive-start:"))
    assert start == f"archive-start:1:{len(content)}"
    assert _members(out)[blob_member(cs)] == content


def _bundle_with(pool, tmp_path, n, **kw):
    sink = PointsSink(tmp_path / "lab.tar", _hdr(), sent=set(), **kw)
    for i in range(n):
        cs, src = _seed(pool, f"blob {i}".encode(), f"{i}.deb")
        sink.link(src, f"host/repo/pool/{i}.deb", len(f"blob {i}"), cs, KIND_PKG)
    return sink


def test_bundle_preflight_reports_progress(pool, tmp_path, monkeypatch):
    from mmw.core import verify
    monkeypatch.setattr(verify, "_STAT_REPORT_EVERY", 5)
    calls = []
    sink = _bundle_with(pool, tmp_path, 12, progress=lambda rel, status: calls.append((rel, status)))
    sink.close()
    pre = [c for c in calls if c[1].startswith("preflight:")]
    assert pre == [("", "preflight:5:12"), ("", "preflight:10:12"), ("", "preflight:12:12")]


def test_bundle_preflight_stop_writes_nothing(pool, tmp_path, monkeypatch):
    from mmw.core import verify
    monkeypatch.setattr(verify, "_STAT_REPORT_EVERY", 5)
    stop = threading.Event()

    def progress(rel, status):
        if status.startswith("preflight:"):
            stop.set()

    sink = _bundle_with(pool, tmp_path, 12, progress=progress, stop_event=stop)
    with pytest.raises(InterruptedError):
        sink.close()
    assert not (tmp_path / "lab.tar").exists()
    assert not (tmp_path / "lab.tar.tmp").exists()


def test_bundle_preflight_is_concurrent(pool, tmp_path, monkeypatch, disk_limits):
    """Serial-fails: row 0's stat waits on an event only row 5's stat sets."""
    disk_limits(read_threads=8)
    from mmw.core import disk_io
    sink = _bundle_with(pool, tmp_path, 10)
    paths = [str(src) for src, _size, _data in sorted(sink._carry.values(), key=lambda v: str(v[0]))]
    row5, timed_out, real = threading.Event(), [], disk_io.ops.stat

    def stat(path):
        if str(path) == paths[0]:
            timed_out.append(not row5.wait(5))
        if str(path) == paths[5]:
            row5.set()
        return real(path)

    monkeypatch.setattr(disk_io.ops, "stat", stat)
    sink.close()
    assert timed_out == [False]
    assert (tmp_path / "lab.tar").exists()


def _stale_tree(root, n=10, base="host/repo"):
    rels = []
    for i in range(n):
        rel = f"{base}/pool/d{i % 3}/f{i}"
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(b"x" * (i + 1))
        rels.append(rel)
    return rels


def _gated_unlink(monkeypatch):
    """Serial-fails gate: the first os.unlink waits until a second one starts."""
    import os
    lock, calls, second, timed_out, real = threading.Lock(), [0], threading.Event(), [], os.unlink

    def unlink(path, *a, **k):
        with lock:
            calls[0] += 1
            n = calls[0]
        if n == 1:
            timed_out.append(not second.wait(5))
        else:
            second.set()
        return real(path, *a, **k)

    monkeypatch.setattr(os, "unlink", unlink)
    return timed_out


def test_finalize_from_record_unlinks_concurrently(tmp_path, monkeypatch, disk_limits):
    disk_limits(write_threads=8)
    root = tmp_path / "pub"
    rels = _stale_tree(root)
    sink = TreeSink(root, prev={r: "cs" for r in rels})
    timed_out = _gated_unlink(monkeypatch)
    assert sink.finalize_group("host/repo", keep=set(), live_checksums=set(), exclude=set()) == \
        (10, sum(range(1, 11)))
    assert timed_out == [False]
    sink.close()


def test_finalize_group_walk_unlinks_concurrently(tmp_path, monkeypatch, disk_limits):
    disk_limits(write_threads=8)
    root = tmp_path / "pub"
    _stale_tree(root)
    sink = TreeSink(root)
    timed_out = _gated_unlink(monkeypatch)
    assert sink.finalize_group("host/repo", keep=set(), live_checksums=set(), exclude=set()) == \
        (10, sum(range(1, 11)))
    assert timed_out == [False]
    sink.close()


def test_finalize_from_record_stops(tmp_path):
    root = tmp_path / "pub"
    rels = _stale_tree(root)
    stop = threading.Event()

    def progress(rel, status):
        if status == "pruned":
            stop.set()

    sink = TreeSink(root, progress=progress, stop_event=stop, prev={r: "cs" for r in rels})
    pruned, _ = sink.finalize_group("host/repo", keep=set(), live_checksums=set(), exclude=set())
    assert 1 <= pruned < 10
    still_there = {r for r in rels if (root / r).exists()}
    assert still_there and still_there <= {rel for rel, _ in sink.manifest_rows}
    sink.close()


def test_finalize_progress_in_row_order(tmp_path, disk_limits):
    disk_limits(write_threads=8)
    root = tmp_path / "pub"
    rels = _stale_tree(root, n=30)
    seen = []
    sink = TreeSink(root, progress=lambda rel, status: status == "pruned" and seen.append(rel),
                    prev={r: "cs" for r in rels})
    sink.finalize_group("host/repo", keep=set(), live_checksums=set(), exclude=set())
    assert seen == sorted(rels)
    sink.close()


def test_prune_by_hash_dir_on_the_lane_keeps_newest_stale(tmp_path, monkeypatch):
    import os
    bh = tmp_path / "by-hash" / "SHA256"
    bh.mkdir(parents=True)
    for i, name in enumerate(["live", "s1", "s2", "s3", "s4", "s5", "s6"]):
        (bh / name).write_bytes(b"x")
        os.utime(bh / name, (1000 + i, 1000 + i))
    timed_out = _gated_unlink(monkeypatch)
    with DiskJob("t", limits=Limits(write_threads=8, read_threads=8)) as job:
        assert sinks_mod._prune_by_hash_dir(bh, {"live"}, job) == 4
    assert sorted(p.name for p in bh.iterdir()) == ["live", "s5", "s6"]
    assert timed_out == [False]


def test_prune_stale_bases_counts_files_and_bytes(tmp_path, disk_limits):
    disk_limits(read_threads=4)
    root = tmp_path / "pub"
    (root / "live.example/repo/a").mkdir(parents=True)
    (root / "live.example/repo/a/f").write_bytes(b"keep")
    for rel, body in [("gone.example/repo/x", b"12345"), ("gone.example/repo/d/y", b"123")]:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(body)
    (root / "gone.example/repo/link").symlink_to(root / "live.example/repo/a/f")
    sink = TreeSink(root)
    assert sink.prune_stale_bases({"live.example/repo"}) == (2, 8)
    assert not (root / "gone.example").exists() and (root / "live.example/repo/a/f").exists()
    sink.close()


def _plan(pool, sink, n=12):
    for i in range(n):
        cs, src = _seed(pool, f"payload {i}".encode() * 50, f"{i}.deb")
        sink.link(src, f"host/repo/pool/{i}.deb", len(f"payload {i}") * 50, cs, KIND_PKG)
        if i % 4 == 0:                       # a repeat: a hardlink entry, no payload
            sink.link(src, f"host/ports/pool/{i}.deb", len(f"payload {i}") * 50, cs, KIND_PKG)


def test_bundle_tar_identical_with_and_without_readahead(pool, tmp_path, disk_limits):
    outs = []
    for readahead in (0, 16):
        disk_limits(read_threads=8, readahead=readahead)
        out = tmp_path / f"lab-{readahead}.tar"
        sink = PointsSink(out, _hdr(), sent=set())
        _plan(pool, sink)
        sink.close()
        outs.append(out.read_bytes())
    assert outs[0] == outs[1]


def test_bundle_vanished_blob_aborts_with_readahead(pool, tmp_path, monkeypatch, disk_limits):
    disk_limits(read_threads=8, readahead=16)
    from mmw.core import disk_io, verify
    out = tmp_path / "lab.tar"
    sink = PointsSink(out, _hdr(), sent=set())
    _plan(pool, sink)
    victim = sorted(src for src, _size, data in sink._carry.values() if data is None)[6]
    real_stats, real_open, handles = verify.blob_stats, disk_io.ops.open_rb, []

    def stats_then_vanish(*a, **k):
        yield from real_stats(*a, **k)
        victim.unlink()                      # gone after the pre-flight passed

    def open_rb(path):
        fh = real_open(path)
        handles.append(fh)
        return fh

    monkeypatch.setattr(verify, "blob_stats", stats_then_vanish)
    monkeypatch.setattr(disk_io.ops, "open_rb", open_rb)
    with pytest.raises(OSError):
        sink.close()
    assert not out.exists() and not out.with_suffix(".tar.tmp").exists()
    assert handles and all(fh.closed for fh in handles)


def test_prune_stale_bases_after_a_stop_deletes_nothing(tmp_path):
    root = tmp_path / "pub"
    (root / "live.example/repo").mkdir(parents=True)
    (root / "gone.example/repo/x").parent.mkdir(parents=True)
    (root / "gone.example/repo/x").write_bytes(b"12345")
    stop = threading.Event()
    stop.set()
    sink = TreeSink(root, stop_event=stop)
    assert sink.prune_stale_bases({"live.example/repo"}) == (0, 0)
    assert (root / "gone.example/repo/x").exists()
    sink.close()
