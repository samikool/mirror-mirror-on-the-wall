"""Switching a repository between versions: only the differing paths,
packages first, then indexes, then release files, deletions last — and a
switch cut short is finished by the next command."""
import io

import pytest

from mmw.site.db import SiteDB
from mmw.site.store import Store
from mmw.site.switch import resume_switches, switch_repo

BASE = "h/ubuntu"


def _roots(root):
    from mmw.site.home import RootConfig, SiteSettings
    from mmw.site.roots import Roots
    return Roots(SiteSettings(roots=[RootConfig(name="main", path=root, serve_path=root)]))


def _setup(tmp_path, versions):
    root = tmp_path / "root"
    store = Store(root)
    db = SiteDB(tmp_path)
    ids = []
    for n, files in enumerate(versions, 1):
        rows = []
        for rel, data in files.items():
            cs, size = store.add_stream(io.BytesIO(data))
            rows.append((f"{BASE}/{rel}", cs, size))
        ids.append(db.add_version("R", "ubuntu", n, f"d{n}", f"2026100{n}T000000Z", None, [BASE], rows))
    return root, store, db, ids


V1 = {"pool/a.deb": b"a1", "dists/noble/main/Packages": b"P1", "dists/noble/InRelease": b"R1",
      "pool/old/gone.deb": b"g"}
V2 = {"pool/a.deb": b"a1", "pool/b.deb": b"b2", "dists/noble/main/Packages": b"P2",
      "dists/noble/InRelease": b"R2"}


def _tree(root):
    return {p.relative_to(root / BASE).as_posix(): p.read_bytes()
            for p in (root / BASE).rglob("*") if p.is_file()}


def test_switch_from_nothing(tmp_path):
    root, store, db, (v1, _) = _setup(tmp_path, [V1, V2])
    switch_repo(db, store, root, "R", v1)
    assert _tree(root) == V1 and db.repo_state("R").serving.id == v1


def test_switch_places_then_deletes_in_order(tmp_path, monkeypatch):
    root, store, db, (v1, v2) = _setup(tmp_path, [V1, V2])
    switch_repo(db, store, root, "R", v1)
    events = []
    real_place = Store.place
    monkeypatch.setattr(Store, "place", lambda self, cs, dest: (events.append(("place", dest.name)),
                                                                real_place(self, cs, dest))[1])
    from mmw.core import disk_io
    real_remove = disk_io.ops.remove
    monkeypatch.setattr(disk_io.ops, "remove", lambda p: (events.append(("rm", p.name)), real_remove(p))[1])
    r = switch_repo(db, store, root, "R", v2)
    assert events == [("place", "b.deb"), ("place", "Packages"), ("place", "InRelease"), ("rm", "gone.deb")]
    assert (r.placed, r.removed) == (3, 1)
    assert _tree(root) == V2
    assert not (root / BASE / "pool/old").exists()                   # emptied directory pruned


def test_switch_resumes_after_interruption(tmp_path, monkeypatch):
    root, store, db, (v1, v2) = _setup(tmp_path, [V1, V2])
    switch_repo(db, store, root, "R", v1)
    real_place = Store.place

    def flaky(self, cs, dest):
        if dest.name == "Packages":
            raise KeyboardInterrupt
        return real_place(self, cs, dest)
    monkeypatch.setattr(Store, "place", flaky)
    with pytest.raises(KeyboardInterrupt):
        switch_repo(db, store, root, "R", v2)
    assert db.pending_switches()[0][:3] == ("R", v1, v2)
    monkeypatch.setattr(Store, "place", real_place)
    assert resume_switches(db, _roots(root)) == ["R"]
    assert _tree(root) == V2 and db.pending_switches() == [] and db.repo_state("R").serving.id == v2


def test_switch_back_to_older(tmp_path):
    root, store, db, (v1, v2) = _setup(tmp_path, [V1, V2])
    switch_repo(db, store, root, "R", v2)
    switch_repo(db, store, root, "R", v1)
    assert _tree(root) == V1
