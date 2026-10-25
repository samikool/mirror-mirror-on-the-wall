"""`mmw site apply`: take a bundle (format 2) onto a site.

Nothing the site serves changes until the whole bundle is in: every member
goes into the store first, hashed on the way, and is matched against the
manifest; every file each repository's new version lists must then be in
the store. Only then are versions recorded and repositories switched — a
held one keeps serving what it served, its new version stored for later.
"""
import io
import tarfile
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
from mmw.site.roots import Roots
from mmw.site.switch import resume_switches, switch_in


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
    stopped: list[str] = field(default_factory=list)    # stored and held: a stop came before their switch


def _receive(src: str, store: Store, manifest_path: Path, dry_run: bool, progress=None, stop_event=None,
             on_manifest=None):
    """Stream the members in: (name → (checksum, size), TLS files). Once the
    manifest has arrived, `on_manifest(path)` may return `route(name) ->
    Store`, the store each later member belongs in (its repository's root)."""
    route = None
    got: dict[str, tuple[str, int]] = {}
    links: dict[str, str] = {}
    tls: dict[str, bytes] = {}
    manifest = False
    try:
        for m in open_source(src):
            if stop_event is not None and stop_event.is_set():
                raise ApplyError(5, "stopped before anything was applied")
            if progress:
                progress("receive", name=m.name, bytes=m.size)
            if m.name == MANIFEST_MEMBER:
                disk_io.ops.write_bytes(manifest_path, m.fh.read())
                manifest = True
                if on_manifest is not None:
                    route = on_manifest(manifest_path)
            elif m.name.startswith(TLS_PREFIX):
                tls[m.name[len(TLS_PREFIX):]] = m.fh.read()
            elif m.name.startswith(".mmw/"):
                continue                        # the bundle's own apply.py and the like
            elif m.kind == "link":
                links[m.name] = m.linkname
            elif dry_run:
                got[m.name] = disk_io.ops.hash_fh(m.fh)
            else:
                got[m.name] = (route(m.name) if route else store).add_stream(m.fh)
    except SourceError as e:
        raise ApplyError(e.code, str(e)) from None
    except (tarfile.ReadError, EOFError) as e:              # cut off inside a member
        raise ApplyError(5, f"bundle is truncated ({e}) — copy it again") from None
    for name, target in links.items():
        if target not in got:
            raise ApplyError(2, f"bundle is malformed: {name!r} links to {target!r}, which it lacks")
        got[name] = got[target]
    if not manifest:
        raise ApplyError(2, "bundle is malformed: no .mmw/manifest.jsonl")
    return got, tls


def place_served_file(store: Store, root: Path, rel: str, checksum: str, *, network: bool) -> None:
    """Put one file in the served tree. nginx's text files are written fresh,
    never hardlinked: the root is substituted into its configs, and an
    operator editing one in place must not rewrite the stored copy behind
    every other link. Everything else is a hardlink into the store."""
    if network and rel.startswith("nginx/") and not rel.endswith(".tar"):
        data = disk_io.ops.read_bytes(store.path(checksum))
        if _nginx_root_config(rel):
            data = data.replace(ROOT_PLACEHOLDER.encode(), str(root).encode())
        disk_io.ops.atomic_write(root / rel, data, tmp_dir=store.place_tmp)
    else:
        store.place(checksum, root / rel)


def _size(store: Store, checksum: str, sizes: dict[str, int]) -> int:
    if checksum not in sizes:
        sizes[checksum] = disk_io.ops.stat(store.path(checksum)).st_size
    return sizes[checksum]


