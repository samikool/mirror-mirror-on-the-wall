"""Adoption: taking over APT trees mmw didn't make.

Find every archive root under a path, verify each Release's signature and
every file against the Release and its indexes, then ingest them — linked
into a root's store — as points on one repository's tracks. The trees found
are never written to, moved or deleted.
"""
from __future__ import annotations

import bz2
import gzip
import hashlib
import json
import lzma
import os
import posixpath
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from mmw.core.apt_index import release_body
from mmw.site.store import ChecksumMismatch
from mmw.site.tracks import _fields, release_date, release_ident

_RELEASES = ("InRelease", "Release")


@dataclass(frozen=True)
class FoundTrack:
    dir: str                   # relative to the archive root: "dists/bookworm", or "" (flat)
    fields: dict[str, str]     # Origin, Label, Suite, Codename (those present)
    date: str                  # the Release's Date: as a stamp
    ident: str                 # release:<sha256 of the Release text>


@dataclass(frozen=True)
class FoundTree:
    path: Path
    flat: bool
    tracks: list[FoundTrack]

    def identity(self) -> tuple[str, ...]:
        """What makes two trees the same upstream suite."""
        f = self.tracks[0].fields if self.tracks else {}
        return (f.get("Origin", ""), f.get("Label", ""), f.get("Codename") or f.get("Suite", ""))


def _release_text(d: Path) -> tuple[bytes | None, bytes | None]:
    """(Release, InRelease) bytes in `d`, those present as regular files."""
    def read(name: str) -> bytes | None:
        p = d / name
        return p.read_bytes() if p.is_file() and not p.is_symlink() else None
    return read("Release"), read("InRelease")


def _track(root: Path, d: Path) -> FoundTrack | None:
    release, inrelease = _release_text(d)
    if release is None and inrelease is None:
        return None
    text = (release if release is not None else release_body(inrelease)).decode("utf-8", "replace")
    fallback = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime((d / ("Release" if release else "InRelease")).stat().st_mtime))
    date, _why = release_date(text, fallback)
    rel = d.relative_to(root).as_posix()
    return FoundTrack("" if rel == "." else rel, _fields(text), date, release_ident(release, inrelease))


def _is_flat(d: Path, names: list[str]) -> bool:
    return any(n in names for n in _RELEASES) and any(n.startswith("Packages") or n.startswith("Sources")
                                                      for n in names)


def find_trees(path: Path) -> list[FoundTree]:
    """Every archive root under `path` (itself included): a directory whose
    `dists/*/` holds a Release or InRelease, or a flat repository (a Release
    beside its Packages). Symlinks are never followed."""
    path = Path(path)
    found: list[FoundTree] = []
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        d = Path(dirpath)
        dists = d / "dists"
        if "dists" in dirnames and not dists.is_symlink():
            tracks = [t for sub in sorted(dists.iterdir()) if sub.is_dir() and not sub.is_symlink()
                      for t in [_track(d, sub)] if t is not None]
            if tracks:
                found.append(FoundTree(d, False, tracks))
                dirnames[:] = []                 # an archive root: nothing nests inside it
                continue
        if _is_flat(d, filenames) and "dists" not in Path(dirpath).parts[len(path.parts):]:
            t = _track(d, d)
            if t is not None:
                found.append(FoundTree(d, True, [t]))
                dirnames[:] = []
                continue
        dirnames[:] = sorted(n for n in dirnames if not (d / n).is_symlink())
    return sorted(found, key=lambda t: str(t.path))


# ── checking a tree ──────────────────────────────────────────────────────────

_DECOMPRESS = {"": lambda b: b, ".xz": lzma.decompress, ".lzma": lzma.decompress,
               ".gz": gzip.decompress, ".bz2": bz2.decompress}
_INDEX_STEMS = ("Packages", "Sources")


