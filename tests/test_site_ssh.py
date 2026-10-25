"""Roots over ssh: one runner, key auth only, nothing secret ever."""
import os
from pathlib import Path

import pytest

from mmw.site import ssh

FAKE = Path(__file__).parent / "fake_ssh"


@pytest.fixture(autouse=True)
def fake_ssh(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", f"{FAKE}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SSH_LOG", str(tmp_path / "ssh.log"))
    return tmp_path / "ssh.log"


def test_parse_location():
    assert ssh.parse_location("lnxp005:/data/mirror") == ("ssh", "lnxp005", "/data/mirror")
    assert ssh.parse_location("sam@10.10.31.31:/data/m") == ("ssh", "sam@10.10.31.31", "/data/m")
    assert ssh.parse_location("/srv/mirror") == ("local", None, "/srv/mirror")
    assert ssh.parse_location("./a:b") == ("local", None, "./a:b")
    assert ssh.parse_location("C:\\mirror") == ("local", None, "C:\\mirror")
    assert ssh.parse_location("relative/dir") == ("local", None, "relative/dir")


def test_argv_is_batch_mode_and_strict():
    argv = ssh.ssh_argv("lnxp005", ["mmw", "site", "_root", "info", "/data/m"])
    assert argv[:7] == ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15"]
    assert argv[7] == "lnxp005"


def test_no_secret_options_ever():
    argv = " ".join(ssh.ssh_argv("h", ["true"]))
    for bad in ("-i ", "IdentityFile", "PasswordAuthentication", "sshpass", "ASKPASS"):
        assert bad not in argv


def test_password_prompt_is_a_hard_fail(monkeypatch):
    monkeypatch.setenv("FAKE_SSH_MODE", "password")
    with pytest.raises(ssh.SshError) as e:
        ssh.check_access("lnxp005")
    assert str(e.value) == ("lnxp005 needs key-based ssh access for mmw (a security requirement) — "
                            "configure it and try again")


def test_unknown_host_key_is_a_hard_fail(monkeypatch):
    monkeypatch.setenv("FAKE_SSH_MODE", "hostkey")
    with pytest.raises(ssh.SshError, match="host key isn't known"):
        ssh.check_access("lnxp005")


def test_ssh_stderr_never_echoed(monkeypatch):
    monkeypatch.setenv("FAKE_SSH_MODE", "password")
    with pytest.raises(ssh.SshError) as e:
        ssh.run("lnxp005", ["true"])
    assert "secret-hint" not in str(e.value)
    monkeypatch.setenv("FAKE_SSH_MODE", "down")
    with pytest.raises(ssh.SshError, match="can't reach lnxp005"):
        ssh.run("lnxp005", ["true"])


def test_remote_paths_are_quoted(tmp_path):
    odd = tmp_path / "a dir; touch PWNED"
    odd.mkdir()
    out = ssh.run("h", ["ls", "-d", str(odd)])
    assert out.decode().strip() == str(odd)
    assert not (Path.cwd() / "PWNED").exists() and not (tmp_path / "PWNED").exists()


def test_run_returns_output_and_takes_input():
    assert ssh.run("h", ["cat"], input=b"hello") == b"hello"
