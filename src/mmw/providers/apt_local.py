"""`apt-local`: a folder of `.deb` files, served as a signed apt repository.

The folder is the upstream. Resolve and fetch scan it; the packages are
pooled through the ordinary downloader (`file://`), and the index files are
built here and handed to sync already filled in, signed with an mmw key. From
the pool on, it is a repository like any other: snapshots, publish, bundle.
"""
from mmw.providers.base import BaseProvider, Dimension, SourceDescription, SourceField


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

    # ── sync (Task 5) ────────────────────────────────────────────────────────

    def resolve(self, source, *, progress=None, context=None):
        raise NotImplementedError

    def fetch_packages(self, mirror_config, client, status_callback=None):
        raise NotImplementedError

    def fetch_metadata(self, mirror_config, client):
        raise NotImplementedError
