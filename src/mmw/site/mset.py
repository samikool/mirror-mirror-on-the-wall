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

    def root_for_repo(self, ref: str) -> tuple[LocalRoot, str]:
        """The root holding repository `ref` (key, or label in any case), and its key."""
        from mmw.site.db import SiteDB
        from mmw.site.ops import resolve_repo
        have = []
        for r in self.roots:
            with SiteDB(r.home) as db:
                states = db.repo_states()
                have += [s.label for s in states]
                try:
                    key = resolve_repo(db, ref)
                except LookupError:
                    continue
                if db.get_meta(f"moving_in:{key}"):
                    continue                        # a move's copy, cut short: its home is the old root
                return r, key
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
