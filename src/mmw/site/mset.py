"""A mirror set: every root one connected-side network's bundles were
applied into. Roots know each other by identity (set id, name), never by
path — this machine's recents say where it has seen them."""
from dataclasses import dataclass, field
from pathlib import Path

from mmw.core import disk_io
from mmw.site import recents
from mmw.site.home import RootInfo, hostnames, read_root
from mmw.site.store import Store


class NoSuchRoot(Exception):
    """A root the set names isn't present on this machine."""


class LocalRoot:
    """A root on a filesystem this machine mounts — the local kind of root
    (an `SshRoot` is reached over ssh)."""

    is_local = True              # what touches this machine's own files (adoption, certificates) needs it

    def __init__(self, info: RootInfo):
        self.info = info

    @property
    def home(self) -> Path:
        return self.info.home

    def hostnames(self) -> set[str]:
        if getattr(self, "_hostnames", None) is None:
            self._hostnames = hostnames(self.home)       # read once per set opened
        return self._hostnames

    def free_bytes(self) -> int:
        from mmw.site.space import real_free
        return real_free(self.info.path)

    def store(self) -> Store:
        return Store(self.info.path)

    # ── the root interface (an ssh root answers the same calls remotely) ─────

    def status(self) -> list:
        from mmw.site.ops import status
        return status(self.home)

    def newest(self, repo: str) -> int | None:
        from mmw.site.db import SiteDB
        with SiteDB(self.home) as db:
            v = db.versions(repo)
            return v[0].number if v else None

    def repo_key(self, ref: str) -> str | None:
        """The key of repository `ref` here (key or label), or None — never a
        move's copy that was cut short."""
        from mmw.site.db import SiteDB
        from mmw.site.ops import resolve_repo
        with SiteDB(self.home) as db:
            try:
                key = resolve_repo(db, ref)
            except LookupError:
                return None
            return None if db.get_meta(f"moving_in:{key}") else key

    def labels(self) -> list[str]:
        return [s.label for s in self.status()]

    def arch(self) -> str:
        from mmw.site.update import machine_arch
        return machine_arch()

    def has(self, checksums: set[str]) -> set[str]:
        store = self.store()
        return {c for c in checksums if store.has(c)}

    def hosts_map(self) -> dict[str, str]:
        from mmw.site.home import load_site
        return dict(load_site(self.home).hosts)

    def hosts_at(self) -> str:
        from mmw.site.home import load_site
        return load_site(self.home).hosts_at

    def set_identity(self, *, members: list[str] | None = None, hosts: dict[str, str] | None = None) -> None:
        import time

        from mmw.site.home import load_site, save_settings
        from mmw.site.lock import SiteLock
        with SiteLock(self.home):                       # every write to a root under its lock
            s = load_site(self.home)
            update = {k: v for k, v in (("members", members), ("hosts", hosts)) if v is not None}
            if hosts is not None:
                update["hosts_at"] = f"{time.time():.6f}"
            save_settings(self.home, s.model_copy(update=update))
        self._hostnames = None

    def keep_setting(self) -> "int | str":
        from mmw.site.home import load_site
        return load_site(self.home).keep

    def history(self, key: str):
        from mmw.site import ops
        return ops.history(self.home, key)

    def deleted(self, key: str) -> list[str]:
        from mmw.site import ops
        return ops.deleted_stamps(self.home, key)

    def serve_preview(self, key: str, when: str, track: str | None = None):
        from mmw.site import ops
        return ops.serve_preview(self.home, key, when, track)

    def serve(self, key: str, when: str, track: str | None = None, progress=None):
        from mmw.site import ops
        return ops.serve(self.home, key, when, track, progress=progress)

    def forward(self, key: str | None) -> list[str]:
        from mmw.site import ops
        return ops.forward(self.home, key)

    def hold(self, key: str) -> str:
        from mmw.site import ops
        return ops.hold(self.home, key)

    def delete(self, key: str, when: str, track: str | None = None, force: bool = False) -> int:
        from mmw.site import ops
        return ops.delete_point(self.home, key, when, track, force)

    def space(self):
        from mmw.site import ops
        return ops.space(self.home)

    def verify(self, repair: bool = False):
        from mmw.site import ops
        return ops.repair(self.home) if repair else ops.verify(self.home)

    def keep(self, keep) -> None:
        from mmw.site import ops
        ops.keep_here(self.home, keep)

    def site_settings(self):
        from mmw.site.home import load_site
        return load_site(self.home)

    def set_options(self, *, keep=None, auto_clean: bool | None = None, dated: bool | None = None) -> None:
        from mmw.site import ops
        ops.keep_here(self.home, keep, auto_clean, dated)

    def clean_plan(self):
        from mmw.site import ops
        return ops.clean_plan_here(self.home)

    def clean(self, points=None) -> int:
        from mmw.site import ops
        return ops.clean(self.home, points)

    def keep_costs(self) -> dict:
        from mmw.site import ops
        return ops.keep_costs_here(self.home)

    def set_track_keep(self, repo: str, track: str, keep) -> None:
        from mmw.site import ops
        ops.set_track_keep(self.home, repo, track, keep)

    def dated_urls(self) -> list[tuple[str, str]]:
        from mmw.site.dated import urls
        from mmw.site.db import SiteDB
        from mmw.site.ops import _roots
        with SiteDB(self.home) as db:
            return urls(db, _roots(self.home))

    def break_lock(self) -> bool:
        from mmw.site.lock import break_lock
        return break_lock(self.home)

    def applier(self, **opts):
        from mmw.site.apply import RootApply
        return RootApply(self.home, **opts)

    def export_blob(self, checksum: str):
        return disk_io.ops.open_rb(self.store().path(checksum))

    def export_plan(self, target: str, repos: list[str] | None, mode: str, since: str | None) -> dict:
        from mmw.site.export import root_export_plan
        return root_export_plan(self.home, target, repos=repos, mode=mode, since=since)

    def export_targets(self) -> list[str]:
        from mmw.site.db import SiteDB
        with SiteDB(self.home) as db:
            return db.export_targets()

    def record_export(self, target: str, sent: list, stamp: str) -> None:
        from mmw.site.db import SiteDB
        from mmw.site.lock import SiteLock
        with SiteLock(self.home), SiteDB(self.home) as db:
            db.record_export(target, [tuple(x) for x in sent], stamp)

    def import_blob(self, fh, checksum: str) -> None:
        self.store().add_stream(fh, expect=checksum)

    def move_out_info(self, key: str) -> dict:
        from mmw.site import ops
        return ops.move_out_info(self.home, key)

    def move_in(self, key: str, info: dict) -> bool:
        from mmw.site import ops
        return ops.move_in(self.home, key, info)

    def move_out_finish(self, key: str, to: str, info: dict) -> bool:
        from mmw.site import ops
        return ops.move_out_finish(self.home, key, to, info)

    def set_moving_in(self, key: str, src: str) -> None:
        from mmw.site import ops
        ops.set_moving_in(self.home, key, src)

    def tidy(self, progress=None) -> list[str]:
        from mmw.site import ops
        return ops.tidy_here(self.home, progress=progress)

    def details(self) -> dict:
        from mmw.site import ops
        return ops.root_details(self.home)

    def tls_notes(self) -> list[str]:
        from mmw.site.certs import tls_notes
        return tls_notes(self.home)

    # ── local packages and keys (a root on this machine only) ────────────────

    def layer_details(self, repo: str) -> dict:
        """How the repository's site layer stands: `local` (its signing, when
        it has one) and `layer_hold` (why it is held, if it is)."""
        from mmw.site.db import SiteDB
        with SiteDB(self.home) as db:
            layer, overlay = db.layer(repo), db.repo_overlay(repo)
            local = (f"{layer.signing}" + (f" · key {layer.key}" if layer.key else "")
                     if layer and (overlay or layer.signing != "upstream") else "")
            return {"local": local, "layer_hold": db.get_meta(f"hold:{repo}") or ""}

    # ── adoption: the trees are on this machine, so only a local root adopts ──

    def adopt_tree(self, tree, check, **kw):
        from mmw.site.adopt import adopt_tree
        return adopt_tree(self.home, tree, check, **kw)

    def adopt_apply_tree(self, tree, **kw):
        from mmw.site.adopt import adopt_apply_tree
        return adopt_apply_tree(tree, home=self.home, **kw)

    def adoption_match(self, tree, repo_key: str | None = None) -> tuple[str, str, str] | None:
        """(repository, label, base) this tree's suite joins here, if any."""
        from mmw.site.adopt import matching_repo
        from mmw.site.db import SiteDB
        with SiteDB(self.home) as db:
            match = (repo_key, db.repo_state(repo_key).bases[0]) if repo_key else matching_repo(db, tree)
            return None if match is None else (match[0], db.repo_state(match[0]).label, match[1])

    def network_keys(self) -> list[bytes]:
        from mmw.site.adopt import network_keys
        return network_keys(self.home)

    def recheck_signatures(self, keyring):
        from mmw.site.adopt import recheck_signatures
        return recheck_signatures(self.home, keyring)

    def originals(self):
        from mmw.site.adopt import originals
        return originals(self.home)

    def remove_original(self, path, other_roots=()):
        from mmw.site.adopt import remove_original
        return remove_original(self.home, path, other_roots)

    def combine(self, repo: str, track: str, whens: list[str], key: str):
        from mmw.site.combine import combine
        return combine(self.home, repo, track, whens, key)

    def point_notes(self, repo: str) -> dict[int, str]:
        from mmw.site.combine import notes
        from mmw.site.db import SiteDB
        with SiteDB(self.home) as db:
            return notes(db, repo)

    def unverified(self, repo: str) -> list[str]:
        from mmw.site.ops import unverified
        return unverified(self.home, repo)

    def packages_target(self) -> tuple[Path, Store]:
        """(home, store) for the local-packages page."""
        return self.home, self.store()

    def local_update(self):
        from mmw.site import local
        return local.update(self.home)

    def keyring_root(self) -> Path:
        return self.home

    def key_users(self, name: str) -> list[str]:
        from mmw.site.db import SiteDB
        with SiteDB(self.home) as db:
            return [s.label for s in db.repo_states()
                    if (lay := db.layer(s.repo)) is not None and lay.key == name]

    def sign(self, *, password: bytes | None, days: int, force: bool):
        from mmw.site.certs import sign_roots
        return sign_roots(self.home, password=password, days=days, force=force)

    def __repr__(self) -> str:
        return f"LocalRoot({self.info.name!r} at {self.info.location!r})"


