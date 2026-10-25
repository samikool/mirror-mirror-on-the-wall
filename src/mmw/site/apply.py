"""`mmw site apply`: take a bundle (format 2) onto a site.

Nothing the site serves changes until the whole bundle is in: every member
goes into the store first, hashed on the way, and is matched against the
manifest; every file each repository's new version lists must then be in
the store. Only then are versions recorded and repositories switched — a
held one keeps serving what it served, its new version stored for later.
"""
import io
import json
import tarfile
from contextlib import contextmanager
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


# v1 bundles carried mmw's runtime; mmw comes from its installer now, and
# updating it from bundles waits for its own design (site/runtime.py).
RUNTIME_PREFIX = ".mmw/runtime/"
RUNTIME_NOTE = "this bundle carries an mmw runtime — ignored; install mmw from its installer file"


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


def members_of(src: str):
    """The bundle's members, with a damaged or cut-off source said as an `ApplyError`."""
    try:
        yield from open_source(src)
    except SourceError as e:
        raise ApplyError(e.code, str(e)) from None
    except (tarfile.ReadError, EOFError) as e:              # cut off inside a member
        raise ApplyError(5, f"bundle is truncated ({e}) — copy it again") from None


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


class RootApply:
    """One root's apply, fed member by member: `begin` with its manifest,
    `take` each member (or `take_blob`, a file another root received),
    then `finish` — check everything, record, then switch each hostname's
    repositories together. Holds the root's lock and database while open.

    `only`: the repositories to switch; the others are stored and held.
    `progress(event, **data)`: check, switch(repo, label), done. `stop_event`:
    before anything changes, a stop raises; once switching, the hostnames
    not reached yet are stored and held — a hostname never half-switched."""

    def __init__(self, home: Path, *, dry_run: bool = False, forward: set[str] | None = None,
                 forward_all: bool = False, only: set[str] | None = None, progress=None, stop_event=None,
                 say=print):
        self.home = Path(home)
        self.dry_run, self.forward, self.forward_all, self.only = dry_run, set(forward or ()), forward_all, only
        self.progress, self.stop_event, self.say = progress, stop_event, say
        self.got: dict[str, tuple[str, int]] = {}
        self.links: dict[str, str] = {}
        self.tls: dict[str, bytes] = {}
        self.seen: set[str] = set()
        self.manifest_path = self.home / "incoming-manifest.jsonl"
        self._manifest = False

    def __enter__(self) -> "RootApply":
        self.settings = load_site(self.home)
        self.roots = Roots(self.settings)
        self.root = Path(self.roots.primary.path)
        self.store = self.roots.store(self.roots.primary)
        self._lock = SiteLock(self.home).__enter__()
        try:
            self.db = SiteDB(self.home).__enter__()
        except BaseException:
            self._lock.__exit__(None, None, None)
            raise
        if not self.dry_run:
            self.store.sweep_tmp()
            resume_switches(self.db, self.roots)
        return self

    def __exit__(self, *exc) -> None:
        disk_io.ops.unlink(self.manifest_path, missing_ok=True)
        try:
            self.db.__exit__(*exc)
        finally:
            self._lock.__exit__(*exc)

    def begin(self, manifest: bytes) -> None:
        disk_io.ops.write_bytes(self.manifest_path, manifest)
        self._manifest = True

    def take(self, m) -> None:
        """One member of the bundle (never the manifest: that is `begin`)."""
        if m.name.startswith(TLS_PREFIX):
            self.tls[m.name[len(TLS_PREFIX):]] = m.fh.read()
        elif m.name.startswith(RUNTIME_PREFIX):
            self.seen.add("runtime")
        elif m.name.startswith(".mmw/"):
            return                              # the bundle's own apply.py and the like
        elif m.kind == "link":
            self.links[m.name] = m.linkname
        elif self.dry_run:
            self.got[m.name] = disk_io.ops.hash_fh(m.fh)
        else:
            self.got[m.name] = self.store.add_stream(m.fh)

    def take_blob(self, name: str, checksum: str, size: int, from_store: Store | None) -> None:
        """A member another root of the set received: its blob copied from there."""
        if not self.dry_run and not self.store.has(checksum) and from_store is not None:
            with disk_io.ops.open_rb(from_store.path(checksum)) as fh:
                self.store.add_stream(fh, expect=checksum)
        self.got[name] = (checksum, size)

    def _settle(self) -> None:
        for name, target in self.links.items():
            if target not in self.got:
                raise ApplyError(2, f"bundle is malformed: {name!r} links to {target!r}, which it lacks")
            self.got[name] = self.got[target]
        if not self._manifest:
            raise ApplyError(2, "bundle is malformed: no .mmw/manifest.jsonl")

    def switch_order(self, repos: dict) -> list[list[str]]:
        """The repositories grouped by hostname, each hostname whole, in order."""
        groups: dict[str, list[str]] = {}
        for repo, meta in repos.items():
            host = (meta.get("bases") or [repo])[0].split("/", 1)[0]
            groups.setdefault(host, []).append(repo)
        return list(groups.values())

    def finish(self) -> "ApplyResult":
        self._settle()
        db, roots, store, root, progress, stop_event = (self.db, self.roots, self.store, self.root,
                                                        self.progress, self.stop_event)
        got, say = self.got, self.say
        result = ApplyResult()
        header, rows = iter_manifest_v2(self.manifest_path)
        rows = list(rows)
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
        sizes = {cs: size for cs, size in got.values()}

        def have(cs):
            # Arrived with this bundle (a dry run only hashed it), or in the store.
            return cs in sizes or store.has(cs)
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
                    f"longer matches this mirror. To follow it: mmw site delete '{label}' v{meta['to']} "
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

        if self.dry_run:
            for repo, plan in planned.items():
                result.lines.append(f"  {plan['meta']['label']}  would go to v{plan['meta']['to']}")
            if "runtime" in self.seen:
                result.lines.append(f"  {RUNTIME_NOTE}")
            return result

        if stop_event is not None and stop_event.is_set():
            raise ApplyError(5, "stopped before anything changed — what arrived is stored")
        recorded: dict[str, int] = {}
        try:
            for repo, plan in planned.items():
                meta = plan["meta"]
                recorded[repo] = db.add_version(
                    repo, meta["label"], meta["to"], meta["digest"], header.created, header.created_label,
                    meta["bases"], [(p, c, _size(store, c, sizes)) for p, c in plan["rows"]], commit=False)
            db.commit()
        except BaseException:
            db.rollback()
            raise

        old = roots.network_files(db, roots.primary)
        db.set_network_files(network, {c: _size(store, c, sizes) for c in network.values()})
        new = roots.network_files(db, roots.primary)
        for rel, checksum in sorted(new.items()):
            if old.get(rel) != checksum or not disk_io.ops.exists(root / rel):
                place_served_file(store, root, rel, checksum, network=True)
        for rel in sorted(set(old) - set(new)):
            if disk_io.ops.remove(root / rel) is not None:
                _prune_empty_parents(str(root), str((root / rel).parent))
        if header.settings:
            db.set_meta("settings", header.settings)
        if roots.multi and db.get_meta("settings"):
            result.network_changed = write_root_nginx(db, roots) or result.network_changed
        for name, data in sorted(self.tls.items()):
            _install_tls(str(root), name, io.BytesIO(data))

        forward, stopped = self.forward, False
        for group in self.switch_order({r: header.repos[r] for r in recorded}):
            # A hostname switches whole: Stop is looked at before each one.
            stopped = stopped or (stop_event is not None and stop_event.is_set())
            for repo in group:
                vid = recorded[repo]
                meta, state = header.repos[repo], db.repo_state(repo)
                was = state.serving.number if state and state.serving else None
                if was is not None and meta["to"] <= was:
                    # An older or already-applied bundle never moves what is served.
                    result.lines.append(f"  {meta['label']}  already at v{was}" if meta["to"] == was else
                                        f"  {meta['label']}  keeps serving v{was} (this bundle brings "
                                        f"v{meta['to']}, older — stored)")
                    continue
                if stopped:
                    db.set_held(repo, True)
                    result.stopped.append(repo)
                    result.lines.append(f"  {meta['label']}  not switched (stopped) — stored v{meta['to']}, held")
                    continue
                if self.only is not None and repo not in self.only:
                    db.set_held(repo, True)
                    result.stored_held.append(repo)
                    result.lines.append(f"  {meta['label']}  stored v{meta['to']}, "
                                        f"{f'still serving v{was}' if was else 'not served'} (held)")
                    continue
                if state and state.held and not self.forward_all and repo not in forward:
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

        if disk_io.ops.is_dir(root):
            render_derived(str(root), say=lambda m: say(f"  {m}"))
        db.record_applied(header.created, header.previous, "full" if header.full else "delta")
        if self.settings.keep_versions is not None:
            from mmw.site.ops import apply_retention
            apply_retention(db, roots, self.settings.keep_versions)
        from mmw.site.certs import tls_notes
        result.actions.extend(tls_notes(self.home))
        if progress:
            progress("done")
        if result.network_changed:
            result.actions.append("nginx config changed — recreate nginx on the serving host "
                                  "(docker compose … up -d --force-recreate)")
        if "runtime" in self.seen:
            result.actions.append(RUNTIME_NOTE)
        return result