@dataclass
class TreeCheck:
    signature: dict[str, str] = field(default_factory=dict)     # track dir → verified | not checked | bad: why
    expected: dict[str, tuple[str, int | None]] = field(default_factory=dict)   # rel → (sha256, size)
    present: dict[str, str] = field(default_factory=dict)       # rel → sha256 hashed (what adoption takes)
    sizes: dict[str, int] = field(default_factory=dict)         # rel → bytes, for what is present
    mismatched: dict[str, list[str]] = field(default_factory=dict)   # rel → track dirs it refuses
    missing: dict[str, list[str]] = field(default_factory=dict)      # rel → track dirs stored incomplete
    strays: dict[str, list[str]] = field(default_factory=dict)       # kind → rel paths (not imported)
    refused: dict[str, str] = field(default_factory=dict)            # track dir → why
    stats: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)   # rel → (dev, ino, size, mtime)


def keyring_from(keys: list[bytes], into: Path) -> Path:
    """One keyring gpgv reads, from armoured or binary public keys."""
    from mmw.core.keys import dearmor, looks_armoured
    into = Path(into)
    into.parent.mkdir(parents=True, exist_ok=True)
    into.write_bytes(b"".join(dearmor(k.decode("ascii", "replace")) if looks_armoured(k) else k for k in keys))
    return into


def _gpgv(keyring: Path, *files: Path) -> str:
    if shutil.which("gpgv") is None:
        return "not checked (gpgv is not installed)"
    run = subprocess.run(["gpgv", "--keyring", str(keyring), *map(str, files)], capture_output=True, timeout=120)
    if run.returncode == 0:
        return "verified"
    lines = [ln for ln in run.stderr.decode(errors="replace").splitlines() if ln.strip()]
    return "bad: " + (lines[-1].removeprefix("gpgv: ") if lines else f"gpgv exit {run.returncode}")


def _signature(d: Path, keyring: Path | None) -> str:
    if keyring is None:
        return "not checked"
    if (d / "InRelease").is_file():
        return _gpgv(keyring, d / "InRelease")
    if (d / "Release.gpg").is_file() and (d / "Release").is_file():
        return _gpgv(keyring, d / "Release.gpg", d / "Release")
    return "not checked (unsigned)"


def _release_sums(text: str) -> dict[str, tuple[str, int]]:
    """The Release's SHA256 section: path under the suite → (sha256, size)."""
    out, inside = {}, False
    for ln in text.splitlines():
        if not ln[:1].isspace():
            inside = ln.strip() == "SHA256:"
            continue
        parts = ln.split()
        if inside and len(parts) == 3:
            out[parts[2]] = (parts[0], int(parts[1]))
    return out


def _stanzas(text: str):
    cur: dict[str, str] = {}
    key = None
    for ln in text.splitlines():
        if not ln.strip():
            if cur:
                yield cur
            cur, key = {}, None
        elif ln[0].isspace() and key is not None:
            cur[key] += "\n" + ln.strip()
        elif ":" in ln:
            key, value = ln.split(":", 1)
            cur[key] = value.strip()
    if cur:
        yield cur


def _named(text: str, stem: str) -> dict[str, tuple[str, int | None]]:
    """Files a Packages or Sources index names, relative to the archive root."""
    out = {}
    for s in _stanzas(text):
        if stem == "Packages":
            if "Filename" in s and "SHA256" in s:
                out[posixpath.normpath(s["Filename"])] = (s["SHA256"], int(s["Size"]) if "Size" in s else None)
        elif "Directory" in s:
            for ln in s.get("Checksums-Sha256", "").splitlines():
                parts = ln.split()
                if len(parts) == 3:
                    out[posixpath.normpath(f"{s['Directory']}/{parts[2]}")] = (parts[0], int(parts[1]))
    return out


def _safe(rel: str) -> bool:
    return bool(rel) and rel not in (".", "..") and not rel.startswith(("/", "../")) \
        and "/../" not in f"/{rel}/"


def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


# (device, inode, size, mtime) → sha256: a file several trees share (hardlinked
# snapshots of one mirror) is read once per run.
_HASHED: dict[tuple[int, int, int, int], str] = {}


