"""Per-network layers: custom debs a network adds to an upstream repository.

The pool keeps upstream pure. A layer is built (with the pool writable) into
replacement rows — the layer's debs, the rewritten `Packages`/`Contents`
indexes, the rewritten `Release` and its new or dropped signatures — stored
per (network, base) in `layer_rows`. Publish and snapshots walk the pool
read-only and swap those rows into the group (`publish_group(overlay=…)`),
so ordering, by-hash, prune, snapshots and bundles carry what is served.
"""
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from mmw.core import disk_io
from mmw.core.pool import KIND_META, KIND_PKG, KIND_RELEASE

LAYERS_DIR = "layers"


def layer_dir(mirror_dir: Path, network: str, repo_name: str) -> Path:
    """Where a network's custom debs for one repository live:
    `{suite}/{component}/*.deb` below it."""
    return Path(mirror_dir) / LAYERS_DIR / network / repo_name


_RELEASE_NAMES = ("Release", "InRelease", "Release.gpg")


@dataclass(frozen=True)
class Clash:
    repo: str
    suite: str
    component: str
    filename: str
    package: str
    version: str
    arch: str
    kind: str                    # "same-version" | "shadowed"
    upstream_version: str = ""


@dataclass
class LayerReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    clashes: list[Clash] = field(default_factory=list)    # undecided — a person should choose


def _work(mirror_dir: Path, network: str, name: str) -> Path:
    return Path(mirror_dir) / "local" / LAYERS_DIR / network / name


def read_decisions(folder: Path) -> dict[str, str]:
    path = folder / "decisions.json"
    if not disk_io.ops.exists(path):
        return {}
    try:
        return json.loads(disk_io.ops.read_bytes(path))
    except ValueError:
        return {}


def write_decisions(folder: Path, decisions: dict[str, str]) -> None:
    disk_io.ops.mkdir(folder, parents=True, exist_ok=True)
    tmp = folder / "decisions.json.tmp"
    disk_io.ops.write_bytes(tmp, json.dumps(decisions, indent=1, sort_keys=True).encode())
    disk_io.ops.replace(tmp, folder / "decisions.json")


def layer_debs(mirror_dir: Path, network: str, name: str) -> dict[tuple[str, str], list]:
    """(suite, component) → the scanned debs of one repository's layer."""
    from mmw.providers.apt_local import scan
    folder = layer_dir(mirror_dir, network, name)
    out: dict[tuple[str, str], list] = {}
    if not disk_io.ops.is_dir(folder):
        return out
    for suite_dir in sorted(p for p in disk_io.ops.iterdir(folder) if disk_io.ops.is_dir(p)):
        for comp_dir in sorted(p for p in disk_io.ops.iterdir(suite_dir) if disk_io.ops.is_dir(p)):
            cache = _work(mirror_dir, network, name) / f"scan-{suite_dir.name}-{comp_dir.name}.json"
            debs = scan(comp_dir, cache)
            if debs:
                out[(suite_dir.name, comp_dir.name)] = debs
    return out


def _published(rows: dict) -> dict[str, dict[str, set[str]]]:
    """suite → component → arches whose Packages the network publishes."""
    out: dict[str, dict[str, set[str]]] = {}
    for rel in rows:
        parts = rel.split("/")
        if len(parts) >= 5 and parts[0] == "dists" and parts[3].startswith("binary-") \
                and parts[4].startswith("Packages"):
            out.setdefault(parts[1], {}).setdefault(parts[2], set()).add(parts[3][len("binary-"):])
    return out


def _read_index(rows: dict, stem: str) -> bytes | None:
    """The decompressed text of an upstream index from its best pooled variant."""
    from mmw.providers.apt import _decompress
    for ext in (".xz", ".gz", ""):
        row = rows.get(stem + ext)
        if row is not None:
            return _decompress(disk_io.ops.read_bytes(Path(row[1])), stem + ext)
    return None


