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
from mmw.site.store import ChecksumMismatch, Store
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
        self.expected: dict[str, str] | None = None     # path → checksum, shared by a set's roots

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
            want = self._expected().get(m.name)
            try:
                self.got[m.name] = self.store.add_stream(m.fh, expect=want)
            except ChecksumMismatch:
                # Damaged on the way: refused here, before anything changed
                # anywhere, and nothing of it kept.
                raise ApplyError(2, f"{m.name} does not match the manifest — the bundle is damaged; copy it again") from None

    def _expected(self) -> dict[str, str]:
        """The manifest's checksum for each path (what the store may already
        have). A set hands every root its one shared map (`self.expected`)."""
        if self.expected is not None:
            return self.expected
        if getattr(self, "_want", None) is None and self._manifest:
            _header, rows = iter_manifest_v2(self.manifest_path)
            self._want = {p: c for p, c, _r in rows}
        return getattr(self, "_want", None) or {}

    def take_blob(self, name: str, checksum: str, size: int, from_root) -> None:
        """A member another root of the set received: its blob copied from there."""
        if not self.dry_run and not self.store.has(checksum) and from_root is not None:
            try:
                with from_root.export_blob(checksum) as fh:
                    self.store.add_stream(fh, expect=checksum)
            except ChecksumMismatch:
                raise ApplyError(2, f"{name} is damaged in another root's store — mmw site verify --repair") from None
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
        """Check everything, then (unless a dry run) record and switch."""
        result = self.check()
        return result if self.dry_run else self.commit()

    def check(self) -> "ApplyResult":
        """Everything checked against the manifest, nothing changed: each
        repository's new version is complete (or refused alone), and so are
        the network files. A set checks every root before any commits."""
        self._want = None                          # the arrivals' map isn't needed past here
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
        self._checked = (header, rows, result, planned, network, sizes)
        return result

    def commit(self, *, others_changed: bool = False) -> "ApplyResult":
        """Record the checked versions, place the network files, switch each
        hostname whole. A stop before anything changed anywhere raises; once
        another root of the set has changed (`others_changed`), a stop
        stores and holds instead."""
        header, rows, result, planned, network, sizes = self._checked
        db, roots, store, root, progress, stop_event = (self.db, self.roots, self.store, self.root,
                                                        self.progress, self.stop_event)
        say = self.say
        if stop_event is not None and stop_event.is_set() and not others_changed:
            raise ApplyError(5, "stopped before anything changed — what arrived is stored")
        # Nothing applied here before: nginx was never started for this root.
        self.first_time = not db.applied()
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


def plan_apply(target, src: str) -> ApplyPlan:
    """What applying `src` would do, from its manifest and member sizes —
    nothing is stored or checked beyond that. `target`: one root's home, or
    a mirror set (each repository as the root holding it sees it)."""
    import tempfile
    from mmw.core.repos import attribute
    if src == "-":
        raise ValueError("a bundle on stdin can't be planned — it can only be read once")
    single = isinstance(target, (str, Path))
    homes = [Path(target)] if single else []
    roots_here = None if single else list(target.roots)
    tmpdir = tempfile.TemporaryDirectory()
    scratch = Path(tmpdir.name) / "plan-manifest.jsonl"
    try:
        sizes = _manifest_and_sizes(src, scratch)
        header, rows = iter_manifest_v2(scratch)
        rows = list(rows)
    except (ValueError, KeyError, TypeError) as e:
        raise ApplyError(2, f"its manifest can't be read: {e}") from None
    finally:
        tmpdir.cleanup()
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
    found: dict[str, tuple] = {}
    if roots_here is None:
        for home in homes:
            with SiteDB(home) as db:
                for repo in repos:
                    st = db.repo_state(repo)
                    if st is not None and repo not in found:
                        found[repo] = (st, Path(load_site(home).root))
        first_root = Path(load_site(homes[0]).root) if homes else None
    else:
        for r in roots_here:
            for st in r.status():
                if st.repo in repos and st.repo not in found:
                    found[st.repo] = (st, Path(r.info.path))
        first_root = Path(roots_here[0].info.path) if roots_here else None
    for repo, meta in sorted(repos.items(), key=lambda kv: kv[1]["label"].lower()):
        st, root = found.get(repo, (None, first_root))
        serving = st.serving.number if st and st.serving else None
        out.append(PlanRow(repo, meta["label"], meta["from"], meta["to"], sent[repo][0], sent[repo][1],
                           serving, bool(st and st.held), serving is not None and meta["to"] <= serving, root))
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


