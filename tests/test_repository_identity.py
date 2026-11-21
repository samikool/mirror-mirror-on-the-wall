"""A repository is known by its content, not its URL: http vs https is no
difference, and a renamed host serving the same Releases is the same
repository. Only Origin/Label/Codename in common is offered, never merged."""
import gzip
import hashlib
import io
import json
import tarfile

import pytest

from apt_tree import make_tree
from mmw.core import config
from mmw.core.publisher.manifest import ManifestHeader, blob_member
from mmw.core.repos import rebase_files, repository_identity, same_release_identity
from mmw.site import home as H
from mmw.site import recents
from mmw.site.adopt import adopt_tree, check_tree, find_trees
from mmw.site.apply import apply_to_set
from mmw.site.db import SiteDB
from mmw.site.mset import open_set
from mmw.site.tracks import split_tracks

BASE = "deb.debian.org/debian"


def sha(b):
    return hashlib.sha256(b).hexdigest()


def release_tree(day, base=BASE, suite="bookworm", origin="Debian", arch="amd64"):
    """(rows, blobs) of one suite on `day` at `base`."""
    rows, blobs, listed = {}, {}, []
    d = f"{base}/dists/{suite}"
    data = f"hello{day}{arch}".encode() * 20
    deb = f"{base}/pool/main/h/hello/hello_{day}_{arch}.deb"
    rows[deb] = sha(data)
    blobs[sha(data)] = data
    idx = (f"Package: hello\nVersion: {day}\nArchitecture: {arch}\n"
           f"Filename: pool/main/h/hello/hello_{day}_{arch}.deb\n").encode()
    for name, body in ((f"main/binary-{arch}/Packages", idx),
                       (f"main/binary-{arch}/Packages.gz", gzip.compress(idx, mtime=0))):
        rows[f"{d}/{name}"] = sha(body)
        blobs[sha(body)] = body
        listed.append((name, body))
    rel = (f"Origin: {origin}\nLabel: {origin}\nSuite: stable\nCodename: {suite}\n"
           f"Date: {day:02d} Nov 2026 00:00:00 UTC\nArchitectures: {arch}\nComponents: main\n"
           "SHA256:\n" + "".join(f" {sha(b)} {len(b)} {n}\n" for n, b in listed)).encode()
    rows[f"{d}/Release"] = sha(rel)
    blobs[sha(rel)] = rel
    return rows, blobs


def bundle(out, network, repos, *, created="20261115T000000Z"):
    """`repos`: key -> (label, base, [(rows, blobs)…]) — a format-3 selection bundle."""
    header_points, trows, members = {}, [], {}
    for key, (label, base, states) in repos.items():
        points = []
        for rows, blobs in states:
            for d in split_tracks(rows, [base], lambda p, r=rows, b=blobs: b.get(r.get(p)), "x"):
                points.append((d, blobs))
        header_points[key] = {"label": label, "bases": [base],
                              "points": [{"track": d.track, "ident": d.ident, "date": d.date,
                                          "stamp": created, "snap_label": None, "fields": dict(d.fields)}
                                         for d, _b in points]}
        holders = {}
        for i, (d, blobs) in enumerate(points):
            for p, c in d.files.items():
                holders.setdefault((p, c), (blobs, []))[1].append(i)
        for (p, c), (blobs, k) in sorted(holders.items()):
            trows.append({"p": p, "c": c, "r": key, "k": k})
            members[blob_member(c)] = blobs[c]
    header = ManifestHeader(network=network, created=created, previous=None, full=False,
                            file_count=len(members), format=3, points=header_points)
    fields = {k: v for k, v in header.__dict__.items() if v is not None or k in ("previous",)}
    fields["selection"] = True
    manifest = (json.dumps(fields, sort_keys=True) + "\n" + "".join(json.dumps(r) + "\n" for r in trows)).encode()
    with tarfile.open(out, "w") as tar:
        for name, data in [(".mmw/manifest.jsonl", manifest), *members.items()]:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
    return str(out)


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "srv"
    home = H.init_root(root, name="main", set_id="s1", network="oak")
    recents.remember(str(root))
    return home, root.resolve()


def _apply(root, path, **kw):
    r = apply_to_set(open_set([str(root)]), path, place={"*": str(root)}, say=lambda *_: None, **kw)
    return [line.strip() for line in r.merged().lines]


def _repos(home):
    with SiteDB(home) as db:
        return {st.repo: st for st in db.repo_states()}


def _adopt(home, path, base):
    (tree,) = find_trees(path)
    return adopt_tree(home, tree, check_tree(tree, None), repo=None, label="Debian", base=base)


# ── helpers ──────────────────────────────────────────────────────────────────

