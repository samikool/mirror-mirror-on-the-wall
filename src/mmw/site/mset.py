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
    (phase 5 adds one reached over ssh)."""

    def __init__(self, info: RootInfo):
        self.info = info

    @property
    def home(self) -> Path:
        return self.info.home

    def hostnames(self) -> set[str]:
        return hostnames(self.home)

    def free_bytes(self) -> int:
        return disk_io.ops.disk_usage(self.info.path).free

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

    def has(self, checksums: set[str]) -> set[str]:
        store = self.store()
        return {c for c in checksums if store.has(c)}

    def hosts_map(self) -> dict[str, str]:
        from mmw.site.home import load_site
        return dict(load_site(self.home).hosts)

    def set_identity(self, *, members: list[str] | None = None, hosts: dict[str, str] | None = None) -> None:
        from mmw.site.home import load_site, save_settings
        s = load_site(self.home)
        update = {k: v for k, v in (("members", members), ("hosts", hosts)) if v is not None}
        save_settings(self.home, s.model_copy(update=update))

    def keep_versions(self) -> int | None:
        from mmw.site.home import load_site
        return load_site(self.home).keep_versions

    def history(self, key: str):
        from mmw.site import ops
        return ops.history(self.home, key)

    def serve_preview(self, key: str, number: int):
        from mmw.site import ops
        return ops.serve_preview(self.home, key, number)

    def serve(self, key: str, number: int):
        from mmw.site import ops
        return ops.serve(self.home, key, number)

    def forward(self, key: str | None) -> list[str]:
        from mmw.site import ops
        return ops.forward(self.home, key)

    def hold(self, key: str) -> str:
        from mmw.site import ops
        return ops.hold(self.home, key)

    def delete(self, key: str, number: int) -> int:
        from mmw.site import ops
        return ops.delete_version(self.home, key, number)

    def space(self):
        from mmw.site import ops
        return ops.space(self.home)

    def verify(self, repair: bool = False):
        from mmw.site import ops
        return ops.repair(self.home) if repair else ops.verify(self.home)

    def keep(self, keep: int | None) -> None:
        from mmw.site import ops
        ops.keep_here(self.home, keep)

    def break_lock(self) -> bool:
        from mmw.site.lock import break_lock
        return break_lock(self.home)

    def applier(self, **opts):
        from mmw.site.apply import RootApply
        return RootApply(self.home, **opts)

    def export_blob(self, checksum: str):
        return disk_io.ops.open_rb(self.store().path(checksum))

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

    def tidy(self) -> list[str]:
        from mmw.site import ops
        return ops.tidy_here(self.home)

    def tls_notes(self) -> list[str]:
        from mmw.site.certs import tls_notes
        return tls_notes(self.home)

    def sign(self, *, password: bytes | None, days: int, force: bool):
        from mmw.site.certs import sign_roots
        return sign_roots(self.home, password=password, days=days, force=force)

    def __repr__(self) -> str:
        return f"LocalRoot({self.info.name!r} at {self.info.location!r})"


@dataclass
class MirrorSet:
    set_id: str
    network: str
    roots: list[LocalRoot]
    missing: list[str] = field(default_factory=list)

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


def open_set(locations: list[str] | None = None, set_id: str | None = None) -> MirrorSet:
    """The set the given (default: recent) locations hold. More than one set
    and no `set_id` is an error that names them."""
    groups: dict[str, list[LocalRoot]] = {}
    seen: set[tuple[str | None, str]] = set()
    for loc in (recents.known() if locations is None else locations):
        info = read_root(loc)
        if info is None or info.set_id is None:
            continue
        if (info.set_id, info.name) in seen:
            continue                                    # the same root by another path
        seen.add((info.set_id, info.name))
        groups.setdefault(info.set_id, []).append(LocalRoot(info))
    if set_id is not None:
        groups = {k: v for k, v in groups.items() if k == set_id}
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
    return MirrorSet(sid, roots[0].info.network or "", roots, [m for m in members if m not in present])