def _release_of(rows: dict, suite: str) -> bytes | None:
    from mmw.core.apt_index import release_body
    for name in ("Release", "InRelease"):
        row = rows.get(f"dists/{suite}/{name}")
        if row is not None:
            return release_body(disk_io.ops.read_bytes(Path(row[1])))
    return None


def build_network_layers(network, cfg, mirror_dir: Path, pool, *,
                         decisions: dict[str, str] | None = None) -> LayerReport:
    """Build (or skip, when unchanged) every layer of `network` into the pool's
    `layer_rows`. Needs the pool writable. Errors name the repository; a
    repository in error keeps no layer rows (it publishes pure upstream)."""
    from mmw.core.publisher.links import _dedup_rows
    from mmw.core.resolve import network_mirrors
    report = LayerReport()
    units = [m for m in network_mirrors(network, cfg, mirror_dir) if m.provider == "apt"]
    by_source: dict[str, list] = {}
    for u in units:
        by_source.setdefault(u.source, []).append(u)
    sources = {s.identity: s for s in cfg.sources}
    built: set[str] = set()
    for ident, members in by_source.items():
        source = sources.get(ident)
        if source is None:
            continue
        base = members[0].mirror_base
        layer = network.layer_for(source.url)
        debs = layer_debs(mirror_dir, network.name, source.name)
        if not debs and (layer is None or (layer.signing == "upstream" and not layer.drop_valid_until)):
            continue
        if debs and (layer is None or layer.signing == "upstream"):
            report.errors.append(
                f"{network.name}: {source.name} has custom packages, so its upstream signature "
                "cannot stay — choose resign or unsigned for it (resign signs with an mmw key)")
            continue
        rows = {r[0]: r for r in _dedup_rows(pool.mirror_files([m.name for m in members]))}
        published = _published(rows)
        missing = [f"{s}/{c}" for s, c in debs if c not in published.get(s, {})]
        if missing:
            report.errors.append(f"{network.name}: {source.name} has custom packages for "
                                 f"{', '.join(missing)}, which this network does not publish")
            continue
        folder = layer_dir(mirror_dir, network.name, source.name)
        chosen = {**read_decisions(folder), **(decisions or {})}
        fingerprint = ""
        if layer.signing == "resign":
            from mmw.core.signing import Keyring, SigningError
            keyring = Keyring(mirror_dir)
            try:
                fingerprint = next((k.fingerprint for k in keyring.list() if k.name == layer.key), "")
            except SigningError as e:
                report.errors.append(f"{network.name}: {source.name}: {e}")
                continue
            finally:
                keyring.stop_agent()
            if not fingerprint:
                report.errors.append(f"{network.name}: {source.name}: no signing key named {layer.key!r}")
                continue
            # Clients fetch it from /mmw-keys/{label}/ beside upstream's own key.
            from mmw.core.keys import write_key
            try:
                write_key(source.label, keyring.public_armor(layer.key), mirror_dir, layer.key)
            finally:
                keyring.stop_agent()
        digest = hashlib.sha256(json.dumps({
            "upstream": sorted((rel, r[4]) for rel, r in rows.items()
                               if rel.startswith("dists/") and rel.rsplit("/", 1)[-1] in _RELEASE_NAMES),
            "debs": sorted((s, c, d.path.name, d.sha256) for (s, c), ds in debs.items() for d in ds),
            "layer": layer.model_dump(mode="json"), "key": fingerprint, "decisions": chosen,
        }, sort_keys=True).encode()).hexdigest()
        built.add(base)
        clashes = _clashes(source.name, rows, published, debs, chosen)
        report.clashes += [c for c in clashes if c.filename not in chosen]
        for c in clashes:
            if c.filename not in chosen:
                what = ("the same version as upstream's, with different contents"
                        if c.kind == "same-version" else f"older than upstream's {c.upstream_version}")
                report.warnings.append(f"{network.name}: {source.name}: {c.filename} ({c.package} "
                                       f"{c.version}) is {what} — kept; decide with `mmw packages add "
                                       "--on-clash`")
        if layer.signing == "unsigned":
            report.warnings.append(
                f"{network.name}: {source.name} is served unsigned — this is unsafe; clients "
                "need [trusted=yes] for it")
        if pool.layer_digest(network.name, base) == digest:
            continue
        dropped = {c.filename for c in clashes if chosen.get(c.filename) == "drop"}
        try:
            out = _rebuild(network.name, source.name, layer, rows, published, debs, dropped,
                           mirror_dir, pool)
        except Exception as e:  # noqa: BLE001 — one repository's failure is reported, not fatal
            report.errors.append(f"{network.name}: {source.name}: layer not built: {e}")
            pool.clear_layer(network.name, base)
            continue
        pool.replace_layer(network.name, base, out, digest)
    for base in pool.layer_bases(network.name) - built:
        pool.clear_layer(network.name, base)             # a layer that went away
    return report


