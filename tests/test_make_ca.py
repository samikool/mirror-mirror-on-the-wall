"""make-ca.py: the site CA, made once on whichever side keeps it — real
openssl, and a CA that sign.py then signs mirror requests with."""
import ast
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

RES = Path(__file__).parent.parent / "src" / "mmw" / "resources"
MAKE_CA, SIGN = RES / "make-ca.py", RES / "sign.py"
pytestmark = pytest.mark.skipif(shutil.which("openssl") is None,
                                reason="requires the openssl binary")


def _run(script, *args, env=None):
    return subprocess.run([sys.executable, str(script), *map(str, args)],
                          capture_output=True, text=True, env=env)


def test_it_needs_only_python_and_openssl():
    tree = ast.parse(MAKE_CA.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"argparse", "getpass", "os", "shutil", "subprocess", "sys"}
    assert not any(isinstance(n, ast.JoinedStr) for n in ast.walk(tree)), \
        "f-strings are avoided in far-side scripts (Python 3.6, by convention)"


def test_the_ca_it_makes_signs_a_mirror_request(tmp_path):
    from cryptography import x509
    r = _run(MAKE_CA, "--out", tmp_path / "ca", "--name", "Lab CA")
    assert r.returncode == 0, r.stderr
    crt, key = tmp_path / "ca/ca.crt", tmp_path / "ca/ca.key"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    cert = x509.load_pem_x509_certificate(crt.read_bytes())
    assert cert.subject.rfc4514_string() == "CN=Lab CA"
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert cert.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign
    # What to do next: trust it on clients, then sign.
    assert "update-ca-certificates" in r.stdout and "certutil" in r.stdout
    assert "sign.py --ca %s --ca-key %s" % (crt, key) in r.stdout

    csr = tmp_path / "mirror.csr"
    subprocess.run(["openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(tmp_path / "mirror.key"), "-out", str(csr),
                    "-subj", "/CN=lab", "-addext", "subjectAltName=DNS:lab.example"],
                   check=True, capture_output=True)
    signed = _run(SIGN, "--ca", crt, "--ca-key", key, csr)
    assert signed.returncode == 0, signed.stderr + signed.stdout
    verify = subprocess.run(["openssl", "verify", "-CAfile", str(crt),
                             str(tmp_path / "mirror.crt")], capture_output=True, text=True)
    assert verify.returncode == 0, verify.stderr


def test_it_never_overwrites_a_ca(tmp_path):
    (tmp_path / "ca.key").write_text("precious")
    r = _run(MAKE_CA, "--out", tmp_path)
    assert r.returncode == 1 and "already" in r.stderr
    assert (tmp_path / "ca.key").read_text() == "precious"
    assert not (tmp_path / "ca.crt").exists()


def test_encrypt_protects_the_key_with_a_passphrase(tmp_path):
    env = dict(os.environ, MMW_CA_PASS="s3cret")
    r = _run(MAKE_CA, "--out", tmp_path, "--encrypt", env=env)
    assert r.returncode == 0, r.stderr
    assert b"ENCRYPTED" in (tmp_path / "ca.key").read_bytes()
    ok = subprocess.run(["openssl", "pkey", "-in", str(tmp_path / "ca.key"), "-noout",
                         "-passin", "env:MMW_CA_PASS"], env=env, capture_output=True)
    assert ok.returncode == 0