@dataclass
class MirrorSet:
    set_id: str
    network: str
    roots: list
    missing: list[str] = field(default_factory=list)
    missing_reason: dict[str, str] = field(default_factory=dict)

    def root_named(self, name: str) -> LocalRoot:
        for r in self.roots:
            if r.info.name == name:
                return r
        if name in self.missing:
            raise NoSuchRoot(f"the root {name!r} isn't here — mount it, then: mmw site open PATH")
        raise NoSuchRoot(f"no root {name!r} — this mirror has: {', '.join(self.names())}")

    def names(self) -> list[str]:
        return [r.info.name for r in self.roots] + list(self.missing)

    def owner(self, host: str) -> LocalRoot | None:
        for r in self.roots:
            if host in r.hostnames():
                return r
        return None

    def root_for_repo(self, ref: str) -> tuple["LocalRoot", str]:
        """The root holding repository `ref` (key, or label in any case), and its key."""
        have = []
        for r in self.roots:
            key = r.repo_key(ref)
            if key is not None:
                return r, key
            have += r.labels()
        raise KeyError(f"no repository {ref!r} — this mirror has: {', '.join(sorted(have, key=str.lower))}")


def root_at(location: str):
    """The root at `location` — a local folder or `[user@]host:/path` — or
    None when there is no mirror there. An ssh host mmw can't use raises
    `ssh.SshError` (in fixed words)."""
    from mmw.site import ssh
    kind, _host, _path = ssh.parse_location(location)
    if kind == "ssh":
        from mmw.site.sshroot import SshRoot, remote_info
        info = remote_info(location)
        if info is None:
            return None
        recents.seen(location, info["set_id"], info["name"])
        return SshRoot(location, info)
    info = read_root(location)
    if info is None:
        return None
    if info.set_id is not None:
        recents.seen(location, info.set_id, info.name)
    return LocalRoot(info)


