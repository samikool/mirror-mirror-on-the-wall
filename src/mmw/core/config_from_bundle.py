"""Reconstruct configuration from settings a kept bundle actually preserved."""
import json
from collections import defaultdict
from pathlib import Path

from mmw.core import config
from mmw.core.publisher.kept import read_manifest_header


_APT_OPTIONS = ("include_sources", "gpg_key_url", "gpg_key", "gpg_key_name", "extra_files", "dist_upgrade")
_MISSING = {
    "vscode": "kinds, platforms, qualities and extension selections",
    "pypi": "Python/platform targets, requirements and compatibility floors",
    "apt-local": "the local folder and signing-key name",
}


def _source(units: list[config.MirrorConfig]) -> config.SourceConfig:
    first = units[0]
    options = {key: getattr(first, key) for key in _APT_OPTIONS}
    for unit in units:
        if unit.provider != first.provider or unit.label != first.label or any(
                getattr(unit, key) != value for key, value in options.items()):
            raise ValueError(f"recorded settings disagree for source {first.source or first.source_url}")
    suites = list(dict.fromkeys(u.suite for u in units if u.suite))
    components = {suite: list(dict.fromkeys(u.component for u in units if u.suite == suite and u.component))
                  for suite in suites}
    common = components[suites[0]] if suites else []
    suffix = (f"-{first.suite}-{first.component}-{first.arch}" if first.suite and first.component
              else f"-{first.arch}")
    name = first.name
    if first.provider != "apt-flat" and name.endswith(suffix):
        name = name[:-len(suffix)]
    return config.SourceConfig(
        url=first.source_url, provider=first.provider, name=name, label=first.label,
        arches=list(dict.fromkeys(u.arch for u in units if u.arch)), suites=suites, components=common,
        suite_overrides=[config.SuiteOverride(suite=suite, components=components[suite])
                         for suite in suites[1:] if components[suite] != common], **options)


def config_from_bundles(paths: list[Path], mirror_dir: Path) -> tuple[str, list[str]]:
    """Return TOML and notes; each network uses only its newest supplied manifest."""
    if not paths:
        raise ValueError("choose at least one bundle")
    latest = {}
    for path in paths:
        header = read_manifest_header(path)
        if header is None:
            raise ValueError(f"{path}: cannot read a format 2 or 3 manifest")
        network = header["network"]
        if network not in latest or header["created"] > latest[network][0]["created"]:
            latest[network] = header, path
    networks, groups = [], defaultdict(list)
    notes = [f"mirror_dir is not recorded; using {mirror_dir}.",
             "Other global settings (connections, threads and database location) use defaults.",
             "Sources outside these bundles and networks never bundled cannot be recovered."]
    for name, (header, path) in latest.items():
        try:
            settings = header.get("settings")
            settings = json.loads(settings) if isinstance(settings, str) else settings
            if not isinstance(settings, dict) or not isinstance(settings.get("network"), dict) or not isinstance(settings.get("mirrors"), list):
                raise ValueError("network and mirrors settings are missing")
            if settings["network"].get("name") != name:
                raise ValueError("settings network does not match the manifest's network")
            network = dict(settings["network"])
            selections = network.get("select", [])
            if not isinstance(selections, list) or any(not isinstance(sel, dict) for sel in selections):
                raise ValueError("network selection settings must be a list of tables")
            network["select"] = [{**{k: v for k, v in sel.items() if k != "dims"}, **sel.get("dims", {})}
                                 for sel in selections]
            networks.append(config.NetworkConfig.model_validate(network))
            for recorded in settings["mirrors"]:
                unit = config.MirrorConfig.model_validate(recorded)
                identity = unit.source or config.normalize_url(unit.source_url)
                if unit.provider not in ("apt", "apt-flat"):
                    note = (f"{unit.provider} source {identity} was not recovered: "
                            f"{_MISSING.get(unit.provider, 'its source settings')} were not recorded; add it again.")
                    if note not in notes:
                        notes.append(note)
                    continue
                if unit not in groups[identity]:
                    groups[identity].append(unit)
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError(f"{path}: cannot recover settings: {exc}") from exc
    sources, _, _ = config.merge_sources([], [_source(units) for units in groups.values()])
    cfg = config.Config(settings=config.Settings(mirror_dir=mirror_dir), sources=sources, networks=networks)
    return config.dumps(cfg), notes
