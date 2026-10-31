"""A site serving another mmw instance's network — folder repositories kept
current on this side (by hand, cron or CI) — without that instance running
and without writing to it.

The instance's pool is opened SQLite-immutable (no locks, no -shm: safe on a
share) and refused while its `-wal` holds anything (in use, or not closed
cleanly). Its network is walked into memory and handed to `RootApply` as a
format-2 manifest plus members — the same checks, versions and switching as
a bundle — with the site's own network files left exactly as they were. Its
hostnames get `nginx/local-{network}.conf` (nginx loads every conf.d file),
its keys go under `keys/` for `/mmw-keys/`.
"""
import hashlib
import io
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from mmw.core import disk_io

_META = "local:"


@dataclass
class LocalResult:
    network: str
    path: str
    error: str | None = None
    updated: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)


def entries(home: Path) -> dict[str, str]:
    """network → instance directory."""
    from mmw.site.db import SiteDB
    with SiteDB(home) as db:
        return {k[len(_META):]: v for k, v in db.metas(_META).items() if v}


def add(home: Path, path: Path, network: str) -> None:
    from mmw.core import config
    from mmw.site.db import SiteDB
    path = Path(path).resolve()
    try:
        cfg = config.load(path)
    except (FileNotFoundError, ValueError) as e:
        raise ValueError(f"no mmw instance at {path} ({e})") from None
    if not any(n.name == network for n in cfg.networks):
        raise ValueError(f"the instance at {path} has no network {network!r} — it has: "
                         + ", ".join(n.name for n in cfg.networks))
    with SiteDB(home) as db:
        db.set_meta(f"{_META}{network}", str(path))


def remove(home: Path, network: str) -> None:
    from mmw.site.db import SiteDB
    with SiteDB(home) as db:
        if not db.get_meta(f"{_META}{network}"):
            raise ValueError(f"no local instance for network {network!r}")
        db.set_meta(f"{_META}{network}", "")


class MemorySink:
    """A walk's files, in memory: rel → (checksum, size, pool path | None, bytes | None)."""

    def __init__(self):
        self.files: dict[str, tuple] = {}
        self.errors: list[str] = []

    def link(self, src: Path, rel: str, size: int, checksum: str, kind: str) -> str:
        self.files[rel] = (checksum, size, Path(src), None)
        return "linked"

    link_pooled = link

    def write(self, rel: str, data: bytes) -> None:
        if rel.startswith("nginx/"):
            return                       # the site writes its own (nginx/local-{network}.conf)
        self.files[rel] = (hashlib.sha256(data).hexdigest(), len(data), None, data)

    def finalize_group(self, base, keep, live_checksums, exclude) -> tuple[int, int]:
        return 0, 0

    def drop_if_empty(self, base: str) -> None:
        pass

    def prune_stale_bases(self, live_bases) -> tuple[int, int]:
        return 0, 0

    def close(self) -> None:
        pass


def _wal_busy(mirror_dir: Path) -> bool:
    from mmw.core.pool import database_path
    db = database_path(mirror_dir)
    wal = db.with_name(db.name + "-wal")
    return disk_io.ops.exists(wal) and disk_io.ops.stat(wal).st_size > 0


def update(home: Path, progress=None) -> list[LocalResult]:
    """Bring every local instance's network in. One result per instance."""
    return [update_one(home, network, path, progress=progress)
            for network, path in sorted(entries(home).items())]


def update_one(home: Path, network: str, path: str, progress=None) -> LocalResult:
    from mmw.core import config
    from mmw.core.pool import Pool, immutable_reads
    from mmw.core.publisher import walk_network
    from mmw.core.publisher.results import NetworkPublishResult
    result = LocalResult(network, path)
    try:
        cfg = config.load(Path(path))
    except (FileNotFoundError, ValueError) as e:
        result.error = f"the instance at {path} cannot be read: {e}"
        return result
    net = next((n for n in cfg.networks if n.name == network), None)
    if net is None:
        result.error = f"the instance at {path} no longer has network {network!r}"
        return result
    mirror_dir = Path(cfg.settings.mirror_dir)
    if not mirror_dir.is_absolute():
        mirror_dir = Path(path) / mirror_dir
    if _wal_busy(mirror_dir):
        result.error = (f"the instance at {path} is busy or was not closed cleanly (its pool's -wal is "
                        "not empty) — try again once its sync has finished")
        return result
    sink, walked = MemorySink(), NetworkPublishResult(network_name=network, dry_run=False)
    with immutable_reads(mirror_dir), Pool(mirror_dir, read_only=True) as pool:
        mirrors = walk_network(net, cfg, mirror_dir, sink, pool, walked, nginx_root=None, prune=False)
    problems = sink.errors + [e for m in walked.mirrors for e in m.errors]
    if problems:
        result.error = "; ".join(problems[:3])
        return result
    repo_bases: dict[str, list[str]] = {}
    for base, repo in walked.base_repos.items():
        repo_bases.setdefault(repo, []).append(base)
    try:
        result.updated, result.lines = _apply(home, cfg, net, mirrors, sink.files, repo_bases)
    except Exception as e:  # noqa: BLE001 — reported per instance
        result.error = str(e)
    return result


