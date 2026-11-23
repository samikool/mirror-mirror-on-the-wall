"""Writing a format-3 bundle (timeline points) by hand, for tests."""
import io
import json
import tarfile

from mmw.core.publisher.manifest import ManifestHeader, blob_member


def point_dict(draft, stamp, snap_label=None) -> dict:
    return {"track": draft.track, "ident": draft.ident, "date": draft.date, "stamp": stamp,
            "snap_label": snap_label, "fields": dict(draft.fields)}


def write_format3(out, network: str, repos: dict, blobs: dict[str, bytes], *, network_files=None,
                  settings=None, selection=None, created="20261115T000000Z", left=None) -> str:
    """`repos`: key -> (label, bases, [(PointDraft, stamp, snap_label)]);
    `blobs`: checksum -> bytes (only those listed go in the tar);
    `network_files`: path -> bytes, rows with no repository."""
    header_points, rows, members = {}, [], {}
    for key, (label, bases, points) in repos.items():
        header_points[key] = {"label": label, "bases": bases,
                              "points": [point_dict(d, st, sl) for d, st, sl in points]}
        holders: dict[tuple[str, str], list[int]] = {}
        for i, (d, _st, _sl) in enumerate(points):
            for p, c in d.files.items():
                holders.setdefault((p, c), []).append(i)
        for (p, c), k in sorted(holders.items()):
            rows.append({"p": p, "c": c, "r": key, "k": k})
            if c in blobs:
                members[blob_member(c)] = blobs[c]
    for p, data in sorted((network_files or {}).items()):
        import hashlib
        c = hashlib.sha256(data).hexdigest()
        rows.append({"p": p, "c": c})
        members[blob_member(c)] = data
    header = ManifestHeader(network=network, created=created, previous=None, full=False,
                            file_count=len(members), format=3, points=header_points, settings=settings,
                            selection=selection, left=left)
    fields = {k: v for k, v in header.__dict__.items() if v is not None or k in ("previous",)}
    manifest = (json.dumps(fields, sort_keys=True) + "\n" + "".join(json.dumps(r) + "\n" for r in rows)).encode()
    with tarfile.open(out, "w") as tar:
        for name, data in [(".mmw/manifest.jsonl", manifest), *members.items()]:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
    return str(out)


def member_paths(bundle) -> dict[str, str]:
    """A bundle's members → the path each one holds: itself, or (format 3,
    named by checksum) the first path the manifest gives that checksum."""
    from mmw.core.publisher.manifest import blob_member
    with tarfile.open(bundle) as tar:
        lines = tar.extractfile(".mmw/manifest.jsonl").read().decode().splitlines()
        names = tar.getnames()
    header = json.loads(lines[0])
    if header.get("format") != 3:
        return {n: n for n in names}
    first: dict[str, str] = {}
    for line in sorted(lines[1:]):
        row = json.loads(line)
        first.setdefault(blob_member(row["c"]), row["p"])
    return {n: first.get(n, n) for n in names}
