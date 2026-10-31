"""Client sources files: one-line `.list` and deb822 `.sources`, round-tripped."""
import pytest
from typer.testing import CliRunner

from mmw.cli import app
from mmw.core import config
from mmw.core.apt_sources import AptEntry, parse_apt_sources, render_deb822, render_one_line
from mmw.providers import apt_local


@pytest.mark.parametrize("render", [render_one_line, render_deb822])
@pytest.mark.parametrize("entry", [AptEntry("https://t.lab/apt", None, [], ["amd64"]),
                                   AptEntry("https://t.lab/apt", "stable", ["main"], []),
                                   AptEntry("https://t.lab/apt", "jammy", ["main", "tools"], ["amd64", "arm64"])])
def test_rendered_entries_parse_back(render, entry):
    [back] = parse_apt_sources(render(entry, signed_by="/etc/apt/keyrings/k.gpg"))
    assert (back.url, back.suite, back.components, back.arches) == \
        (entry.url, entry.suite, entry.components, entry.arches)
    assert back.gpg_key_name == "k.gpg"


def test_flat_one_line_and_deb822_text():
    e = AptEntry("https://t.lab/apt", None, [], [])
    assert render_one_line(e, signed_by="/k.gpg") == "deb [signed-by=/k.gpg] https://t.lab/apt ./\n"
    assert render_one_line(e, signed_by=None) == "deb https://t.lab/apt ./\n"
    assert render_deb822(e, signed_by="/k.gpg") == (
        "Types: deb\nURIs: https://t.lab/apt\nSuites: ./\nSigned-By: /k.gpg\n")
    s = AptEntry("https://t.lab/apt", "stable", ["main"], ["amd64"])
    assert render_one_line(s, signed_by=None, trusted=True) == \
        "deb [arch=amd64 trusted=yes] https://t.lab/apt stable main\n"
    assert render_deb822(s, signed_by=None, trusted=True) == (
        "Types: deb\nURIs: https://t.lab/apt\nSuites: stable\nComponents: main\n"
        "Architectures: amd64\nTrusted: yes\n")


def _src(**kw):
    return config.LocalAptSource(provider="apt-local", url="https://tools.lab/apt", folder="/d",
                                 key="tools", **kw)


def test_sources_file_names_the_key_install_command():
    src = _src()
    text = apt_local.sources_file(src, "list")
    assert f"curl -fsSL https://tools.lab/mmw-keys/{src.label}/tools.gpg" in text
    assert text.rstrip().endswith("deb [signed-by=/etc/apt/keyrings/tools.gpg] https://tools.lab/apt ./")
    deb822 = apt_local.sources_file(_src(structured=True), "deb822")
    assert "Suites: stable\nComponents: main\nSigned-By: /etc/apt/keyrings/tools.gpg\n" in deb822
    with pytest.raises(ValueError):
        apt_local.sources_file(src, "yaml")


def test_cli_prints_it(tmp_path):
    src = _src()
    config.save(config.Config(settings=config.Settings(mirror_dir=tmp_path / "m"), sources=[src]), tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(tmp_path), "sources-file", src.name, "--format", "list"])
    assert result.exit_code == 0, result.output
    assert result.output.rstrip().endswith("https://tools.lab/apt ./")
    result = runner.invoke(app, ["--config", str(tmp_path), "sources-file", src.name])
    assert "Types: deb" in result.output
    result = runner.invoke(app, ["--config", str(tmp_path), "sources-file", "nope"])
    assert result.exit_code == 1 and "no folder repository named nope" in result.output