# ── a set of roots: one pass, routed by hostname ─────────────────────────────

def _host(meta: dict, repo: str) -> str:
    return (meta.get("bases") or [repo])[0].split("/", 1)[0]


@dataclass
class SetApplyPlan:
    network: str
    created: str
    hosts: dict[str, list[str]]                 # hostname → repository keys
    labels: dict[str, str]                      # repository key → label
    owner: dict[str, str | None]                # hostname → root name (None: new to the mirror)
    sizes: dict[str, int]                       # hostname → bytes of its files
    network_bytes: int
    missing: list[str] = field(default_factory=list)
    sized: bool = True                          # sizes known (a tar on stdin, without "s": not)
    repo_sizes: dict[str, int] = field(default_factory=dict)
    # hostname (None: the network's own files) → checksum → size: what any
    # placement needs is worked out from this, never by reading the bundle again.
    host_checksums: dict = field(default_factory=dict)
    locations: dict[str, str] = field(default_factory=dict)     # root name → location, for the roots here


@dataclass
class SetApplyResult:
    per_root: dict[str, "ApplyResult"] = field(default_factory=dict)
    held_back: dict[str, str] = field(default_factory=dict)      # hostname → why
    lines: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    created: str = ""
    made: list[str] = field(default_factory=list)                # roots this apply created
    paths: dict[str, str] = field(default_factory=dict)          # root name → its path here

    def merged(self) -> "ApplyResult":
        """One `ApplyResult` for the whole set (what the TUI shows)."""
        out = ApplyResult(created=self.created, lines=list(self.lines), actions=list(self.actions))
        for r in self.per_root.values():
            out.updated += r.updated
            out.stored_held += r.stored_held
            out.refused.update(r.refused)
            out.stopped += r.stopped
            out.network_changed = out.network_changed or r.network_changed
        return out


def nginx_steps(result: "SetApplyResult") -> list[tuple[str, list[str]]]:
    """What a person runs on the serving host after an apply, per root: a new
    root's first start (load the image, start it), a changed one's recreate."""
    out = []
    for name, r in result.per_root.items():
        path = result.paths.get(name)
        if path is None:
            continue
        nginx = Path(path) / "nginx"
        if name in result.made:
            images = sorted(p.name for p in disk_io.ops.glob(nginx, "*.tar"))
            cmds = [f"docker load -i {nginx}/{images[0]}"] if images else []
            out.append((name, cmds + [f"cd {nginx} && docker compose up -d"]))
        elif r.network_changed:
            out.append((name, [f"cd {nginx} && docker compose up -d --force-recreate"]))
    return out


class _Member:
    def __init__(self, name, kind, fh, linkname, size):
        self.name, self.kind, self.fh, self.linkname, self.size = name, kind, fh, linkname, size


