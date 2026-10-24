"""Describe a bundle tar member by member — name, order, type, mode, link
target and content hash — so a refactor can prove it wrote the same bundle.
The manifest's own bytes carry the stamps, so it is described by its rows."""
import hashlib
import json
import tarfile
from pathlib import Path


def describe(bundle: Path) -> list[dict]:
    out = []
    with tarfile.open(bundle) as tar:
        for m in tar:
            d = {"name": m.name, "type": m.type.decode(), "mode": m.mode, "size": m.size,
                 "link": m.linkname or None}
            if m.isfile():
                data = tar.extractfile(m).read()
                if m.name == ".mmw/manifest.jsonl":
                    lines = data.decode().splitlines()
                    header = json.loads(lines[0])
                    d["header_keys"] = sorted(header)
                    d["rows"] = lines[1:]
                    d.pop("size")
                else:
                    d["sha256"] = hashlib.sha256(data).hexdigest()
            out.append(d)
    return out
