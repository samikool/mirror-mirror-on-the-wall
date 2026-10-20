"""VscodeProvider — the Microsoft Marketplace as an mmw provider.

Units are (kind × platform [× quality]). Resolving builds the extension
catalog once per sync (top N ∪ ids ∪ all, plus dependencies and pack
members); each extensions unit's `fetch_all` reads it (Task 6).
"""
import copy
import gzip
import hashlib
import json
import posixpath
import threading
from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit

import httpx

from mmw.core import disk_io
from mmw.core.http import retrying_client

from mmw.core.config import ExtensionSelection
from mmw.core.history import UnitHistory
from mmw.core.pool import KIND_PKG
from mmw.providers.base import BaseProvider, Dimension, MetadataFile, PackageInfo, ProgressUnit, Site, SourceDescription, SourceField
from mmw.providers.vscode import marketplace as mp
from mmw.providers.vscode import image as gallery_image
from mmw.providers.vscode import publish as vpub
from mmw.providers.vscode import updates
from mmw.providers.vscode.platforms import KINDS, QUALITIES, TARGET_PLATFORMS, builds


def _client() -> httpx.Client:
    return retrying_client(follow_redirects=True, timeout=60)


MALICIOUS_URL = "https://main.vscode-cdn.net/extensions/marketplace.json"
# What the stable workbench fetches from its CDN at startup: the alerts
# (upstream has them for stable only; insider 404s), the chat participant
# registry, profile templates and the MCP server list. Each 404 offline is a
# logged warning; each is a few KB.
WORKBENCH_URLS = tuple(f"https://main.vscode-cdn.net/{p}" for p in (
    "core/stable.json", "extensions/chat.json", "core/profile-templates.json",
    "mcp/servers.json"))
IMAGE_CACHE = "vscode-gallery-image"      # under mirror_dir: the saved python image


# The witness memo is a function of this file's code as well as of checksums.
_WITNESS_CODE = hashlib.sha256(disk_io.ops.read_bytes(Path(__file__))).hexdigest()


def _named_assets(path: str, data) -> list[str]:
    """The published files one metadata JSON names."""
    if not isinstance(data, dict):
        return []
    if path.startswith(".vscode-meta/extensions/"):
        urls = [f.get("source", "") for v in data.get("versions", [])
                for f in v.get("files", [])]
    else:
        urls = [data.get("url", "")]
    return sorted({rel for rel in map(asset_repo_path, urls) if rel})


def _load_witness_memo(path: Path) -> dict[str, list[str]]:
    try:
        saved = json.loads(gzip.decompress(disk_io.ops.read_bytes(path)))
    except (OSError, ValueError, EOFError):
        return {}
    return saved.get("named", {}) if saved.get("code") == _WITNESS_CODE else {}