class _Spool:
    """Content more than one root of a set needs, held here for the length of
    one apply; it answers `export_blob` like a root does."""

    def __init__(self, path: Path):
        self.path = path

    def put(self, fh, checksum: str, name: str) -> None:
        import hashlib
        disk_io.ops.mkdir(self.path, parents=True, exist_ok=True)
        dest = self.path / checksum
        if disk_io.ops.exists(dest):
            while fh.read(1 << 20):
                pass
            return
        h, tmp = hashlib.sha256(), self.path / (checksum + ".part")
        with disk_io.ops.open_file(tmp, "wb") as out:
            while chunk := fh.read(1 << 20):
                h.update(chunk)
                out.write(chunk)
        if h.hexdigest() != checksum:
            disk_io.ops.unlink(tmp, missing_ok=True)
            raise ApplyError(2, f"{name} does not match the manifest — the bundle is damaged; copy it again")
        disk_io.ops.replace(tmp, dest)

    def has(self, checksum: str) -> bool:
        return disk_io.ops.exists(self.path / checksum)

    def open(self, checksum: str):
        return disk_io.ops.open_rb(self.path / checksum)

    def export_blob(self, checksum: str):
        return self.open(checksum)


def _read_manifest_member(gen, scratch: Path):
    first = next(gen, None)
    if first is None or first.name != MANIFEST_MEMBER:
        raise ApplyError(2, "bundle is malformed: its first member isn't .mmw/manifest.jsonl")
    data = first.fh.read()
    disk_io.ops.write_bytes(scratch, data)
    return data


def _plan(mset, header, rows, src: str, scratch_dir: Path) -> tuple["SetApplyPlan", dict]:
    from mmw.site.home import load_site
    repos = header.repos or {}
    hosts: dict[str, list[str]] = {}
    for repo, meta in sorted(repos.items()):
        hosts.setdefault(_host(meta, repo), []).append(repo)
    host_of = {repo: _host(meta, repo) for repo, meta in repos.items()}
    sizes = {h: 0 for h in hosts}
    network_bytes, sized = 0, True
    if any(s is None for _p, _c, _r, s in rows):
        if src == "-":
            sized = False
        else:
            known = _manifest_and_sizes(src, scratch_dir / "sizes-manifest.jsonl")
            rows = [(p, c, r, known.get(p, 0)) for p, c, r, _s in rows]
    repo_sizes = {r: 0 for r in repos}
    host_checksums: dict = {}
    for _p, c, r, s in rows:
        if r is None:
            network_bytes += s or 0
        elif r in host_of:
            sizes[host_of[r]] += s or 0
            repo_sizes[r] += s or 0
        host_checksums.setdefault(host_of.get(r) if r is not None else None, {})[c] = s or 0
    owner: dict[str, str | None] = {}
    named: dict[str, str] = {}
    # The newest map last, so it wins over a root's stale one.
    for root in sorted(mset.roots if mset else [], key=lambda r: r.hosts_at()):
        named.update(root.hosts_map())
    for h in hosts:
        held = mset.owner(h) if mset else None
        owner[h] = held.info.name if held else named.get(h)
    plan = SetApplyPlan(header.network, header.created, hosts, {k: m["label"] for k, m in repos.items()},
                        owner, sizes, network_bytes, list(mset.missing) if mset else [], sized,
                        repo_sizes, host_checksums,
                        {r.info.name: r.info.location for r in (mset.roots if mset else [])})
    return plan, {"rows": rows, "host_of": host_of}


def placement_needs(mset, plan: SetApplyPlan, targets: dict[str, str],
                    cache: dict | None = None) -> dict[str, tuple[int, int]]:
    """For a placement (hostname → a root's name, or a location for a new
    one): each root's (bytes it needs, bytes free). What a root's store
    already holds doesn't count; the network's own files count in every
    root, including roots none of the hostnames go to. `cache` (kept by
    the caller across calls) holds each root's store hits and free space, so
    a screen recomputing on every keypress checks each store once."""
    cache = {} if cache is None else cache
    every = {c for by_cs in plan.host_checksums.values() for c in by_cs}
    from mmw.site import ssh
    from mmw.site.mset import root_at
    present = {r.info.name: r for r in (mset.roots if mset else [])}
    by_root: dict[str, list[str]] = {name: [] for name in present}
    for h, t in targets.items():
        by_root.setdefault(t, []).append(h)
    out = {}
    for key, hs in by_root.items():
        if key not in cache:
            root = present.get(key)
            if root is None:
                root = root_at(key)
            if root is not None:
                cache[key] = (root.has(every), root.free_bytes())
            elif ssh.parse_location(key)[0] == "ssh":
                from mmw.site.sshroot import free_at
                cache[key] = (set(), free_at(key))
            else:
                parent = Path(key).expanduser()
                while not disk_io.ops.exists(parent):
                    parent = parent.parent
                cache[key] = (set(), disk_io.ops.disk_usage(parent).free)
        have, free = cache[key]
        need_cs = dict(plan.host_checksums.get(None, {}))
        for h in hs:
            need_cs.update(plan.host_checksums.get(h, {}))
        out[key] = (sum(size for c, size in need_cs.items() if c not in have), free)
    return out