def _clashes(name, rows, published, debs, chosen) -> list[Clash]:
    from mmw.core.apt_index import parse_packages, version_cmp
    out = []
    for (suite, comp), ds in debs.items():
        for arch in sorted(published[suite][comp]):
            text = _read_index(rows, f"dists/{suite}/{comp}/binary-{arch}/Packages")
            upstream = parse_packages(text.decode("utf-8", errors="replace")) if text else []
            for d in ds:
                c = d.control
                if c["Architecture"] not in (arch, "all"):
                    continue
                for u in upstream:
                    if u.get("Package") != c["Package"] or u.get("Architecture") not in (arch, "all"):
                        continue
                    cmp = version_cmp(u.get("Version", ""), c["Version"])
                    if cmp == 0 and u.get("SHA256") != d.sha256:
                        kind = "same-version"
                    elif cmp > 0:
                        kind = "shadowed"
                    else:
                        continue
                    clash = Clash(name, suite, comp, d.path.name, c["Package"], c["Version"], arch,
                                  kind, u.get("Version", ""))
                    if all(o.filename != clash.filename for o in out):
                        out.append(clash)
    return out


def _rebuild(network: str, name: str, layer, rows, published, debs, dropped, mirror_dir, pool):
    """The replacement rows for one repository: debs, indexes, releases."""
    from mmw.core.apt_index import Entry, compressed, contents_text, packages_text, rewrite_release
    out: list[tuple] = []

    def add_blob(rel: str, body: bytes, kind: str) -> None:
        digest = hashlib.sha256(body).hexdigest()
        filename = rel.rsplit("/", 1)[-1]
        if not pool.contains(digest):
            pool.add(body, digest, "sha256", filename, len(body))
        out.append((rel, digest, len(body), kind, filename))

    changed: dict[str, dict[str, bytes]] = {}           # suite → path (under dists/suite/) → bytes
    for (suite, comp), ds in sorted(debs.items()):
        ds = [d for d in ds if d.path.name not in dropped]
        if not ds:
            continue
        for d in ds:
            rel = f"pool/mmw/{comp}/{d.path.name}"
            if not pool.contains(d.sha256):
                _pool_file(pool, d)
            out.append((rel, d.sha256, d.size, KIND_PKG, d.path.name))
        entries = {d.path.name: Entry(d.control, f"pool/mmw/{comp}/{d.path.name}", d.size, d.sha256, d.md5)
                   for d in ds}
        for arch in sorted(published[suite][comp]):
            mine = [entries[d.path.name] for d in ds if d.control["Architecture"] in (arch, "all")]
            if not mine:
                continue
            stem = f"dists/{suite}/{comp}/binary-{arch}/Packages"
            upstream = (_read_index(rows, stem) or b"").rstrip(b"\n")
            text = (upstream + b"\n\n" if upstream else b"") + packages_text(mine)
            for fname, body in compressed("Packages", text).items():
                changed.setdefault(suite, {})[f"{comp}/binary-{arch}/{fname}"] = body
            if layer.contents:
                ours = [(p, f"{d.control.get('Section', 'misc')}/{d.control['Package']}")
                        for d in ds if d.control["Architecture"] in (arch, "all")
                        for p in _data_paths(d)]
                where = _contents_path(rows, suite, comp, arch)
                existing = _read_index(rows, f"dists/{suite}/{where}") or b""
                merged = [tuple(line.rsplit(None, 1)) for line in
                          existing.decode("utf-8", errors="replace").splitlines() if line.strip()]
                owners = [(p, o) for p, os_ in merged for o in os_.split(",")]
                changed.setdefault(suite, {})[f"{where}.gz"] = compressed("x", contents_text(owners + ours))["x.gz"]
    for suite, files in changed.items():
        for rel, body in sorted(files.items()):
            add_blob(f"dists/{suite}/{rel}", body, KIND_META)
    suites = set(changed) | ({s for s in published} if layer.signing == "resign" or layer.drop_valid_until
                             else set())
    for suite in sorted(suites):
        upstream = _release_of(rows, suite)
        if upstream is None:
            raise ValueError(f"dists/{suite} has no Release in the pool")
        release = rewrite_release(upstream, changed.get(suite, {}), drop_valid_until=layer.drop_valid_until)
        add_blob(f"dists/{suite}/Release", release, KIND_RELEASE)
        if layer.signing == "resign":
            from mmw.providers.apt_local import _signatures
            inrelease, gpg = _signatures(_work(mirror_dir, network, name) / suite, layer.key,
                                         mirror_dir, release)
            add_blob(f"dists/{suite}/InRelease", inrelease, KIND_RELEASE)
            add_blob(f"dists/{suite}/Release.gpg", gpg, KIND_RELEASE)
        else:                                           # unsigned: nothing may vouch for the change
            for sig in ("InRelease", "Release.gpg"):
                out.append((f"dists/{suite}/{sig}", "", 0, "", ""))
    return out


