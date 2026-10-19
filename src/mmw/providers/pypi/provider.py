"""PypiProvider — pypi.org as an mmw provider.

Units are one universal unit (pure wheels and sdists) plus one per target
(python × platform × arch). Resolving refreshes the cached index, runs the
keep policy once per sync and holds the catalog; each unit's `fetch_all`
reads its slice of it, plus the files its history still retains.
"""
import dataclasses
import tarfile
import tempfile
import threading
import zipfile
from datetime import date
from email.parser import HeaderParser
from pathlib import Path

import httpx

from mmw.core.format import fmt_size
from mmw.core.history import UnitHistory
from mmw.providers.base import (BaseProvider, Dimension, MetadataFile, PackageInfo, ProgressUnit,
                                ResolveContext, Site, SourceDescription, SourceField)
from mmw.providers.pypi import build
from mmw.providers.pypi import history as hist
from mmw.providers.pypi import index, records, simple
from mmw.providers.pypi import publish as pypub
from mmw.providers.pypi import uv as uvmod
from mmw.providers.pypi.closure import UNIVERSAL, KeepSet, Report, keep_closure
from mmw.providers.pypi.requirements import resolve_requirements
from mmw.providers.pypi.targets import ARCHES, PLATFORMS, PYTHONS, Target, targets

REPORT_PATH = ".pypi-meta/report.json"


class _Universe:
    """The closure's view of the cached index; metadata is fetched a wave at a time."""

    def __init__(self, cache: index.IndexCache, context: ResolveContext, say=None):
        self.cache, self.context, self.say = cache, context, say
        self.failures: list[str] = []

    def names(self):
        return self.cache.names()

    def files(self, name):
        return self.cache.files(name)

    def scan(self):
        return self.cache.scan()

    def prefetch(self, files):
        self.failures += index.fetch_metadata(self.cache, files, index._client,
                                              threads=self.context.max_connections, say=self.say,
                                              stop_event=self.context.stop_event)

    def metadata(self, file):
        body = self.cache.metadata(file.metadata_sha256) if file.metadata_sha256 else None
        if body is None:
            return None
        headers = HeaderParser().parsestr(body.decode("utf-8", errors="replace"))
        return headers.get_all("Requires-Dist") or [], headers.get_all("Provides-Extra") or []


class _PinnedUniverse:
    """The requirements flow's view: a project's page is fetched fresh the
    first time it is asked for in this resolve — no full-index refresh."""

    def __init__(self, cache: index.IndexCache, context: ResolveContext):
        self.cache, self.context = cache, context
        self.fresh: set[str] = set()

    def files(self, name):
        if name not in self.fresh:
            index.refresh_projects(self.cache, [name], index._client, threads=1,
                                   stop_event=self.context.stop_event)
            self.fresh.add(name)
        return self.cache.files(name)


class _Catalog:
    def __init__(self, cache: index.IndexCache, keep: KeepSet | None = None, report: Report | None = None,
                 req_keep: KeepSet | None = None, req_report: Report | None = None):
        self.cache = cache
        self.keep, self.report = keep, report
        self.req_keep, self.req_report = req_keep, req_report
        self.failed: set[str] = set()
        self.window_days: int | None = None   # widest network retention; None = forever

    def flow(self, name: str):
        return (self.req_keep, self.req_report) if name == "req" else (self.keep, self.report)


def _has_requirements(source) -> bool:
    return bool(source.requirements or source.requirements_file)


def _build_requires(f, cache):
    """An sdist's build requirements; an unreadable sdist is reported and
    treated like one without a pyproject.toml rather than sinking the target."""
    try:
        return build.build_requires(f, cache, index._client)
    except (httpx.HTTPError, ValueError, OSError, tarfile.TarError, zipfile.BadZipFile) as e:
        return build.BuildRequires(list(build.LEGACY), f"could not read the sdist: {e}")


def _requirements_text(source) -> str:
    parts = [f"{len(source.requirements)} lines"] if source.requirements else []
    if source.requirements_file:
        parts.append(source.requirements_file)
    return " + ".join(parts) or "none"