def shared_filesystem_short(needs: dict[str, tuple[int, int]], locations: dict[str, str],
                            free_of=None) -> list[str]:
    """Roots on one filesystem draw on one free space: the ones whose needs
    together don't fit (each alone may). Local roots only."""
    from mmw.site import ssh
    groups: dict[int, list[str]] = {}
    for key, loc in locations.items():
        if key not in needs or ssh.parse_location(loc)[0] == "ssh":
            continue
        parent = Path(loc).expanduser()
        while not disk_io.ops.exists(parent):
            parent = parent.parent
        groups.setdefault(disk_io.ops.stat(parent).st_dev, []).append(key)
    for keys in groups.values():
        if len(keys) < 2:
            continue
        free = free_of(keys[0]) if free_of else needs[keys[0]][1]
        if sum(needs[k][0] for k in keys) > free:
            return sorted(keys)
    return []


def plan_set_apply(mset, src: str) -> SetApplyPlan:
    """What applying `src` to `mset` (None: a first apply) would do — read
    from the bundle's manifest alone (its first member)."""
    import tempfile
    from mmw.core.publisher.manifest import iter_manifest_sized
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / "manifest.jsonl"
        gen = members_of(src)
        try:
            _read_manifest_member(gen, scratch)
        finally:
            gen.close()
        header, rows = iter_manifest_sized(scratch)
        plan, _ = _plan(mset, header, list(rows), src, Path(tmp))
    return plan


def _filtered_manifest(header, rows, repos: set[str]) -> bytes:
    """This root's part of the manifest: its repositories and every
    network-level file. `file_count` is checked for the whole set instead."""
    from dataclasses import asdict, replace
    head = replace(header, repos={k: v for k, v in (header.repos or {}).items() if k in repos},
                   file_count=None)
    fields = asdict(head)
    for optional in ("format", "repos", "settings"):
        if fields[optional] is None:
            del fields[optional]
    out = [json.dumps(fields, sort_keys=True)]
    for p, c, r, _s in rows:
        if r is None or r in repos:
            out.append(json.dumps({"p": p, "c": c, **({"r": r} if r is not None else {})}))
    return ("\n".join(out) + "\n").encode()


def _root_name(location: str, taken: set[str]) -> str:
    base = Path(location.rstrip("/")).name or "root"
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    return name


