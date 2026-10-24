"""`mmw site apply`: take a bundle (format 2) onto a site.

Nothing the site serves changes until the whole bundle is in: every member
goes into the store first, hashed on the way, and is matched against the
manifest; every file each repository's new version lists must then be in
the store. Only then are versions recorded and repositories switched — a
held one keeps serving what it served, its new version stored for later.
"""
import io
from dataclasses import dataclass, field
from pathlib import Path

from mmw.core import disk_io
from mmw.core.publisher.manifest import iter_manifest_v2
from mmw.core.snapshots import repo_digests
from mmw.resources.apply import (
    MANIFEST_MEMBER, ROOT_PLACEHOLDER, TLS_PREFIX, _install_tls, _nginx_root_config,
    _prune_empty_parents, render_derived,
)
from mmw.site.db import SiteDB
from mmw.site.home import load_site
from mmw.site.lock import SiteLock
from mmw.site.reader import SourceError, open_source
from mmw.site.store import Store
from mmw.site.switch import resume_switches, switch_repo


class ApplyError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class ApplyResult:
    created: str = ""
    updated: list[str] = field(default_factory=list)
    stored_held: list[str] = field(default_factory=list)
    refused: dict[str, str] = field(default_factory=dict)
    network_changed: bool = False
    actions: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)


def _receive(src: str, store: Store, manifest_path: Path, dry_run: bool):
    """Stream the members in: (name → (checksum, size), TLS files)."""
    got: dict[str, tuple[str, int]] = {}
    links: dict[str, str] = {}
    tls: dict[str, bytes] = {}
    manifest = False
    try:
        for m in open_source(src):
            if m.name == MANIFEST_MEMBER:
                disk_io.ops.write_bytes(manifest_path, m.fh.read())
                manifest = True
            elif m.name.startswith(TLS_PREFIX):
                tls[m.name[len(TLS_PREFIX):]] = m.fh.read()
            elif m.name.startswith(".mmw/"):
                continue                        # the bundle's own apply.py and the like
            elif m.kind == "link":
                links[m.name] = m.linkname
            elif dry_run:
                got[m.name] = disk_io.ops.hash_fh(m.fh)
            else:
                got[m.name] = store.add_stream(m.fh)
    except SourceError as e:
        raise ApplyError(e.code, str(e)) from None
    for name, target in links.items():
        if target not in got:
            raise ApplyError(2, f"bundle is malformed: {name!r} links to {target!r}, which it lacks")
        got[name] = got[target]
    if not manifest:
        raise ApplyError(2, "bundle is malformed: no .mmw/manifest.jsonl")
    return got, tls


def _place_network_file(store: Store, root: Path, rel: str, checksum: str) -> None:
    if _nginx_root_config(rel):
        # A fresh file, never a hardlink: the root is substituted into it,
        # and writing through a link would rewrite the stored blob.
        data = disk_io.ops.read_bytes(store.path(checksum))
        disk_io.ops.atomic_write(root / rel, data.replace(ROOT_PLACEHOLDER.encode(), str(root).encode()),
                                 tmp_dir=store.place_tmp)
    else:
        store.place(checksum, root / rel)


def _size(store: Store, got: dict, checksum: str, sizes: dict[str, int]) -> int:
    if checksum not in sizes:
        sizes[checksum] = disk_io.ops.stat(store.path(checksum)).st_size
    return sizes[checksum]


