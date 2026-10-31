"""mmw's own signing keys, over the system `gpg`.

A repository mmw builds (a folder repository; later, a re-signed upstream)
has no upstream signature to pass through, so mmw signs it. The keys live in
a private keyring, `{mirror_dir}/.signing/gnupg`, never the user's own. A key
is known to mmw by a short name (`names.json` maps it to a fingerprint), so an
imported key keeps whatever user ID it came with.

`cryptography` does not do OpenPGP; `gpg` is on every Debian, Ubuntu and RHEL
machine, and is only needed by a side that signs.
"""
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from mmw.core import disk_io

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_TIMEOUT = 120


class SigningError(Exception):
    """Every signing failure, worded for the user."""


@dataclass(frozen=True)
class SigningKey:
    name: str
    fingerprint: str
    created: str          # ISO date


def valid_name(name: str) -> bool:
    return bool(_NAME.fullmatch(name or ""))


class Keyring:
    def __init__(self, mirror_dir: Path):
        self.dir = Path(mirror_dir) / ".signing"
        self.home = self.dir / "gnupg"
        self._names = self.dir / "names.json"

    # ── gpg ──────────────────────────────────────────────────────────────────

    def _gpg(self, *args: str, data: bytes | None = None) -> bytes:
        if shutil.which("gpg") is None:
            raise SigningError("gpg is not installed — install gnupg to sign repositories")
        disk_io.ops.mkdir(self.home, parents=True, exist_ok=True)
        disk_io.ops.chmod(self.home, 0o700)
        cmd = ["gpg", "--homedir", str(self.home), "--batch", "--no-tty", "--yes",
               "--pinentry-mode", "loopback", "--passphrase", "", *args]
        try:
            run = subprocess.run(cmd, input=data, capture_output=True, timeout=_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise SigningError(f"gpg did not finish within {_TIMEOUT} s") from None
        if run.returncode != 0:
            lines = [ln for ln in run.stderr.decode(errors="replace").splitlines() if ln.strip()]
            raise SigningError(f"gpg failed: {lines[-1] if lines else f'exit {run.returncode}'}")
        return run.stdout

    def _secret_keys(self) -> dict[str, str]:
        """fingerprint → created ISO date, for every secret key held."""
        out, fpr_next, created = {}, False, ""
        for line in self._gpg("--with-colons", "--list-secret-keys").decode().splitlines():
            f = line.split(":")
            if f[0] == "sec":
                created = datetime.fromtimestamp(int(f[5] or 0), timezone.utc).date().isoformat()
                fpr_next = True
            elif f[0] == "fpr" and fpr_next:
                out[f[9]] = created
                fpr_next = False
        return out

    # ── names ────────────────────────────────────────────────────────────────

    def _read_names(self) -> dict[str, str]:
        if not disk_io.ops.exists(self._names):
            return {}
        return json.loads(disk_io.ops.read_bytes(self._names))

    def _write_names(self, names: dict[str, str]) -> None:
        disk_io.ops.mkdir(self.dir, parents=True, exist_ok=True)
        tmp = self._names.with_name("names.json.tmp")
        disk_io.ops.write_bytes(tmp, json.dumps(names, indent=1, sort_keys=True).encode())
        disk_io.ops.replace(tmp, self._names)

    def _fingerprint(self, name: str) -> str:
        fpr = self._read_names().get(name)
        if fpr is None:
            raise SigningError(f"no signing key named {name!r}")
        return fpr

    def _check_new_name(self, name: str) -> None:
        if not valid_name(name):
            raise SigningError(f"{name!r} is not a key name — use letters, digits, '.', '_' or '-'")
        if name in self._read_names():
            raise SigningError(f"a signing key named {name!r} already exists")

    # ── keys ─────────────────────────────────────────────────────────────────

    def list(self) -> list[SigningKey]:
        held = self._secret_keys()
        return [SigningKey(name, fpr, held[fpr])
                for name, fpr in sorted(self._read_names().items()) if fpr in held]

    def generate(self, name: str) -> SigningKey:
        self._check_new_name(name)
        before = set(self._secret_keys())
        self._gpg("--quick-gen-key", f"mmw: {name}", "ed25519", "sign", "never")
        return self._register(name, before)

    def import_armored(self, text: str, name: str) -> SigningKey:
        self._check_new_name(name)
        if "PRIVATE KEY BLOCK" not in text:
            raise SigningError("that is a public key — a private key is needed to sign")
        before = set(self._secret_keys())
        self._gpg("--import", data=text.encode())
        key = self._register(name, before, imported=True)
        try:
            self.detach_sign(name, b"mmw import check\n")
        except SigningError:
            # mmw signs unattended (cron, CI): a key that asks for a
            # passphrase would make every sync fail later, so refuse it now.
            self.remove(name)
            raise SigningError("passphrase-protected keys are not supported — remove the "
                               "passphrase first (gpg --edit-key KEY passwd, then an empty one)") from None
        return key

    def _register(self, name: str, before: set[str], imported: bool = False) -> SigningKey:
        held = self._secret_keys()
        new = [f for f in held if f not in before]
        if not new:
            if imported:
                raise SigningError("that key is already in mmw's keyring — use its existing name")
            raise SigningError("gpg made no key")
        names = self._read_names()
        names[name] = new[0]
        self._write_names(names)
        return SigningKey(name, new[0], held[new[0]])

    def public_armor(self, name: str) -> str:
        return self._gpg("--armor", "--export", self._fingerprint(name)).decode()

    def stop_agent(self) -> None:
        """gpg starts an agent per keyring and leaves it running; a one-shot
        command (a cron sync) stops it when done. The next call starts one."""
        if shutil.which("gpgconf") is None or not disk_io.ops.exists(self.home):
            return
        subprocess.run(["gpgconf", "--homedir", str(self.home), "--kill", "gpg-agent"],
                       capture_output=True, timeout=_TIMEOUT)

    def fingerprint_of(self, armor: str) -> str | None:
        """The primary fingerprint of an armoured key, without importing it."""
        out = self._gpg("--with-colons", "--import-options", "show-only", "--import", data=armor.encode())
        fpr_next = False
        for line in out.decode().splitlines():
            f = line.split(":")
            if f[0] in ("sec", "pub"):
                fpr_next = True
            elif f[0] == "fpr" and fpr_next:
                return f[9]
        return None

    def import_carried(self, name: str, armor: str) -> str | None:
        """A key that came in a bundle: imported under `name` unless one by
        that name is here already. Returns a line to tell, or None."""
        here = {k.name: k.fingerprint for k in self.list()}
        if name in here:
            if here[name] == self.fingerprint_of(armor):
                return None
            return (f"signing key {name!r} arrived in this bundle but a different key by that name is "
                    "here — kept the one here; rename one to use both")
        self.import_armored(armor, name)
        return f"signing key {name!r} imported from the bundle"

    def private_armor(self, name: str) -> str:
        return self._gpg("--armor", "--export-secret-keys", self._fingerprint(name)).decode()

    def remove(self, name: str) -> None:
        fpr = self._fingerprint(name)
        self._gpg("--delete-secret-and-public-key", fpr)
        names = self._read_names()
        names.pop(name, None)
        self._write_names(names)

    def clearsign(self, name: str, data: bytes) -> bytes:
        return self._gpg("--local-user", self._fingerprint(name), "--digest-algo", "SHA512",
                         "--clearsign", data=data)

    def detach_sign(self, name: str, data: bytes) -> bytes:
        return self._gpg("--local-user", self._fingerprint(name), "--digest-algo", "SHA512",
                         "--armor", "--detach-sign", data=data)