def _selection(source) -> str:
    parts = ["all of PyPI"] if source.all else []
    if source.requirements or source.requirements_file:
        parts.append(f"requirements ({_requirements_text(source)})")
    return " + ".join(parts) or "nothing selected"


def _unit_key(unit) -> str:
    if not unit.dims:
        return UNIVERSAL
    return Target(unit.dims["python"], unit.dims["platform"], unit.dims["arch"]).key


def _package(f) -> PackageInfo:
    release = f"{f.project}=={f.version}"
    return PackageInfo(name=release, url=f.url, checksum=f.sha256, checksum_type="sha256", size=f.size,
                       repo_path=f.repo_path, filename=f.filename, item_id=release)


class PypiProvider(BaseProvider):
    name = "pypi"
    label = "PyPI"
    description = "Python packages from pypi.org: latest + dependency closure per target"
    # The universal unit has no coordinates: every target filter passes it.
    dimensions = (
        Dimension("pythons", "python", "Pythons", absent_matches=True),
        Dimension("platforms", "platform", "Platforms", absent_matches=True),
        Dimension("arches", "arch", "Arches", absent_matches=True),
    )
    publishes = True
    carries_history = True
    retries_failures = True
    site_per_base = False
    fields = (
        SourceField("name", "Name", "text"),
        SourceField("label", "Label", "text"),
        SourceField("pythons", "Pythons", "multi", PYTHONS, "Empty means every supported Python."),
        SourceField("platforms", "Platforms", "multi", PLATFORMS, "Empty means all platforms."),
        SourceField("arches", "Arches", "multi", ARCHES, "Empty means all arches."),
        SourceField("glibc", "Oldest glibc", "text", hint="e.g. 2.31 (Ubuntu 20.04)."),
        SourceField("musl", "Oldest musl", "text", hint="e.g. 1.2 (Alpine 3.13+)."),
        SourceField("macos", "Oldest macOS", "text", hint="e.g. 11.0."),
        SourceField("sdists", "Source distributions", "toggle",
                    hint="Mirror every kept release's sdist (needed ones are kept regardless)."),
        SourceField("all", "All of PyPI", "toggle",
                    hint="Latest + dependency closure of every project: about 0.7–1.3 TB per target set."),
        SourceField("requirements", "Requirements", "textarea",
                    hint="One per line, requirements.txt style; resolved together per target."),
        SourceField("requirements_file", "Requirements file", "text",
                    hint="A requirements.txt-style file, relative to mmw.toml; re-read every sync."),
    )

    def __init__(self):
        self.catalog: dict[str, _Catalog] = {}
        self._lock = threading.Lock()

    # ── presentation ────────────────────────────────────────────────────────

    def group(self, source):
        return "PyPI"

    def source_summary(self, source):
        return (f"{', '.join(source.pythons) or 'all pythons'} · "
                f"{', '.join(source.platforms) or 'all platforms'} · "
                f"{', '.join(source.arches) or 'all arches'} · "
                f"{_selection(source)}")

    def describe(self, source) -> SourceDescription:
        return SourceDescription(
            fields=[("Provider", "pypi"), ("Pythons", list(source.pythons)),
                    ("Platforms", list(source.platforms)), ("Arches", list(source.arches)),
                    ("OS floors", f"glibc {source.glibc} · musl {source.musl} · macOS {source.macos}"),
                    ("Requirements", _requirements_text(source))],
            flags=[f for f in ("all", "sdists") if getattr(source, f)],
            location_label="Upstream", location=source.url)

    def unit_label(self, unit):
        flow = "requirements" if unit.options.get("flow") == "req" else "all"
        if not unit.dims:
            return f"{flow} · pure wheels · sdists", ""
        return f"{flow} · {unit.dims['python']} · {unit.dims['platform']} · {unit.dims['arch']}", ""

    def unit_describe(self, unit):
        flow = ("Flow", "requirements list" if unit.options.get("flow") == "req" else "all of PyPI")
        if not unit.dims:
            return [flow, ("Files", "pure wheels and sdists, every target")]
        return [flow, ("Python", unit.dims["python"]), ("Platform", unit.dims["platform"]),
                ("Arch", unit.dims["arch"])]

    # ── config ──────────────────────────────────────────────────────────────

    def to_toml(self, source) -> dict:
        from mmw.core.config import PypiSource
        defaults = PypiSource(provider="pypi")
        d: dict = {"provider": "pypi"}
        for key in ("name", "label", "pythons", "platforms", "arches", "glibc", "musl", "macos",
                    "sdists", "all", "requirements", "requirements_file"):
            if getattr(source, key) != getattr(defaults, key):
                d[key] = getattr(source, key)
        return d

    # ── config → units ──────────────────────────────────────────────────────

    def expand(self, source) -> list:
        """The straight mirror's units, then the requirements list's: same
        shape, `options["flow"] = "req"` telling them apart."""
        units = []
        if source.all:
            units += self._flow_units(source, "all")
        if _has_requirements(source):
            units += self._flow_units(source, "req")
        return units

    @staticmethod
    def _flow_units(source, flow: str) -> list:
        from mmw.core.config import MirrorConfig
        ts = targets(source.pythons, source.platforms, source.arches)
        common = dict(provider="pypi", source_url=source.url, source=source.identity, label=source.label)
        tag = {"flow": "req"} if flow == "req" else {}
        units = [MirrorConfig(name=f"{source.name}-{flow}-universal",
                              options={**tag, "targets": [t.key for t in ts]}, **common)]
        units += [MirrorConfig(name=f"{source.name}-{flow}-{t.key}", options=dict(tag),
                               dims={"python": t.python, "platform": t.platform, "arch": t.arch}, **common)
                  for t in ts]
        return units

    def covers(self, source, unit) -> bool:
        return source.covers(unit)

    def needs_resolving(self, source) -> bool:
        return source.all or _has_requirements(source)

    def resolve(self, source, *, progress=None, context=None):
        from mmw.core.resolve import ResolvedSource
        say = progress or (lambda _m: None)
        context = context or ResolveContext()
        notes: list[str] = []
        errors: list[str] = []
        if not source.all and not _has_requirements(source):
            notes.append(f"{source.name}: nothing selected (all / requirements)")
            return ResolvedSource(source, [], notes, errors)
        lines: list[str] = []
        lines_read = False             # a list that could not be read keeps its previous state
        if _has_requirements(source):
            try:
                lines = source.requirement_lines(context.config_dir)
                lines_read = True
            except (OSError, ValueError) as e:
                errors.append(f"{source.name}: requirements not read — {e}")
        cache = index.IndexCache(context.mirror_dir)
        catalog = _Catalog(cache)
        # Without the config (tests, previews) nothing is trimmed.
        catalog.window_days = (records.widest_window(context.cfg, source.identity)
                               if context.cfg is not None else None)
        units: list = []
        if source.all:
            if not self._resolve_all(source, cache, catalog, context, say, notes, errors):
                return ResolvedSource(source, [], notes, errors)
            units += self._flow_units(source, "all")
        if lines_read and not lines:
            # Emptied on purpose: the requirements units sync to empty (and
            # retention gives lockfiles their grace), never to their old set.
            catalog.req_keep, catalog.req_report = KeepSet(), Report()
            notes.append(f"{source.name}: requirements list is empty — its units sync to nothing")
            units += self._flow_units(source, "req")
        elif lines:
            if not self._resolve_requirements(source, lines, cache, catalog, context, say, notes, errors):
                return ResolvedSource(source, [], notes, errors)
            # A failed target keeps its previous state. req-universal always
            # ships: what the failed targets had is carried (see fetch_all).
            units += [u for u in self._flow_units(source, "req")
                      if not u.dims or _unit_key(u) not in catalog.failed]
        with self._lock:
            self.catalog[source.identity] = catalog
        return ResolvedSource(source, units, notes, errors)

    def _resolve_requirements(self, source, lines, cache, catalog, context, say, notes, errors) -> bool:
        ts = targets(source.pythons, source.platforms, source.arches)
        if context.mirror_dir:
            uv_cache = uvmod.cache_dir(context.mirror_dir)
        else:                               # no mirror (previews): a cache for this resolve only
            scratch = tempfile.TemporaryDirectory(prefix="mmw-uv-")
            self._scratch = scratch         # removed when the next resolve replaces it, or at exit
            uv_cache = Path(scratch.name)
        universe = _PinnedUniverse(cache, context)
        say(f"{source.name}: resolving the requirements list ({len(lines)} lines) for {len(ts)} target(s)…")
        result = resolve_requirements(
            lines, ts, source.floors, sdists=source.sdists, universe=universe,
            compile_fn=lambda reqs, t: uvmod.compile_for(reqs, t, source.floors, index_url=uvmod.INDEX_URL,
                                                         cache_dir=uv_cache),
            build_fn=lambda f: _build_requires(f, cache), stop_event=context.stop_event, progress=say)
        if context.stop_event is not None and context.stop_event.is_set():
            errors.append(f"{source.name}: stopped while resolving the requirements list")
            return False
        catalog.req_keep, catalog.req_report, catalog.failed = result.keep, result.report, result.failed
        for key in sorted(result.failed):
            why = next((row[2] for row in result.report.unsatisfiable if row[0] == key), "")
            errors.append(f"{source.name}: requirements for {key} could not be resolved — {why}")
        files = {f.filename: f for unit in result.keep.files.values() for f in unit.values()}
        notes.append(f"{source.name}: requirements ({len(lines)} lines) → {len(files)} files, "
                     f"{fmt_size(sum(f.size for f in files.values()))} across "
                     f"{len(ts) - len(result.failed)} target(s); {result.report.summary()}")
        return True

    def _resolve_all(self, source, cache, catalog, context, say, notes, errors) -> bool:
        say(f"{source.name}: reading the PyPI index…")
        refreshed = index.refresh(cache, index._client, threads=context.max_connections, say=say,
                                  stop_event=context.stop_event)
        if refreshed.refused:
            errors.append(f"{source.name}: {refreshed.refused}")
            return False
        if refreshed.stopped:
            errors.append(f"{source.name}: stopped while reading the PyPI index "
                          f"({len(cache.names())} projects cached; the next sync continues)")
            return False
        if refreshed.failed:
            notes.append(f"{source.name}: {len(refreshed.failed)} project page(s) could not be read; "
                         f"their previous state is used: {', '.join(refreshed.failed[:5])}"
                         f"{'…' if len(refreshed.failed) > 5 else ''}")
        universe = _Universe(cache, context, say)
        ts = targets(source.pythons, source.platforms, source.arches)
        say(f"{source.name}: resolving latest + dependencies for {len(ts)} target(s)…")
        keep, report = keep_closure(universe, ts, source.floors, sdists=source.sdists,
                                    stop_event=context.stop_event, progress=say)
        if context.stop_event is not None and context.stop_event.is_set():
            errors.append(f"{source.name}: stopped while resolving dependencies")
            return False
        if universe.failures:
            notes.append(f"{source.name}: core metadata unreadable for {len(universe.failures)} file(s); "
                         f"their dependencies are reported unknown: {universe.failures[0]}")
        catalog.keep, catalog.report = keep, report
        files = {f.filename: f for unit in keep.files.values() for f in unit.values()}
        notes.append(f"{source.name}: {len(cache.names())} projects → {len(files)} files, "
                     f"{fmt_size(sum(f.size for f in files.values()))} across {len(ts) + 1} units; "
                     f"{report.summary()}")
        return True

    # ── fetch ───────────────────────────────────────────────────────────────

    # ── publish ─────────────────────────────────────────────────────────────

    def publish(self, ctx, units):
        """Files at files.pythonhosted.org/… (pool rows, narrowed by the
        network's retention), then the generated pypi.org pages."""
        from mmw.core.publisher.links import publish_by_host, reconcile_extra_prefix
        from mmw.resources import pypi_render_bytes
        rows, recs = pypub.published_rows(ctx.pool, units, ctx.network, date.today())
        results, hosts = publish_by_host(ctx, rows)
        if ctx.prune and "files.pythonhosted.org" not in hosts:
            # No file survives this network's retention: the host still has to
            # be emptied, which publish_by_host only does for hosts with rows.
            ctx.sink.finalize_group("files.pythonhosted.org", set(), set(), set())
        if ctx.stop_event is not None and ctx.stop_event.is_set():
            return results, set(pypub.HOSTS)
        published = {row[0] for row in rows}
        with_metadata = {path.rsplit("/", 1)[1].removesuffix(".metadata")
                         for path in published if path.endswith(".metadata")}
        by_project: dict[str, list] = {}
        for path, rec in recs.items():
            if path in published:
                by_project.setdefault(rec.project, []).append(rec)
        # The pages are derived (spec §5.0): only their data and the renderer
        # go through the sink; render_derived makes pypi.org/ from them.
        files = simple.shards([simple.entry_of(p, by_project[p], with_metadata) for p in sorted(by_project)])
        files[simple.RENDERER] = pypi_render_bytes()
        for rel, data in files.items():
            ctx.sink.write(rel, data)
        if ctx.prune:
            reconcile_extra_prefix(ctx.sink, "pypi-data", set(files))
        return results, {"files.pythonhosted.org"}

    def contribute(self, ctx) -> None:
        from mmw.core.publisher.links import reconcile_extra_prefix
        units = [u for u in ctx.units if u.provider == self.name]
        if units:
            ctx.sink.write("PYPI.md", pypub.pypi_md(ctx.network))
        elif ctx.prune:
            # No renderer left: render_derived removes the pages it made.
            reconcile_extra_prefix(ctx.sink, "pypi-data", set())
            ctx.sink.drop_if_empty("pypi-data")
            if getattr(ctx.sink, "root", None) is not None:
                (ctx.sink.root / "PYPI.md").unlink(missing_ok=True)

    def preview(self, network, cfg, pool, mirror_dir, result) -> None:
        """Dry-run: what this network publishes only because of its retention."""
        from mmw.core.config import retention_days
        from mmw.core.resolve import network_mirrors
        units = [u for u in network_mirrors(network, cfg, mirror_dir) if u.provider == self.name]
        if not units:
            return
        days, today = retention_days(network.retention), date.today()
        sizes: dict[str, int] = {}
        for unit in units:
            left = {path for path, e in hist.load(pypub._read(pool, unit.name, hist.HISTORY_PATH)).items()
                    if e.left_at is not None and hist.in_window(e.left_at, days, today)}
            for row in pool.mirror_files([unit.name]):
                if row[0] in left:
                    sizes[row[0]] = row[2]
        if sizes:
            result.warnings.append(f"pypi: {len(sizes)} files ({fmt_size(sum(sizes.values()))}) published "
                                   f"only through retention ({network.retention})")

    def nginx_sites(self, units):
        return [Site("pypi.org", root="pypi.org", negotiated_index="/simple/", json_paths="/pypi/"),
                Site("files.pythonhosted.org", root="files.pythonhosted.org")]

    def cert_names(self, network, units) -> list[str]:
        return list(pypub.HOSTS) if units else []

    def progress_unit(self, mirror) -> ProgressUnit:
        return ProgressUnit("releases", mirror.source)

    def fetch_all(self, mirror_config, client, status_callback=None, history=None):
        cat = self.catalog.get(mirror_config.source)
        if cat is None:
            raise RuntimeError(f"{mirror_config.name}: the PyPI selection was not resolved "
                               "in this process — sync resolves first; re-run the sync")
        keep, report = cat.flow(mirror_config.options.get("flow", "all"))
        if keep is None:
            raise RuntimeError(f"{mirror_config.name}: the PyPI selection was not resolved "
                               "in this process — sync resolves first; re-run the sync")
        history = history or UnitHistory.empty()
        key = _unit_key(mirror_config)
        kept = sorted(keep.files.get(key, {}).values(), key=lambda f: f.repo_path)
        current = [_package(f) for f in kept]
        held = {p.repo_path for p in current}
        before = hist.load(history.read(hist.HISTORY_PATH))
        carried = []
        if key == UNIVERSAL and mirror_config.options.get("flow") == "req" and cat.failed:
            # A failed target's pure files are not in this resolve; whatever the
            # unit held as current stays current until that target resolves again.
            carried = [p for p in hist.as_packages(e for e in before.values() if e.left_at is None)
                       if p.repo_path not in held]
            current += carried
            held |= {p.repo_path for p in carried}
        today = date.today()
        # What no network still wants leaves history and refs; prune frees it.
        advanced = {path: e for path, e in hist.advance(before, current, today.isoformat()).items()
                    if hist.in_window(e.left_at, cat.window_days, today)}
        retained = [p for p in hist.retained(advanced) if p.repo_path not in held]
        packages = current + retained
        # Every kept wheel's PEP 658 metadata, by hash and without its body: the
        # engine skips what the pool holds and fetches only the rest, so a
        # million-wheel unit never holds a million bodies.
        metadata = [MetadataFile(url=f.url + ".metadata", repo_path=f.repo_path + ".metadata",
                                 expected_sha256=f.metadata_sha256)
                    for f in kept if f.kind == "wheel" and f.metadata_sha256]
        # A retained or carried wheel keeps its core metadata, known from the unit's own refs.
        for p in [*retained, *carried]:
            checksum = history.checksum(p.repo_path + ".metadata")
            if checksum is not None:
                metadata.append(MetadataFile(url=p.url + ".metadata", repo_path=p.repo_path + ".metadata",
                                             expected_sha256=checksum))
        report = report if key == UNIVERSAL else report.for_units({key})
        metadata.append(MetadataFile(url="", repo_path=REPORT_PATH, content=report.to_json()))
        metadata.append(MetadataFile(url="", repo_path=hist.HISTORY_PATH, content=hist.dump(advanced)))
        metadata.append(MetadataFile(url="", repo_path=records.RECORDS_PATH,
                                     content=records.dump(self._records(cat, kept, [*retained, *carried],
                                                                        history))))
        return packages, metadata

    @staticmethod
    def _records(cat, kept, older, history) -> dict:
        """Current files from the index; retained/carried ones from the last
        records, their yank state refreshed from the index. A unit synced
        before records existed has none: those come from the index outright.
        Each project's index entry is decoded once, however many files it has."""
        out = {f.repo_path: records.record_of(f) for f in kept}
        previous = records.load(history.read(records.RECORDS_PATH))
        by_project: dict[str, list] = {}
        for p in older:
            project = p.item_id.split("==", 1)[0]
            by_project.setdefault(project, []).append(p)
        for project, packages in by_project.items():
            now = {f.repo_path: f for f in cat.cache.files(project)}
            for p in packages:
                rec, current = previous.get(p.repo_path), now.get(p.repo_path)
                if rec is not None:
                    out[p.repo_path] = rec if current is None else dataclasses.replace(rec, yanked=current.yanked)
                elif current is not None:
                    out[p.repo_path] = records.record_of(current)
        return out

    def fetch_packages(self, mirror_config, client, status_callback=None):
        return self.fetch_all(mirror_config, client, status_callback)[0]

    def fetch_metadata(self, mirror_config, client):
        return self.fetch_all(mirror_config, client)[1]

    def repair_packages(self, unit, pool, *, status_callback=None, stop_event=None):
        saved = pool.retry_plan(unit.name)
        if saved is None:
            raise RuntimeError("no saved PyPI retry plan; run a normal sync first")
        token, failures = saved
        return [PackageInfo(**f["package"]) for f in failures], token