def _files(root: Path) -> dict[str, os.stat_result]:
    """Every regular file under `root` (no symlinks, none followed) → its stat."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        d = Path(dirpath)
        dirnames[:] = [n for n in dirnames if not (d / n).is_symlink()]
        for n in filenames:
            p = d / n
            st = p.lstat()
            if not p.is_symlink() and p.is_file():
                out[p.relative_to(root).as_posix()] = st
    return out


def _stat_key(st: os.stat_result) -> tuple[int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def check_tree(tree: FoundTree, keyring: Path | None, progress=None) -> TreeCheck:
    """Signature, and every file against what the Release and its indexes
    promise. A file that differs, a bad signature, a Release that isn't its
    InRelease or a path leaving the tree refuses its suite; a file that isn't
    there (or can't be read) leaves it incomplete; files nothing names are
    strays (by-hash, installer files under a suite and the archive root's own
    files are kept). `progress(rel, size)` per file hashed."""
    root = tree.path
    files = _files(root)
    check = TreeCheck()
    check.stats = {rel: _stat_key(st) for rel, st in files.items()}
    unreadable: set[str] = set()

    def sha(rel: str) -> str | None:
        key = check.stats[rel]
        if key not in _HASHED:
            try:
                _HASHED[key] = _hash(root / rel)
            except OSError:
                unreadable.add(rel)
                return None
            if progress:
                progress(rel, files[rel].st_size)
        return _HASHED[key]

    wants: dict[str, dict[str, tuple[str, int | None]]] = {}     # rel → track dir → (sha, size)

    def want(rel: str, tdir: str, value: tuple[str, int | None]) -> None:
        wants.setdefault(rel, {})[tdir] = value

    for t in tree.tracks:
        d = root / t.dir if t.dir else root
        check.signature[t.dir] = _signature(d, keyring)
        if check.signature[t.dir].startswith("bad"):
            check.refused[t.dir] = f"its signature is bad ({check.signature[t.dir][5:]})"
        release, inrelease = _release_text(d)
        if release is not None and inrelease is not None and release_body(inrelease) != release:
            check.refused.setdefault(t.dir, "its Release and InRelease differ")
        body = release if release is not None else release_body(inrelease)
        text = body.decode("utf-8", "replace")
        prefix = f"{t.dir}/" if t.dir else ""
        listed = {prefix + p: v for p, v in _release_sums(text).items()}
        stems: dict[str, list[str]] = {}
        for rel in listed:
            name = rel.rsplit("/", 1)[-1]
            stem = next((s for s in _INDEX_STEMS for ext in _DECOMPRESS if name == s + ext), None)
            if stem is not None:
                stems.setdefault(rel[: len(rel) - len(name) + len(stem)], []).append(rel)
            if rel in files:                                 # an index variant not mirrored is fine
                want(rel, t.dir, listed[rel])
        for stem_path, variants in sorted(stems.items()):
            here = [v for v in variants if v in files]
            if not here:                                     # no variant of this index at all
                pick = stem_path if stem_path in listed else sorted(variants)[0]
                want(pick, t.dir, listed[pick])
                continue
            order = {ext: i for i, ext in enumerate(_DECOMPRESS)}
            for v in sorted(here, key=lambda v: order[v[len(stem_path):]]):
                if sha(v) != listed[v][0]:
                    continue
                stem = stem_path.rsplit("/", 1)[-1]
                try:
                    text_ix = _DECOMPRESS[v[len(stem_path):]]((root / v).read_bytes()).decode("utf-8", "replace")
                except (OSError, EOFError, lzma.LZMAError, ValueError):
                    continue
                for rel, value in _named(text_ix, stem).items():
                    if not _safe(rel):
                        check.refused.setdefault(t.dir, f"an index names {rel!r}, outside the tree")
                        continue
                    want(rel, t.dir, value)
                break
    for rel, by_track in sorted(wants.items()):
        owners = sorted(by_track)
        check.expected[rel] = next(iter(by_track.values()))
        if rel not in files:
            check.missing[rel] = owners
            continue
        got = sha(rel)
        if got is None:
            check.missing[rel] = owners
            continue
        bad = [tdir for tdir, (cs, _size) in by_track.items() if cs != got]
        if bad:
            check.mismatched[rel] = sorted(bad)
            for tdir in bad:
                check.refused.setdefault(tdir, f"{rel} differs from what its Release and indexes say")
        check.present[rel] = got
        check.expected[rel] = (got, by_track[owners[0]][1])
    suites = [f"{t.dir}/" for t in tree.tracks if t.dir]
    for rel in sorted(files):
        if rel in wants:
            continue
        if any(rel.startswith(s) for s in suites) or "/" not in rel:
            got = sha(rel)
            if got is None:
                check.strays.setdefault("unreadable", []).append(rel)
            elif "/by-hash/SHA256/" in rel and rel.rsplit("/", 1)[-1] != got:
                check.strays.setdefault("damaged", []).append(rel)
            else:
                check.present[rel] = got                 # a suite's own file, or the archive root's
        else:
            check.strays.setdefault("pool" if rel.startswith("pool/") else "other", []).append(rel)
    check.sizes = {rel: files[rel].st_size for rel in check.present}
    return check


# ── adopting into a root ─────────────────────────────────────────────────────

@dataclass
class AdoptResult:
    repo: str
    label: str
    lines: list[str] = field(default_factory=list)
    refused: dict[str, str] = field(default_factory=dict)      # suite → why
    incomplete: dict[str, int] = field(default_factory=dict)   # suite → files missing
    unverified: list[str] = field(default_factory=list)        # suites adopted with no signature check
    linked: int = 0
    copied: int = 0
    copied_bytes: int = 0


def _suite_name(track_dir: str, fields: dict[str, str]) -> str:
    tail = track_dir.rsplit("/", 1)[-1] if track_dir else ""
    return (tail if "/dists/" in f"/{track_dir}" else "") or fields.get("Codename") or fields.get("Suite") or "files"


def unverified_key(point) -> str:
    """The meta key marking a point adopted without a signature check — by
    its track and ident, which no other point ever reuses."""
    return f"unverified:{point.track}:{point.ident}"


def new_repo_key(base: str) -> str:
    from mmw.core.config import normalize_url
    return normalize_url(f"https://{base.strip('/')}/")


def _ingest(src_root: Path, present: dict[str, str], stats: dict, sizes: dict[str, int], store,
            root_path: Path, result: AdoptResult) -> None:
    """Every file adopted into the store: hardlinked when the tree is on the
    store's filesystem, else copied — after checking there is room."""
    from mmw.core import disk_io
    from mmw.site.space import real_free
    disk_io.ops.mkdir(store.dir, parents=True, exist_ok=True)
    same_fs = os.stat(src_root).st_dev == os.stat(store.dir).st_dev
    todo = {cs: rel for rel, cs in present.items() if not store.has(cs)}
    if not same_fs:
        need = sum(sizes[rel] for rel in todo.values())
        free = real_free(root_path)
        if need > free:
            from mmw.core.format import fmt_size
            raise ValueError(f"{src_root} is on another filesystem: adopting it copies {fmt_size(need)}, "
                             f"and {root_path} has {fmt_size(free)} free")
    disk_io.ops.mkdir(store.tmp, parents=True, exist_ok=True)
    for cs, rel in sorted(todo.items(), key=lambda kv: kv[1]):
        src = src_root / rel
        if same_fs and _linkable(src) and _link_in(src, cs, stats.get(rel), store):
            result.linked += 1
            continue
        try:
            with open(src, "rb") as fh:
                _cs, size = store.add_stream(fh, expect=cs)
        except ChecksumMismatch:
            raise ValueError(f"{src} changed after it was checked — stop what writes to the tree, "
                             "then adopt it again") from None
        result.copied += 1
        result.copied_bytes += size


def _linkable(src: Path) -> bool:
    """A hardlink keeps the file's owner and mode: one nginx can't read is
    copied instead."""
    return bool(os.stat(src).st_mode & 0o004)


def _link_in(src: Path, cs: str, seen: tuple[int, int, int, int] | None, store) -> bool:
    """Hardlink `src` in as blob `cs` — only if it is still the very file that
    was hashed (a mirror tool may have renamed another in since). False: it
    can't be linked here (copy it). A changed file raises."""
    tmp = store.tmp / f"adopt-{os.getpid()}-{cs[:16]}"
    try:
        os.link(src, tmp)
    except OSError:
        return False
    try:
        if seen is None or _stat_key(os.stat(tmp)) != seen:
            raise ValueError(f"{src} changed after it was checked — stop what writes to the tree, "
                             "then adopt it again")
        dest = store.path(cs)
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, dest)
        return True
    finally:
        tmp.unlink(missing_ok=True)


