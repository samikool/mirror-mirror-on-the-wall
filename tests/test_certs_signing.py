"""Signing a network's CSR with the site CA, and checking the result.

EC keys throughout: these tests are about the signing and checking logic,
not RSA, and RSA-4096 generation is slow enough to matter in a suite meant
to stay fast. `sign_csr` must not care what kind of key either side holds.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from mmw.core import certs, config


def _key():
    return ec.generate_private_key(ec.SECP256R1())


def _write_key(key, path: Path, password: bytes | None = None) -> Path:
    enc = (serialization.BestAvailableEncryption(password) if password
           else serialization.NoEncryption())
    path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc))
    return path


def _ca(tmp_path: Path, name: str = "Test Root CA", issuer=None, password=None):
    """A CA cert + key on disk. `issuer=(cert, key)` makes it an intermediate."""
    key = _key()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(timezone.utc)
    signer_cert, signer_key = issuer if issuer else (None, key)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(signer_cert.subject if signer_cert else subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(signer_key, hashes.SHA256())
    )
    stem = name.replace(" ", "_")
    crt = tmp_path / f"{stem}.crt"
    crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return crt, _write_key(key, tmp_path / f"{stem}.key", password), cert, key


def _csr(out_dir: Path, hostnames: list[str]) -> Path:
    """What generate_key_and_csr leaves behind, with a fast key."""
    out_dir.mkdir(parents=True, exist_ok=True)
    key = _key()
    _write_key(key, out_dir / "mirror.key")
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostnames[0])]))
        .add_extension(x509.SubjectAlternativeName(
            [x509.DNSName(h) for h in hostnames]), critical=False)
        .sign(key, hashes.SHA256())
    )
    path = out_dir / "mirror.csr"
    path.write_bytes(csr.public_bytes(serialization.Encoding.PEM))
    return path


def _leaf(crt_path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificates(crt_path.read_bytes())[0]


# --- signing ---------------------------------------------------------------

def test_sign_produces_a_server_cert_for_every_csr_name(tmp_path):
    ca_crt, ca_key, ca_cert, _ = _ca(tmp_path)
    out = tmp_path / "net"
    csr = _csr(out, ["developer.download.nvidia.com", "nvidia.github.io"])

    crt = certs.sign_csr(csr, ca_crt, ca_key, days=30)

    assert crt == out / "mirror.crt"
    leaf = _leaf(crt)
    sans = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert sans.get_values_for_type(x509.DNSName) == [
        "developer.download.nvidia.com", "nvidia.github.io"]
    assert leaf.issuer == ca_cert.subject
    eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.SERVER_AUTH in eku
    assert leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    # Really signed by the CA, not just labelled with its name.
    leaf.verify_directly_issued_by(ca_cert)


def test_sign_validity_follows_days(tmp_path):
    ca_crt, ca_key, _, _ = _ca(tmp_path)
    csr = _csr(tmp_path / "net", ["a.example"])
    leaf = _leaf(certs.sign_csr(csr, ca_crt, ca_key, days=90))
    span = leaf.not_valid_after_utc - leaf.not_valid_before_utc
    assert timedelta(days=89) < span < timedelta(days=91)


def test_root_ca_is_not_appended(tmp_path):
    """Clients already trust the root; sending it is noise."""
    ca_crt, ca_key, _, _ = _ca(tmp_path)
    crt = certs.sign_csr(_csr(tmp_path / "net", ["a.example"]), ca_crt, ca_key)
    assert len(x509.load_pem_x509_certificates(crt.read_bytes())) == 1


def test_intermediate_ca_is_appended_as_the_chain(tmp_path):
    """Clients trust the root; nginx must send the intermediate or they
    cannot build a path from the leaf to it."""
    _, _, root_cert, root_key = _ca(tmp_path, "Root")
    int_crt, int_key, int_cert, _ = _ca(tmp_path, "Intermediate",
                                        issuer=(root_cert, root_key))
    crt = certs.sign_csr(_csr(tmp_path / "net", ["a.example"]), int_crt, int_key)
    chain = x509.load_pem_x509_certificates(crt.read_bytes())
    assert [c.subject for c in chain] == [chain[0].subject, int_cert.subject]


def test_encrypted_ca_key_with_password(tmp_path):
    ca_crt, ca_key, _, _ = _ca(tmp_path, password=b"hunter2")
    crt = certs.sign_csr(_csr(tmp_path / "net", ["a.example"]), ca_crt, ca_key,
                         password=b"hunter2")
    assert crt.exists()


def test_encrypted_ca_key_without_password_says_so(tmp_path):
    ca_crt, ca_key, _, _ = _ca(tmp_path, password=b"hunter2")
    with pytest.raises(certs.CAKeyEncrypted):
        certs.sign_csr(_csr(tmp_path / "net", ["a.example"]), ca_crt, ca_key)


def test_mismatched_ca_cert_and_key_are_refused(tmp_path):
    ca_crt, _, _, _ = _ca(tmp_path, "One")
    _, other_key, _, _ = _ca(tmp_path, "Two")
    with pytest.raises(ValueError, match="does not match"):
        certs.sign_csr(_csr(tmp_path / "net", ["a.example"]), ca_crt, other_key)


def test_a_non_ca_certificate_is_refused_as_the_ca(tmp_path):
    ca_crt, ca_key, _, _ = _ca(tmp_path)
    leaf_crt = certs.sign_csr(_csr(tmp_path / "net", ["a.example"]), ca_crt, ca_key)
    with pytest.raises(ValueError, match="not a CA"):
        certs.sign_csr(_csr(tmp_path / "net2", ["b.example"]), leaf_crt,
                       tmp_path / "net" / "mirror.key")


# --- checking --------------------------------------------------------------

def test_check_is_clean_for_a_fresh_matching_cert(tmp_path):
    ca_crt, ca_key, _, _ = _ca(tmp_path)
    out = tmp_path / "net"
    certs.sign_csr(_csr(out, ["a.example", "b.example"]), ca_crt, ca_key)
    assert certs.check_cert(out, ["a.example", "b.example"]) == []


def test_check_reports_a_hostname_the_cert_does_not_cover(tmp_path):
    """The network gained a host after signing: the CSR is rewritten on the
    next publish, but the signed cert is not — and nothing said so."""
    ca_crt, ca_key, _, _ = _ca(tmp_path)
    out = tmp_path / "net"
    certs.sign_csr(_csr(out, ["a.example"]), ca_crt, ca_key)
    problems = certs.check_cert(out, ["a.example", "changelogs.ubuntu.com"])
    assert any("changelogs.ubuntu.com" in p for p in problems)


def test_check_reports_expiry_soon(tmp_path):
    ca_crt, ca_key, _, _ = _ca(tmp_path)
    out = tmp_path / "net"
    certs.sign_csr(_csr(out, ["a.example"]), ca_crt, ca_key, days=10)
    assert any("expires" in p for p in certs.check_cert(out, ["a.example"]))


def test_check_reports_a_cert_for_a_different_key(tmp_path):
    """mirror.key replaced (restored from elsewhere): nginx would refuse to
    start on a cert/key pair that does not match."""
    ca_crt, ca_key, _, _ = _ca(tmp_path)
    out = tmp_path / "net"
    certs.sign_csr(_csr(out, ["a.example"]), ca_crt, ca_key)
    _write_key(_key(), out / "mirror.key")
    assert any("key" in p for p in certs.check_cert(out, ["a.example"]))


def test_check_with_no_cert_says_unsigned(tmp_path):
    out = tmp_path / "net"
    _csr(out, ["a.example"])
    assert certs.check_cert(out, ["a.example"]) == ["not signed yet (no mirror.crt)"]


# --- hostnames -------------------------------------------------------------

def test_dist_upgrade_adds_changelogs_to_the_cert_names(monkeypatch):
    net = config.NetworkConfig(name="lab", dist_upgrade=True, certs=True)
    cfg = config.Config(settings=config.Settings(mirror_dir=Path("/tmp/x")),
                        networks=[net])
    monkeypatch.setattr(certs, "network_mirrors", lambda n, c, d: [
        config.MirrorConfig(source_url="http://ports.ubuntu.com/ubuntu-ports",
                            name="p", provider="apt", label="ubuntu")])
    assert certs.collect_hostnames(net, cfg) == [
        "changelogs.ubuntu.com", "ports.ubuntu.com"]


def test_without_dist_upgrade_changelogs_is_not_a_cert_name(monkeypatch):
    net = config.NetworkConfig(name="lab", certs=True)
    cfg = config.Config(settings=config.Settings(mirror_dir=Path("/tmp/x")),
                        networks=[net])
    monkeypatch.setattr(certs, "network_mirrors", lambda n, c, d: [
        config.MirrorConfig(source_url="http://ports.ubuntu.com/ubuntu-ports",
                            name="p", provider="apt", label="ubuntu")])
    assert certs.collect_hostnames(net, cfg) == ["ports.ubuntu.com"]


# --- config ----------------------------------------------------------------

def test_a_legacy_bundle_tls_key_still_loads_and_is_dropped_on_save(tmp_path):
    """`bundle_tls` was folded into `certs` (certs on = HTTPS wherever the
    network lands, bundles included). Configs that still carry the key must
    load — unknown keys are otherwise errors — and lose it on the next save."""
    config.config_path(tmp_path).write_text(
        f'[settings]\nmirror_dir = "{tmp_path / "m"}"\n\n'
        '[[networks]]\nname = "lab"\ncerts = true\nbundle_tls = true\n\n'
        '[[networks]]\nname = "old"\nbundle_tls = false\n')
    cfg = config.load(tmp_path)
    assert [(n.name, n.certs) for n in cfg.networks] == [("lab", True), ("old", True)]
    assert "bundle_tls" not in config.NetworkConfig.model_fields
    config.save(cfg, tmp_path)
    assert "bundle_tls =" not in config.config_path(tmp_path).read_text()


# --- `mmw certs --ca` -------------------------------------------------------

@pytest.fixture
def signing_env(tmp_path, monkeypatch):
    """A config dir with one certs network, whose CSR generation is the fast
    EC stand-in above, writing where the real one would."""
    from mmw.commands import certs as certs_cmd
    mirror_dir = tmp_path / "mirror"
    (tmp_path / "mmw.toml").write_text(
        f'[settings]\nmirror_dir = "{mirror_dir}"\n\n'
        '[[networks]]\nname = "lab"\ncerts = true\n'
    )

    def fake_generate(net, cfg):
        out = certs.cert_dir(net, cfg)
        names = ["a.example", "b.example"]
        if not (out / "mirror.csr").exists():
            _csr(out, names)
        return names, out / "mirror.key", out / "mirror.cnf", out / "mirror.csr", out / "CERTS.md"

    monkeypatch.setattr(certs_cmd, "generate_certs", fake_generate)
    return tmp_path, mirror_dir / "certs" / "lab"


def test_certs_command_signs_with_the_ca(signing_env, capsys):
    from mmw.commands import certs as certs_cmd
    cfg_dir, out = signing_env
    ca_crt, ca_key, ca_cert, _ = _ca(cfg_dir)
    assert certs_cmd.run(cfg_dir, ca=ca_crt, ca_key=ca_key) == 0
    _leaf(out / "mirror.crt").verify_directly_issued_by(ca_cert)
    assert "covers every hostname" in capsys.readouterr().out


def test_certs_command_without_ca_reports_unsigned_and_succeeds(signing_env, capsys):
    from mmw.commands import certs as certs_cmd
    cfg_dir, out = signing_env
    assert certs_cmd.run(cfg_dir) == 0
    assert not (out / "mirror.crt").exists()
    assert "not signed yet" in capsys.readouterr().out


def test_certs_command_needs_both_ca_halves(signing_env, capsys):
    from mmw.commands import certs as certs_cmd
    cfg_dir, _ = signing_env
    ca_crt, _, _, _ = _ca(cfg_dir)
    assert certs_cmd.run(cfg_dir, ca=ca_crt) == 1
    assert "--ca-key" in capsys.readouterr().err


def test_certs_command_encrypted_key_uses_env_passphrase(signing_env, monkeypatch):
    from mmw.commands import certs as certs_cmd
    cfg_dir, out = signing_env
    ca_crt, ca_key, _, _ = _ca(cfg_dir, password=b"s3cret")
    monkeypatch.setenv(certs_cmd.PASSPHRASE_ENV, "s3cret")
    assert certs_cmd.run(cfg_dir, ca=ca_crt, ca_key=ca_key) == 0
    assert (out / "mirror.crt").exists()


def test_certs_command_encrypted_key_without_tty_or_env_fails_clearly(
        signing_env, monkeypatch, capsys):
    from mmw.commands import certs as certs_cmd
    cfg_dir, out = signing_env
    ca_crt, ca_key, _, _ = _ca(cfg_dir, password=b"s3cret")
    monkeypatch.delenv(certs_cmd.PASSPHRASE_ENV, raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert certs_cmd.run(cfg_dir, ca=ca_crt, ca_key=ca_key) == 1
    assert certs_cmd.PASSPHRASE_ENV in capsys.readouterr().err
    assert not (out / "mirror.crt").exists()


def test_tls_is_on_by_default_and_off_survives_a_save(tmp_path):
    """Every network serves HTTPS once signed (PyPI and VS Code clients only
    speak it); `certs = false` is the opt-out, and a save keeps it."""
    config.config_path(tmp_path).write_text(
        f'[settings]\nmirror_dir = "{tmp_path / "m"}"\n\n'
        '[[networks]]\nname = "on"\n\n[[networks]]\nname = "off"\ncerts = false\n')
    cfg = config.load(tmp_path)
    assert [(n.name, n.certs) for n in cfg.networks] == [("on", True), ("off", False)]
    config.save(cfg, tmp_path)
    assert [(n.name, n.certs) for n in config.load(tmp_path).networks] == [("on", True), ("off", False)]
