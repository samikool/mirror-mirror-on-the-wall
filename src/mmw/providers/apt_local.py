"""`apt-local`: a folder of `.deb` files, served as a signed apt repository.

The folder is the upstream. Resolve and fetch scan it; the packages are
pooled through the ordinary downloader (`file://`), and the index files are
built here and handed to sync already filled in, signed with an mmw key. From
the pool on, it is a repository like any other: snapshots, publish, bundle.
"""
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from mmw.core import disk_io
from mmw.core.apt_index import (
    Entry, compressed, contents_text, data_paths, packages_text, read_control, release_text, rfc2822,
)
from mmw.providers.base import (
    BaseProvider, Dimension, MetadataFile, PackageInfo, SourceDescription, SourceField,
)


class AptLocalProvider(BaseProvider):
    name = "apt-local"
    label = "Local folder"
    description = "A folder of .deb files, signed and served at a URL you choose"
    # One unit carries every architecture the folder holds, so it has no arch
    # coordinate — and, like apt-flat's, absent matches: a network narrowed to
    # arm64 must not drop the folder repository.
    dimensions = (Dimension("arches", "arch", "Architectures", absent_matches=True, short="arch"),)
    publishes = True
    fields = (
        SourceField("name", "Name", "text"),
        SourceField("url", "Served at", "text",
                    hint="The address clients use, e.g. https://tools.example.lan/apt"),
        SourceField("folder", "Folder", "path", hint="The folder of .deb files; relative to mmw.toml."),
        SourceField("structured", "dists/ layout", "toggle",
                    hint="Off: a flat repository (deb URL ./). On: dists/{suite}/{component}/."),
        SourceField("suite", "Suite", "text", hint="dists/ layout only."),
        SourceField("component", "Component", "text", hint="dists/ layout only."),
        SourceField("key", "Signing key", "signing-key"),
        SourceField("contents", "apt-file support", "toggle",
                    hint="Lets apt-file find these packages. Reads every deb's file list, so "
                         "syncing and signing can take significantly longer."),
    )

    # ── presentation ─────────────────────────────────────────────────────────

    def source_summary(self, source) -> str:
        layout = f"{source.suite}/{source.component}" if source.structured else "flat"
        return f"{layout} · key {source.key}"

    def describe(self, source) -> SourceDescription:
        layout = (f"dists/{source.suite}/{source.component}" if source.structured
                  else "flat (deb URL ./)")
        return SourceDescription(
            fields=[("Provider", "apt-local"), ("Layout", layout), ("Signing key", source.key)],
            flags=["apt-file support"] if source.contents else [],
            location_label="Folder", location=source.folder)

    def unit_label(self, unit):
        if unit.dims:
            return f"{unit.dims['suite']} / {unit.dims['component']}", "folder"
        return "flat", "folder"

    # ── config ───────────────────────────────────────────────────────────────

    def to_toml(self, source) -> dict:
        from mmw.core.config import LocalAptSource, derive_label, derive_name
        defaults = LocalAptSource(provider="apt-local", url=source.url, folder="x", key="x")
        d: dict = {"url": source.url, "provider": "apt-local", "folder": source.folder,
                   "key": source.key}
        if source.name != derive_name(source.url, provider="apt-local"):
            d["name"] = source.name
        if source.label != derive_label(source.url):
            d["label"] = source.label
        for key in ("structured", "suite", "component", "contents"):
            if getattr(source, key) != getattr(defaults, key):
                d[key] = getattr(source, key)
        return d

    def expand(self, source) -> list:
        from mmw.core.config import MirrorConfig
        dims = {"suite": source.suite, "component": source.component} if source.structured else {}
        return [MirrorConfig(
            name=source.name, provider="apt-local", source_url=source.url,
            source=source.identity, label=source.label, dims=dims,
            options={"folder": source.folder, "structured": source.structured,
                     "suite": source.suite, "component": source.component,
                     "key": source.key, "contents": source.contents})]

    def covers(self, source, unit) -> bool:
        return source.covers(unit)

    # ── sync ─────────────────────────────────────────────────────────────────

    def resolve(self, source, *, progress=None, context=None):
        """Scan now, so a missing folder fails like an unreachable upstream, and
        put the signing key's public half on the unit for keys/{label}/."""
        from mmw.core.resolve import ResolvedSource
        from mmw.core.signing import Keyring, SigningError
        mirror_dir = context.mirror_dir if context is not None else None
        if mirror_dir is None:
            return ResolvedSource(source, [], [], [f"{source.name}: no mirror directory to sign in"])
        folder = source.folder_path(context.config_dir)
        keyring = Keyring(mirror_dir)
        try:
            debs = scan(folder, _work_dir(mirror_dir, source.name) / "scan.json")
            public = keyring.public_armor(source.key)
        except (ValueError, SigningError) as e:
            return ResolvedSource(source, [], [], [f"{source.name}: {e}"])
        finally:
            keyring.stop_agent()
        if progress:
            progress(f"{source.name}: {len(debs)} package(s) in {folder}")
        [unit] = self.expand(source)
        unit.options.update(folder=str(folder), mirror_dir=str(mirror_dir), gpg_key=public,
                            gpg_key_name=f"{source.key}.gpg")
        return ResolvedSource(source, [unit], [], [])

    def fetch_all(self, mirror_config, client, status_callback=None, history=None):
        return build(mirror_config)

    def verify_witness(self, unit, pool, say=None, stop_event=None):
        """Every deb each pooled `Packages` index names is in the pool."""
        from mmw.core.verify import VerifyResult, _check_checksums
        from mmw.providers.apt import _parse_apt_sha256
        result = VerifyResult(unit.name, unit.provider)
        indexes = [rel for rel in pool.ref_map(unit.name)
                   if rel == "Packages" or rel.endswith("/Packages")]
        if not indexes:
            result.error = "Packages index not in pool — has this repository been synced?"
            return result
        checksums: set[str] = set()
        for rel in indexes:
            path = pool.resolve(unit.name, rel)
            if path is None:
                result.error = f"{rel} is listed but not in the pool"
                return result
            checksums.update(_parse_apt_sha256(path))
        return _check_checksums(pool, result, sorted(checksums))

    def fetch_packages(self, mirror_config, client, status_callback=None):
        return build(mirror_config)[0]

    def fetch_metadata(self, mirror_config, client):
        return build(mirror_config)[1]


