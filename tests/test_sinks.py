import hashlib
from pathlib import Path

import pytest

from mmw.core import config
from mmw.core.pool import KIND_PKG, Pool
from mmw.core.publisher.sinks import TreeSink


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
