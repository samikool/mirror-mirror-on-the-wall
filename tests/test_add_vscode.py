"""`mmw add vscode` writes the same source the TUI form will."""
from typer.testing import CliRunner

from mmw.cli import app
from mmw.core import config

runner = CliRunner()


def _init(tmp_path):
    config.save(config.Config(settings=config.Settings(mirror_dir=tmp_path / "m")), tmp_path)


def _run(tmp_path, *args):
    return runner.invoke(app, ["--config", str(tmp_path), "add", "vscode", *args])


def test_adds_a_vscode_source(tmp_path):
    _init(tmp_path)
    ids = tmp_path / "ids.txt"
    ids.write_text("golang.go\n# a comment\n\nms-python.python\n")
    r = _run(tmp_path, "--kind", "extensions", "--platform", "linux-x64",
             "--top-n", "50", "--ids-file", str(ids), "--id", "redhat.vscode-yaml")
    assert r.exit_code == 0, r.output
    (src,) = config.load(tmp_path).sources
    assert src.provider == "vscode" and src.kinds == ["extensions"]
    assert src.extensions.top_n == 50
    assert src.extensions.ids == ["golang.go", "ms-python.python", "redhat.vscode-yaml"]


def test_running_it_again_replaces(tmp_path):
    _init(tmp_path)
    assert _run(tmp_path, "--kind", "desktop").exit_code == 0
    assert _run(tmp_path, "--kind", "server").exit_code == 0
    (src,) = config.load(tmp_path).sources
    assert src.kinds == ["server"]


def test_dry_run_writes_nothing(tmp_path):
    _init(tmp_path)
    r = _run(tmp_path, "--all", "--dry-run")
    assert r.exit_code == 0 and 'provider = "vscode"' in r.output
    assert config.load(tmp_path).sources == []


def test_bad_input_exits_2(tmp_path):
    _init(tmp_path)
    r = _run(tmp_path, "--platform", "amiga")
    assert r.exit_code == 2 and "amiga" in r.output


def test_apt_add_honours_the_config_option(tmp_path, monkeypatch):
    """`mmw -c DIR add -f FILE` must write DIR/mmw.toml, not the working
    directory's (it used to ignore -c)."""
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    _init(tmp_path)
    src = tmp_path / "ubuntu.list"
    src.write_text("deb http://archive.ubuntu.com/ubuntu noble main\n")
    r = runner.invoke(app, ["--config", str(tmp_path), "add", "-f", str(src)])
    assert r.exit_code == 0, r.output
    assert [s.url for s in config.load(tmp_path).sources] == ["http://archive.ubuntu.com/ubuntu"]
    assert not (elsewhere / "mmw.toml").exists()


def test_dry_run_works_without_a_config(tmp_path):
    r = _run(tmp_path, "--top-n", "5", "--dry-run")
    assert r.exit_code == 0 and "top_n = 5" in r.output


def test_replacing_says_what_was_replaced(tmp_path):
    _init(tmp_path)
    assert _run(tmp_path, "--top-n", "50").exit_code == 0
    r = _run(tmp_path, "--id", "golang.go")
    assert r.exit_code == 0 and "Replaced" in r.output and "top 50" in r.output