def adopt_tree(home: Path, tree: FoundTree, check: TreeCheck, *, repo: str | None, label: str,
               base: str, progress=None) -> AdoptResult:
    """Adopt a checked tree into the root of `home`: its files into the store,
    each suite a point on `repo`'s track at `base` (None: a new repository
    served at https://`base`/), then serve what its timeline chooses — the
    newest complete point, unless pinned. A refused suite is skipped; a suite
    with files missing is stored incomplete. The tree is not touched."""
    import json

    from mmw.core.publisher.manifest import now_stamp
    from mmw.site.apply import write_root_nginx
    from mmw.site.ops import _render, _reserve, _session
    from mmw.site.timeline import _reader, import_points
    from mmw.site.tracks import split_tracks

    base = base.strip("/")
    new = repo is None
    repo = repo or new_repo_key(base)
    result = AdoptResult(repo, label)
    names = {t.dir: _suite_name(t.dir, t.fields) for t in tree.tracks}
    for tdir, why in check.refused.items():
        result.refused[names.get(tdir, tdir or "files")] = why
    stamp = now_stamp()
    with _session(home) as (db, roots):
        state = db.repo_state(repo)
        if state is not None:
            label = state.label
            result.label = label
        root_cfg = roots.of(state)
        roots.check_mounted(root_cfg)
        store = roots.store(root_cfg)
        _ingest(tree.path, check.present, check.stats, check.sizes, store, Path(root_cfg.path), result)
        rows = {f"{base}/{rel}": cs for rel, cs in check.present.items()}
        sizes = {cs: check.sizes[rel] for rel, cs in check.present.items()}
        for rel, owners in check.missing.items():
            want, size = check.expected[rel]
            rows[f"{base}/{rel}"] = want
            sizes.setdefault(want, size or 0)
        refused_dirs = {f"{base}/{d}".rstrip("/") for d in check.refused}
        drafts = [d for d in split_tracks(rows, [base], _reader(store, rows), stamp)
                  if d.track not in refused_dirs]
        if new and state is None:
            db.ensure_repo(repo, label, [base])
        outcomes = import_points(db, store, repo, label, [base], [(d, stamp, None) for d in drafts],
                                 sizes, set())
        verified = {f"{base}/{d}".rstrip("/") for d, s in check.signature.items() if s == "verified"}
        for d, o in zip(drafts, outcomes):
            suite = _suite_name(d.track[len(base) + 1:] if d.track != base else "", d.fields) if d.track else "files"
            if o.status == "conflict":
                result.refused[suite] = o.note
                continue
            if o.status == "incomplete":
                result.incomplete[suite] = o.missing
            if d.track and d.track not in verified and o.point is not None and o.status == "new":
                db.set_meta(unverified_key(db.point(o.point)), "1")
                result.unverified.append(suite)
            result.lines.append(f"  {label}  {suite} {d.date[:4]}-{d.date[4:6]}-{d.date[6:8]} "
                                f"{'stored, incomplete' if o.status == 'incomplete' else o.status}")
        if new and state is None:
            from mmw.core.config import MirrorConfig
            unit = MirrorConfig(name=label, provider="apt-flat" if tree.flat else "apt",
                                source_url=f"https://{base}/", source=repo, label=label)
            known = json.loads(db.get_meta("foreign_mirrors") or "{}")
            known[repo] = [unit.model_dump(mode="json")]
            db.set_meta("foreign_mirrors", json.dumps(known, sort_keys=True))
            if db.get_meta("settings"):
                write_root_nginx(db, roots)
        switched = _reserve(db, roots, repo)
        result.lines.extend(f"  {label}  {w}" for w in switched.warnings)
        _render(roots)
        db.set_meta(_ORIGINAL + str(tree.path.resolve()),
                    json.dumps({"stamp": stamp, "refused": sorted(result.refused)}))
    return result


