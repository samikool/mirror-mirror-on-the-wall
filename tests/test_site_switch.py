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
            for p in (root / BASE).rglob("*") if p.is_file() and not p.relative_to(root / BASE).parts[0].startswith("@")}


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


def test_obsolete_package_cleanup_failure_does_not_block_target(tmp_path, monkeypatch):
    from mmw.core import disk_io
    root, store, db, (v1, v2) = _setup(tmp_path, [V1, V2])
    switch_repo(db, store, root, 'R', v1)
    remove = disk_io.ops.remove

    def denied(path):
        if path.name == 'gone.deb':
            raise PermissionError('cleanup denied')
        return remove(path)

    monkeypatch.setattr(disk_io.ops, 'remove', denied)
    result = switch_repo(db, store, root, 'R', v2)
    assert db.repo_state('R').serving.id == v2
    assert db.pending_switches() == []
    assert all((root / BASE / rel).read_bytes() == body for rel, body in V2.items())
    assert (root / BASE / 'pool/old/gone.deb').exists()
    assert any('gone.deb' in warning and 'could not be removed' in warning for warning in result.warnings)


def test_retired_suite_cleanup_failure_is_a_warning(tmp_path, monkeypatch):
    from mmw.core import disk_io
    target = {p.replace('noble', 'jammy'): body for p, body in V2.items()}
    root, store, db, (v1, v2) = _setup(tmp_path, [V1, target])
    switch_repo(db, store, root, 'R', v1)
    remove = disk_io.ops.remove

    def denied(path):
        if path.name == 'InRelease' and 'noble' in path.parts:
            raise PermissionError('cleanup denied')
        return remove(path)

    monkeypatch.setattr(disk_io.ops, 'remove', denied)
    result = switch_repo(db, store, root, 'R', v2)
    assert db.repo_state('R').serving.id == v2
    assert all((root / BASE / rel).read_bytes() == body for rel, body in target.items())
    assert any('noble/InRelease' in warning for warning in result.warnings)


def test_stale_signature_in_an_active_suite_must_be_removed(tmp_path, monkeypatch):
    from mmw.core import disk_io
    target = {p: body for p, body in V2.items() if not p.endswith('InRelease')}
    target['dists/noble/Release'] = b'unsigned R2'
    root, store, db, (v1, v2) = _setup(tmp_path, [V1, target])
    switch_repo(db, store, root, 'R', v1)
    remove = disk_io.ops.remove

    def denied(path):
        if path.name == 'InRelease':
            raise PermissionError('active signature still present')
        return remove(path)

    monkeypatch.setattr(disk_io.ops, 'remove', denied)
    with pytest.raises(PermissionError, match='active signature'):
        switch_repo(db, store, root, 'R', v2)
    assert db.repo_state('R').serving.id == v1


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
