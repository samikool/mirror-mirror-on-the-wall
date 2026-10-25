"""The far side's certificates: the operator's CA (loaded, or made here),
one key and certificate per root, HTTPS turned on when it fits."""
import pytest
from cryptography import x509
from typer.testing import CliRunner

from mmw import cli
from mmw.core.certs import CAKeyEncrypted
from mmw.site import certs as C
from mmw.site import home as H


@pytest.fixture
def site(tmp_path):
    H.init_site(tmp_path / "h", tmp_path / "srv")
    return tmp_path / "h"


def test_make_then_load_records_paths_only(site, tmp_path):
    crt, key = C.make_ca(tmp_path / "ca", "Lab CA", None)
    assert (key.stat().st_mode & 0o777) == 0o600
    subject = C.load_ca(site, crt, key, None)
    assert "Lab CA" in subject
    s = H.load_site(site)
    assert (s.ca_cert, s.ca_key) == (crt.resolve(), key.resolve())
    text = (site / "site.toml").read_text()
    assert "PRIVATE KEY" not in text and str(key.resolve()) in text


def test_make_refuses_existing(tmp_path):
    C.make_ca(tmp_path / "ca", "Lab CA", None)
    with pytest.raises(FileExistsError):
        C.make_ca(tmp_path / "ca", "Lab CA", None)


def test_load_refuses_mismatch(site, tmp_path):
    crt1, _key1 = C.make_ca(tmp_path / "ca1", "One", None)
    _crt2, key2 = C.make_ca(tmp_path / "ca2", "Two", None)
    with pytest.raises(ValueError, match="does not match"):
        C.load_ca(site, crt1, key2, None)
    assert H.load_site(site).ca_cert is None


def test_load_encrypted_asks(site, tmp_path):
    crt, key = C.make_ca(tmp_path / "ca", "Lab CA", b"s3cret")
    with pytest.raises(CAKeyEncrypted):
        C.load_ca(site, crt, key, None)
    assert "Lab CA" in C.load_ca(site, crt, key, b"s3cret")


def test_cli_ca_make_and_show(site, tmp_path):
    run = lambda *a: CliRunner().invoke(cli.app, ["site", "ca", *a, "--home", str(site)])  # noqa: E731
    r = run("make", "--out", str(tmp_path / "ca"), "--name", "Lab CA")
    assert r.exit_code == 0, r.output
    assert "trust" in r.output.lower() and (tmp_path / "ca/ca.crt").exists()
    r = run("show")
    assert r.exit_code == 0 and "Lab CA" in r.output and "expires" in r.output
    cert = x509.load_pem_x509_certificate((tmp_path / "ca/ca.crt").read_bytes())
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
