#!/usr/bin/env python3
"""Make the site CA that signs mirror certificates — once, wherever the CA
will live (usually the airgapped side, beside sign.py).

    python3 make-ca.py                          # ./ca.crt and ./ca.key
    python3 make-ca.py --out ~/site-ca --name "Lab CA" --encrypt

Then sign each mirror's request with sign.py, and install ca.crt (never
ca.key) on every client — the steps are printed at the end. An existing CA
is never overwritten: every client already trusts it.

Needs Python 3.6+ and the `openssl` binary (1.1.1 or 3.x) — nothing from mmw.
"""
import argparse
import getpass
import os
import shutil
import subprocess
import sys

EXIT_ERRORS = 1
EXIT_TOOLING = 5
_PASS_ENV = "MMW_CA_PASS"       # also how sign.py hands openssl a passphrase

TRUST = """\
Next:

  1. Sign each mirror's request (sign.py ships beside this script):
       python3 sign.py --ca {crt} --ca-key {key} /srv/www/lab

  2. Install {crt} — never the key — on every client:
       Ubuntu/Debian:  sudo cp {crt} /usr/local/share/ca-certificates/site-ca.crt
                       sudo update-ca-certificates
       RHEL/Fedora:    sudo cp {crt} /etc/pki/ca-trust/source/anchors/site-ca.crt
                       sudo update-ca-trust
       Chrome / VS Code on Linux also read the user's NSS database:
                       certutil -d sql:$HOME/.pki/nssdb -A -t "C,," -n "{name}" -i {crt}
                       (certutil: libnss3-tools / nss-tools)
       Firefox:        Settings > Certificates > Authorities > Import
       Windows:        open {crt} > Install > Local Machine > Trusted Root Certification Authorities
       macOS:          sudo security add-trusted-cert -d -r trustRoot \\
                         -k /Library/Keychains/System.keychain {crt}

  Keep {key} private and backed up: losing it means re-trusting a new CA
  everywhere.
"""


def _passphrase():
    if os.environ.get(_PASS_ENV):
        return os.environ[_PASS_ENV]
    first = getpass.getpass("Passphrase for the CA key: ")
    if not first or first != getpass.getpass("Again: "):
        raise SystemExit("the passphrases are empty or do not match")
    return first


def main(argv=None):
    ap = argparse.ArgumentParser(description="Make the site CA that signs mirror certificates.")
    ap.add_argument("--out", default=".", help="directory for ca.crt and ca.key (default .)")
    ap.add_argument("--name", default="mmw site CA", help="the CA's name, as clients show it")
    ap.add_argument("--days", type=int, default=3650, help="validity (default 3650)")
    ap.add_argument("--encrypt", action="store_true",
                    help="protect ca.key with a passphrase (asked for; sign.py asks again)")
    args = ap.parse_args(argv)
    if shutil.which("openssl") is None:
        sys.stderr.write("the openssl binary is not on PATH — install it and run again\n")
        return EXIT_TOOLING
    crt, key = os.path.join(args.out, "ca.crt"), os.path.join(args.out, "ca.key")
    existing = [p for p in (crt, key) if os.path.lexists(p)]
    if existing:
        sys.stderr.write("%s already exists — a CA is made once; clients trust it. "
                         "Remove it first to start over.\n" % ", ".join(existing))
        return EXIT_ERRORS
    if not os.path.isdir(args.out):
        os.makedirs(args.out, mode=0o700)

    env, secret = None, ["-nodes"]
    if args.encrypt:
        env = dict(os.environ)
        env[_PASS_ENV] = _passphrase()
        secret = ["-passout", "env:" + _PASS_ENV]
    old = os.umask(0o077)        # the key is never readable by others, not even briefly
    try:
        proc = subprocess.Popen(
            ["openssl", "req", "-x509", "-newkey", "rsa:4096", "-sha256",
             "-days", str(args.days), "-subj", "/CN=" + args.name,
             "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign",
             "-addext", "subjectKeyIdentifier=hash",
             "-keyout", key, "-out", crt] + secret,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        _, err = proc.communicate()
    finally:
        os.umask(old)
    if proc.returncode != 0:
        for p in (crt, key):
            if os.path.exists(p):
                os.unlink(p)
        sys.stderr.write("openssl failed: %s\n" % (
            err.decode("utf-8", "replace").strip().splitlines() or ["?"])[-1])
        return EXIT_ERRORS
    os.chmod(key, 0o600)
    os.chmod(crt, 0o644)
    sys.stdout.write("Made %s (%s, %d days) and %s.\n\n" % (crt, args.name, args.days, key))
    sys.stdout.write(TRUST.format(crt=crt, key=key, name=args.name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
