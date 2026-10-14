"""VscodeSource in mmw.toml: load, validate, round-trip; apt files unchanged."""
import pytest
from pydantic import ValidationError

from mmw.core import config

VS = """[[sources]]
provider = "vscode"
kinds = ["extensions", "server"]
platforms = ["linux-x64"]

[sources.extensions]
top_n = 200
ids = ["golang.go", "MS-Python.Python"]
"""


def _cfg(tmp_path, body: str) -> config.Config:
    (tmp_path / "mmw.toml").write_text(f'[settings]\nmirror_dir = "{tmp_path}/m"\n\n' + body)
    return config.load(tmp_path)


def test_a_vscode_source_loads(tmp_path):
    (src,) = _cfg(tmp_path, VS).sources
    assert isinstance(src, config.VscodeSource)
    assert src.identity == "https://marketplace.visualstudio.com"
    assert src.extensions.top_n == 200
    assert src.extensions.ids == ["golang.go", "ms-python.python"]
    assert src.name == "vscode"


def test_round_trip_is_minimal_and_stable(tmp_path):
    cfg = _cfg(tmp_path, VS)
    config.save(cfg, tmp_path)
    first = (tmp_path / "mmw.toml").read_text()
    assert 'provider = "vscode"' in first and "url" not in first and "name" not in first
    config.save(config.load(tmp_path), tmp_path)
    assert (tmp_path / "mmw.toml").read_text() == first


def test_an_apt_file_without_provider_keys_still_loads_and_saves_identically(tmp_path):
    body = '[[sources]]\nurl = "http://archive.ubuntu.com/ubuntu"\nsuites = ["noble"]\n'
    cfg = _cfg(tmp_path, body)
    assert isinstance(cfg.sources[0], config.SourceConfig)
    config.save(cfg, tmp_path)
    once = (tmp_path / "mmw.toml").read_text()
    config.save(config.load(tmp_path), tmp_path)
    assert (tmp_path / "mmw.toml").read_text() == once


@pytest.mark.parametrize("bad,match", [
    ('kinds = ["plugins"]', "plugins"),
    ('platforms = ["amiga"]', "amiga"),
    ('qualities = ["nightly"]', "nightly"),
    ('url = "https://open-vsx.org"', "marketplace"),
])
def test_bad_values_fail_loudly(tmp_path, bad, match):
    with pytest.raises(ValidationError, match=match):
        _cfg(tmp_path, f'[[sources]]\nprovider = "vscode"\n{bad}\n')


def test_bad_extension_id(tmp_path):
    with pytest.raises(ValidationError, match="publisher.name"):
        _cfg(tmp_path, '[[sources]]\nprovider = "vscode"\n[sources.extensions]\nids = ["nodot"]\n')


def test_only_one_vscode_source(tmp_path):
    with pytest.raises(ValidationError, match="one vscode source"):
        _cfg(tmp_path, '[[sources]]\nprovider = "vscode"\n\n[[sources]]\nprovider = "vscode"\nname = "b"\n')


def test_merge_replaces_a_vscode_source():
    old = config.VscodeSource(provider="vscode", kinds=["desktop"])
    new = config.VscodeSource(provider="vscode", kinds=["server"])
    merged, added, updated = config.merge_sources([old], [new])
    assert merged[0].kinds == ["server"] and (added, updated) == (0, 1)
