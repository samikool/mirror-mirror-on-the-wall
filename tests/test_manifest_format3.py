"""Manifest format 3: a bundle of timeline points — each repository's points
in the header, each file tagged with the points that hold it."""
import json

from mmw.core.publisher.manifest import (ManifestHeader, iter_manifest_points, iter_manifest_sized,
                                         iter_manifest_v2, write_manifest)

POINTS = {"https://ex.org/debian/": {"label": "debian", "bases": ["ex.org/debian"], "points": [
    {"track": "ex.org/debian/dists/bookworm", "ident": "release:aa", "date": "20261101T000000Z",
     "stamp": "20261101T120000Z", "snap_label": None, "fields": {"Suite": "bookworm"}},
    {"track": "ex.org/debian/dists/bookworm", "ident": "release:bb", "date": "20261102T000000Z",
     "stamp": "20261102T120000Z", "snap_label": "nov", "fields": {"Suite": "bookworm"}}]}}

ROWS = [("ex.org/debian/dists/bookworm/Release", "c1", "https://ex.org/debian/", 10, (0,)),
        ("ex.org/debian/pool/a.deb", "c2", "https://ex.org/debian/", 20, (0, 1)),
        ("keys/debian/k.asc", "c3", None, 5, ())]


def _header(**kw):
    return ManifestHeader(network="oak", created="20261115T000000Z", previous=None, full=False, **kw)


def test_format3_round_trip(tmp_path):
    path = tmp_path / "m.jsonl"
    write_manifest(path, _header(format=3, points=POINTS, file_count=3), ROWS)
    header, rows = iter_manifest_points(path)
    assert header.format == 3 and header.points == POINTS
    assert list(rows) == ROWS


def test_format2_bytes_unchanged(tmp_path):
    path = tmp_path / "m.jsonl"
    write_manifest(path, _header(format=2, repos={}), [("x", "c", None)])
    assert "points" not in json.loads(path.read_text().splitlines()[0])


def test_older_readers_ignore_k(tmp_path):
    path = tmp_path / "m.jsonl"
    write_manifest(path, _header(format=3, points=POINTS), ROWS)
    assert list(iter_manifest_v2(path)[1]) == [(p, c, r) for p, c, r, _s, _k in ROWS]
    assert list(iter_manifest_sized(path)[1]) == [(p, c, r, s) for p, c, r, s, _k in ROWS]