def apply_bundle(home: Path, src: str, *, forward: set[str] | None = None, forward_all: bool = False,
                 dry_run: bool = False, say=print, only: set[str] | None = None, progress=None,
                 stop_event=None, place: dict[str, str] | None = None) -> ApplyResult:
    """Apply the bundle at `src`. `only`: the repositories to switch; the
    others in the bundle are stored and held. `progress(event, **data)`:
    receive(name, bytes), check, switch(repo, label), done. `stop_event`:
    before anything changes, a stop raises; once repositories are switching,
    the ones not reached yet are stored and held. `place`: repository →
    root name for repositories new to this site (default: the primary)."""
    settings = load_site(home)
    roots = Roots(settings)
    root = Path(roots.primary.path)
    store = roots.store(roots.primary)
    result = ApplyResult()
    with SiteLock(home), SiteDB(home) as db:
        if not dry_run:
            store.sweep_tmp()
            resume_switches(db, roots)
        manifest_path = Path(home) / "incoming-manifest.jsonl"
        placement: dict[str, object] = {}

        def on_manifest(path):
            from mmw.core.repos import attribute
            head, _rows = iter_manifest_v2(path)
            _rows.close()
            placement.update(_place(db, roots, head.repos or {}, place or {}))
            for r in {placement[k].name: placement[k] for k in placement}.values():
                try:
                    roots.check_mounted(r)
                except FileNotFoundError as e:
                    raise ApplyError(5, str(e)) from None
            bases = {k: m["bases"] for k, m in (head.repos or {}).items()}
            return lambda name: roots.store(placement.get(attribute(name, bases), roots.primary))
        try:
            got, tls = _receive(src, store, manifest_path, dry_run, progress, stop_event,
                                on_manifest=on_manifest)
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
        if not placement:
            placement.update(_place(db, roots, header.repos or {}, place or {}))
        if roots.multi and not header.settings:
            raise ApplyError(5, "bundle again with this mmw — it doesn't say enough to set up nginx per root")

        sizes = {cs: size for cs, size in got.values()}

        def have(cs):
            # Arrived with this bundle (a dry run only hashed it), or in a store.
            return cs in sizes or any(roots.store(r).has(cs) for r in roots.all)
        if progress:
            progress("check")

        # Check everything before writing anything: each repository's new
        # version is complete (or refused alone), and so are the network files.
        planned: dict[str, dict] = {}
        for repo, meta in sorted((header.repos or {}).items()):
            mine = [(p, c) for p, c, r in rows if r == repo]
            label = meta["label"]
            if repo_digests(mine, {repo: meta["bases"]})[repo] != meta["digest"]:
                raise ApplyError(2, f"{label}: its files do not match the version the bundle names")
            here = db.version(repo, meta["to"])
            if here is not None and here.digest != meta["digest"]:
                result.refused[repo] = (
                    f"v{meta['to']} here is not the connected side's v{meta['to']} — its history no "
                    f"longer matches this site. To follow it: mmw site delete '{label}' v{meta['to']} "
                    "(and any later), then apply again")
                continue
            if not all(have(c) for _p, c in mine):
                result.refused[repo] = (f"needs v{meta['from']} here first — on the connected side: "
                                        f"mmw bundle -n {header.network} --repo '{label}' --from none")
                continue
            planned[repo] = {"meta": meta, "rows": mine}
        network = {p: c for p, c, r in rows if r is None}
        missing = [p for p, c in network.items() if not have(c)]
        if missing:
            raise ApplyError(3, f"{len(missing)} network file(s) are not here (first: {missing[0]}) — "
                                f"bring a full bundle: mmw bundle -n {header.network} --from none")
        before = db.network_files()
        result.network_changed = any(
            (before.get(p) != network.get(p)) for p in set(before) | set(network) if p.startswith("nginx/"))

        if dry_run:
            for repo, plan in planned.items():
                result.lines.append(f"  {plan['meta']['label']}  would go to v{plan['meta']['to']}")
            return result

        if stop_event is not None and stop_event.is_set():
            raise ApplyError(5, "stopped before anything changed — what arrived is stored")
        recorded: dict[str, int] = {}
        try:
            for repo, plan in planned.items():
                meta = plan["meta"]
                target = roots.store(placement[repo])
                for _p, c in plan["rows"]:
                    _ensure(roots, target, c)
                recorded[repo] = db.add_version(
                    repo, meta["label"], meta["to"], meta["digest"], header.created, header.created_label,
                    meta["bases"], [(p, c, _size(target, c, sizes)) for p, c in plan["rows"]], commit=False)
                if db.repo_state(repo).root is None and placement[repo].name != roots.primary.name:
                    db.set_root(repo, placement[repo].name, commit=False)
            db.commit()
        except BaseException:
            db.rollback()
            raise

        old = {r.name: roots.network_files(db, r) for r in roots.all}
        db.set_network_files(network, {c: _size(store, c, sizes) for c in network.values()})
        for r in roots.all:
            rroot, rstore = Path(r.path), roots.store(r)
            if r.name != roots.primary.name and not disk_io.ops.is_dir(rroot / ".mmw" / "store"):
                continue
            new = roots.network_files(db, r)
            for rel, checksum in sorted(new.items()):
                if old[r.name].get(rel) != checksum or not disk_io.ops.exists(rroot / rel):
                    _ensure(roots, rstore, checksum)
                    place_served_file(rstore, rroot, rel, checksum, network=True)
            for rel in sorted(set(old[r.name]) - set(new)):
                if disk_io.ops.remove(rroot / rel) is not None:
                    _prune_empty_parents(str(rroot), str((rroot / rel).parent))
        if header.settings:
            db.set_meta("settings", header.settings)          # for nginx per root, now or later
        if roots.multi:
            result.network_changed = write_root_nginx(db, roots) or result.network_changed
        for name, data in sorted(tls.items()):
            _install_tls(str(root), name, io.BytesIO(data))

        forward = set(forward or ())
        for repo, vid in recorded.items():
            meta, state = header.repos[repo], db.repo_state(repo)
            was = state.serving.number if state and state.serving else None
            if was is not None and meta["to"] <= was:
                # An older or already-applied bundle never moves what is served.
                result.lines.append(f"  {meta['label']}  already at v{was}" if meta["to"] == was else
                                    f"  {meta['label']}  keeps serving v{was} (this bundle brings v{meta['to']}, "
                                    "older — stored)")
                continue
            if stop_event is not None and stop_event.is_set():
                db.set_held(repo, True)
                result.stopped.append(repo)
                result.lines.append(f"  {meta['label']}  not switched (stopped) — stored v{meta['to']}, held")
                continue
            if only is not None and repo not in only:
                db.set_held(repo, True)
                result.stored_held.append(repo)
                result.lines.append(f"  {meta['label']}  stored v{meta['to']}, "
                                    f"{f'still serving v{was}' if was else 'not served'} (held)")
                continue
            if state and state.held and not forward_all and repo not in forward:
                result.stored_held.append(repo)
                result.lines.append(f"  {meta['label']}  stored v{meta['to']}, still serving v{was} (held)")
                continue
            if progress:
                progress("switch", repo=repo, label=meta["label"])
            switch_in(db, roots, repo, vid)
            result.updated.append(repo)
            result.lines.append(f"  {meta['label']}  {f'v{was}' if was else 'full'} → v{meta['to']}")
        for repo, why in result.refused.items():
            result.lines.append(f"  {header.repos[repo]['label']}  refused: {why}")

        for r in roots.all:
            if disk_io.ops.is_dir(r.path):
                render_derived(str(r.path), say=lambda m: say(f"  {m}"))
        db.record_applied(header.created, header.previous, "full" if header.full else "delta")
        from mmw.site.ops import _tidy
        _tidy(db, roots)
        if settings.keep_versions is not None:
            from mmw.site.ops import apply_retention
            apply_retention(db, roots, settings.keep_versions)
        if progress:
            progress("done")
        if result.network_changed:
            result.actions.append("nginx config changed — recreate nginx on the serving host "
                                  "(docker compose … up -d --force-recreate)")
    return result