# ── client sources file ──────────────────────────────────────────────────────

SOURCES_FORMATS = ("deb822", "list")


def sources_file(source, fmt: str) -> str:
    """What a client drops into /etc/apt/sources.list.d/ — `deb822` (Ubuntu
    24.04+, Debian 12+) or `list` (one line: jammy, Debian 11) — headed by the
    command that installs the repository's key where the entry expects it."""
    from urllib.parse import urlparse

    from mmw.core.apt_sources import AptEntry, render_deb822, render_one_line
    if fmt not in SOURCES_FORMATS:
        raise ValueError(f"format must be one of {', '.join(SOURCES_FORMATS)}, not {fmt!r}")
    parsed = urlparse(source.url)
    keyring = f"/etc/apt/keyrings/{source.key}.gpg"
    key_url = f"{parsed.scheme}://{parsed.netloc}/mmw-keys/{source.label}/{source.key}.gpg"
    entry = AptEntry(source.url.rstrip("/"), source.suite if source.structured else None,
                     [source.component] if source.structured else [], [])
    render = render_deb822 if fmt == "deb822" else render_one_line
    ext = "sources" if fmt == "deb822" else "list"
    return (f"# {source.name} — served by mmw. Install the signing key first:\n"
            f"#   sudo install -d -m 0755 /etc/apt/keyrings\n"
            f"#   sudo curl -fsSL {key_url} -o {keyring}\n"
            f"# then save this file as /etc/apt/sources.list.d/{source.name}.{ext}\n"
            + render(entry, signed_by=keyring))


# ── scanning ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ScannedDeb:
    path: Path
    size: int
    mtime: float
    sha256: str
    md5: str
    control: dict[str, str]
    files: tuple[str, ...] | None = None     # data paths, read only for Contents


def _work_dir(mirror_dir, name: str) -> Path:
    return Path(mirror_dir) / "local" / name


def _hashes(path: Path) -> tuple[str, str]:
    sha, md5 = hashlib.sha256(), hashlib.md5()
    for block in disk_io.ops.iter_chunks(path):
        sha.update(block)
        md5.update(block)
    return sha.hexdigest(), md5.hexdigest()


