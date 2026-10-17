"""Every mmw.toml setting is editable from the TUI — that is the TUI's job.

Each config field is mapped to the screen file and widget id that edits it.
A field added to a model without a mapping fails here, so the TUI cannot
quietly fall behind the config file again (it had: settings, a repository's
URL, label, key URL and suite overrides, and a network's name were all
reachable only by editing mmw.toml).
"""
from pathlib import Path

import pytest

from mmw.core import config

TUI = Path(__file__).resolve().parent.parent / "src" / "mmw" / "tui"

# field -> (file under src/mmw/tui, a widget id or code marker that edits it)
EDITORS = {
    ("Settings", "mirror_dir"): ("sections/settings.py", "settings-mirror-dir"),
    ("Settings", "max_connections"): ("sections/settings.py", "settings-max-connections"),
    ("Settings", "theme"): ("sections/settings.py", "settings-theme"),
    ("Settings", "database"): ("sections/settings.py", "settings-database"),

    ("SourceConfig", "url"): ("source_screens.py", "edit-source-url"),
    ("SourceConfig", "provider"): ("source_screens.py", "edit-source-provider"),
    ("SourceConfig", "name"): ("source_screens.py", "edit-source-name"),
    ("SourceConfig", "label"): ("source_screens.py", "edit-source-label"),
    ("SourceConfig", "suites"): ("source_screens.py", "btn-{dim}"),
    ("SourceConfig", "components"): ("source_screens.py", "btn-{dim}"),
    ("SourceConfig", "arches"): ("source_screens.py", "btn-{dim}"),
    ("SourceConfig", "suite_overrides"): ("source_screens.py", "edit-source-overrides"),
    ("SourceConfig", "include_sources"): ("source_screens.py", "include-sources-switch"),
    ("SourceConfig", "gpg_key_url"): ("source_screens.py", "edit-source-key-url"),
    ("SourceConfig", "gpg_key"): ("source_screens.py", "btn-key"),
    ("SourceConfig", "gpg_key_name"): ("source_screens.py", "btn-key"),
    ("SourceConfig", "extra_files"): ("source_screens.py", "edit-source-extra-files"),
    ("SourceConfig", "dist_upgrade"): ("source_screens.py", "du-switch"),

    ("SuiteOverride", "suite"): ("source_screens.py", "edit-source-overrides"),
    ("SuiteOverride", "components"): ("source_screens.py", "edit-source-overrides"),

    ("NetworkConfig", "name"): ("network_screens.py", "inp-net-name"),
    ("NetworkConfig", "root"): ("network_screens.py", "inp-root"),
    ("NetworkConfig", "bundle_dir"): ("network_screens.py", "inp-bundle-dir"),
    ("NetworkConfig", "select"): ("network_screens.py", "repo-checklist"),
    ("NetworkConfig", "prune"): ("network_screens.py", "prune-switch"),
    ("NetworkConfig", "dist_upgrade"): ("network_screens.py", "du-switch"),
    ("NetworkConfig", "certs"): ("network_screens.py", "certs-switch"),
    ("NetworkConfig", "nginx"): ("network_screens.py", "nginx-switch"),

    ("NetworkSelect", "source"): ("network_screens.py", "repo-checklist"),
    # One list per provider dimension (suites/components/arches for apt),
    # all edited by the network editor's per-repository axis block.
    ("NetworkSelect", "dims"): ("network_screens.py", "axis-block"),
}

MODELS = [config.Settings, config.SourceConfig, config.SuiteOverride,
          config.NetworkConfig, config.NetworkSelect]


def test_every_config_field_has_a_tui_editor():
    fields = {(m.__name__, f) for m in MODELS for f in m.model_fields}
    missing = sorted(fields - set(EDITORS))
    assert not missing, f"no TUI editor mapped for: {missing}"


@pytest.mark.parametrize("field,where", sorted(EDITORS.items()))
def test_mapped_editor_exists(field, where):
    filename, marker = where
    assert marker in (TUI / filename).read_text(), f"{field}: {marker!r} not in {filename}"