# ── planning, for the TUI's review ───────────────────────────────────────────

@dataclass(frozen=True)
class PlanRow:
    repo: str
    label: str
    from_number: int | None
    to_number: int
    files_sent: int
    bytes_sent: int
    serving_number: int | None
    held: bool
    unchanged: bool
    root: Path


@dataclass(frozen=True)
class ApplyPlan:
    created: str
    network: str
    rows: list
    network_files: int
    bytes: int


def _manifest_and_sizes(src: str, scratch: Path) -> dict[str, int]:
    """Write the bundle's manifest to `scratch`; the sizes of its members."""
    from mmw.resources.apply import is_safe_path
    path = Path(src)
    sizes: dict[str, int] = {}
    if disk_io.ops.is_dir(path):
        manifest = path / MANIFEST_MEMBER
        if not disk_io.ops.exists(manifest):
            raise ApplyError(2, f"{path} is not a stage directory: it has no {MANIFEST_MEMBER}")
        disk_io.ops.write_bytes(scratch, disk_io.ops.read_bytes(manifest))
        for dirpath, _dirs, files in disk_io.ops.walk(path):
            for fn in files:
                rel = (Path(dirpath) / fn).relative_to(path).as_posix()
                sizes[rel] = disk_io.ops.stat(Path(dirpath) / fn).st_size
        return sizes
    try:
        with tarfile.open(path, mode="r:") as tar:
            found = False
            for m in tar:
                name = m.name[2:] if m.name.startswith("./") else m.name
                if name == MANIFEST_MEMBER and m.isreg():
                    disk_io.ops.write_bytes(scratch, tar.extractfile(m).read())
                    found = True
                elif m.isreg() and is_safe_path(name):
                    sizes[name] = m.size
    except (tarfile.ReadError, EOFError, OSError) as e:
        raise ApplyError(5, f"{path} is not a bundle, or is truncated: {e}") from None
    if not found:
        raise ApplyError(2, f"{path} has no {MANIFEST_MEMBER} — it is not an mmw bundle")
    return sizes