def scan(folder: Path, cache_path: Path, *, contents: bool = False) -> list[ScannedDeb]:
    """Every `.deb` under `folder`, sorted by path. A deb whose relative path,
    size and mtime match the cache is not read again. Two debs naming the same
    package, version and architecture with different bytes are refused."""
    if not disk_io.ops.is_dir(folder):
        raise ValueError(f"folder {folder} does not exist or is not a directory")
    # Never read an unreadable folder as an empty one: that would publish an
    # empty repository and prune every deb clients rely on. rglob swallows
    # permission errors, so walk, and let a walk error stop the scan.
    if not os.access(folder, os.R_OK | os.X_OK):
        raise ValueError(f"folder {folder} cannot be read (permissions)")

    def unreadable(err: OSError):
        raise ValueError(f"folder {err.filename} cannot be read: {err.strerror}")

    paths = sorted(Path(root) / name
                   for root, _dirs, names in disk_io.ops.walk(folder, onerror=unreadable)
                   for name in names if name.endswith(".deb"))
    paths = [p for p in paths if disk_io.ops.is_file(p)]
    # Debs are served by file name, so two of one name would share a URL.
    by_name: dict[str, Path] = {}
    for p in paths:
        if p.name in by_name:
            raise ValueError(f"{by_name[p.name].relative_to(folder)} and {p.relative_to(folder)} "
                             f"share the file name {p.name} — rename one")
        by_name[p.name] = p
    cache = {}
    if disk_io.ops.exists(cache_path):
        try:
            cache = json.loads(disk_io.ops.read_bytes(cache_path))
        except ValueError:
            cache = {}
    fresh, out, seen = {}, [], {}
    for path in paths:
        rel = path.relative_to(folder).as_posix()
        st = disk_io.ops.stat(path)
        hit = cache.get(rel)
        # ctime and inode too: `cp -p` or `rsync -t` over an old deb keeps its
        # size and mtime, but a write always moves ctime.
        stamp = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "ctime_ns": st.st_ctime_ns,
                 "ino": st.st_ino}
        if hit and all(hit.get(k) == v for k, v in stamp.items()):
            entry = hit
        else:
            sha, md5 = _hashes(path)
            entry = {**stamp, "sha256": sha, "md5": md5, "control": read_control(path)}
        if contents and entry.get("files") is None:
            entry = {**entry, "files": data_paths(path)}
        fresh[rel] = entry
        deb = ScannedDeb(path, entry["size"], st.st_mtime, entry["sha256"], entry["md5"],
                         entry["control"], tuple(entry["files"]) if entry.get("files") is not None else None)
        c = deb.control
        key = (c["Package"], c["Version"], c["Architecture"])
        if key in seen:
            if seen[key].sha256 != deb.sha256:
                raise ValueError(f"{seen[key].path.relative_to(folder)} and {rel} are both "
                                 f"{key[0]} {key[1]} ({key[2]}) but differ — keep one")
            continue                                   # the same deb twice: once is enough
        seen[key] = deb
        out.append(deb)
    if fresh != cache:
        disk_io.ops.mkdir(cache_path.parent, parents=True, exist_ok=True)
        tmp = cache_path.with_name(cache_path.name + ".tmp")
        # Not sort_keys: each control keeps its field order (Package first).
        disk_io.ops.write_bytes(tmp, json.dumps(fresh).encode())
        disk_io.ops.replace(tmp, cache_path)
    return out


# ── building ─────────────────────────────────────────────────────────────────

