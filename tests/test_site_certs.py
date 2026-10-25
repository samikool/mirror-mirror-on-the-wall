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
    subject = C.load_ca(crt, key, None)
    assert "Lab CA" in subject
    from mmw.site import recents
    assert recents.ca_paths() == (crt.resolve(), key.resolve())     # on this machine, never in a root
    text = (site / "site.toml").read_text()
    assert "PRIVATE KEY" not in text and "ca_key" not in text
    assert "PRIVATE KEY" not in (recents.state_dir() / "ca.json").read_text()


def test_make_refuses_existing(tmp_path):
    C.make_ca(tmp_path / "ca", "Lab CA", None)
    with pytest.raises(FileExistsError):
        C.make_ca(tmp_path / "ca", "Lab CA", None)


def test_load_refuses_mismatch(site, tmp_path):
    crt1, _key1 = C.make_ca(tmp_path / "ca1", "One", None)
    _crt2, key2 = C.make_ca(tmp_path / "ca2", "Two", None)
    with pytest.raises(ValueError, match="does not match"):
        C.load_ca(crt1, key2, None)
    assert H.load_site(site).ca_cert is None


def test_load_encrypted_asks(site, tmp_path):
    crt, key = C.make_ca(tmp_path / "ca", "Lab CA", b"s3cret")
    with pytest.raises(CAKeyEncrypted):
        C.load_ca(crt, key, None)
    assert "Lab CA" in C.load_ca(crt, key, b"s3cret")


def test_cli_ca_make_and_show(site, tmp_path):
    run = lambda *a: CliRunner().invoke(cli.app, ["site", "ca", *a])  # noqa: E731
    r = run("make", "--out", str(tmp_path / "ca"), "--name", "Lab CA")
    assert r.exit_code == 0, r.output
    assert "trust" in r.output.lower() and (tmp_path / "ca/ca.crt").exists()
    r = run("show")
    assert r.exit_code == 0 and "Lab CA" in r.output and "expires" in r.output
    cert = x509.load_pem_x509_certificate((tmp_path / "ca/ca.crt").read_bytes())
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca


# ── signing every root ───────────────────────────────────────────────────────

from test_site_apply import _bundle, env  # noqa: E402,F401
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
    C.load_ca(crt, key, password)
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


def test_encrypted_ca_needs_passphrase(env, tmp_path, monkeypatch):  # noqa: F811
    home, root = _applied(env, tmp_path, password=b"pw")
    monkeypatch.delenv("MMW_CA_PASSPHRASE", raising=False)
    r = CliRunner().invoke(cli.app, ["site", "certs"])
    assert r.exit_code == 1 and "MMW_CA_PASSPHRASE" in r.output and "terminal" in r.output
    assert not (root / ".mmw/tls/mirror.crt").exists()
    monkeypatch.setenv("MMW_CA_PASSPHRASE", "pw")
    r = CliRunner().invoke(cli.app, ["site", "certs"])
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


def test_mmw_bundle_carries_no_tls_key(two_mirrors, tmp_path):  # noqa: F811
    import tarfile
    from test_bundle_mmw import _mmw
    from mmw.core.publisher.bundle import bundle_network
    cfg, net, md = _mmw(two_mirrors)
    assert net.certs
    r = bundle_network(net, cfg, md, out_path=tmp_path, from_ref=None)
    with tarfile.open(r.out_path) as tar:
        assert not [n for n in tar.getnames() if n.startswith(".mmw/tls/")]


# ── review findings ──────────────────────────────────────────────────────────

def test_a_new_ca_re_signs(env, tmp_path):  # noqa: F811
    home, root = _applied(env, tmp_path)
    C.sign_roots(home, password=None)
    crt2, key2 = C.make_ca(tmp_path / "ca2", "New CA", None)
    C.load_ca(crt2, key2, None)
    (r,) = C.sign_roots(home, password=None)
    assert r.action == "signed"
    leaf = x509.load_pem_x509_certificates((root / ".mmw/tls/mirror.crt").read_bytes())[0]
    assert "New CA" in leaf.issuer.rfc4514_string()


def test_a_key_from_an_older_bundle_is_replaced_once(env, tmp_path):  # noqa: F811
    import io
    from mmw.core.certs import generate_key_and_csr
    from mmw.resources.apply import _install_tls
    home, root = _applied(env, tmp_path)
    near = tmp_path / "near"
    key, _cnf, _csr = generate_key_and_csr(["example.com"], near)          # what the near side made …
    _install_tls(str(root), "mirror.key", io.BytesIO(key.read_bytes()))    # … and an older bundle carried
    (r,) = C.sign_roots(home, password=None)
    assert (root / ".mmw/tls/mirror.key").read_bytes() != key.read_bytes()
    assert any("replaced" in n for n in r.notes)
    made_here = (root / ".mmw/tls/mirror.key").read_bytes()
    (again,) = C.sign_roots(home, password=None)
    assert again.action == "kept" and (root / ".mmw/tls/mirror.key").read_bytes() == made_here


def test_a_corrupt_certificate_does_not_fail_the_apply(env, tmp_path):  # noqa: F811
    from mmw.site.apply import apply_bundle
    from test_snapshot_repo_versions import _change_a
    cfg, net, md, home, root = env
    home, root = _applied(env, tmp_path)
    C.sign_roots(home, password=None)
    (root / ".mmw/tls/mirror.crt").write_text("not a certificate")
    _change_a(md)
    r = apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "2")), say=lambda *a: None)
    assert r.updated and any("mirror.crt" in a or "certificate" in a for a in r.actions)


def test_ca_make_encrypt_asks_twice(site, tmp_path, monkeypatch):
    import getpass
    import sys
    answers = iter(["one", "two"])
    monkeypatch.delenv("MMW_CA_PASSPHRASE", raising=False)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    from mmw.commands import site as cmd
    assert cmd.ca_make(tmp_path / "ca", "Lab CA", encrypt=True) == 1
    assert not (tmp_path / "ca/ca.key").exists()


def test_certs_takes_the_lock(env, tmp_path):  # noqa: F811
    from mmw.site.lock import LockHeld, SiteLock
    home, root = _applied(env, tmp_path)
    with SiteLock(home):
        with pytest.raises(LockHeld):
            C.sign_roots(home, password=None)
