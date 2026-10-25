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


def _stub_mmw(home: Path, version: str, real: str | None = None) -> None:
    """A remote user's ~/.local/bin/mmw that says `version` (and forwards the rest to `real`)."""
    b = home / ".local" / "bin"
    b.mkdir(parents=True, exist_ok=True)
    rest = f'exec "{real}" "$@"' if real else "exit 1"
    (b / "mmw").write_text(f'#!/bin/sh\n[ "$1" = --version ] && {{ echo "mmw {version}"; exit 0; }}\n{rest}\n')
    (b / "mmw").chmod(0o755)


def test_version_skew_is_refused_until_installed(monkeypatch, tmp_path):
    import io
    import sys
    import tarfile

    from mmw.core.runtime import mmw_version
    from mmw.site.mset import root_at
    home = tmp_path / "remote-home"
    monkeypatch.setenv("FAKE_REMOTE_HOME", str(home))
    monkeypatch.setenv("PATH", f"{FAKE}:/usr/bin:/bin")              # the remote has no mmw of its own
    ssh.forget_checked()
    with pytest.raises(ssh.RemoteMmw, match="isn't installed on farhost"):
        root_at(f"farhost:{tmp_path / 'r'}")
    _stub_mmw(home, "0.1.0")
    ssh.forget_checked()
    with pytest.raises(ssh.RemoteMmw, match=r"older \(0\.1\.0 < "):
        root_at(f"farhost:{tmp_path / 'r'}")
    # An installer whose install.sh puts a current mmw on the remote's path.
    real = str(Path(sys.executable).with_name("mmw"))
    script = (f'#!/bin/sh\nmkdir -p "$HOME/.local/bin"\nprintf \'#!/bin/sh\\n[ "$1" = --version ] && '
              f'{{ echo "mmw {mmw_version()}"; exit 0; }}\\nexec "{real}" "$@"\\n\' > "$HOME/.local/bin/mmw"\n'
              'chmod 755 "$HOME/.local/bin/mmw"\n').encode()
    inst = tmp_path / f"mmw-{mmw_version()}-x86_64.tar"
    with tarfile.open(inst, "w") as t:
        info = tarfile.TarInfo(f"mmw-{mmw_version()}/install.sh")
        info.size, info.mode = len(script), 0o755
        t.addfile(info, io.BytesIO(script))
    ssh.install_remote("farhost", inst)
    ssh.forget_checked()
    assert root_at(f"farhost:{tmp_path / 'r'}") is None                # mmw there now; no mirror yet


def test_install_remote_streams_the_installer_tar(tmp_path, fake_ssh):
    import tarfile
    inst = tmp_path / "mmw-9.9.9-x86_64.tar"
    script = tmp_path / "install.sh"
    script.write_text("#!/bin/sh\necho installed\n")
    with tarfile.open(inst, "w") as t:
        t.add(script, arcname="mmw-9.9.9/install.sh")
    ssh.install_remote("farhost", inst)
    log = fake_ssh.read_bytes()
    assert b"STDIN:" in log and b"mmw-9.9.9/install.sh" in log and b"scp" not in log


def test_cli_open_install_pushes_then_opens(tmp_path, fake_ssh, monkeypatch):
    import tarfile

    from typer.testing import CliRunner

    from mmw import cli
    inst = tmp_path / "mmw-9.9.9-x86_64.tar"
    script = tmp_path / "install.sh"
    script.write_text("#!/bin/sh\necho installed\n")
    with tarfile.open(inst, "w") as t:
        t.add(script, arcname="mmw-9.9.9/install.sh")
    r = CliRunner().invoke(cli.app, ["site", "open", f"farhost:{tmp_path / 'nothing'}", "--install", str(inst)])
    assert "mmw installed on farhost" in r.output and "no mirror at" in r.output
    monkeypatch.setenv("FAKE_SSH_MODE", "password")
    r = CliRunner().invoke(cli.app, ["site", "open", f"farhost:{tmp_path}"])
    assert r.exit_code == 1 and "needs key-based ssh access" in r.output