def test_identity_ignores_scheme_only():
    assert repository_identity("http://deb.debian.org/debian") == repository_identity("https://deb.debian.org/debian/")
    assert repository_identity("http://deb.debian.org/debian") != repository_identity("http://deb.debian.org/Debian")
    assert repository_identity("vscode") == "vscode"
    assert same_release_identity({"release:a", "content:x"}, {"release:a"})
    assert not same_release_identity({"content:x"}, {"content:x"})
    assert rebase_files({"new.d.org/debian/dists/x/Release": "c", "other/y": "d"}, "new.d.org/debian",
                        "d.org/debian") == {"d.org/debian/dists/x/Release": "c", "other/y": "d"}


def test_adding_the_same_repository_by_another_scheme_merges():
    have = config.SourceConfig(url="http://deb.debian.org/debian", suites=["bookworm"], arches=["amd64"])
    new = config.SourceConfig(url="https://deb.debian.org/debian", suites=["bookworm"], arches=["arm64"])
    merged, added, folded = config.merge_sources([have], [new])
    assert (len(merged), added, folded) == (1, 0, 1)
    assert set(merged[0].arches) == {"amd64", "arm64"}


# ── the far side ─────────────────────────────────────────────────────────────

def test_raw_adopt_then_http_bundle_is_one_repository(site, tmp_path):
    home, root = site
    _adopt(home, make_tree(tmp_path / "old", day=1), BASE)
    lines = _apply(root, bundle(tmp_path / "b.tar", "oak",
                                {"http://deb.debian.org/debian": ("Debian", BASE, [release_tree(5)])}))
    repos = _repos(home)
    assert len(repos) == 1, (list(repos), lines)
    with SiteDB(home) as db:
        (repo,) = repos
        (track,) = [t for t in db.tracks(repo) if t.name.endswith("dists/bookworm")]
        assert len(db.points(track.id)) == 2
    assert (root / BASE / "pool/main/h/hello/hello_5_amd64.deb").exists()


def test_shared_release_claims_across_hosts(site, tmp_path):
    home, root = site
    old_base = "new.deb.debian.org/debian"
    shared_rows, shared_blobs = release_tree(3, base=old_base)
    tree = tmp_path / "tree"
    for path, cs in shared_rows.items():
        p = tree / path[len(old_base) + 1:]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(shared_blobs[cs])
    _adopt(home, tree, old_base)
    lines = _apply(root, bundle(tmp_path / "b.tar", "oak", {"http://deb.debian.org/debian": (
        "Debian", BASE, [release_tree(3), release_tree(5)])}))
    repos = _repos(home)
    assert len(repos) == 1, (list(repos), lines)
    (st,) = repos.values()
    assert st.bases == [BASE]
    with SiteDB(home) as db:
        tracks = [t for t in db.tracks(st.repo) if t.name]
        assert [t.name for t in tracks] == [f"{BASE}/dists/bookworm"]
        assert [p.date[:8] for p in db.points(tracks[0].id)] == ["20261105", "20261103"]
    assert (root / BASE / "dists/bookworm/Release").exists()


def test_ports_and_archive_are_offered_not_merged(site, tmp_path):
    home, root = site
    _apply(root, bundle(tmp_path / "a.tar", "oak", {"http://archive.ubuntu.com/ubuntu": (
        "ubuntu", "archive.ubuntu.com/ubuntu", [release_tree(3, base="archive.ubuntu.com/ubuntu",
                                                             suite="noble", origin="Ubuntu")])}))
    lines = _apply(root, bundle(tmp_path / "p.tar", "oak", {"http://ports.ubuntu.com/ubuntu-ports": (
        "ubuntu-ports", "ports.ubuntu.com/ubuntu-ports",
        [release_tree(3, base="ports.ubuntu.com/ubuntu-ports", suite="noble", origin="Ubuntu", arch="arm64")])},
        created="20261116T000000Z"))
    assert len(_repos(home)) == 2
    assert any("looks like ubuntu" in line and "mmw site merge 'ubuntu' 'ubuntu-ports'" in line
               for line in lines), lines


def test_two_repositories_on_one_base_are_said(site, tmp_path):
    home, root = site
    with SiteDB(home) as db:
        db.ensure_repo("https://x.org/debian", "x-one", ["x.org/debian"])
        db.ensure_repo("https://x.org/debian-again", "x-two", ["x.org/debian"])
    lines = _apply(root, bundle(tmp_path / "b.tar", "oak",
                                {"http://deb.debian.org/debian": ("Debian", BASE, [release_tree(5)])}))
    assert "x-one and x-two both serve x.org/debian — mmw site merge 'x-one' 'x-two'" in lines, lines