def _contents_path(rows, suite: str, comp: str, arch: str) -> str:
    """Where this suite keeps Contents (under dists/suite/): beside the
    component (Ubuntu 20.04+), else at the suite (older), else beside the
    component, new."""
    if f"dists/{suite}/{comp}/Contents-{arch}.gz" in rows:
        return f"{comp}/Contents-{arch}"
    if f"dists/{suite}/Contents-{arch}.gz" in rows:
        return f"Contents-{arch}"
    return f"{comp}/Contents-{arch}"


def _data_paths(deb) -> tuple[str, ...]:
    from mmw.core.apt_index import data_paths
    return deb.files if deb.files is not None else tuple(data_paths(deb.path))


def _pool_file(pool, deb) -> None:
    """Copy a layer deb into the pool (the downloader's file:// path: hashed,
    checked, never moved — the folder is the user's)."""
    from mmw.core.downloader import Status, _download_sync
    from mmw.providers.base import PackageInfo
    pkg = PackageInfo(name=deb.control["Package"], url=deb.path.as_uri(), checksum=deb.sha256,
                      checksum_type="sha256", size=deb.size, repo_path=deb.path.name,
                      filename=deb.path.name)
    result = _download_sync(pkg, pool, None, None)
    if result.status is not Status.DOWNLOADED:
        raise ValueError(f"{deb.path}: {result.error or 'could not be pooled'}")


def apply_build(network, cfg, mirror_dir: Path, pool, result) -> LayerReport:
    """Build `network`'s layers before a walk, folding what it found into the
    walk's result: warnings as warnings, errors as a failed "layers" entry
    (a snapshot then refuses, a publish reports it and serves pure upstream
    for that repository)."""
    from mmw.core.publisher.results import MirrorPublishResult
    report = build_network_layers(network, cfg, mirror_dir, pool)
    result.warnings.extend(report.warnings)
    if report.errors:
        r = MirrorPublishResult(mirror_name="layers")
        r.errors.extend(report.errors)
        result.mirrors.append(r)
    return report


# ── adding, removing and listing packages (CLI and TUI share these) ──────────

