#!/usr/bin/env python3
"""Sign mirror certificates with your CA — on the airgapped side, where the CA
lives. Nothing has to go back across.

Both published trees and bundles of a `certs = true` network carry the
key and certificate signing request in {root}/.mmw/tls/. This
signs those requests, any number in one go:

    python3 sign.py --ca ca.crt --ca-key ca.key /srv/www/lab /srv/www/oak
    python3 sign.py --ca ca.crt --ca-key ca.key /srv/www       # every root under it
    python3 sign.py --ca ca.crt --ca-key ca.key some/mirror.csr --out ./certs

Each certificate is written beside its request as mirror.crt — where the
deployment's nginx config looks for it; then `python3 apply.py --tls --root ROOT`
turns HTTPS on (recreate the container). `--out DIR` also collects NAME.crt and NAME.key
per mirror, plus ca.crt, for Nginx Proxy Manager or wherever you load
certificates.

A certificate that still matches its key, covers every hostname the request
asks for and is not expiring within 30 days is left alone (`--force` re-signs).

Needs Python 3.6+ and the `openssl` binary (1.1.1 or 3.x) — nothing from mmw.
"""
import argparse
import getpass
import os
import re
import shutil
import subprocess
import sys
import tempfile

EXIT_ERRORS = 1
EXIT_TOOLING = 5
RENEW_WITHIN = 30 * 86400          # seconds: re-sign a certificate this close to expiry
_PASS_ENV = "MMW_CA_PASS"


class SignError(Exception):
    pass


def openssl(args, env=None):
    """Run `openssl ARGS`; its stdout as text, or SignError with its stderr."""
    proc = subprocess.Popen(["openssl"] + args, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env)
    out, err = proc.communicate()
    if proc.returncode != 0:
        raise SignError((err.decode("utf-8", "replace").strip().splitlines() or ["failed"])[-1])
    return out.decode("utf-8", "replace")


def _dns_names(text):
    # Wildcard SANs (`*.gallerycdn.vsassets.io`) pass through unchanged.
    return sorted(set(re.findall(r"DNS:([^\s,]+)", text)))


def request_hosts(csr):
    return _dns_names(openssl(["req", "-in", csr, "-noout", "-text"]))


def cert_hosts(crt):
    return _dns_names(openssl(["x509", "-in", crt, "-noout", "-text"]))


def _name(csr):
    """A name for a request: the mirror root it sits in (ROOT/.mmw/tls/), the
    directory it sits in, or its own file name."""
    d = os.path.dirname(os.path.abspath(csr))
    if os.path.basename(d) == "tls" and os.path.basename(os.path.dirname(d)) == ".mmw":
        return os.path.basename(os.path.dirname(os.path.dirname(d)))
    stem = os.path.splitext(os.path.basename(csr))[0]
    return os.path.basename(d) if stem == "mirror" else stem


def find_requests(paths):
    """(name, csr path) for each request named or found. A directory is a
    mirror root, a directory holding a mirror.csr, or a directory of mirror
    roots — one level down, never a walk (a mirror root holds millions of
    files)."""
    found = []
    for p in paths:
        if os.path.isfile(p):
            found.append(p)
            continue
        if not os.path.isdir(p):
            raise SignError("%s: no such file or directory" % p)
        here = [os.path.join(p, ".mmw", "tls", "mirror.csr"), os.path.join(p, "mirror.csr")]
        hits = [c for c in here if os.path.isfile(c)]
        if not hits:
            for child in sorted(os.listdir(p)):
                c = os.path.join(p, child, ".mmw", "tls", "mirror.csr")
                if os.path.isfile(c):
                    hits.append(c)
        if not hits:
            raise SignError("%s: no mirror.csr in it, in its .mmw/tls/, or in a mirror root "
                            "one level down" % p)
        found.extend(hits)
    seen, out = set(), []
    for csr in found:
        real = os.path.realpath(csr)
        if real not in seen:
            seen.add(real)
            out.append((_name(csr), csr))
    return out


def why_resign(csr, crt):
    """Why `crt` no longer serves `csr` — or None if it still does."""
    if not os.path.isfile(crt):
        return "no certificate yet"
    if openssl(["x509", "-in", crt, "-noout", "-pubkey"]) != \
            openssl(["req", "-in", csr, "-noout", "-pubkey"]):
        return "certificate is for a different key"
    missing = sorted(set(request_hosts(csr)) - set(cert_hosts(crt)))
    if missing:
        return "certificate does not cover %s" % ", ".join(missing)
    try:
        openssl(["x509", "-in", crt, "-noout", "-checkend", str(RENEW_WITHIN)])
    except SignError:
        return "certificate expires within 30 days"
    return None