def _save_witness_memo(path: Path, named: dict[str, list[str]]) -> None:
    try:
        disk_io.ops.mkdir(path.parent, parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        disk_io.ops.write_bytes(tmp, gzip.compress(json.dumps(
            {"code": _WITNESS_CODE, "named": named}).encode(), 1))
        disk_io.ops.replace(tmp, path)
    except OSError:
        pass                    # an optimisation; the check stands without it


def asset_repo_path(url: str) -> str | None:
    """`{host}/{path}` of an upstream URL — where it is published (stage 4) —
    or None when it is not a plain https/http file URL a tree can hold at
    exactly one path: no query, fragment, port, credentials or backslash, and
    a path already in normal form (`a/./b`, `a//b`, `a/../b` and a trailing
    `/` would let two URLs alias one published file)."""
    from mmw.core.downloader import is_safe_repo_path
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.query \
            or parts.fragment or parts.port or parts.username or parts.password \
            or "\\" in url:
        return None
    path = parts.path.lstrip("/")
    if not path or path.endswith("/") or posixpath.normpath(path) != path:
        return None
    rel = f"{parts.hostname.lower()}/{path}"
    if not is_safe_repo_path(path) or not is_safe_repo_path(rel):
        return None
    return rel


def _json_meta(repo_path: str, obj, url: str = "") -> MetadataFile:
    content = json.dumps(obj, sort_keys=True).encode()
    return MetadataFile(url=url, repo_path=repo_path, content=content,
                        expected_sha256=hashlib.sha256(content).hexdigest())


def _selection_text(sel) -> str:
    parts = []
    if sel.all:
        parts.append("all")
    if sel.top_n:
        parts.append(f"top {sel.top_n}")
    if sel.ids:
        parts.append(f"{len(sel.ids)} listed")
    return " + ".join(parts) or "none"


class VscodeProvider(BaseProvider):
    name = "vscode"
    label = "VS Code"
    description = "Extensions, desktop builds and server builds from the Microsoft Marketplace"
    # `platforms` passes a unit with no platform: the universal-extensions
    # unit belongs to every platform. `qualities` passes extension units.
    dimensions = (
        Dimension("kinds", "kind", "Kinds"),
        Dimension("platforms", "platform", "Platforms", absent_matches=True),
        Dimension("qualities", "quality", "Qualities", absent_matches=True),
    )
    publishes = True
    carries_history = True
    retries_failures = True
    site_per_base = False
    fields = (
        SourceField("name", "Name", "text"),
        SourceField("label", "Label", "text"),
        SourceField("kinds", "Kinds", "multi", KINDS, "Empty means all kinds."),
        SourceField("platforms", "Platforms", "multi", TARGET_PLATFORMS, "Empty means all platforms."),
        SourceField("qualities", "Qualities", "multi", QUALITIES, "Empty means stable and insider."),
        SourceField("extensions.top_n", "Top N", "optional-positive-int", hint="Most-installed extensions; blank disables."),
        SourceField("extensions.ids", "Extension IDs", "textarea", hint="publisher.name, one per line; # comments allowed."),
        SourceField("extensions.all", "All extensions", "toggle", hint="Very large: may take substantial disk space and time."),
        SourceField("extensions.prerelease", "Prerelease", "toggle", hint="Allow prerelease extension versions."),
    )

    def group(self, source):
        return "VS Code · Marketplace"

    def source_summary(self, source):
        return (f"{', '.join(source.kinds) or 'all kinds'} · "
                f"{', '.join(source.platforms) or 'all platforms'} · "
                f"{_selection_text(source.extensions)} extensions")

    def search_text(self, source):
        return " ".join((super().search_text(source), self.group(source), self.source_summary(source),
                         *source.extensions.ids))

    def __init__(self):
        self.catalog: dict[str, dict[str, dict]] = {}
        self._lock = threading.Lock()

    # ── config → units ──────────────────────────────────────────────────────

    @staticmethod
    def _effective(source):
        return (source.kinds or list(KINDS), source.platforms or list(TARGET_PLATFORMS),
                source.qualities or list(QUALITIES))

    def expand(self, source) -> list:
        """Extensions: one unit for universal builds (no platform coordinate,
        so every platform filter passes it and each universal file is fetched
        once) plus one per platform for platform-specific builds. Desktop and
        server: one per platform × quality that has builds."""
        from mmw.core.config import MirrorConfig
        kinds, platforms, qualities = self._effective(source)
        units = []
        common = dict(provider="vscode", source_url=source.url, source=source.identity,
                      label=source.label)
        for kind in kinds:
            if kind == "extensions":
                units.append(MirrorConfig(
                    name=f"{source.name}-extensions-universal", dims={"kind": kind},
                    options={"prerelease": source.extensions.prerelease, "platforms": platforms},
                    **common))
                units += [MirrorConfig(
                    name=f"{source.name}-extensions-{platform}",
                    dims={"kind": kind, "platform": platform},
                    options={"prerelease": source.extensions.prerelease}, **common)
                    for platform in platforms]
                continue
            for platform in platforms:
                if not builds(kind, platform):
                    continue
                units += [MirrorConfig(
                    name=f"{source.name}-{kind}-{platform}-{quality}",
                    dims={"kind": kind, "platform": platform, "quality": quality}, **common)
                    for quality in qualities]
        return units

    def covers(self, source, unit) -> bool:
        return source.covers(unit)

    def needs_resolving(self, source) -> bool:
        return "extensions" in self._effective(source)[0]

    def resolve(self, source, *, progress=None, context=None):
        from mmw.core.resolve import ResolvedSource
        say = progress or (lambda _m: None)
        units = self.expand(source)
        builds_only = [u for u in units if u.dims["kind"] != "extensions"]
        notes: list[str] = []
        errors: list[str] = []
        if not self.needs_resolving(source):
            return ResolvedSource(source, units, notes, errors)
        sel = source.extensions
        if not (sel.all or sel.top_n or sel.ids):
            notes.append(f"{source.name}: no extensions selected (extensions.top_n / ids / all)")
            with self._lock:
                self.catalog[source.identity] = {}
            return ResolvedSource(source, units, notes, errors)
        try:
            with _client() as client:
                catalog = self._select(client, source, say, notes)
        except Exception as e:  # noqa: BLE001 — reported; desktop/server need no gallery
            errors.append(f"{source.name}: extensions not resolved — {e}")
            return ResolvedSource(source, builds_only, notes, errors)
        platforms = self._effective(source)[1]
        installable = any(mp.pick_versions(e, p, prerelease=sel.prerelease)
                          for e in catalog.values() for p in platforms)
        if not installable:
            errors.append(f"{source.name}: the extension selection resolved to no installable "
                          f"build for {', '.join(platforms)}")
            return ResolvedSource(source, builds_only, notes, errors)
        with self._lock:
            self.catalog[source.identity] = catalog
        return ResolvedSource(source, units, notes, errors)

    def _select(self, client, source, say, notes) -> dict[str, dict]:
        sel = source.extensions
        chosen: dict[str, dict] = {}
        if sel.all:
            say(f"{source.name}: listing every extension…")
            chosen.update({mp.ext_id(e): e for e in mp.search_all(client, on_wait=say)})
        if sel.top_n:
            say(f"{source.name}: top {sel.top_n} by installs…")
            chosen.update({mp.ext_id(e): e for e in mp.top_n(client, sel.top_n, on_wait=say)})
        if sel.ids:
            found = mp.by_names(client, sel.ids, on_wait=say)
            missing = sorted(set(sel.ids) - set(found))
            if missing:
                notes.append(f"{source.name}: not found on the marketplace: {', '.join(missing)}")
            chosen.update(found)
        malicious = self._malicious(client, source, notes)
        self._drop_malicious(chosen, malicious, source, notes)
        direct = set(chosen)
        # A latest-only answer is the latest build per platform, so it can
        # hide the release a client should get: re-ask with every version.
        self._refresh_prereleases(client, chosen, sel.prerelease, say)
        frontier = set(chosen)
        while frontier:
            wanted = set()
            for i in frontier:
                wanted |= mp.dependencies(chosen[i], prerelease=sel.prerelease)
            wanted -= set(chosen)
            wanted -= malicious
            invalid = set()
            for ident in wanted:
                try:
                    ExtensionSelection(ids=[ident])
                except ValueError:
                    invalid.add(ident)
            if invalid:
                notes.append(f"{source.name}: skipped {len(invalid)} malformed upstream dependency ID(s): "
                             + ", ".join(sorted(invalid)[:5]) + ("…" if len(invalid) > 5 else ""))
                wanted -= invalid
            if not wanted:
                break
            found = mp.by_names(client, sorted(wanted), on_wait=say)
            missing = sorted(wanted - set(found))
            if missing:
                notes.append(f"{source.name}: {len(missing)} dependencies not on the marketplace: "
                             + ", ".join(missing[:5]) + ("…" if len(missing) > 5 else ""))
            self._refresh_prereleases(client, found, sel.prerelease, say)
            chosen.update(found)
            frontier = set(found)
        added = len(chosen) - len(direct)
        notes.append(f"{source.name}: {_selection_text(sel)} → {len(chosen)} extensions"
                     f" ({added} added as dependencies / pack members); sizes are known "
                     "once downloaded")
        for platform in self._effective(source)[1]:
            without = sorted(i for i, e in chosen.items()
                             if not mp.pick_versions(e, platform, prerelease=sel.prerelease))
            if without:
                notes.append(f"{source.name}: no {platform} build for {len(without)} extension(s)"
                             f": {', '.join(without[:5])}{'…' if len(without) > 5 else ''}")
        return chosen

    @staticmethod
    def _malicious(client, source, notes) -> set[str]:
        """Microsoft's list of known-malicious extension ids (as vscodeoffline
        does, before anything is downloaded). Unreadable → noted, not fatal."""
        try:
            resp = client.get(MALICIOUS_URL)
            resp.raise_for_status()
            return {str(i).lower() for i in resp.json().get("malicious", [])}
        except Exception as e:  # noqa: BLE001
            notes.append(f"{source.name}: could not read the malicious-extension list ({e}); "
                         "nothing was filtered by it")
            return set()

    @staticmethod
    def _drop_malicious(exts: dict[str, dict], malicious: set[str], source, notes) -> None:
        flagged = sorted(set(exts) & malicious)
        for i in flagged:
            del exts[i]
        if flagged:
            notes.append(f"{source.name}: skipped known-malicious extension(s): {', '.join(flagged)}")

    @staticmethod
    def _refresh_prereleases(client, exts: dict[str, dict], prerelease: bool, say) -> None:
        if prerelease:
            return
        stale = [i for i, e in exts.items() if any(mp.is_prerelease(v) for v in e.get("versions", []))]
        if stale:
            exts.update(mp.by_names(client, stale, flags=mp.FLAGS_ALL_VERSIONS, on_wait=say))

    # ── publish ─────────────────────────────────────────────────────────────

    def publish(self, ctx, units):
        """Every payload at its upstream host/path, grouped by host (each host
        is a base); provider JSON under `.vscode-meta/` is never published."""
        from mmw.core.publisher.links import _dedup_rows, publish_by_host
        rows = [r for r in _dedup_rows(ctx.pool.mirror_files([u.name for u in units]))
                if not r[0].startswith(".vscode-meta/")]
        return publish_by_host(ctx, rows)

    def contribute(self, ctx) -> None:
        from mmw.core.publisher.links import reconcile_extra_prefix
        from mmw.core.publisher.results import MirrorPublishResult
        from mmw.resources import gallery_script_bytes
        units = [u for u in ctx.units if u.provider == self.name]
        if not units:
            if ctx.prune:
                reconcile_extra_prefix(ctx.sink, vpub.GALLERY_DIR, set())
                ctx.sink.drop_if_empty(vpub.GALLERY_DIR)
            return
        d = vpub.GALLERY_DIR
        say = (lambda message: ctx.progress(d, f"gallery:{message}")) if ctx.progress else None
        updates = vpub.updates(ctx.pool, units)
        try:
            catalog_json, hosts = vpub.catalog_files(
                ctx.pool, units, ctx.mirror_dir / vpub.META_CACHE, recheck=ctx.recheck, say=say,
                stop_event=ctx.stop_event)
        except InterruptedError:
            return                      # stopped: nothing half-built reaches the tree
        files = {
            f"{d}/catalog.json": catalog_json,
            f"{d}/updates.json": json.dumps(updates, sort_keys=True).encode(),
            f"{d}/gallery.py": gallery_script_bytes(),
            f"{d}/VSCODE.md": vpub.vscode_md(ctx.network, gallery_image.TAR_NAME, updates, hosts=hosts),
        }
        if say:
            say("done")
        for rel, data in files.items():
            ctx.sink.write(rel, data)
        written = set(files)
        problems = MirrorPublishResult(mirror_name="vscode gallery")
        img = gallery_image.gallery_image(
            ctx.mirror_dir / IMAGE_CACHE, stop_event=ctx.stop_event,
            progress=(lambda phase: ctx.progress(d, f"gallery-image:{phase}")) if ctx.progress else None)
        if ctx.stop_event is not None and ctx.stop_event.is_set():
            return
        if img.path is None:
            # A warning, not an error: a bundling machine without Docker is
            # the ordinary airgap case, and an error would refuse the snapshot.
            ctx.result.warnings.append(
                f"{img.error} — the gallery image is not included; the far side "
                f"needs {gallery_image.IMAGE} ({gallery_image.PLATFORM}) loaded some other way")
        else:
            rel = f"{d}/{gallery_image.TAR_NAME}"
            if ctx.sink.link(img.path, rel, disk_io.ops.stat(img.path).st_size, img.checksum,
                             KIND_PKG) == "error":
                problems.errors.append(f"{rel}: could not be placed")
            else:
                written.add(rel)
        if problems.errors:
            ctx.result.mirrors.append(problems)
        if ctx.prune:
            reconcile_extra_prefix(ctx.sink, d, written)

    def nginx_sites(self, units):
        return [
            Site(r"~^[^.]+\.gallerycdn\.vsassets\.io$", root="$host", cors=True),
            *[Site(h, root=h, cors=True) for h in vpub.STATIC_HOSTS],
            Site("update.code.visualstudio.com", proxy=("/",)),
            Site("code.visualstudio.com", proxy=("/",)),
            Site("marketplace.visualstudio.com",
                 proxy=("/", "/_apis/public/gallery/extensionquery"), exact=True,
                 # An installed extension's "is there a newer one?" check.
                 proxy_prefixes=("/_apis/public/gallery/vscode/",)),
            Site(r"~^[^.]+\.gallery\.vsassets\.io$", proxy=("/",)),
        ]

    def cert_names(self, network, units) -> list[str]:
        """Every VS Code name — only for a network that publishes VS Code units."""
        return list(vpub.HOSTS) if units else []

    # ── presentation ────────────────────────────────────────────────────────

    def describe(self, source) -> SourceDescription:
        return SourceDescription(
            fields=[("Provider", "vscode"), ("Kinds", list(source.kinds)),
                    ("Platforms", list(source.platforms)), ("Qualities", list(source.qualities)),
                    ("Extensions", _selection_text(source.extensions))],
            flags=["prerelease"] if source.extensions.prerelease else [],
            location_label="Upstream", location=source.url)

    def unit_label(self, unit):
        parts = [unit.dims.get("kind", ""), unit.dims.get("platform", "universal")]
        if unit.dims.get("quality"):
            parts.append(unit.dims["quality"])
        return " · ".join(parts), ""

    def unit_describe(self, unit):
        rows = [("Kind", unit.dims.get("kind", "")),
                ("Platform", unit.dims.get("platform", "universal"))]
        if unit.dims.get("quality"):
            rows.append(("Quality", unit.dims["quality"]))
        return rows

    # ── config ──────────────────────────────────────────────────────────────

    def to_toml(self, source) -> dict:
        d: dict = {"provider": "vscode"}
        if source.name != "vscode":
            d["name"] = source.name
        for key in ("kinds", "platforms", "qualities"):
            if getattr(source, key):
                d[key] = getattr(source, key)
        ext = source.extensions
        e = {k: v for k, v in (("top_n", ext.top_n), ("ids", ext.ids),
                               ("all", ext.all), ("prerelease", ext.prerelease)) if v}
        if e:
            d["extensions"] = e
        return d

    # ── fetch (Task 6) ──────────────────────────────────────────────────────

    def progress_unit(self, mirror) -> ProgressUnit:
        if mirror.dims.get("kind") == "extensions":
            return ProgressUnit("extensions", mirror.source)
        return ProgressUnit("builds")

    def fetch_all(self, mirror_config, client, status_callback=None, history=None):
        history = history or UnitHistory.empty()
        kind = mirror_config.dims.get("kind")
        if kind == "extensions":
            return self._fetch_extensions(mirror_config, status_callback)
        return self._fetch_builds(mirror_config, client, history, status_callback)

    def _fetch_extensions(self, unit, say):
        catalog = self.catalog.get(unit.source)
        if catalog is None:
            raise RuntimeError(f"{unit.name}: the extension selection was not resolved "
                               "in this process — sync resolves first; re-run the sync")
        platform = unit.dims.get("platform")          # None: the universal unit
        prerelease = bool(unit.options.get("prerelease", False))
        packages: list[PackageInfo] = []
        metadata: list[MetadataFile] = []
        refused: list[str] = []
        statistics: dict = {}
        for ident in sorted(catalog):
            versions = self._unit_versions(catalog[ident], unit, prerelease)
            if not versions:
                continue
            ext = catalog[ident]
            for v in versions:
                for f in v.get("files", []):
                    rel = asset_repo_path(f.get("source", ""))
                    if rel is None:
                        refused.append(f.get("source", ""))
                        continue
                    packages.append(PackageInfo(
                        name=f"{ident}@{v['version']} {f.get('assetType', '')}",
                        url=f["source"], checksum="", checksum_type="sha256", size=0,
                        repo_path=rel, filename=rel.rsplit("/", 1)[-1], checksum_known=False,
                        item_id=ident))
            stored = copy.deepcopy(ext)
            stored["versions"] = versions
            # One file for every extension's stats (see vpub.STATISTICS): in
            # the description they changed it, and its checksum, every sync.
            if "statistics" in stored:
                statistics[ident] = stored.pop("statistics")
            metadata.append(_json_meta(
                f".vscode-meta/extensions/{platform or 'universal'}/{ident}.json", stored))
        metadata.append(_json_meta(
            f"{vpub.STATISTICS}{platform or 'universal'}.json", statistics))
        if refused and say:
            say(f"refused {len(refused)} asset URL(s) that cannot be mirrored at their own path")
        if platform is None:
            # Once per source: the universal unit carries the malicious list.
            metadata.append(MetadataFile(url=MALICIOUS_URL,
                                         repo_path=asset_repo_path(MALICIOUS_URL), is_release=True))
        return packages, metadata

    @staticmethod
    def _unit_versions(ext, unit, prerelease) -> list[dict]:
        """The builds this unit owns: a platform unit its platform-specific
        builds; the universal unit every universal build some selected
        platform resolves to (each once)."""
        platform = unit.dims.get("platform")
        if platform is not None:
            return [v for v in mp.pick_versions(ext, platform, prerelease=prerelease)
                    if v.get("targetPlatform") == platform]
        out: list[dict] = []
        for p in unit.options.get("platforms") or TARGET_PLATFORMS:
            for v in mp.pick_versions(ext, p, prerelease=prerelease):
                if v.get("targetPlatform") in (None, "universal") and v not in out:
                    out.append(v)
        return out

    def _fetch_builds(self, unit, client, history, say):
        kind, platform, quality = (unit.dims["kind"], unit.dims["platform"],
                                   unit.dims["quality"])
        packages: list[PackageInfo] = []
        metadata: list[MetadataFile] = []
        for ident in builds(kind, platform):
            current = updates.latest(client, ident, quality, on_wait=say)
            if current is None:
                if say:
                    say(f"no {ident} {quality} build upstream")
                continue
            keep = updates.retained(current, history, ident, quality) if kind == "server" \
                else [current]
            for build in keep:
                rel = asset_repo_path(build["url"])
                if rel is None:
                    if say:
                        say(f"refused build URL {build['url']}")
                    continue
                packages.append(PackageInfo(
                    name=f"{ident} {build.get('productVersion', '')} ({build['version'][:7]})",
                    url=build["url"], checksum=build["sha256hash"], checksum_type="sha256",
                    size=0, repo_path=rel, filename=rel.rsplit("/", 1)[-1]))
                metadata.append(_json_meta(updates.meta_path(ident, quality, build["version"]),
                                           build))
        if kind == "desktop" and quality == "stable":
            metadata.extend(MetadataFile(url=url, repo_path=asset_repo_path(url),
                                         is_release=True) for url in WORKBENCH_URLS)
        return packages, metadata

    def repair_packages(self, unit, pool, *, status_callback=None, stop_event=None):
        """Missing assets from saved metadata; never resolve the marketplace.

        Unknown-checksum failures have no package ref yet, so reading only
        missing_for() cannot recover them. The saved JSON is the desired set.
        """
        saved = pool.retry_plan(unit.name)
        if saved is not None:
            token, failures = saved
            return [PackageInfo(**f["package"]) for f in failures], token
        if status_callback:
            status_callback("Older mirror: discovering missing files once to initialize its retry list...")
        refs = pool.ref_map(unit.name)
        witness = pool.meta_refs(unit.name)
        if not witness:
            raise RuntimeError("no saved VS Code metadata; run a normal sync first")
        held = {rel: path for rel, path, *_ in pool.mirror_files([unit.name])}
        packages = {}
        for path in sorted(witness):
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError("retry discovery stopped")
            try:
                data = json.loads(disk_io.ops.read_bytes(Path(held[path])))
                if not isinstance(data, dict):
                    raise ValueError("expected a JSON object")
                if path.startswith(".vscode-meta/extensions/"):
                    assets = [(f["source"], "", data.get("extensionName", path))
                              for v in data["versions"] for f in v["files"]]
                elif "url" in data:
                    assets = [(data["url"], data["sha256hash"], path)]
                else:
                    continue  # malicious-extension list has no package URLs
                for url, checksum, name in assets:
                    rel = asset_repo_path(url)
                    if rel is None:
                        raise ValueError(f"unsafe asset URL: {url!r}")
                    if rel in held:
                        continue
                    checksum = checksum or refs.get(rel, "")
                    packages[rel] = PackageInfo(
                        name=f"{name}: {rel}", url=url, checksum=checksum,
                        checksum_type="sha256", size=0, repo_path=rel,
                        filename=rel.rsplit("/", 1)[-1], checksum_known=bool(checksum))
            except (KeyError, OSError, ValueError, TypeError) as exc:
                raise RuntimeError(f"cannot repair from {path}: {exc}") from exc
        return list(packages.values()), witness

    def verify_witness(self, unit, pool, say=None, stop_event=None):
        """Re-read the unit's pooled JSON and check the pool holds every file it
        names — an independent second opinion on the refs."""
        from mmw.core.verify import VerifyResult
        result = VerifyResult(unit.name, unit.provider)
        started = perf_counter()
        # Keep desired metadata separate from available blobs: an absent package
        # row must not make its metadata disappear from the witness check.
        if say:
            say("listing pool references…")
        refs = pool.ref_map(unit.name)
        held = {rel: path for rel, path, *_ in pool.mirror_files([unit.name])}
        result.timings["lookup"] = perf_counter() - started
        started = perf_counter()
        expected: set[str] = set()
        # The statistics file names no files: nothing to read it for.
        metas = sorted(p for p in refs if p.startswith(".vscode-meta/")
                       and not p.startswith(vpub.STATISTICS))
        # What a JSON names is fixed by its checksum: only metadata not seen
        # by an earlier check is read (a mirror's 120k JSONs, otherwise, on
        # every publish). Each named file is still checked against the pool.
        memo_path = pool.pool_dir.parent / vpub.META_CACHE / f"witness-{unit.name}.json.gz"
        memo = _load_witness_memo(memo_path)
        unread = [held[p] for p in metas if p in held and refs[p] not in memo]
        if say:
            say(f"reading {len(unread):,} new descriptions ({len(metas):,} in all)…")
        with disk_io.DiskJob("witness", stop_event=stop_event) as job:
            bodies = disk_io.read_files(unread, job=job, on_progress=(
                (lambda done, total: say(f"read {done:,}/{total:,} descriptions…")) if say else None))
        result.timings.update(job.stats.timings())
        if stop_event is not None and stop_event.is_set():
            result.error = "stopped"
            return result
        named_by: dict[str, list[str]] = {}
        for path in metas:
            checksum = refs[path]
            if path not in held:
                result.error = f"unreadable {path}"
                result.timings["metadata"] = perf_counter() - started
                return result
            if checksum not in memo:
                try:
                    body = bodies[held[path]]
                    if isinstance(body, OSError):
                        raise body
                    data = json.loads(body)
                except (KeyError, OSError, ValueError):
                    result.error = f"unreadable {path}"
                    result.timings["metadata"] = perf_counter() - started
                    return result
                memo[checksum] = _named_assets(path, data)
            named_by[checksum] = memo[checksum]
            expected.update(memo[checksum])
        if bodies or len(named_by) != len(memo):
            _save_witness_memo(memo_path, named_by)
        result.timings["metadata"] = perf_counter() - started
        if say:
            say(f"checking {len(expected):,} files are in the pool…")
        started = perf_counter()
        result.total = len(expected)
        result.present = len(expected.intersection(held))
        result.missing_count = result.total - result.present
        result.timings["assets"] = perf_counter() - started
        return result

    def fetch_packages(self, mirror_config, client, status_callback=None):
        return self.fetch_all(mirror_config, client, status_callback=status_callback)[0]

    def fetch_metadata(self, mirror_config, client):
        return self.fetch_all(mirror_config, client)[1]