def matching_repo(db, tree) -> tuple[str, str] | None:
    """(repository, base) already holding this tree's suite — the same
    Origin, Label and codename — where its points go; None: none does."""
    origin, label, name = tree.identity()
    if not name:
        return None
    for st in db.repo_states():
        for t in db.tracks(st.repo):
            if t.name and name in (t.codename, t.suite) and (t.origin or "") == origin \
                    and (t.label or "") == label:
                base = t.name.split("/dists/", 1)[0]
                return st.repo, next((b for b in st.bases if b == base), base)
    return None


def network_keys(home: Path) -> list[bytes]:
    """The public keys this root serves (`keys/…`): what upstream signs with."""
    from mmw.site.db import SiteDB
    from mmw.site.store import Store
    from mmw.site.home import load_site
    out = []
    store = Store(Path(load_site(home).root))
    with SiteDB(home) as db:
        for rel, cs in sorted(db.network_files().items()):
            if rel.startswith("keys/") and rel.endswith((".gpg", ".asc")) and store.has(cs):
                out.append(store.path(cs).read_bytes())
    return out


# ── a tree apply.py manages ──────────────────────────────────────────────────

ADOPTED = "adopted:"          # a repository key until the network's bundle names it


@dataclass
class ApplyTree:
    path: Path
    network: str
    created: str
    created_label: str | None
    rows: dict[str, str]        # every served path → sha256, as its manifest says


