"""The far side updates from the installer next to a bundle — every machine or none."""
import platform
from pathlib import Path
from types import SimpleNamespace

import pytest

from mmw.core import update as U
from mmw.site import update as F

ARCH = {"arm64": "aarch64"}.get(platform.machine(), platform.machine())
OTHER = "aarch64" if ARCH == "x86_64" else "x86_64"


@pytest.fixture(autouse=True)
def _version(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "current_version", lambda: "1.2.0")
    monkeypatch.setattr(U, "installer_prefix", lambda: tmp_path / "prefix")   # installed by install.sh


def _beside(tmp_path, *names):
    b = tmp_path / "oak-20261101T000000Z.tar"
    b.write_bytes(b"bundle")
    for n in names:
        (tmp_path / n).write_bytes(b"installer")
    return b


def test_installer_beside_bundle_found(tmp_path):
    b = _beside(tmp_path, f"mmw-1.3.0-{ARCH}.tar", f"mmw-1.2.5-{ARCH}.tar")
    assert F.installer_beside(str(b), ARCH) == (tmp_path / f"mmw-1.3.0-{ARCH}.tar", "1.3.0")
    stage = tmp_path / "oak-20261101T000000Z"
    stage.mkdir()
    assert F.installer_beside(str(stage), ARCH)[1] == "1.3.0"         # next to a stage folder too
    assert F.installer_beside("-", ARCH) is None                       # stdin: nothing beside it


def test_older_or_other_arch_installer_ignored(tmp_path):
    b = _beside(tmp_path, f"mmw-1.1.0-{ARCH}.tar", f"mmw-9.0.0-{OTHER}.tar")
    assert F.installer_beside(str(b), ARCH) is None


def _root(name, arch, kind="ssh"):
    return SimpleNamespace(info=SimpleNamespace(name=name, location=f"h:/{name}" if kind == "ssh" else f"/{name}"),
                           arch=lambda: arch, host="h" if kind == "ssh" else None, kind=kind)


def test_a_root_without_its_installer_stops_everything(tmp_path, monkeypatch):
    b = _beside(tmp_path, f"mmw-1.3.0-{ARCH}.tar")
    mset = SimpleNamespace(roots=[_root("main", ARCH, "local"), _root("far", OTHER)])
    far = F.far_offer(str(b), mset)
    problem = F.preflight(far)
    assert problem is not None and "far" in problem and OTHER in problem
    monkeypatch.setattr(F, "_install_remote", lambda *a: pytest.fail("updated a remote"))
    monkeypatch.setattr(F, "_install_here", lambda *a: pytest.fail("updated this machine"))
    with pytest.raises(ValueError, match=OTHER):
        F.apply_far_update(far, print)


def test_remotes_update_before_this_machine(tmp_path, monkeypatch):
    b = _beside(tmp_path, f"mmw-1.3.0-{ARCH}.tar", f"mmw-1.3.0-{OTHER}.tar")
    mset = SimpleNamespace(roots=[_root("main", ARCH, "local"), _root("far", OTHER)])
    order = []
    monkeypatch.setattr(F, "_install_remote", lambda root, tar, log: order.append(("remote", root.info.name, tar.name)))
    monkeypatch.setattr(F, "_install_here", lambda tar, log: order.append(("here", tar.name)))
    F.apply_far_update(F.far_offer(str(b), mset), print)
    assert order == [("remote", "far", f"mmw-1.3.0-{OTHER}.tar"), ("here", f"mmw-1.3.0-{ARCH}.tar")]


def test_no_offer_without_a_newer_installer(tmp_path):
    b = _beside(tmp_path)
    assert F.far_offer(str(b), SimpleNamespace(roots=[])) is None


def test_cli_apply_offers_before_applying(tmp_path, monkeypatch):
    from mmw.commands import site as cmd
    monkeypatch.delenv("MMW_NO_UPDATE_CHECK", raising=False)
    b = _beside(tmp_path, f"mmw-1.3.0-{ARCH}.tar")
    monkeypatch.setattr(cmd, "_interactive_far", lambda: True)
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or "n")
    applied = []
    monkeypatch.setattr("mmw.site.apply.apply_to_set", lambda *a, **k: applied.append(1) or (_ for _ in ()).throw(
        FileNotFoundError("stop here")))
    import io
    import sys
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    cmd.apply(str(b), root=[f"*={tmp_path / 'r'}"])
    assert asked and "1.3.0" in err.getvalue() and applied == [1]     # asked first, No still applies


# ── review findings ──────────────────────────────────────────────────────────

def test_this_machine_checked_before_any_remote(tmp_path, monkeypatch):
    b = _beside(tmp_path, f"mmw-1.3.0-{ARCH}.tar", f"mmw-1.3.0-{OTHER}.tar")
    mset = SimpleNamespace(roots=[_root("far", OTHER)])
    monkeypatch.setattr(U, "installer_prefix", lambda: None)             # e.g. pip-installed here
    far = F.far_offer(str(b), mset)
    assert "installer" in F.preflight(far)
    monkeypatch.setattr(F, "_install_remote", lambda *a: pytest.fail("updated a remote first"))
    with pytest.raises(ValueError):
        F.apply_far_update(far, print)


def test_a_root_that_doesnt_say_its_arch_is_asked(monkeypatch):
    from mmw.site import ssh, sshroot
    root = sshroot.SshRoot("h:/x", {"path": "/x", "name": "x", "set_id": "s", "network": "n", "members": ["x"],
                                     "hostnames": []})
    monkeypatch.setattr(ssh, "run", lambda host, argv, **k: b"aarch64\n" if argv == ["uname", "-m"] else b"")
    assert root.arch() == "aarch64"                                    # never assumed x86_64


# ── an ssh root without mmw: the installer to push there ────────────────────

def test_installer_for_a_remote_is_at_least_this_version(tmp_path):
    b = _beside(tmp_path, "mmw-1.0.0-x86_64.tar", "mmw-1.2.0-x86_64.tar", "mmw-1.3.0-aarch64.tar")
    # The remote must be no older than this machine: 1.2.0 (here) is fine, 1.0.0 isn't.
    assert F.installer_for("x86_64", [str(b)], "1.2.0").name == "mmw-1.2.0-x86_64.tar"
    assert F.installer_for("aarch64", [str(b)], "1.2.0").name == "mmw-1.3.0-aarch64.tar"
    assert F.installer_for("x86_64", [str(b)], "1.3.0") is None


def test_installer_for_looks_beside_every_recent_bundle(tmp_path):
    (tmp_path / "old").mkdir()
    (tmp_path / "new").mkdir()
    old = _beside(tmp_path / "old", "mmw-1.2.0-x86_64.tar")
    new = _beside(tmp_path / "new")
    assert F.installer_for("x86_64", [str(new), str(old), "-", str(tmp_path / "gone" / "b.tar")],
                           "1.2.0").parent == old.parent


def test_remote_arch_asks_the_machine(monkeypatch):
    from mmw.site import ssh
    monkeypatch.setattr(ssh, "run", lambda host, argv: b"arm64\n" if argv == ["uname", "-m"] else b"")
    assert ssh.remote_arch("nas") == "aarch64"