def build(unit) -> tuple[list[PackageInfo], list[MetadataFile]]:
    """The folder as one repository snapshot: its packages, and every index
    and release file, built and signed now."""
    o = unit.options
    mirror_dir, folder = Path(o["mirror_dir"]), Path(o["folder"])
    work = _work_dir(mirror_dir, unit.name)
    debs = scan(folder, work / "scan.json", contents=o.get("contents", False))
    structured = o.get("structured", False)
    component = o.get("component", "main")
    pool_dir = f"pool/{component}/" if structured else ""
    base = unit.source_url.rstrip("/")

    packages = [PackageInfo(name=d.control["Package"], url=d.path.as_uri(), checksum=d.sha256,
                            checksum_type="sha256", size=d.size, repo_path=pool_dir + d.path.name,
                            filename=d.path.name) for d in debs]
    entries = [(d, Entry(d.control, (pool_dir or "./") + d.path.name, d.size, d.sha256, d.md5))
               for d in debs]
    state_path = work / "state.json"
    state = _read_json(state_path)
    arches = sorted({d.control["Architecture"] for d in debs} - {"all"}) or (["all"] if debs else [])
    if not debs and state.get("arches"):
        # Emptied, not new: keep serving (empty) indexes for the architectures
        # clients already fetch, or every one of them warns on update.
        arches = state["arches"]

    def for_arch(arch):
        return [e for d, e in entries if d.control["Architecture"] in (arch, "all")]

    files: dict[str, bytes] = {}          # relative to the Release's directory
    files_root: dict[str, bytes] = {}     # at the repository root, not in Release
    if structured:
        for arch in arches or ["all"]:
            files.update({f"{component}/binary-{arch}/{k}": v for k, v in
                          compressed("Packages", packages_text(for_arch(arch))).items()})
    else:
        files.update(compressed("Packages", packages_text([e for _, e in entries])))
    if o.get("contents"):
        for arch in arches or ["all"]:
            rows = [(path, f"{d.control.get('Section', 'misc')}/{d.control['Package']}")
                    for d, _ in entries if d.control["Architecture"] in (arch, "all")
                    for path in d.files or ()]
            text = contents_text(rows)
            prefix = f"{component}/" if structured else ""
            files[f"{prefix}Contents-{arch}"] = text
            files[f"{prefix}Contents-{arch}.gz"] = compressed("x", text)["x.gz"]

    label = unit.label or unit.name
    fields = {"Origin": label, "Label": label}
    if structured:
        suite = o.get("suite", "stable")
        fields.update(Suite=suite, Codename=suite, Components=component)
    fields["Architectures"] = " ".join(arches)
    # Date only ever moves forward: apt ignores a Release older than the one
    # it holds, so a Date from the newest deb went backwards when that deb was
    # removed and clients silently kept the old list. Unchanged content keeps
    # its Date, so an unchanged folder still rebuilds byte for byte.
    body = hashlib.sha256(release_text(fields, files)).hexdigest()
    if state.get("body") == body and state.get("date"):
        date = state["date"]
    else:
        date = max(int(time.time()), int(state.get("date", 0)) + 1)
    fields["Date"] = rfc2822(date)
    release = release_text(fields, files)
    if state != {"arches": arches, "body": body, "date": date}:
        _write_json(state_path, {"arches": arches, "body": body, "date": date})
    inrelease, release_gpg = _signatures(work, o["key"], mirror_dir, release)

    # The client's sources files, beside the repository, so `curl` gets them.
    source = _as_source(unit)
    for fmt, ext in (("deb822", "sources"), ("list", "list")):
        files_root[f"{unit.name}.{ext}"] = sources_file(source, fmt).encode()

    top = f"dists/{o.get('suite', 'stable')}/" if structured else ""
    metadata = [MetadataFile(url=f"{base}/{top}{rel}", repo_path=top + rel, content=body)
                for rel, body in sorted(files.items())]
    metadata += [MetadataFile(url=f"{base}/{rel}", repo_path=rel, content=body)
                 for rel, body in sorted(files_root.items())]
    metadata += [MetadataFile(url=f"{base}/{top}{rel}", repo_path=top + rel, content=body,
                              is_release=True)
                 for rel, body in (("Release", release), ("InRelease", inrelease),
                                   ("Release.gpg", release_gpg))]
    return packages, metadata


def _read_json(path: Path) -> dict:
    if not disk_io.ops.exists(path):
        return {}
    try:
        return json.loads(disk_io.ops.read_bytes(path))
    except ValueError:
        return {}


def _write_json(path: Path, data: dict) -> None:
    disk_io.ops.mkdir(path.parent, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    disk_io.ops.write_bytes(tmp, json.dumps(data, sort_keys=True).encode())
    disk_io.ops.replace(tmp, path)


def _as_source(unit):
    from mmw.core.config import LocalAptSource
    o = unit.options
    return LocalAptSource(provider="apt-local", url=unit.source_url, folder=o["folder"], key=o["key"],
                          name=unit.name, label=unit.label, structured=o.get("structured", False),
                          suite=o.get("suite", "stable"), component=o.get("component", "main"))


def _signatures(work: Path, key: str, mirror_dir: Path, release: bytes) -> tuple[bytes, bytes]:
    """InRelease and Release.gpg for `release` — the kept ones when this exact
    Release was already signed by this exact key, so an unchanged folder
    gives byte-identical files and no new repository version."""
    from mmw.core.signing import Keyring, SigningError
    keyring = Keyring(mirror_dir)
    fingerprint = next((k.fingerprint for k in keyring.list() if k.name == key), None)
    if fingerprint is None:
        raise SigningError(f"no signing key named {key!r}")
    digest = hashlib.sha256(release).hexdigest()
    kept_path = work / "signatures.json"
    if disk_io.ops.exists(kept_path):
        try:
            kept = json.loads(disk_io.ops.read_bytes(kept_path))
            if kept.get("release") == digest and kept.get("fingerprint") == fingerprint:
                return kept["inrelease"].encode(), kept["release_gpg"].encode()
        except (ValueError, KeyError):
            pass
    try:
        inrelease, release_gpg = keyring.clearsign(key, release), keyring.detach_sign(key, release)
    finally:
        keyring.stop_agent()        # a cron sync must not leave a daemon behind
    disk_io.ops.mkdir(work, parents=True, exist_ok=True)
    tmp = kept_path.with_name("signatures.json.tmp")
    disk_io.ops.write_bytes(tmp, json.dumps({
        "release": digest, "fingerprint": fingerprint,
        "inrelease": inrelease.decode(), "release_gpg": release_gpg.decode()}).encode())
    disk_io.ops.replace(tmp, kept_path)
    return inrelease, release_gpg
