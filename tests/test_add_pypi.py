"""`mmw add pypi` writes the config's one PyPI source."""
from typer.testing import CliRunner

from mmw.cli import app
from mmw.core import config

runner = CliRunner()


def _init(tmp_path):
    config.save(config.Config(settings=config.Settings(mirror_dir=tmp_path / "m")), tmp_path)


def _run(tmp_path, *args):
    return runner.invoke(app, ["--config", str(tmp_path), "add", "pypi", *args])


def test_adds_a_pypi_source(tmp_path):
    _init(tmp_path)
    r = _run(tmp_path, "--python", "3.12", "--python", "3.13", "--platform", "linux-glibc",
             "--arch", "amd64", "--arch", "arm64", "--glibc", "2.28", "--all")
    assert r.exit_code == 0, r.output
    assert "Added the pypi source." in r.output
    (src,) = config.load(tmp_path).sources
    assert src.provider == "pypi" and src.pythons == ["3.12", "3.13"] and src.arches == ["amd64", "arm64"]
    assert src.glibc == "2.28" and src.all and not src.sdists


def test_running_it_again_replaces_and_keeps_other_sources(tmp_path):
    _init(tmp_path)
    assert runner.invoke(app, ["--config", str(tmp_path), "add", "vscode", "--kind", "server"]).exit_code == 0
    assert _run(tmp_path, "--all").exit_code == 0
    r = _run(tmp_path, "--all", "--sdists")
    assert r.exit_code == 0 and "Replaced the pypi source" in r.output
    providers = sorted(s.provider for s in config.load(tmp_path).sources)
    assert providers == ["pypi", "vscode"]
    assert next(s for s in config.load(tmp_path).sources if s.provider == "pypi").sdists


def test_dry_run_writes_nothing(tmp_path):
    r = _run(tmp_path, "--all", "--dry-run")
    assert r.exit_code == 0 and 'provider = "pypi"' in r.output and "all = true" in r.output


def test_bad_input_exits_2(tmp_path):
    _init(tmp_path)
    r = _run(tmp_path, "--python", "2.7")
    assert r.exit_code == 2 and "Not added" in r.output


def test_apt_add_rejects_two_arches(tmp_path):
    _init(tmp_path)
    src = tmp_path / "x.list"
    src.write_text("deb http://archive.ubuntu.com/ubuntu noble main\n")
    r = runner.invoke(app, ["--config", str(tmp_path), "add", "-f", str(src), "--arch", "amd64", "--arch", "arm64"])
    assert r.exit_code == 2 and "one --arch" in r.output


def test_add_pypi_req_flags(tmp_path):
    _init(tmp_path)
    r = _run(tmp_path, "--req", "requests", "--req", "numpy<2", "--requirements-file", "r.txt")
    assert r.exit_code == 0, r.output
    (src,) = config.load(tmp_path).sources
    assert src.requirements == ["requests", "numpy<2"] and src.requirements_file == "r.txt"
