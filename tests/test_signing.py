"""mmw's signing keyring over the system gpg."""
import shutil
import subprocess

import pytest

from mmw.core import signing
from mmw.core.signing import Keyring, SigningError

pytestmark = pytest.mark.skipif(shutil.which("gpg") is None or shutil.which("gpgv") is None,
                                reason="needs gpg and gpgv")


def gpgv_ok(tmp_path, public_armor: str, signature: bytes, data: bytes) -> bool:
    """Verify a detached signature with gpgv against only `public_armor`."""
    work = tmp_path / "gpgv"
    work.mkdir(exist_ok=True)
    keyring = work / "key.gpg"
    home = work / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    subprocess.run(["gpg", "--homedir", str(home), "--batch", "--yes", "--dearmor", "-o", str(keyring)],
                   input=public_armor.encode(), check=True, capture_output=True)
    (work / "sig").write_bytes(signature)
    (work / "data").write_bytes(data)
    run = subprocess.run(["gpgv", "--keyring", str(keyring), str(work / "sig"), str(work / "data")],
                         capture_output=True)
    return run.returncode == 0


def export_secret(mirror_dir, name: str) -> str:
    home = mirror_dir / ".signing" / "gnupg"
    run = subprocess.run(["gpg", "--homedir", str(home), "--batch", "--armor", "--export-secret-keys",
                          f"mmw: {name}"], capture_output=True, check=True)
    return run.stdout.decode()


def test_generate_list_sign_verify(tmp_path):
    kr = Keyring(tmp_path)
    key = kr.generate("tools")
    assert [k.name for k in kr.list()] == ["tools"]
    assert key.fingerprint and len(key.fingerprint) == 40
    signed = kr.clearsign("tools", b"Origin: x\n")
    assert signed.startswith(b"-----BEGIN PGP SIGNED MESSAGE-----")
    sig = kr.detach_sign("tools", b"Origin: x\n")
    assert gpgv_ok(tmp_path, kr.public_armor("tools"), sig, b"Origin: x\n")
    assert not gpgv_ok(tmp_path, kr.public_armor("tools"), sig, b"Origin: y\n")
    assert oct((tmp_path / ".signing" / "gnupg").stat().st_mode & 0o777) == "0o700"


def test_import_needs_a_private_key_and_relabels(tmp_path):
    other = Keyring(tmp_path / "other")
    other.generate("orig")
    with pytest.raises(SigningError, match="public key"):
        Keyring(tmp_path).import_armored(other.public_armor("orig"), "x")
    mine = Keyring(tmp_path).import_armored(export_secret(tmp_path / "other", "orig"), "mine")
    assert mine.name == "mine"
    assert [k.name for k in Keyring(tmp_path).list()] == ["mine"]
    sig = Keyring(tmp_path).detach_sign("mine", b"d")
    assert gpgv_ok(tmp_path, other.public_armor("orig"), sig, b"d")


def test_remove_and_unknown_key(tmp_path):
    kr = Keyring(tmp_path)
    kr.generate("a")
    kr.remove("a")
    assert kr.list() == []
    with pytest.raises(SigningError, match="no signing key named 'a'"):
        kr.public_armor("a")


def test_bad_and_duplicate_names_and_missing_gpg(tmp_path, monkeypatch):
    kr = Keyring(tmp_path)
    kr.generate("a")
    for bad in ("", "a b", "../x", "-x"):
        with pytest.raises(SigningError):
            kr.generate(bad)
    with pytest.raises(SigningError, match="already"):
        kr.generate("a")
    monkeypatch.setattr(signing.shutil, "which", lambda _n: None)
    with pytest.raises(SigningError, match="gpg is not installed"):
        Keyring(tmp_path / "n").list()


def test_a_passphrase_protected_key_is_refused_at_import(tmp_path):
    home = tmp_path / "pw"
    home.mkdir(mode=0o700)
    gpg = ["gpg", "--homedir", str(home), "--batch", "--pinentry-mode", "loopback", "--passphrase", "pw"]
    subprocess.run([*gpg, "--quick-gen-key", "locked", "ed25519", "sign", "never"], check=True, capture_output=True)
    armored = subprocess.run([*gpg, "--armor", "--export-secret-keys", "locked"], check=True,
                             capture_output=True).stdout.decode()
    subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "all"], capture_output=True)
    kr = Keyring(tmp_path / "m")
    with pytest.raises(SigningError, match="passphrase"):
        kr.import_armored(armored, "locked")
    assert kr.list() == []