class PackagesError(ValueError):
    """A refused packages operation; `usage` = the caller asked wrongly (exit 2)."""

    def __init__(self, message: str, *, usage: bool = False):
        super().__init__(message)
        self.usage = usage


def needs_signing(name: str) -> str:
    return (f"Adding packages to {name} changes its index, so its upstream signature cannot stay. "
            "Choose --resign KEY (clients then trust that mmw key for it instead) or --unsigned "
            "(unsafe: clients need [trusted=yes] for it).")


@dataclass
class AddOutcome:
    cfg: object
    copied: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)     # already there, same bytes
    clashes: list[Clash] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def collect_debs(paths) -> list[Path]:
    debs: list[Path] = []
    for p in map(Path, paths):
        if disk_io.ops.is_dir(p):
            debs += sorted(q for q in disk_io.ops.rglob(p, "*.deb") if disk_io.ops.is_file(q))
        elif p.suffix == ".deb" and disk_io.ops.is_file(p):
            debs.append(p)
        else:
            raise PackagesError(f"{p} is not a .deb file or a folder of them")
    if not debs:
        raise PackagesError("no .deb files found in " + ", ".join(map(str, paths)))
    return debs


def find_source(cfg, name: str):
    source = next((s for s in cfg.sources if s.name == name), None)
    if source is None:
        raise PackagesError(f"no repository named {name!r}")
    if source.provider not in ("apt", "apt-local"):
        raise PackagesError(f"{name} is not an apt repository")
    return source


def find_network(cfg, name: str | None):
    if not name:
        raise PackagesError("an upstream repository takes packages per network — pass --network",
                            usage=True)
    net = next((n for n in cfg.networks if n.name == name), None)
    if net is None:
        raise PackagesError(f"no network named {name!r}")
    return net


def published_dims(network, cfg, mirror_dir: Path, source) -> dict[str, dict[str, set[str]]]:
    """suite → component → arches of `source` that `network` publishes."""
    from mmw.core.resolve import network_mirrors
    out: dict[str, dict[str, set[str]]] = {}
    for u in network_mirrors(network, cfg, mirror_dir):
        if u.source == source.identity and u.dims.get("suite") and u.dims.get("component"):
            out.setdefault(u.dims["suite"], {}).setdefault(u.dims["component"], set()).add(u.dims.get("arch", ""))
    return out


def _scanned(path: Path):
    from mmw.core.apt_index import read_control
    from mmw.providers.apt_local import ScannedDeb, _hashes
    st = disk_io.ops.stat(path)
    sha, md5 = _hashes(path)
    return ScannedDeb(path, st.st_size, st.st_mtime, sha, md5, read_control(path))


def _copy_in(debs: list[Path], dest: Path, outcome: AddOutcome) -> None:
    disk_io.ops.mkdir(dest, parents=True, exist_ok=True)
    for deb in debs:
        target = dest / deb.name
        if disk_io.ops.exists(target):
            from mmw.providers.apt_local import _hashes
            if _hashes(target)[0] == _hashes(deb)[0]:
                outcome.skipped.append(deb)
                continue
            raise PackagesError(f"{target} already exists with different contents — remove it first")
        disk_io.ops.copy_file(deb, target)
        outcome.copied.append(target)