def _apply(home: Path, cfg, net, mirrors, files: dict, repo_bases: dict[str, list[str]]):
    from mmw.core.publisher.manifest import ManifestHeader, write_manifest
    from mmw.core.repos import attribute
    from mmw.core.snapshots import repo_digests
    from mmw.site.apply import RootApply, _Member
    from mmw.site.db import SiteDB
    repo_rows = {rel: attribute(rel, repo_bases) for rel in files}
    digests = repo_digests([(rel, files[rel][0]) for rel, repo in repo_rows.items() if repo],
                           repo_bases)
    labels = {s.identity: s.name for s in cfg.sources}
    repos = {}
    with SiteDB(home) as db:
        for repo, digest in digests.items():
            versions = db.versions(repo)
            same = next((v for v in versions if v.digest == digest), None)
            state = db.repo_state(repo)
            repos[repo] = {"label": labels.get(repo, repo),
                           "from": state.serving.number if state and state.serving else None,
                           "to": same.number if same else (versions[0].number + 1 if versions else 1),
                           "digest": digest, "bases": sorted(repo_bases[repo])}
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    rows = sorted((rel, files[rel][0], repo) for rel, repo in repo_rows.items() if repo)
    ra = RootApply(home, say=lambda *_a: None, network_files=False)
    with ra:
        missing = {}
        for rel, cs, _repo in rows:
            if not ra.store.has(cs) and cs not in {files[r][0] for r in missing}:
                missing[rel] = files[rel]
        header = ManifestHeader(network=net.name, created=stamp, previous=None, full=True,
                                file_count=len(missing), format=2, repos=repos, created_label="local")
        manifest = Path(home) / "local-manifest.jsonl"
        write_manifest(manifest, header, rows)
        try:
            ra.begin(disk_io.ops.read_bytes(manifest))
        finally:
            disk_io.ops.unlink(manifest, missing_ok=True)
        for rel, (cs, size, src, data) in missing.items():
            fh = io.BytesIO(data) if data is not None else disk_io.ops.open_rb(src)
            try:
                ra.take(_Member(rel, "file", fh, None, size))
            finally:
                fh.close()
        applied = ra.finish()
        _serve_extras(ra, net, mirrors, files, set(repos))
    return applied.updated, applied.lines


def _serve_extras(ra, net, mirrors, files: dict, repos: set[str]) -> None:
    """The instance's hostnames in nginx, its keys under keys/."""
    from mmw.site.nginx import site_nginx_files
    root = Path(ra.root)
    settings = json.dumps({"network": net.model_dump(mode="json"),
                           "mirrors": [m.legacy_dict() for m in mirrors]})
    built = site_nginx_files(settings, repos, ra.roots.primary, primary=False)
    conf = next((data for rel, data in built.items() if rel.startswith("nginx/") and rel.endswith(".conf")), None)
    if conf is not None:
        disk_io.ops.mkdir(root / "nginx", parents=True, exist_ok=True)
        disk_io.ops.atomic_write(root / "nginx" / f"local-{net.name}.conf", conf,
                                 tmp_dir=ra.store.place_tmp)
    for rel, (_cs, _size, src, data) in files.items():
        if rel.startswith("keys/"):
            body = data if data is not None else disk_io.ops.read_bytes(src)
            disk_io.ops.mkdir((root / rel).parent, parents=True, exist_ok=True)
            disk_io.ops.atomic_write(root / rel, body, tmp_dir=ra.store.place_tmp)
