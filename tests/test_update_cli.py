"""The CLI offers a newer mmw at the end of a command; `mmw update`."""
import time

import pytest
from typer.testing import CliRunner

from mmw import cli
from mmw.commands import update as cmd
from mmw.core import update as U


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.delenv("MMW_NO_UPDATE_CHECK", raising=False)


def _checker(newest=None, slow=False):
    c = U.Checker(current="1.2.0")
    if slow:
        c.start(fetch=lambda url, timeout: time.sleep(5) or b"{}")
    else:
        c.newest = newest
        c._done.set()
    return c


def test_offline_check_adds_no_delay_and_no_noise(capsys):
    started = time.monotonic()
    cmd.after_command(_checker(slow=True), None)
    assert time.monotonic() - started < 2.0
    assert capsys.readouterr().err == ""


def test_no_terminal_means_one_line_never_a_prompt(capsys, monkeypatch):
    monkeypatch.setattr(cmd, "_interactive", lambda: False)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("prompted without a terminal"))
    cmd.after_command(_checker("1.3.0"), None)
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "mmw 1.3.0 is available (you have 1.2.0) — mmw update" in err


def test_terminal_yes_runs_the_update(capsys, monkeypatch):
    monkeypatch.setattr(cmd, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "")               # Enter: the default, yes
    monkeypatch.setattr(U, "detect_install", lambda: U.InstallKind("uv-tool", ["true"], "uv"))
    ran = []
    monkeypatch.setattr(U, "run_update", lambda kind, progress=print: ran.append(kind) or (True, ""))
    cmd.after_command(_checker("1.3.0"), None)
    assert ran and "mmw updated to 1.3.0" in capsys.readouterr().err


def test_major_defaults_to_no(capsys, monkeypatch):
    monkeypatch.setattr(cmd, "_interactive", lambda: True)
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or "")
    monkeypatch.setattr(U, "run_update", lambda *a, **k: pytest.fail("updated on a default no"))
    c = U.Checker(current="1.9.0")
    c.newest = "2.0.0"
    c._done.set()
    cmd.after_command(c, None)
    err = capsys.readouterr().err
    assert asked and "[y/N]" in err and "major" in err


def test_site_commands_never_check(monkeypatch, tmp_path):
    monkeypatch.setattr(U.Checker, "start", lambda self, fetch=None: pytest.fail("checked the network"))
    CliRunner().invoke(cli.app, ["site", "roots"])
    CliRunner().invoke(cli.app, ["update", "--help"])


def test_update_command_when_current(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "newest_release", lambda **k: U.current_version())
    (tmp_path / "mmw.toml").write_text(f'[settings]\nmirror_dir = "{tmp_path / "pool"}"\n')
    r = CliRunner().invoke(cli.app, ["--config", str(tmp_path), "update"])
    assert r.exit_code == 0 and "is the newest" in r.output


def test_update_command_runs_the_upgrade(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "newest_release", lambda **k: "99.0.0")
    monkeypatch.setattr(U, "detect_install", lambda: U.InstallKind("venv", ["true"], "pip"))
    monkeypatch.setattr(U, "run_update", lambda kind, progress=print: (True, ""))
    (tmp_path / "mmw.toml").write_text(f'[settings]\nmirror_dir = "{tmp_path / "pool"}"\n')
    r = CliRunner().invoke(cli.app, ["--config", str(tmp_path), "update", "--yes"])
    assert r.exit_code == 0 and "mmw updated to 99.0.0" in r.output


# ── review findings ──────────────────────────────────────────────────────────

def test_a_pipe_on_stdout_is_never_prompted(monkeypatch):
    import sys
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    assert not cmd._interactive()


def test_prompt_goes_to_stderr_and_ctrl_c_is_no(capsys, monkeypatch):
    monkeypatch.setattr(cmd, "_interactive", lambda: True)

    def interrupted(prompt=""):
        assert prompt == ""                                            # never written to stdout
        raise KeyboardInterrupt
    monkeypatch.setattr("builtins.input", interrupted)
    monkeypatch.setattr(U, "run_update", lambda *a, **k: pytest.fail("updated after Ctrl-C"))
    cmd.after_command(_checker("1.3.0"), None)
    out = capsys.readouterr()
    assert "Update now?" in out.err and out.out == ""


def test_help_and_errors_never_check(monkeypatch, tmp_path):
    monkeypatch.setattr(U.Checker, "start", lambda self, fetch=None: pytest.fail("checked for --help"))
    monkeypatch.setattr("sys.argv", ["mmw", "sync", "--help"])
    CliRunner().invoke(cli.app, ["sync", "--help"])


def test_no_offer_after_an_exception(monkeypatch):
    import sys
    monkeypatch.setattr(cmd, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("asked after Ctrl-C"))
    try:
        raise KeyboardInterrupt
    except KeyboardInterrupt:
        cmd.after_command(_checker("1.3.0"), None, exc=sys.exc_info()[1])
