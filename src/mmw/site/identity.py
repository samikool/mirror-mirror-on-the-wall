"""Which repository here an arriving one is — by content, not by URL.

An arriving repository is one already here when its key differs only by
scheme, when it is served at the same bases, or when it carries a Release
identical to one already held (the same bytes: the same archive, wherever it
was fetched from). It is then applied under the key it has here. A tree
adopted from apply.py (`adopted:<base>`) is claimed and renamed by the
bundle instead, as before. Sharing only Origin, Label and codename — as
archive.ubuntu.com and ports.ubuntu.com do — is said, never merged.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace

ADOPTED = "adopted:"


@dataclass
class Known:
    key: str
    label: str
    bases: list[str]
    idents: set[str] = field(default_factory=set)
    fields: set[tuple[str, str, str]] = field(default_factory=set)


@dataclass
class Matches:
    mapping: dict[str, str] = field(default_factory=dict)      # arriving key → the key it has here
    rebase: dict[str, tuple[str, str]] = field(default_factory=dict)  # arriving key → (its base, ours)
    adopted: set[str] = field(default_factory=set)             # arriving keys that claim an adopted tree
    lines: list[str] = field(default_factory=list)


def _fields(origin, label, name) -> tuple[str, str, str] | None:
    return (origin or "", label or "", name) if name else None


def known_from_db(db) -> list[Known]:
    out = []
    for st in db.repo_states():
        k = Known(st.repo, st.label, list(st.bases))
        for t in db.tracks(st.repo):
            if f := _fields(t.origin, t.label, t.codename or t.suite):
                k.fields.add(f)
            k.idents |= {p.ident for p in db.points(t.id, stubs=True)}
        out.append(k)
    return out


def known_from_set(mset) -> list[Known]:
    from mmw.site import ops
    roots = {r.info.name: r for r in mset.roots}
    out = []
    for name, st in ops.set_status(mset):
        k = Known(st.repo, st.label, list(st.bases))
        try:
            history = roots[name].history(st.repo)
        except Exception:  # noqa: BLE001 — an unreadable history still matches by key and bases
            history = []
        for t, points in history:
            if f := _fields(t.origin, t.label, t.codename or t.suite):
                k.fields.add(f)
            k.idents |= {p.ident for p, *_rest in points}
        out.append(k)
    return out


def _incoming(header) -> dict[str, dict]:
    """key → {label, bases, idents, fields} of what the bundle carries."""
    out = {}
    if header.format == 3:
        for key, meta in (header.points or {}).items():
            pts = meta.get("points") or []
            out[key] = {"label": meta["label"], "bases": list(meta["bases"]),
                        "idents": {p["ident"] for p in pts},
                        "fields": {f for p in pts if (f := _fields((p.get("fields") or {}).get("Origin"),
                                                                    (p.get("fields") or {}).get("Label"),
                                                                    (p.get("fields") or {}).get("Codename")))}}
    else:
        for key, meta in (header.repos or {}).items():
            out[key] = {"label": meta["label"], "bases": list(meta.get("bases") or []), "idents": set(),
                        "fields": set()}
    return out


def _under(mine: list[str], theirs: list[str]) -> bool:
    """Every one of `theirs` is one of `mine` or lies under one."""
    return bool(theirs) and all(any(b == m or b.startswith(m + "/") for m in mine) for b in theirs)


def match(known: list[Known], header, *, foreign: bool = False) -> Matches:
    """`foreign`: another network's bundle — it adds history to what is
    here and never moves it: a repository it brings from another host is
    re-based onto the base it is served at here."""
    from mmw.core.repos import repository_identity, same_release_identity
    out = Matches()
    keys = {k.key for k in known}
    arriving = _incoming(header)
    for key, inc in sorted(arriving.items()):
        if key in keys:
            continue
        same, why = None, ""
        for k in known:
            if not k.key.startswith(ADOPTED) and repository_identity(k.key) == repository_identity(key):
                same, why = k, "the same repository by another URL scheme"
            elif sorted(k.bases) == sorted(inc["bases"]) or (k.key.startswith(ADOPTED) and _under(inc["bases"], k.bases)):
                same, why = k, f"served at {', '.join(k.bases)} here"
            elif same_release_identity(k.idents, inc["idents"]):
                same, why = k, "it holds an identical Release"
            if same is not None:
                break
        if same is None:
            look = next((k for k in known if k.fields & inc["fields"]), None)
            if look is not None:
                origin, _label, name = sorted(look.fields & inc["fields"])[0]
                out.lines.append(f"{inc['label']} looks like {look.label} (same {origin} {name}) — if it is one "
                                 f"repository: mmw site merge '{look.label}' '{inc['label']}'")
            continue
        if same.key.startswith(ADOPTED):
            out.adopted.add(key)                     # the bundle claims it as it applies
            continue
        if same.key in arriving or same.key in out.mapping.values():
            out.lines.append(f"{inc['label']} is the same archive as {same.label}, which this bundle brings too "
                             f"— applied on its own: mmw site merge '{inc['label']}' '{same.label}'")
            continue
        if foreign and sorted(same.bases) != sorted(inc["bases"]):
            if header.format != 3 or len(same.bases) != 1 or len(inc["bases"]) != 1:
                out.lines.append(f"{inc['label']} looks like {same.label} ({why}) but is served elsewhere — "
                                 f"applied on its own: mmw site merge '{inc['label']}' '{same.label}'")
                continue
            out.rebase[key] = (inc["bases"][0], same.bases[0])
        out.mapping[key] = same.key
        out.lines.append(f"{inc['label']} is {same.label} here ({why})")
    return out


def _moved(path: str, bases: tuple[str, str] | None) -> str:
    if bases is None:
        return path
    from mmw.core.repos import rebase_files
    return next(iter(rebase_files({path: ""}, *bases)))


def _rebased_meta(meta: dict, bases: tuple[str, str] | None) -> dict:
    if bases is None:
        return meta
    return {**meta, "bases": [bases[1]],
            "points": [{**p, "track": _moved(p["track"], bases)} for p in meta.get("points") or []]}


def rekey(header, rows: list[tuple], mapping: dict[str, str], rebase: dict | None = None):
    """The header and rows with each arriving key as it is here (and, for a
    key in `rebase`, its paths at the base it has here). A row's path is its
    first item, its repository its third."""
    rebase = rebase or {}
    if not mapping:
        return header, rows
    if header.format == 3:
        header = replace(header, points={mapping.get(k, k): _rebased_meta(v, rebase.get(k))
                                         for k, v in (header.points or {}).items()})
    else:
        header = replace(header, repos={mapping.get(k, k): v for k, v in (header.repos or {}).items()})
    rows = [(_moved(row[0], rebase.get(row[2])), row[1], mapping.get(row[2], row[2]), *row[3:]) for row in rows]
    return header, rows


def rekeyed_manifest(data: bytes, mapping: dict[str, str], rebase: dict | None = None) -> bytes:
    """A manifest's bytes with its repository keys (and re-based paths) as they are here."""
    rebase = rebase or {}
    if not mapping:
        return data
    lines = data.decode().splitlines()
    head = json.loads(lines[0])
    if isinstance(head.get("points"), dict):
        head["points"] = {mapping.get(k, k): _rebased_meta(v, rebase.get(k)) for k, v in head["points"].items()}
    if isinstance(head.get("repos"), dict):
        head["repos"] = {mapping.get(k, k): v for k, v in head["repos"].items()}
    out = [json.dumps(head, sort_keys=True)]
    for line in lines[1:]:
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("r") in mapping:
            row["p"] = _moved(row["p"], rebase.get(row["r"]))
            row["r"] = mapping[row["r"]]
        out.append(json.dumps(row))
    return ("\n".join(out) + "\n").encode()


def shared_bases(db) -> list[str]:
    """One line per base two repositories here both serve."""
    owners: dict[str, list[str]] = {}
    for st in db.repo_states():
        for b in st.bases:
            owners.setdefault(b, []).append(st.label)
    return [f"{a} and {b} both serve {base} — mmw site merge '{a}' '{b}'"
            for base, labels in sorted(owners.items()) if len(labels) > 1
            for a, b in [sorted(labels)[:2]]]
