"""Kept manifests are evidence of what crossed the airgap."""
import io
import tarfile

import pytest

from mmw.core.points import resolve_when
from mmw.core.publisher.kept import read_manifest_header, state_at
from mmw.core.publisher.manifest import ManifestHeader, write_manifest


def kept(folder, stamp, network="lab", *, fmt=3, tar=True):
    ledger = folder / f"{network}-{stamp}.manifest.jsonl"
    write_manifest(ledger, ManifestHeader(network, stamp, None, True, format=fmt),
                   [("pool/a.deb", "abc"), ("nginx/mirror.conf", "def")])
    if not tar:
        return ledger
    path = folder / f"{network}-{stamp}.tar"
    with tarfile.open(path, "w") as archive:
        archive.add(ledger, arcname=".mmw/manifest.jsonl")
    ledger.unlink()
    return path


def test_state_at_picks_newest_on_or_before(tmp_path):
    for day in (1, 14, 20):
        kept(tmp_path, f"202610{day:02}T000000Z")
    state = state_at([tmp_path], "lab", resolve_when("2026-10-15"), skipped=[])
    assert state.created == "20261014T000000Z"
    assert state.checksums == frozenset({"abc", "def"})


def test_other_networks_are_ignored(tmp_path):
    lab = kept(tmp_path, "20261001T000000Z")
    kept(tmp_path, "20261014T000000Z", "prod")
    assert state_at([tmp_path], "lab", "20261015T000000Z", skipped=[]).where == str(lab)


def test_broken_bundle_is_skipped_and_named(tmp_path):
    good = kept(tmp_path, "20261001T000000Z")
    (tmp_path / "broken.tar").write_bytes(b"broken")
    with tarfile.open(tmp_path / "wrong.tar", "w") as archive:
        archive.addfile(tarfile.TarInfo("README"), io.BytesIO())
    skipped = []
    assert state_at([tmp_path], "lab", "20261015T000000Z", skipped=skipped).where == str(good)
    assert len(skipped) == 2
    assert any("broken.tar" in s for s in skipped)
    assert any("wrong.tar" in s for s in skipped)


def test_reads_a_1_4_format_2_manifest(tmp_path):
    path = kept(tmp_path, "20261001T000000Z", fmt=2)
    assert read_manifest_header(path)["format"] == 2
    assert state_at([tmp_path], "lab", "20261015T000000Z", skipped=[]).checksums == {"abc", "def"}


@pytest.mark.parametrize("compressed", [False, True])
def test_ledger_manifest_counts_too(tmp_path, compressed):
    path = kept(tmp_path, "20261001T000000Z", tar=False)
    if compressed:
        import zstandard
        path.with_suffix(path.suffix + ".zst").write_bytes(zstandard.ZstdCompressor().compress(path.read_bytes()))
        path.unlink()
    assert state_at([tmp_path], "lab", "20261015T000000Z", skipped=[]).created == "20261001T000000Z"


def test_only_the_first_member_is_read(tmp_path, monkeypatch):
    path = kept(tmp_path, "20261001T000000Z")
    original = tarfile.TarFile.next

    def first_only(self):
        if len(self.members) >= 1 and self.firstmember is None:
            raise AssertionError("read a second member")
        return original(self)
    monkeypatch.setattr(tarfile.TarFile, "next", first_only)
    assert read_manifest_header(path)["network"] == "lab"


def test_unreadable_header_returns_none(tmp_path):
    assert read_manifest_header(tmp_path / "missing.tar") is None


def test_incomplete_tmp_and_corrupt_rows_are_named(tmp_path):
    kept(tmp_path, "20261001T000000Z")
    (tmp_path / "later.tar.tmp").write_bytes(b"unfinished")
    bad = kept(tmp_path, "20261002T000000Z", tar=False)
    with bad.open("a") as stream:
        stream.write('{"p": "broken"}\n')
    skipped = []
    assert state_at([tmp_path], "lab", "20261015T000000Z", skipped=skipped).created == "20261001T000000Z"
    assert len(skipped) == 2
    assert any("later.tar.tmp" in why for why in skipped)


def test_corrupt_compressed_ledger_is_skipped(tmp_path):
    bad = tmp_path / "broken.manifest.jsonl.zst"
    bad.write_bytes(b"\x28\xb5\x2f\xfdgarbage")
    skipped = []
    assert read_manifest_header(bad) is None
    assert state_at([tmp_path], "lab", "20261015T000000Z", skipped=skipped) is None
    assert len(skipped) == 1 and str(bad) in skipped[0]
