"""PypiProvider — pypi.org as an mmw provider.

Units are one universal unit (pure wheels and sdists) plus one per target
(python × platform × arch). Resolving refreshes the cached index, runs the
keep policy once per sync and holds the catalog; each unit's `fetch_all`
reads its slice of it, plus the files its history still retains.
"""
import threading
from datetime import date
from email.parser import HeaderParser

from mmw.core.format import fmt_size
from mmw.core.history import UnitHistory
from mmw.providers.base import (BaseProvider, Dimension, MetadataFile, PackageInfo, ProgressUnit,
                                ResolveContext, SourceDescription, SourceField)
from mmw.providers.pypi import history as hist
from mmw.providers.pypi import index
from mmw.providers.pypi.closure import UNIVERSAL, KeepSet, Report, keep_closure
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


class _Catalog:
    def __init__(self, keep: KeepSet, report: Report, cache: index.IndexCache):
        self.keep, self.report, self.cache = keep, report, cache


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
    publishes = False          # stage 3
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
                f"{'all of PyPI' if source.all else 'nothing selected'}")

    def describe(self, source) -> SourceDescription:
        return SourceDescription(
            fields=[("Provider", "pypi"), ("Pythons", list(source.pythons)),
                    ("Platforms", list(source.platforms)), ("Arches", list(source.arches)),
                    ("OS floors", f"glibc {source.glibc} · musl {source.musl} · macOS {source.macos}")],
            flags=[f for f in ("all", "sdists") if getattr(source, f)],
            location_label="Upstream", location=source.url)

    def unit_label(self, unit):
        if not unit.dims:
            return "pure wheels · sdists", ""
        return f"{unit.dims['python']} · {unit.dims['platform']} · {unit.dims['arch']}", ""

    def unit_describe(self, unit):
        if not unit.dims:
            return [("Files", "pure wheels and sdists, every target")]
        return [("Python", unit.dims["python"]), ("Platform", unit.dims["platform"]),
                ("Arch", unit.dims["arch"])]

    # ── config ──────────────────────────────────────────────────────────────

    def to_toml(self, source) -> dict:
        from mmw.core.config import PypiSource
        defaults = PypiSource(provider="pypi")
        d: dict = {"provider": "pypi"}
        for key in ("name", "label", "pythons", "platforms", "arches", "glibc", "musl", "macos",
                    "sdists", "all"):
            if getattr(source, key) != getattr(defaults, key):
                d[key] = getattr(source, key)
        return d

    # ── config → units ──────────────────────────────────────────────────────

    def expand(self, source) -> list:
        from mmw.core.config import MirrorConfig
        if not source.all:
            return []
        ts = targets(source.pythons, source.platforms, source.arches)
        common = dict(provider="pypi", source_url=source.url, source=source.identity, label=source.label)
        units = [MirrorConfig(name=f"{source.name}-all-universal", options={"targets": [t.key for t in ts]},
                              **common)]
        units += [MirrorConfig(name=f"{source.name}-all-{t.key}",
                               dims={"python": t.python, "platform": t.platform, "arch": t.arch}, **common)
                  for t in ts]
        return units

    def covers(self, source, unit) -> bool:
        return source.covers(unit)

    def needs_resolving(self, source) -> bool:
        return source.all

    def resolve(self, source, *, progress=None, context=None):
        from mmw.core.resolve import ResolvedSource
        say = progress or (lambda _m: None)
        context = context or ResolveContext()
        notes: list[str] = []
        errors: list[str] = []
        if not source.all:
            notes.append(f"{source.name}: nothing selected (all / requirements)")
            return ResolvedSource(source, [], notes, errors)
        cache = index.IndexCache(context.mirror_dir)
        say(f"{source.name}: reading the PyPI index…")
        refreshed = index.refresh(cache, index._client, threads=context.max_connections, say=say,
                                  stop_event=context.stop_event)
        if refreshed.stopped:
            errors.append(f"{source.name}: stopped while reading the PyPI index "
                          f"({len(cache.names())} projects cached; the next sync continues)")
            return ResolvedSource(source, [], notes, errors)
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
            return ResolvedSource(source, [], notes, errors)
        if universe.failures:
            notes.append(f"{source.name}: core metadata unreadable for {len(universe.failures)} file(s); "
                         f"their dependencies are reported unknown: {universe.failures[0]}")
        with self._lock:
            self.catalog[source.identity] = _Catalog(keep, report, cache)
        files = {f.filename: f for unit in keep.files.values() for f in unit.values()}
        units = self.expand(source)
        notes.append(f"{source.name}: {len(cache.names())} projects → {len(files)} files, "
                     f"{fmt_size(sum(f.size for f in files.values()))} across {len(units)} units; "
                     f"{report.summary()}")
        return ResolvedSource(source, units, notes, errors)

    # ── fetch ───────────────────────────────────────────────────────────────

    def progress_unit(self, mirror) -> ProgressUnit:
        return ProgressUnit("releases", mirror.source)

    def fetch_all(self, mirror_config, client, status_callback=None, history=None):
        cat = self.catalog.get(mirror_config.source)
        if cat is None:
            raise RuntimeError(f"{mirror_config.name}: the PyPI selection was not resolved "
                               "in this process — sync resolves first; re-run the sync")
        history = history or UnitHistory.empty()
        key = _unit_key(mirror_config)
        kept = sorted(cat.keep.files.get(key, {}).values(), key=lambda f: f.repo_path)
        current = [_package(f) for f in kept]
        held = {p.repo_path for p in current}
        before = hist.load(history.read(hist.HISTORY_PATH))
        advanced = hist.advance(before, current, date.today().isoformat())
        retained = [p for p in hist.retained(advanced) if p.repo_path not in held]
        packages = current + retained
        # Every kept wheel's PEP 658 metadata, by hash and without its body: the
        # engine skips what the pool holds and fetches only the rest, so a
        # million-wheel unit never holds a million bodies.
        metadata = [MetadataFile(url=f.url + ".metadata", repo_path=f.repo_path + ".metadata",
                                 expected_sha256=f.metadata_sha256)
                    for f in kept if f.kind == "wheel" and f.metadata_sha256]
        # A retained wheel keeps its core metadata, known from the unit's own refs.
        for p in retained:
            checksum = history.checksum(p.repo_path + ".metadata")
            if checksum is not None:
                metadata.append(MetadataFile(url=p.url + ".metadata", repo_path=p.repo_path + ".metadata",
                                             expected_sha256=checksum))
        report = cat.report if key == UNIVERSAL else cat.report.for_units({key})
        metadata.append(MetadataFile(url="", repo_path=REPORT_PATH, content=report.to_json()))
        metadata.append(MetadataFile(url="", repo_path=hist.HISTORY_PATH, content=hist.dump(advanced)))
        return packages, metadata

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
