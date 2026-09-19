import base64
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

import httpx

KEYS_DIR = "keys"


@dataclass
class KeySyncResult:
    dest_dir: Path
    fetched: int = 0
    skipped: int = 0   # already on disk
    failed: list[str] = field(default_factory=list)  # "name: reason"

    @property
    def ok(self) -> bool:
        return not self.failed

_IMPORT_README = """\
APT Keys — Import Instructions
==============================

Modern method (Debian 10+, Ubuntu 20.04+):
  gpg --dearmor < archive-key-12.asc > /etc/apt/trusted.gpg.d/debian-12.gpg
  # Then reference it in your sources.list:
  # deb [signed-by=/etc/apt/trusted.gpg.d/debian-12.gpg] http://your-mirror/debian bookworm main

Both encodings of each key are provided:
  <name>.asc   ASCII armour — for `gpg --import` or `apt-key add`
  <name>.gpg   binary keyring — drop straight into /usr/share/keyrings/
               and reference it with `signed-by=`

Legacy method:
  apt-key add archive-key-12.asc
  # Note: apt-key is deprecated in Debian 11+ and Ubuntu 22.04+.

Valid-Until workaround for airgapped networks:
  echo \'Acquire::Check-Valid-Until "false";\' > /etc/apt/apt.conf.d/99airgap
"""

_ARMOUR_HEADER = "-----BEGIN PGP PUBLIC KEY BLOCK-----"
_ARMOUR_FOOTER = "-----END PGP PUBLIC KEY BLOCK-----"


def _crc24(data: bytes) -> int:
    """OpenPGP's armour checksum (RFC 4880 §6.1)."""
    crc = 0xB704CE
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
    return crc & 0xFFFFFF


def enarmor(raw: bytes) -> str:
    """Binary OpenPGP key → ASCII armour.

    `/usr/share/keyrings/*.gpg` files are raw packet streams: arbitrary bytes,
    NULs included, not valid UTF-8. `mmw.toml` is a text file, so a key can only
    be stored there armoured — which is exactly base64 of those same bytes plus
    a CRC-24. Lossless; `dearmor(enarmor(x)) == x`.
    """
    body = "\n".join(textwrap.wrap(base64.b64encode(raw).decode("ascii"), 64))
    checksum = base64.b64encode(_crc24(raw).to_bytes(3, "big")).decode("ascii")
    return f"{_ARMOUR_HEADER}\n\n{body}\n={checksum}\n{_ARMOUR_FOOTER}\n"


def dearmor(armour: str) -> bytes:
    """ASCII armour → the binary key it encodes."""
    payload = [
        line.strip() for line in armour.splitlines()
        if line.strip() and not line.startswith("-----") and not line.startswith("=")
        and ":" not in line
    ]
    return base64.b64decode("".join(payload))


def looks_armoured(data: bytes) -> bool:
    return _ARMOUR_HEADER.encode() in data[:200]


def _armour_bytes(raw: bytes) -> str:
    """Whichever encoding these bytes are in, return them armoured."""
    return raw.decode("utf-8", errors="replace") if looks_armoured(raw) else enarmor(raw)


def read_key(path: Path) -> str:
    """Read a key file in either encoding and return it armoured."""
    return _armour_bytes(path.read_bytes())


def fetch_key(url: str, client: httpx.Client) -> str:
    """Fetch a key from a URL in either encoding and return it armoured — the
    add-time equivalent of `read_key()` for a URL instead of a local path.

    Never assume the URL hands back armour: NVIDIA's own install docs (among
    others) hand out a key and then pipe it through `gpg --dearmor`
    themselves, because `signed-by=` wants the binary form, and a vendor is
    just as likely to publish the binary form directly. mmw's `write_key()`
    already emits both `.asc` and `.gpg` from one armoured value, so the only
    work here is fetch-and-normalize — whichever form arrives, this returns
    the same armoured text either way.
    """
    resp = client.get(url, timeout=15, follow_redirects=True)
    resp.raise_for_status()
    return _armour_bytes(resp.content)


def find_keys(directory: Path) -> dict[str, Path]:
    """Every key file under `directory`, by basename.

    Recursive, because people keep keys in nested trees. Matching is by
    basename, which is what a `Signed-By:` path gives us.
    """
    found: dict[str, Path] = {}
    try:
        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.suffix.lower() in (".gpg", ".asc", ".key", ".pub"):
                found.setdefault(path.name, path)
    except OSError:
        pass
    return found


def _fetch_key_to(dest: Path, url: str, client: httpx.Client) -> None:
    """Fetch a key via tmp+rename so an interrupted write never leaves a
    half-written file that the exists() check would treat as done."""
    resp = client.get(url, timeout=15)
    resp.raise_for_status()
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(resp.content)
    tmp.replace(dest)


def write_key(mirror_name: str, armour: str, mirror_dir: Path,
              stem: str | None = None) -> KeySyncResult:
    """Write a key that came with the config — no fetch involved.

    **Both encodings are emitted**: `.asc` (armoured) and `.gpg` (binary).
    Importing is a manual step on the airgapped side, and different clients want
    different forms — `apt-key add` and `gpg --import` take armour, while
    `signed-by=` conventionally points at a binary keyring in
    `/usr/share/keyrings/`. Emitting both removes the guess.

    `stem` is the filename the config asked for (`cuda_debian_prod`), so the
    file a client expects is the file it gets.
    """
    dest_dir = mirror_dir / KEYS_DIR / mirror_name
    dest_dir.mkdir(parents=True, exist_ok=True)
    result = KeySyncResult(dest_dir=dest_dir)

    stem = (stem or mirror_name).rsplit(".", 1)[0]
    body = armour if armour.endswith("\n") else armour + "\n"
    try:
        binary = dearmor(body)
    except Exception as e:
        result.failed.append(f"{stem}: not a valid armoured key ({e})")
        return result

    for dest, payload in ((dest_dir / f"{stem}.asc", body.encode()),
                          (dest_dir / f"{stem}.gpg", binary)):
        try:
            if dest.exists() and dest.read_bytes() == payload:
                result.skipped += 1
                continue
            tmp = dest.with_name(dest.name + ".tmp")
            tmp.write_bytes(payload)
            tmp.replace(dest)          # never leave a half-written key
            result.fetched += 1
        except OSError as e:
            result.failed.append(f"{dest.name}: {e}")

    readme = dest_dir / "IMPORT.md"
    if not readme.exists():
        readme.write_text(_IMPORT_README)
    return result


def sync_custom_key(mirror_name: str, key_url: str, mirror_dir: Path, client: httpx.Client) -> KeySyncResult:
    """Fetch a single GPG key URL into mirror_dir/keys/{mirror_name}/."""
    dest_dir = mirror_dir / KEYS_DIR / mirror_name
    dest_dir.mkdir(parents=True, exist_ok=True)
    result = KeySyncResult(dest_dir=dest_dir)
    filename = key_url.rstrip("/").split("/")[-1] or "gpgkey"
    dest = dest_dir / filename
    if dest.exists():
        result.skipped += 1
    else:
        try:
            _fetch_key_to(dest, key_url, client)
            result.fetched += 1
        except Exception as e:
            result.failed.append(f"{filename}: {e}")
    readme = dest_dir / "IMPORT.md"
    if not readme.exists():
        readme.write_text(_IMPORT_README)
    return result