def find_apply_tree(path: Path) -> ApplyTree | None:
    """The tree at `path` if apply.py (or a publish) keeps its manifest there."""
    from mmw.core.publisher.manifest import RECORD_PATH, iter_manifest
    record = Path(path) / RECORD_PATH
    if not record.is_file():
        return None
    header, rows = iter_manifest(record)
    return ApplyTree(Path(path), header.network, header.created, header.created_label,
                     {p: c for p, c in rows if not p.startswith(".mmw/") and _safe(p)})


def apt_bases(rows: dict[str, str]) -> list[str]:
    """Each APT archive root the rows hold: a dir with `dists/*/Release`, or a
    flat repository's Release beside its Packages. Longest first."""
    index_dirs = {p.rsplit("/", 1)[0] for p in rows if "/" in p
                  and p.rsplit("/", 1)[1].startswith(("Packages", "Sources"))}
    bases: set[str] = set()
    for p in rows:
        if "/" not in p or p.rsplit("/", 1)[1] not in ("Release", "InRelease"):
            continue
        d = p.rsplit("/", 1)[0]
        if "/dists/" in f"/{d}/":
            base = f"/{d}".split("/dists/", 1)[0].lstrip("/")
            if base:
                bases.add(base)
        elif d in index_dirs:
            bases.add(d)
    return sorted(bases, key=lambda b: (-len(b), b))