def apply_bundle(home: Path, src: str, *, forward: set[str] | None = None, forward_all: bool = False,
                 dry_run: bool = False, say=print) -> ApplyResult:
    settings = load_site(home)
    root = Path(settings.root)
    store = Store(root)
    result = ApplyResult()
    with SiteLock(home), SiteDB(home) as db:
        if not dry_run:
            store.sweep_tmp()
            resume_switches(db, store, root)
        manifest_path = Path(home) / "incoming-manifest.jsonl"
        try:
            got, tls = _receive(src, store, manifest_path, dry_run)
            header, rows = iter_manifest_v2(manifest_path)
            rows = list(rows)
        finally:
            disk_io.ops.unlink(manifest_path, missing_ok=True)
        if (header.format or 1) < 2:
            raise ApplyError(5, 'this is an apply.py bundle (far_side = "apply") — apply it with apply.py, '
                                'or set far_side = "mmw" on the connected side and bundle again')
        result.created = header.created
        expected = {p: c for p, c, _r in rows}
        for name, (checksum, _size_) in got.items():
            if name not in expected:
                raise ApplyError(2, f"bundle is malformed: {name!r} is not in its manifest")
            if expected[name] != checksum:
                raise ApplyError(2, f"{name} does not match the manifest — the bundle is damaged; "
                                    "copy it again")
        if header.file_count is not None and len(got) != header.file_count:
            raise ApplyError(5, f"bundle is truncated: expected {header.file_count} file(s), "
                                f"got {len(got)} — copy it again")
        have = store.has
        sizes = {cs: size for cs, size in got.values()}

        # Each repository's new version: complete, or refused alone.
        recorded: dict[str, int] = {}
        for repo, meta in sorted((header.repos or {}).items()):
            mine = [(p, c) for p, c, r in rows if r == repo]
            label = meta["label"]
            if repo_digests(mine, {repo: meta["bases"]})[repo] != meta["digest"]:
                raise ApplyError(2, f"{label}: its files do not match the version the bundle names")
            if not all(have(c) or c in sizes for _p, c in mine):
                result.refused[repo] = (f"needs v{meta['from']} here first — on the connected side: "
                                        f"mmw bundle -n {header.network} --repo '{label}' --from none")
                continue
            if dry_run:
                recorded[repo] = -1
                continue
            recorded[repo] = db.add_version(
                repo, label, meta["to"], meta["digest"], header.created, header.created_label,
                meta["bases"], [(p, c, _size(store, got, c, sizes)) for p, c in mine])

        network = {p: c for p, c, r in rows if r is None}
        missing = [p for p, c in network.items() if not (have(c) or c in sizes)]
        if missing:
            raise ApplyError(3, f"{len(missing)} network file(s) are not here (first: {missing[0]}) — "
                                "bring a full bundle: mmw bundle --from none")
        before = db.network_files()
        result.network_changed = any(
            (before.get(p) != network.get(p)) for p in set(before) | set(network) if p.startswith("nginx/"))

        if dry_run:
            for repo in recorded:
                result.lines.append(f"  {header.repos[repo]['label']}  would go to v{header.repos[repo]['to']}")
            return result

        for rel, checksum in sorted(network.items()):
            if before.get(rel) != checksum or not disk_io.ops.exists(root / rel):
                _place_network_file(store, root, rel, checksum)
        for rel in sorted(set(before) - set(network)):
            if disk_io.ops.remove(root / rel) is not None:
                _prune_empty_parents(str(root), str((root / rel).parent))
        db.set_network_files(network, {c: _size(store, got, c, sizes) for c in network.values()})
        for name, data in sorted(tls.items()):
            _install_tls(str(root), name, io.BytesIO(data))

        forward = set(forward or ())
        for repo, vid in recorded.items():
            meta, state = header.repos[repo], db.repo_state(repo)
            was = state.serving.number if state and state.serving else None
            if state and state.held and not forward_all and repo not in forward:
                result.stored_held.append(repo)
                result.lines.append(f"  {meta['label']}  stored v{meta['to']}, still serving v{was} (held)")
                continue
            switch_repo(db, store, root, repo, vid)
            result.updated.append(repo)
            result.lines.append(f"  {meta['label']}  {f'v{was}' if was else 'full'} → v{meta['to']}")
        for repo, why in result.refused.items():
            result.lines.append(f"  {header.repos[repo]['label']}  refused: {why}")

        render_derived(str(root), say=lambda m: say(f"  {m}"))
        db.record_applied(header.created, header.previous, "full" if header.full else "delta")
        try:
            from mmw.site.ops import apply_retention
        except ImportError:
            apply_retention = None
        if apply_retention is not None and settings.keep_versions is not None:
            apply_retention(db, store, settings.keep_versions)
        if result.network_changed:
            result.actions.append("nginx config changed — recreate nginx on the serving host "
                                  "(docker compose … up -d --force-recreate)")
    return result