def apply_to_set(mset, src: str, *, place: dict[str, str] | None = None, dry_run: bool = False,
                 forward: set[str] | None = None, forward_all: bool = False, only: set[str] | None = None,
                 progress=None, stop_event=None, say=print) -> SetApplyResult:
    """Apply the bundle at `src` to a set of roots (`mset`; None for a first
    apply) in one pass. Each hostname goes to the root that holds it; a new
    one to `place[host]` or `place["*"]` (a root location). A hostname whose
    root isn't here is held back; the rest apply. Each root's space is
    checked before anything is written."""
    import tempfile
    from contextlib import ExitStack

    from mmw.core.format import fmt_size
    from mmw.core.publisher.manifest import iter_manifest_sized
    from mmw.site import recents
    from mmw.site import ssh
    from mmw.site.home import init_root, new_set_id
    from mmw.site.mset import root_at
    place = dict(place or {})
    result = SetApplyResult()
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        scratch = Path(tmp) / "manifest.jsonl"
        gen = members_of(src)
        stack.callback(gen.close)
        with _truncation():
            _read_manifest_member(gen, scratch)
        header, rows = iter_manifest_sized(scratch)
        rows = list(rows)
        if (header.format or 1) < 2:
            raise ApplyError(5, 'this is an apply.py bundle (far_side = "apply") — apply it with apply.py, '
                                'or set far_side = "mmw" on the connected side and bundle again')
        if mset is not None and header.network != mset.network:
            raise ApplyError(5, f"this bundle is for network {header.network!r} — this mirror holds "
                                f"{mset.network!r}; nothing was applied")
        plan, extra = _plan(mset, header, rows, src, Path(tmp))
        result.created = header.created
        rows, host_of = extra["rows"], extra["host_of"]

        # Where each hostname goes.
        present = {r.info.name: r for r in (mset.roots if mset else [])}
        targets: dict[str, object] = {}            # hostname → LocalRoot, or a location to make one at
        unplaced = []
        for h in plan.hosts:
            name = plan.owner[h]
            if name in present:
                targets[h] = present[name]
            elif name is not None:
                result.held_back[h] = (f"its root {name!r} isn't here — mount it, then: mmw site open PATH "
                                       "(the rest applied)")
            elif h in place or "*" in place:
                targets[h] = place.get(h, place.get("*"))
            else:
                unplaced.append(h)
        if unplaced:
            raise ApplyError(1, f"new hostname(s) with no root yet: {', '.join(sorted(unplaced))} — "
                                "choose one: --root HOST=PATH (or --root '*'=PATH)")
        locations = {loc for loc in targets.values() if isinstance(loc, str)}
        found = {loc: root_at(loc) for loc in locations}
        set_id = mset.set_id if mset else next(
            (r.info.set_id for r in found.values() if r is not None), None) or new_set_id()
        for loc, r in list(found.items()):
            if r is not None and r.info.set_id is None:
                raise ApplyError(5, f"{loc} was made before mirrors described themselves (a v1 site) — "
                                    "apply to a new folder instead")
            if r is not None and r.info.network != header.network:
                raise ApplyError(5, f"{loc} holds the mirror of network {r.info.network!r}, not "
                                    f"{header.network!r} — pick another root")
            if r is not None and r.info.set_id != set_id:
                raise ApplyError(5, f"{loc} already holds the mirror of network {r.info.network!r} — pick another root")
            if r is not None and r.info.name in present:
                targets.update({h: present[r.info.name] for h, t in targets.items() if t == loc})

        # Space, per root, before anything is written.
        placed = {h: (t if isinstance(t, str) else t.info.name) for h, t in targets.items()}
        needs = placement_needs(mset, plan, placed) if plan.sized else {}
        if not dry_run:
            for key, (need, free) in needs.items():
                if need > free:
                    where = f"{key!r} ({present[key].info.location})" if key in present else key
                    raise ApplyError(5, f"root {where} needs {fmt_size(need)} but has {fmt_size(free)} free — "
                                        "nothing was written; free space or place its hostnames elsewhere")
            locs = {k: (present[k].info.location if k in present else k) for k in needs}
            together = shared_filesystem_short(needs, locs)
            if together:
                total = sum(needs[k][0] for k in together)
                raise ApplyError(5, f"roots {', '.join(together)} share one filesystem: together they need "
                                    f"{fmt_size(total)} but it has {fmt_size(needs[together[0]][1])} free — "
                                    "nothing was written")
        if dry_run:
            for h in plan.hosts:
                t = placed.get(h)
                if t is None:
                    continue
                n = len(plan.hosts[h])
                where = t if t in present else f"new root at {t}"
                result.lines.append(f"  {h}  ({n} repo{'s' if n != 1 else ''}, {fmt_size(plan.sizes[h])}) → {where}")
            for h, why in result.held_back.items():
                result.lines.append(f"  {h}: held back — {why}")
            for key, (need, free) in needs.items():
                result.lines.append(f"  root {key}: needs {fmt_size(need)} of {fmt_size(free)} free "
                                    f"{'✓' if need <= free else '✗'}")
            if not plan.sized:
                result.lines.append("  space not checked: this bundle's sizes aren't known until it is read "
                                    "(a tar on stdin, made before sizes were recorded)")
            return result

        # Never a new root where a root of a mirror was (an unmounted share's
        # empty mount point), nor while a root of this mirror is missing.
        new_locs = sorted({t for t in targets.values() if isinstance(t, str)})
        def norm(loc: str) -> str:
            return loc if ssh.parse_location(loc)[0] == "ssh" else str(Path(loc).expanduser().resolve())
        seen_here = {norm(k) for k in recents.known()}
        for loc in new_locs:
            if found.get(loc) is None and norm(loc) in seen_here:
                raise ApplyError(5, f"{loc} was a root of a mirror on this machine but holds none now — "
                                    "is it mounted? Nothing was written")
        if mset is not None and mset.missing and any(found.get(loc) is None for loc in new_locs):
            raise ApplyError(5, f"root(s) {', '.join(mset.missing)} of this mirror aren't here — mount them "
                                "(mmw site open PATH) before a new hostname starts a new root; nothing was written")

        # New roots, then every root's view of the set.
        taken = set(present) | set(mset.missing if mset else [])
        made: dict = {}
        for loc in sorted({t for t in targets.values() if isinstance(t, str)}):
            r = found.get(loc)
            if r is None:
                name = _root_name(loc, taken)
                taken.add(name)
                if ssh.parse_location(loc)[0] == "ssh":
                    from mmw.site.sshroot import init_remote
                    init_remote(loc, name=name, set_id=set_id, network=header.network)
                else:
                    init_root(Path(loc).expanduser(), name=name, set_id=set_id, network=header.network)
                r = root_at(loc)
            made[loc] = r
        for h, t in list(targets.items()):
            if isinstance(t, str):
                targets[h] = made[t]
        everyone = [*present.values(), *[r for r in made.values() if r.info.name not in present]]
        # Every root here takes part: the network files (and its nginx) reach
        # each one, even one none of this bundle's hostnames go to.
        roots: dict[str, LocalRoot] = {r.info.name: r for r in [*targets.values(), *everyone]}
        # The set's order stays as it was (its first root serves the
        # network's own hostnames); new roots join at the end.
        members = list(dict.fromkeys([*(mset.roots[0].info.members if mset else []),
                                      *(r.info.name for r in everyone)]))
        hosts_map: dict[str, str] = {}
        for r in everyone:
            hosts_map.update(r.hosts_map())
        hosts_map.update({h: t.info.name for h, t in targets.items()})
        for r in everyone:
            r.set_identity(members=members, hosts=hosts_map)
            recents.remember(r.info.location)

        # One pass over the bundle: each member to its hostname's root.
        order = list(roots)
        appliers = {}
        for name in order:
            repos = {r for h, t in targets.items() if t.info.name == name for r in plan.hosts[h]}
            ra = stack.enter_context(roots[name].applier(forward=forward, forward_all=forward_all,
                                                         only=only, progress=progress, stop_event=stop_event,
                                                         say=say))
            ra.via = roots[name]          # the root this applier writes into (its interface)
            ra.begin(_filtered_manifest(header, rows, repos))
            appliers[name] = ra
        cs_all = {p: c for p, c, _r, _s in rows}
        for ra in appliers.values():
            ra.expected = cs_all                   # one map for every root, not one each
        first = appliers[order[0]]
        repo_of = {p: r for p, _c, r, _s in rows}
        cs_of = {p: c for p, c, _r, _s in rows}
        size_of = {c: size or 0 for _p, c, _r, size in rows}
        # Content more than one root needs (the network's own files, and
        # files hostnames in different roots share) is spooled here once and
        # handed to each — never read back from a root still receiving it.
        needs: dict[str, set[str]] = {}
        for _p, c, r, _s in rows:
            if r is None:
                needs.setdefault(c, set()).update(order)
            else:
                h = host_of.get(r)
                if h in targets and h not in result.held_back:
                    needs.setdefault(c, set()).add(targets[h].info.name)
        shared = {c for c, names in needs.items() if len(names) > 1}
        spool = _Spool(Path(tmp) / "spool")
        started: set[str] = set()
        received = 0
        runtime_seen = False
        with _truncation():
            for m in gen:
                if stop_event is not None and stop_event.is_set():
                    raise ApplyError(5, "stopped before anything was applied — what arrived is stored; "
                                        "nothing served changed")
                repo = repo_of.get(m.name)
                h = host_of.get(repo) if repo is not None else None
                if (progress and repo is not None and repo not in started and h not in result.held_back
                        and m.kind != "link"):
                    started.add(repo)                      # named before its first file is counted
                    progress("repo", repo=repo, label=plan.labels.get(repo, repo), host=h)
                if progress:
                    progress("receive", name=m.name, bytes=m.size)
                if m.name.startswith(TLS_PREFIX):
                    data = m.fh.read()
                    for ra in appliers.values():
                        ra.tls[m.name[len(TLS_PREFIX):]] = data
                    continue
                if m.name.startswith(RUNTIME_PREFIX):
                    runtime_seen = True                    # said once, for the whole set
                    continue
                if m.name.startswith(".mmw/"):
                    continue
                received += 1
                c = cs_of.get(m.name)
                if m.kind == "link":
                    if repo is None:
                        for ra in appliers.values():
                            ra.links[m.name] = m.linkname
                    elif h not in result.held_back:
                        appliers[targets[h].info.name].take(m)
                    continue
                if c in shared:
                    spool.put(m.fh, c, m.name)             # held back or not: another root needs it
                if repo is not None and h in result.held_back:
                    continue                               # its root isn't here
                takers = list(appliers.values()) if repo is None else [appliers[targets[h].info.name]]
                for ra in takers:
                    ra.take(_Member(m.name, "file", spool.open(c) if c in shared else m.fh, None, m.size))
        # A member may link to one that went to another root (shared content):
        # its bytes come from the spool.
        for ra in appliers.values():
            for name, target in list(ra.links.items()):
                if target in ra.got or target in ra.links:
                    continue
                c = cs_of.get(target)
                if c is None or not spool.has(c):
                    continue                               # RootApply says it is malformed
                ra.take_blob(name, c, size_of.get(c, 0), spool)
                del ra.links[name]
        if header.file_count is not None and received != header.file_count:
            raise ApplyError(5, f"bundle is truncated: expected {header.file_count} file(s), "
                                f"got {received} — copy it again")
        for name in order:
            appliers[name].check()                 # every root checked before any changes
        changed = False
        result.paths = {name: str(roots[name].info.path) for name in order}
        for name in order:
            r = appliers[name].commit(others_changed=changed)
            changed = changed or bool(r.updated or r.stored_held or r.stopped) or r.network_changed
            if appliers[name].first_time:
                result.made.append(name)               # its nginx is started for the first time
            r.actions = [a for a in r.actions if not a.startswith("nginx config changed — recreate nginx on the serving host")]   # nginx_steps says it
            result.per_root[name] = r
            if len(order) > 1:
                result.lines.append(f"  [{name}]")
            result.lines += r.lines
            result.actions += [a for a in r.actions if a not in result.actions]
        for h, why in result.held_back.items():
            result.lines.append(f"  {h}: held back — {why}")
        if runtime_seen:
            result.actions.append(RUNTIME_NOTE)
    return result