def adopt_apply_tree(tree: ApplyTree, *, home: Path | None = None, progress=None) -> AdoptResult:
    """Adopt a tree apply.py manages: every file its manifest lists, hashed
    against it and linked into the store; each APT base a repository
    (`adopted:<base>`, until the network's next bundle names it), the rest the
    network's files. `home` None: in place — the tree becomes a root, its
    served files linked, never changed."""
    from mmw.site import home as H
    from mmw.site.apply import place_served_file
    from mmw.site.ops import _render, _reserve, _session
    from mmw.site.timeline import import_rows

    from mmw.site.db import SiteDB
    in_place = home is None
    if in_place:
        home = H.root_home(tree.path)
        if (home / H.SETTINGS).exists():
            with SiteDB(home) as db:
                if db.applied() or db.repo_states():
                    raise ValueError(f"{tree.path} is already an mmw mirror — nothing to convert")
        else:
            H.init_root(tree.path, name="main", set_id=H.new_set_id(), network=tree.network)
    result = AdoptResult("", tree.network)
    files = _files(tree.path)
    bases = apt_bases(tree.rows)

    def base_of(rel: str) -> str | None:
        return next((b for b in bases if rel.startswith(b + "/")), None)

    present: dict[str, str] = {}
    stats: dict = {}
    sizes: dict[str, int] = {}
    network: dict[str, str] = {}
    for rel, want in sorted(tree.rows.items()):
        st = files.get(rel)
        base = base_of(rel)
        if st is None:
            continue                                   # missing: a repository's point is incomplete
        key = _stat_key(st)
        if key not in _HASHED:
            try:
                _HASHED[key] = _hash(tree.path / rel)
            except OSError:
                continue
            if progress:
                progress(rel, st.st_size)
        got = _HASHED[key]
        if base is not None and got != want:
            result.refused.setdefault(base, f"{rel} differs from the tree's manifest")
            continue
        present[rel], stats[rel], sizes[rel] = got, key, st.st_size
        if base is None:
            network[rel] = got                         # nginx text is rewritten here: what is served counts
    with _session(home) as (db, roots):
        root_cfg = roots.primary
        roots.check_mounted(root_cfg)
        store = roots.store(root_cfg)
        _ingest(tree.path, present, stats, sizes, store, Path(root_cfg.path), result)
        blob_sizes = {present[r]: sizes[r] for r in present}
        keys = []
        serving = {b: st for st in db.repo_states() for b in st.bases}
        for base in bases:
            if base in result.refused:
                continue
            rows = {rel: cs for rel, cs in tree.rows.items() if base_of(rel) == base}
            known = serving.get(base)
            # A base this mirror already serves: its points join that repository.
            key, label = (known.repo, known.label) if known is not None else (ADOPTED + base, base)
            bases_of = list(known.bases) if known is not None else [base]
            outcomes = import_rows(db, store, key, label, bases_of, rows,
                                   {c: blob_sizes.get(c, 0) for c in rows.values()},
                                   tree.created, tree.created_label, set(), own=known is None)
            keys.append(key)
            for o in outcomes:
                suite = o.suite or "files"
                if o.status == "conflict":
                    result.refused[f"{base} {suite}"] = o.note
                elif o.status == "incomplete":
                    result.incomplete[f"{base} {suite}"] = o.missing
            result.lines.append(f"  {base}  {len(outcomes)} suite(s) adopted")
        mine = H.load_site(home).network
        if in_place or (mine == tree.network and not db.network_files()):
            db.set_network_files(network, {network[r]: sizes[r] for r in network})
            if not in_place:
                for rel, cs in sorted(network.items()):
                    place_served_file(store, Path(root_cfg.path), rel, cs, network=True)
        elif network:
            result.lines.append(f"  network {tree.network}'s own files ({len(network)}) left out — this mirror "
                                f"is {mine}'s, with its own")
        if in_place:
            db.record_applied(tree.created, None, "adopted")
        for key in keys:
            switched = _reserve(db, roots, key)
            result.lines.extend(f"  {key[len(ADOPTED):]}  {w}" for w in switched.warnings)
        _render(roots)
        if not in_place:
            db.set_meta(_ORIGINAL + str(tree.path.resolve()),
                        json.dumps({"stamp": tree.created, "refused": sorted(result.refused)}))
    if in_place:
        record = tree.path / ".mmw" / "manifest.jsonl"
        os.replace(record, record.with_name("manifest.apply.jsonl"))   # converted: never again
    result.repo = ",".join(keys)
    return result


# ── checking signatures later ────────────────────────────────────────────────