def open_set(locations: list[str] | None = None, set_id: str | None = None) -> MirrorSet:
    """The set the given (default: recent) locations hold. More than one set
    and no `set_id` is an error that names them. An ssh root that can't be
    reached is missing, with why."""
    from mmw.site import ssh
    groups: dict[str, list] = {}
    seen: set[tuple[str | None, str]] = set()
    v1: list[str] = []
    unreachable: dict[str, dict[str, str]] = {}          # set id → root name → why
    for loc in (recents.known() if locations is None else locations):
        try:
            root = root_at(loc)
        except (ssh.SshError, ssh.RemoteMmw) as e:
            last = recents.last_seen(loc)
            if last:
                unreachable.setdefault(last["set_id"], {})[last["name"]] = str(e)
            continue
        if root is None:
            continue
        if root.info.set_id is None:
            v1.append(loc)
            continue
        info = root.info
        if (info.set_id, info.name) in seen:
            continue                                    # the same root by another path
        seen.add((info.set_id, info.name))
        groups.setdefault(info.set_id, []).append(root)
    if set_id is not None:
        groups = {k: v for k, v in groups.items() if k == set_id}
    if not groups and v1:
        raise LookupError(f"{v1[0]} was made before mirrors described themselves (a v1 site) — start it again: "
                          "mmw site apply BUNDLE --root '*'=NEWPATH")
    if not groups:
        raise LookupError("no mirror opened on this machine yet — mmw site open ROOT, or apply a bundle")
    if len(groups) > 1:
        raise ValueError("this machine has opened more than one mirror — name one with --root: " + "; ".join(
            f"{roots[0].info.network} ({', '.join(r.info.location for r in roots)})" for roots in groups.values()))
    (sid, roots), = groups.items()
    members: list[str] = []
    for r in roots:
        members += [m for m in r.info.members if m not in members]
    present = {r.info.name for r in roots}
    missing = [m for m in members if m not in present]
    reasons = {m: unreachable.get(sid, {}).get(m, "not opened on this machine") for m in missing}
    return MirrorSet(sid, roots[0].info.network or "", roots, missing, reasons)
