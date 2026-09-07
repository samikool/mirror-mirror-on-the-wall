import pytest

from mmw.core.publisher.manifest import (
    ManifestHeader, delta, latest_manifest_path, ledger_dir,
    now_stamp, read_manifest, write_manifest,
)


def test_manifest_round_trip(tmp_path):
    hdr = ManifestHeader(network="lab", created="20260906T143000Z",
                         previous=None, full=True)
    files = [("a/b.deb", "aa" * 32), ("a/c.deb", "bb" * 32)]
    p = tmp_path / "m.jsonl.zst"
    write_manifest(p, hdr, files)

    got_hdr, got_files = read_manifest(p)
    assert got_hdr == hdr
    assert got_files == dict(files)


def test_manifest_round_trip_empty_files(tmp_path):
    """No files at all — an empty network, or a full re-bundle of nothing new
    — must still round-trip the header and produce an empty file map, not
    blow up on an empty JSONL body."""
    hdr = ManifestHeader(network="lab", created="20260906T143000Z",
                         previous=None, full=True)
    p = tmp_path / "empty.jsonl.zst"
    write_manifest(p, hdr, [])

    got_hdr, got_files = read_manifest(p)
    assert got_hdr == hdr
    assert got_files == {}


def test_write_manifest_cleans_up_tmp_on_failure(tmp_path):
    """A `files` generator that raises mid-stream (a DB error while walking
    pool rows is a real path) must not leave a `.tmp` file behind next to the
    manifest it never produced."""
    hdr = ManifestHeader(network="lab", created="20260906T143000Z",
                         previous=None, full=True)
    p = tmp_path / "m.jsonl.zst"

    def _boom():
        yield ("a/b.deb", "aa" * 32)
        raise RuntimeError("pool read failed")

    with pytest.raises(RuntimeError):
        write_manifest(p, hdr, _boom())

    assert not p.exists()
    assert not p.with_suffix(p.suffix + ".tmp").exists()


def test_delta_sends_new_and_changed_only():
    prev = {"same": "11", "changed": "22", "gone": "33"}
    cur = {"same": "11", "changed": "99", "new": "44"}
    send, delete = delta(prev, cur)
    assert send == {"changed", "new"}
    assert delete == {"gone"}


def test_delta_against_empty_prev_sends_everything():
    cur = {"a": "1", "b": "2"}
    send, delete = delta({}, cur)
    assert send == {"a", "b"}
    assert delete == set()


def test_ledger_dir_is_under_mirror_dir(tmp_path):
    assert ledger_dir(tmp_path, "lab") == tmp_path / "bundles" / "lab"


def test_latest_manifest_is_newest_by_timestamp(tmp_path):
    d = ledger_dir(tmp_path, "lab")
    d.mkdir(parents=True)
    hdr = ManifestHeader(network="lab", created="x", previous=None, full=True)
    for stamp in ("20260901T090000Z", "20260906T143000Z", "20260903T120000Z"):
        write_manifest(d / f"{stamp}.manifest.jsonl.zst", hdr, [])
    assert latest_manifest_path(tmp_path, "lab").name.startswith("20260906T143000Z")


def test_latest_manifest_is_none_when_never_bundled(tmp_path):
    assert latest_manifest_path(tmp_path, "lab") is None


def test_now_stamp_shape():
    s = now_stamp()
    assert len(s) == 16 and s[8] == "T" and s.endswith("Z")
