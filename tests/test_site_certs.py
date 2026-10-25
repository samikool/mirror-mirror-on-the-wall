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


# ── signing every root ───────────────────────────────────────────────────────

from test_site_apply import _bundle, env  # noqa: E402,F401
from test_site_roots import split_env  # noqa: E402,F401
from test_dry_run_totals import two_mirrors  # noqa: E402,F401


def _sans(path):
    cert = x509.load_pem_x509_certificates(path.read_bytes())[0]
    return sorted(cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
                  .value.get_values_for_type(x509.DNSName))


def _applied(env, tmp_path, password=None):  # noqa: F811
    from mmw.site.apply import apply_bundle
    cfg, net, md, home, root = env
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=lambda *a: None)
    crt, key = C.make_ca(tmp_path / "ca", "Lab CA", password)
    C.load_ca(home, crt, key, password)
    return home, root


def test_sign_single_root(env, tmp_path):  # noqa: F811
    home, root = _applied(env, tmp_path)
    (r,) = C.sign_roots(home, password=None)
    assert r.action == "signed" and r.hostnames == C.root_hostnames(root) == ["example.com"]
    tls = root / ".mmw/tls"
    assert _sans(tls / "mirror.crt") == ["example.com"]
    assert (tls / "https.conf").exists() and (tls.stat().st_mode & 0o777) == 0o700
    assert ((tls / "mirror.key").stat().st_mode & 0o777) == 0o600
    (again,) = C.sign_roots(home, password=None)
    assert again.action == "kept"


def test_force_resigns(env, tmp_path):  # noqa: F811
    home, root = _applied(env, tmp_path)
    C.sign_roots(home, password=None)
    before = (root / ".mmw/tls/mirror.crt").read_bytes()
    key_before = (root / ".mmw/tls/mirror.key").read_bytes()
    (r,) = C.sign_roots(home, password=None, force=True)
    assert r.action == "signed" and (root / ".mmw/tls/mirror.crt").read_bytes() != before
    assert (root / ".mmw/tls/mirror.key").read_bytes() == key_before          # the key is never regenerated


def test_each_root_signed_for_its_hosts(split_env, tmp_path):  # noqa: F811
    from mmw.site.apply import apply_bundle
    cfg, net, md, home, main, second = split_env
    kb = next(s.identity for s in cfg.sources if s.name == "b")
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), place={kb: "second"},
                 say=lambda *a: None)
    crt, key = C.make_ca(tmp_path / "ca", "Lab CA", None)
    C.load_ca(home, crt, key, None)
    results = {r.root: r for r in C.sign_roots(home, password=None)}
    assert _sans(main / ".mmw/tls/mirror.crt") == ["a.example.com"]
    assert _sans(second / ".mmw/tls/mirror.crt") == ["b.example.com"]
    assert results["main"].action == results["second"].action == "signed"
    assert "/srv/mmw/.mmw/tls/mirror.crt" in (second / ".mmw/tls/https.conf").read_text()


def test_encrypted_ca_needs_passphrase(env, tmp_path, monkeypatch):  # noqa: F811
    home, root = _applied(env, tmp_path, password=b"pw")
    monkeypatch.delenv("MMW_CA_PASSPHRASE", raising=False)
    r = CliRunner().invoke(cli.app, ["site", "certs", "--home", str(home)])
    assert r.exit_code == 1 and "MMW_CA_PASSPHRASE" in r.output and "terminal" in r.output
    assert not (root / ".mmw/tls/mirror.crt").exists()
    monkeypatch.setenv("MMW_CA_PASSPHRASE", "pw")
    r = CliRunner().invoke(cli.app, ["site", "certs", "--home", str(home)])
    assert r.exit_code == 0, r.output
    assert "signed" in r.output and "recreate nginx" in r.output


def test_ca_missing_refused(env, tmp_path):  # noqa: F811
    home, root = _applied(env, tmp_path)
    (tmp_path / "ca/ca.key").rename(tmp_path / "ca/moved.key")
    with pytest.raises(FileNotFoundError, match="ca.key"):
        C.sign_roots(home, password=None)


def test_no_ca_yet(env, tmp_path):  # noqa: F811
    from mmw.site.apply import apply_bundle
    cfg, net, md, home, root = env
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=lambda *a: None)
    with pytest.raises(ValueError, match="no CA yet"):
        C.sign_roots(home, password=None)