def add_packages(cfg, mirror_dir: Path, paths, *, into: str, network: str | None = None,
                 suite: str | None = None, component: str | None = None, signing: str | None = None,
                 key: str | None = None, on_clash: str | None = None, ask=None,
                 config_dir: Path | None = None) -> AddOutcome:
    """Copy debs into a folder repository, or into a network's layer of an
    upstream repository (creating its layer entry from `signing`/`key`).
    `ask(clash) -> "keep" | "drop" | "cancel"` decides clashes when given;
    else `on_clash`, else keep (with a note). Returns the new config."""
    debs = collect_debs(paths)
    source = find_source(cfg, into)
    outcome = AddOutcome(cfg)
    if source.provider == "apt-local":
        if network or suite or component or signing:
            raise PackagesError(f"{into} is a folder repository: no --network, --suite, --component "
                                "or signing — its own settings decide", usage=True)
        _copy_in(debs, source.folder_path(config_dir), outcome)
        outcome.notes.append(f"sync {source.name} to publish them")
        return outcome
    net = find_network(cfg, network)
    layer = net.layer_for(source.url)
    if layer is None or layer.signing == "upstream":
        if signing not in ("resign", "unsigned"):
            raise PackagesError(needs_signing(source.name), usage=True)
    if signing is not None:
        from mmw.core.config import NetworkLayer
        layer = NetworkLayer(**{**(layer.model_dump() if layer else {"source": source.url}),
                                "signing": signing, "key": key if signing == "resign" else None})
    if layer.signing == "resign":
        from mmw.core.signing import Keyring
        keyring = Keyring(mirror_dir)
        try:
            if layer.key not in {k.name for k in keyring.list()}:
                raise PackagesError(f"no signing key named {layer.key!r} — make one with "
                                    f"`mmw signing-key generate {layer.key}`")
        finally:
            keyring.stop_agent()
    dims = published_dims(net, cfg, mirror_dir, source)
    suite = suite or (sorted(dims)[0] if dims else None)
    component = component or "main"
    if suite is None or component not in dims.get(suite, {}):
        raise PackagesError(f"{net.name} does not publish {source.name} {suite}/{component} — pick a "
                            "suite and component it publishes")
    folder = layer_dir(mirror_dir, net.name, source.name)
    decisions = read_decisions(folder)
    from mmw.core.pool import Pool
    from mmw.core.publisher.links import _dedup_rows
    from mmw.core.resolve import network_mirrors
    names = [u.name for u in network_mirrors(net, cfg, mirror_dir) if u.source == source.identity]
    with Pool(mirror_dir, read_only=True) as pool:
        rows = {r[0]: r for r in _dedup_rows(pool.mirror_files(names))}
    clashes = _clashes(source.name, rows, {suite: {component: dims[suite][component]}},
                       {(suite, component): [_scanned(d) for d in debs]}, {})
    for clash in clashes:
        choice = ask(clash) if ask is not None else (on_clash or "keep")
        if choice == "cancel":
            raise PackagesError("cancelled — nothing was added")
        decisions[clash.filename] = choice
        what = ("the same version as upstream's, with different contents" if clash.kind == "same-version"
                else f"older than upstream's {clash.upstream_version}")
        outcome.notes.append(f"{clash.filename} ({clash.package} {clash.version}) is {what} — {choice}")
    outcome.clashes = clashes
    _copy_in(debs, folder / suite / component, outcome)
    if clashes:
        write_decisions(folder, decisions)
    layers = [la for la in net.layers if la.identity != layer.identity] + [layer]
    new_net = net.model_copy(update={"layers": layers})
    outcome.cfg = cfg.model_copy(update={"networks": [new_net if n.name == net.name else n
                                                      for n in cfg.networks]})
    if layer.signing == "unsigned":
        outcome.notes.append(f"{source.name} will be served unsigned on {net.name} — unsafe; clients "
                             "need [trusted=yes] for it")
    outcome.notes.append(f"publish (or bundle) {net.name} to serve them")
    return outcome


def remove_package(cfg, mirror_dir: Path, filename: str, *, into: str, network: str | None = None,
                   config_dir: Path | None = None) -> list[Path]:
    source = find_source(cfg, into)
    if source.provider == "apt-local":
        folder = source.folder_path(config_dir)
    else:
        folder = layer_dir(mirror_dir, find_network(cfg, network).name, source.name)
    found = [p for p in disk_io.ops.rglob(folder, filename)] if disk_io.ops.is_dir(folder) else []
    if not found:
        raise PackagesError(f"{filename} is not in {into}")
    for p in found:
        disk_io.ops.unlink(p)
    return found