def plan_apply(home: Path, src: str) -> ApplyPlan:
    """What applying `src` would do, from its manifest and member sizes —
    nothing is stored or checked beyond that."""
    from mmw.core.repos import attribute
    if src == "-":
        raise ValueError("a bundle on stdin can't be planned — it can only be read once")
    settings = load_site(home)
    scratch = Path(home) / "plan-manifest.jsonl"
    try:
        sizes = _manifest_and_sizes(src, scratch)
        header, rows = iter_manifest_v2(scratch)
        rows = list(rows)
    except (ValueError, KeyError, TypeError) as e:
        raise ApplyError(2, f"its manifest can't be read: {e}") from None
    finally:
        disk_io.ops.unlink(scratch, missing_ok=True)
    if (header.format or 1) < 2:
        raise ApplyError(5, 'this is an apply.py bundle (far_side = "apply") — apply it with apply.py, '
                            'or set far_side = "mmw" on the connected side and bundle again')
    repos = header.repos or {}
    bases = {r: m["bases"] for r, m in repos.items()}
    sent: dict[str, list[int]] = {r: [0, 0] for r in repos}
    for name, size in sizes.items():
        repo = attribute(name, bases)
        if repo in sent:
            sent[repo][0] += 1
            sent[repo][1] += size
    out = []
    with SiteDB(home) as db:
        for repo, meta in sorted(repos.items(), key=lambda kv: kv[1]["label"].lower()):
            st = db.repo_state(repo)
            serving = st.serving.number if st and st.serving else None
            out.append(PlanRow(repo, meta["label"], meta["from"], meta["to"], sent[repo][0], sent[repo][1],
                               serving, bool(st and st.held), serving is not None and meta["to"] <= serving,
                               Path(settings.root)))
    network = sum(1 for _p, _c, r in rows if r is None)
    return ApplyPlan(header.created, header.network, out, network,
                     sum(v for k, v in sizes.items() if not k.startswith(".mmw/")))


# ── several roots ────────────────────────────────────────────────────────────

def _ensure(roots: Roots, target: Store, checksum: str) -> None:
    """Make `checksum` present in `target`, copying it from another root's
    store when that is where it is."""
    if target.has(checksum):
        return
    for r in roots.all:
        other = roots.store(r)
        if other is not target and other.has(checksum):
            with disk_io.ops.open_rb(other.path(checksum)) as fh:
                target.add_stream(fh, expect=checksum)
            return


def _hosts(bases: list[str]) -> set[str]:
    return {b.split("/", 1)[0] for b in bases}


def _place(db, roots: Roots, repos: dict, place: dict[str, str]) -> dict:
    """Repository → its root: where it already is, else `place`, else the
    primary. Two repositories sharing a hostname must share a root."""
    out = {}
    labels, bases = {}, {}
    for st in db.repo_states():
        out[st.repo] = roots.of(st)
        labels[st.repo], bases[st.repo] = st.label, st.bases
    for repo, meta in repos.items():
        labels[repo], bases[repo] = meta["label"], meta["bases"]
        if repo not in out:
            out[repo] = roots.named(place.get(repo)) if place.get(repo) else roots.primary
    check_shared_hosts(out, labels, bases)
    return {k: v for k, v in out.items() if k in repos}


def check_shared_hosts(placement: dict, labels: dict, bases: dict) -> None:
    by_host: dict[str, tuple[str, str]] = {}
    for repo, root in placement.items():
        for host in _hosts(bases.get(repo, [])):
            seen = by_host.setdefault(host, (repo, root.name))
            if seen[1] != root.name:
                raise ValueError(f"{labels[seen[0]]} and {labels[repo]} both serve {host} — nginx routes a "
                                 "hostname to one place, so they must share a root (move them together)")


def write_root_nginx(db, roots: Roots) -> bool:
    """Generate every root's nginx for the repositories placed there; whether
    any of it changed."""
    import hashlib
    import json
    from mmw.site.nginx import site_nginx_files
    settings_json = db.get_meta("settings")
    changed = False
    for r in roots.all:
        rroot = Path(r.path)
        if not disk_io.ops.is_dir(rroot / ".mmw" / "store"):
            continue
        files = site_nginx_files(settings_json, roots.placed_in(db, r), r, primary=r.name == roots.primary.name)
        before = roots.generated(db, r)
        after = {rel: hashlib.sha256(data).hexdigest() for rel, data in files.items()}
        for rel, data in files.items():
            if before.get(rel) != after[rel] or not disk_io.ops.exists(rroot / rel):
                disk_io.ops.atomic_write(rroot / rel, data, tmp_dir=roots.store(r).place_tmp)
        stale = set(before) - set(after)
        if r.name == roots.primary.name:
            # The bundle's own nginx text, from when this was a one-root site.
            stale |= {p for p in db.network_files() if p.startswith("nginx/") and not p.endswith(".tar")
                      and p not in after and disk_io.ops.exists(rroot / p)}
        for rel in stale:
            disk_io.ops.remove(rroot / rel)
        if before != after or stale:
            changed = True
            db.set_meta(f"nginx:{r.name}", json.dumps(after, sort_keys=True))
    return changed