def sign(csr, crt, ca, ca_key, days, env):
    """Sign `csr` into `crt`, carrying its hostnames as subjectAltName (OpenSSL
    1.1.1 has no -copy_extensions, so they are read and written explicitly)."""
    openssl(["req", "-in", csr, "-noout", "-verify"])
    hosts = request_hosts(csr)
    if not hosts:
        raise SignError("the request names no hostnames")
    ext = tempfile.NamedTemporaryFile("w", suffix=".ext", delete=False)
    try:
        ext.write("basicConstraints = critical,CA:FALSE\n"
                  "keyUsage = critical,digitalSignature,keyEncipherment\n"
                  "extendedKeyUsage = serverAuth\n"
                  "subjectKeyIdentifier = hash\n"
                  "authorityKeyIdentifier = keyid,issuer\n"
                  "subjectAltName = %s\n" % ",".join("DNS:" + h for h in hosts))
        ext.close()
        tmp = crt + ".tmp"
        serial = str(int.from_bytes(os.urandom(16), "big") >> 1)
        args = ["x509", "-req", "-in", csr, "-CA", ca, "-CAkey", ca_key,
                "-set_serial", serial, "-days", str(days), "-sha256",
                "-extfile", ext.name, "-out", tmp]
        if env is not None:
            args += ["-passin", "env:" + _PASS_ENV]
        openssl(args, env=env)
        os.chmod(tmp, 0o644)
        os.rename(tmp, crt)
    finally:
        os.unlink(ext.name)
    return hosts


def _ca_env(ca, ca_key):
    """An environment carrying the CA key's passphrase, asked once — or None
    when the key is not encrypted. Checks the key belongs to the CA cert."""
    with open(ca_key, "rb") as fh:
        encrypted = b"ENCRYPTED" in fh.read()
    env = None
    if encrypted:
        env = dict(os.environ)
        env[_PASS_ENV] = getpass.getpass("Passphrase for %s: " % ca_key)
    pass_args = ["-passin", "env:" + _PASS_ENV] if env is not None else []
    if openssl(["x509", "-in", ca, "-noout", "-pubkey"]) != \
            openssl(["pkey", "-in", ca_key, "-pubout"] + pass_args, env=env):
        raise SignError("%s is not the key of %s" % (ca_key, ca))
    return env


def _collect(out, name, crt, csr, ca):
    """NAME.crt, NAME.key (0600) and ca.crt in `out`."""
    if not os.path.isdir(out):
        os.makedirs(out)
    shutil.copyfile(crt, os.path.join(out, name + ".crt"))
    key = os.path.join(os.path.dirname(csr), "mirror.key")
    if os.path.isfile(key):
        dest = os.path.join(out, name + ".key")
        if os.path.lexists(dest):
            os.unlink(dest)
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as w, open(key, "rb") as r:
            shutil.copyfileobj(r, w)
    shutil.copyfile(ca, os.path.join(out, "ca.crt"))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Sign mirror certificate requests with your CA.")
    ap.add_argument("paths", nargs="+", metavar="PATH",
                    help="a mirror root, a directory of mirror roots, or a .csr file")
    ap.add_argument("--ca", required=True, help="the CA certificate (PEM)")
    ap.add_argument("--ca-key", required=True, help="the CA private key (PEM)")
    ap.add_argument("--days", type=int, default=730, help="validity (default 730)")
    ap.add_argument("--out", help="also collect NAME.crt, NAME.key and ca.crt here")
    ap.add_argument("--force", action="store_true",
                    help="re-sign even certificates that are still good")
    args = ap.parse_args(argv)
    if shutil.which("openssl") is None:
        sys.stderr.write("the openssl binary is not on PATH — install it and run again\n")
        return EXIT_TOOLING
    try:
        requests = find_requests(args.paths)
        env = _ca_env(args.ca, args.ca_key)
    except (SignError, OSError) as e:
        sys.stderr.write("%s\n" % e)
        return EXIT_ERRORS

    width = max(len(n) for n, _ in requests)
    failed, roots = 0, []
    for name, csr in requests:
        crt = os.path.join(os.path.dirname(csr), "mirror.crt")
        try:
            reason = "--force" if args.force else why_resign(csr, crt)
            if reason is None:
                status = "current — left alone"
            else:
                hosts = sign(csr, crt, args.ca, args.ca_key, args.days, env)
                status = "signed (%s): %d hostname(s), %s" % (
                    reason, len(hosts),
                    openssl(["x509", "-in", crt, "-noout", "-enddate"]).strip()
                    .replace("notAfter=", "until "))
            if args.out:
                _collect(args.out, name, crt, csr, args.ca)
        except (SignError, OSError) as e:
            failed += 1
            status = "FAILED: %s" % e
        sys.stdout.write("%-*s  %s\n" % (width, name, status))
        d = os.path.dirname(os.path.abspath(csr))
        if not status.startswith("FAILED") and d.endswith(os.path.join(".mmw", "tls")):
            roots.append(os.path.dirname(os.path.dirname(d)))
    if roots:
        sys.stdout.write("\nTurn HTTPS on in the generated nginx config, then reload nginx:\n")
        for root in roots:
            sys.stdout.write("  python3 apply.py --tls --root %s\n" % root)
    if args.out:
        sys.stdout.write("\nCollected in %s: NAME.crt, NAME.key, ca.crt\n" % args.out)
    return EXIT_ERRORS if failed else 0


if __name__ == "__main__":
    sys.exit(main())