def recheck_signatures(home: Path, keyring: Path) -> list[tuple[str, str, str]]:
    """Check the Release signature of every point adopted unverified, from
    the store, with `keyring`; a verified one loses its mark. (label,
    "suite date", verdict) per point checked."""
    import tempfile

    from mmw.site.ops import _session, suite_of
    out = []
    with _session(home) as (db, roots), tempfile.TemporaryDirectory() as tmp:
        marked = set(db.metas("unverified:"))
        for st in db.repo_states():
            store = roots.store(roots.of(st))
            for t in db.tracks(st.repo):
                for p in db.points(t.id):
                    key = unverified_key(p)
                    if key not in marked:
                        continue
                    files = db.point_files(p.id)
                    d = Path(tmp) / str(p.id)
                    d.mkdir()
                    for name in ("InRelease", "Release", "Release.gpg"):
                        cs = files.get(f"{t.name}/{name}")
                        if cs is not None and store.has(cs):
                            shutil.copyfile(store.path(cs), d / name)
                    verdict = _signature(d, keyring)
                    if verdict == "verified":
                        db.delete_meta(key)
                    out.append((st.label, f"{suite_of(t)} {p.date[:4]}-{p.date[4:6]}-{p.date[6:8]}", verdict))
    return out


# ── adopted originals ────────────────────────────────────────────────────────

_ORIGINAL = "adopted_from:"


@dataclass(frozen=True)
class Original:
    path: Path
    stamp: str             # when it was adopted
    present: bool
    files: int
    frees: int             # bytes only it holds: what removing it gives back
    refused: list = field(default_factory=list)   # what wasn't adopted from it: lost with it


def record_original(home: Path, path: Path, stamp: str, refused: list | None = None) -> None:
    import json
    from mmw.site.db import SiteDB
    with SiteDB(home) as db:
        db.set_meta(_ORIGINAL + str(Path(path).resolve()), json.dumps({"stamp": stamp, "refused": refused or []}))


def _recorded(value: str) -> tuple[str, list]:
    import json
    try:
        data = json.loads(value)
    except ValueError:
        return value, []
    return (data.get("stamp", ""), list(data.get("refused", []))) if isinstance(data, dict) else (value, [])


def originals(home: Path) -> list[Original]:
    """Every tree adopted from (not in place), with what removing it frees."""
    from mmw.site.db import SiteDB
    with SiteDB(home) as db:
        recorded = db.metas(_ORIGINAL)
    out = []
    for key, value in sorted(recorded.items()):
        path = Path(key[len(_ORIGINAL):])
        stamp, refused = _recorded(value)
        if not path.is_dir():
            out.append(Original(path, stamp, False, 0, 0, refused))
            continue
        files = _files(path)
        out.append(Original(path, stamp, True, len(files),
                            sum(st.st_size for st in files.values() if st.st_nlink == 1), refused))
    return out


def _holds_a_mirror(path: Path) -> Path | None:
    """A mirror's own directory (a root's `.mmw/site` or store) or another
    filesystem mounted inside `path`, if there is one."""
    for dirpath, dirnames, _files_here in os.walk(path, followlinks=False):
        d = Path(dirpath)
        if d.name == ".mmw" and ({"site", "store"} & set(dirnames)) and d.parent != path:
            return d.parent
        if d != path and os.path.ismount(d):
            return d
    if (path / ".mmw" / "site").exists() or (path / ".mmw" / "store").exists():
        return path
    return None


def remove_original(home: Path, path: Path, other_roots: list[Path] = ()) -> int:
    """Delete an adopted tree — only one adoption recorded, never a root of
    this mirror (or `other_roots`), inside one or holding one, nor a tree
    holding any mirror at all. The bytes it freed."""
    from mmw.site.db import SiteDB
    from mmw.site.home import load_site
    path = Path(path).resolve()
    found = next((o for o in originals(home) if o.path == path), None)
    if found is None:
        raise ValueError(f"{path} is not an adopted tree — `mmw site adopted` lists them")
    for rp in [Path(r.path).resolve() for r in load_site(home).roots] + [Path(p).resolve() for p in other_roots]:
        if path == rp or rp in path.parents or path in rp.parents:
            raise ValueError(f"{path} is, is inside or holds {rp}, a root of this mirror — not removing it")
    if found.present:
        held = _holds_a_mirror(path)
        if held is not None:
            raise ValueError(f"{path} holds a mirror or a mounted filesystem ({held}) — not removing it")
        shutil.rmtree(path)
    with SiteDB(home) as db:
        db.delete_meta(_ORIGINAL + str(path))
    return found.frees