def apply_bundle(home: Path, src: str, *, forward: set[str] | None = None, forward_all: bool = False,
                 dry_run: bool = False, say=print, only: set[str] | None = None, progress=None,
                 stop_event=None, place: dict[str, str] | None = None) -> "ApplyResult":
    """Apply the bundle at `src` to the one root whose home is `home` (see
    `RootApply`). `progress` also gets receive(name, bytes) per member.
    An mmw runtime in the bundle is ignored, with a note."""
    with RootApply(home, dry_run=dry_run, forward=forward, forward_all=forward_all, only=only,
                   progress=progress, stop_event=stop_event, say=say) as ra:
        with _truncation():
            for m in members_of(src):
                if stop_event is not None and stop_event.is_set():
                    raise ApplyError(5, "stopped before anything was applied")
                if progress:
                    progress("receive", name=m.name, bytes=m.size)
                if m.name == MANIFEST_MEMBER:
                    ra.begin(m.fh.read())
                else:
                    ra.take(m)
        return ra.finish()


@contextmanager
def _truncation():
    """A bundle cut off inside a member (found while reading it) is said plainly."""
    try:
        yield
    except (tarfile.ReadError, EOFError) as e:
        raise ApplyError(5, f"bundle is truncated ({e}) — copy it again") from None

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
        files = site_nginx_files(settings_json, roots.placed_in(db, r), r, primary=roots.serves_extras(r))
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