def list_packages(cfg, mirror_dir: Path, network: str | None = None,
                  config_dir: Path | None = None) -> list[tuple[str, str, str]]:
    """(where, location, file name) for every custom deb: folder repositories
    (unless a network is named), then each network's layers."""
    out = []
    if network is None:
        for s in cfg.sources:
            if s.provider == "apt-local":
                folder = s.folder_path(config_dir)
                if disk_io.ops.is_dir(folder):
                    out += [(s.name, "folder", p.name) for p in sorted(disk_io.ops.rglob(folder, "*.deb"))]
    for net in cfg.networks:
        if network is not None and net.name != network:
            continue
        for s in cfg.sources:
            folder = layer_dir(mirror_dir, net.name, s.name)
            if disk_io.ops.is_dir(folder):
                out += [(f"{net.name}: {s.name}", f"{p.parent.parent.name}/{p.parent.name}", p.name)
                        for p in sorted(disk_io.ops.rglob(folder, "*.deb"))]
    return out


def set_signing(cfg, network: str, into: str, *, signing: str | None = None, key: str | None = None,
                contents: bool | None = None, drop_valid_until: bool | None = None):
    from mmw.core.config import NetworkLayer
    source = find_source(cfg, into)
    if source.provider != "apt":
        raise PackagesError(f"{into} is not an upstream mirror — a folder repository is always signed")
    net = find_network(cfg, network)
    layer = net.layer_for(source.url)
    data = layer.model_dump() if layer else {"source": source.url}
    if signing is not None:
        data.update(signing=signing, key=key if signing == "resign" else None)
    if contents is not None:
        data["contents"] = contents
    if drop_valid_until is not None:
        data["drop_valid_until"] = drop_valid_until
    new = NetworkLayer(**data)
    layers = [la for la in net.layers if la.identity != new.identity] + [new]
    new_net = net.model_copy(update={"layers": layers})
    return cfg.model_copy(update={"networks": [new_net if n.name == net.name else n for n in cfg.networks]})


def layer_sources_file(cfg, mirror_dir: Path, network: str, into: str, fmt: str) -> str:
    """What a client of `network` puts in sources.list.d/ for a layered
    upstream repository: one entry per published suite, signed-by the mmw
    key (resign) or trusted=yes (unsigned, and said to be unsafe)."""
    from urllib.parse import urlparse

    from mmw.core.apt_sources import AptEntry, render_deb822, render_one_line
    from mmw.providers.apt_local import SOURCES_FORMATS
    if fmt not in SOURCES_FORMATS:
        raise PackagesError(f"format must be one of {', '.join(SOURCES_FORMATS)}, not {fmt!r}", usage=True)
    source = find_source(cfg, into)
    net = find_network(cfg, network)
    layer = net.layer_for(source.url)
    if layer is None or layer.signing == "upstream":
        raise PackagesError(f"{source.name} has no layer on {net.name}: its clients use upstream's "
                            "own sources file and key")
    dims = published_dims(net, cfg, mirror_dir, source)
    parsed = urlparse(source.url)
    keyring = f"/etc/apt/keyrings/{layer.key}.gpg"
    head = [f"# {source.name} as {net.name} serves it — with custom packages."]
    if layer.signing == "resign":
        head += ["# Signed by mmw's key, not upstream's. Install it first:",
                 "#   sudo install -d -m 0755 /etc/apt/keyrings",
                 f"#   sudo curl -fsSL {parsed.scheme}://{parsed.netloc}/mmw-keys/{source.label}/"
                 f"{layer.key}.gpg -o {keyring}"]
    else:
        head += ["# UNSIGNED — this is unsafe: apt cannot tell these files came from you."]
    render = render_deb822 if fmt == "deb822" else render_one_line
    body = []
    for suite in sorted(dims):
        entry = AptEntry(source.url.rstrip("/"), suite, sorted(dims[suite]), [])
        body.append(render(entry, signed_by=keyring if layer.signing == "resign" else None,
                           trusted=layer.signing == "unsigned"))
    return "\n".join(head) + "\n" + ("\n" if fmt == "deb822" else "").join(body)
