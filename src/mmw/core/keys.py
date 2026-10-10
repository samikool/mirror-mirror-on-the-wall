import base64
import hashlib
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

import httpx

KEYS_DIR = "keys"

# Key URLs that name nothing: two repositories sharing a `label` would
# otherwise collide on one filename in one directory.
_GENERIC_KEY_NAMES = frozenset({
    "gpgkey", "key", "keys", "gpg", "asc", "pub", "pubkey", "public",
    "publickey", "release", "signing-key", "repo",
})


@dataclass
class KeySyncResult:
    dest_dir: Path
    fetched: int = 0
    skipped: int = 0   # already on disk
    failed: list[str] = field(default_factory=list)  # "name: reason"
    # The bytes a URL fetch received, as sent — the coordinator stores them as
    # the key URL's upstream copy instead of fetching the same URL twice.
    raw: bytes | None = None

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
    """ASCII armour → the binary key it encodes. Raises `ValueError` if it is
    not one.

    **This is the only place anything checks that a "key" is a key.** The old
    version skipped every line it did not recognise and decoded the rest with
    `validate=False`, which silently discards non-base64 characters — so
    `write_key("mirror", "this is definitely not a key at all")` reported
    success and wrote both a `.asc` and a `.gpg`. On the far side of an airgap
    that surfaces as an opaque `gpg --import` failure against a file nothing
    can refetch. Header, footer, strict base64 and (when present) the CRC-24
    are all checked here instead.

    Lossless in both directions: `dearmor(enarmor(x)) == x`.
    """
    lines = armour.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.strip() == _ARMOUR_HEADER), None)
    end = next((i for i, ln in enumerate(lines)
                if i > (start if start is not None else -1)
                and ln.strip() == _ARMOUR_FOOTER), None)
    if start is None or end is None:
        raise ValueError("not an OpenPGP public key block (no BEGIN/END armour)")

    body = lines[start + 1:end]
    i = 0
    # Optional armour headers ("Version: GnuPG v1"), ended by a blank line.
    while i < len(body) and body[i].strip() and ":" in body[i]:
        i += 1
    while i < len(body) and not body[i].strip():
        i += 1

    payload: list[str] = []
    checksum: str | None = None
    for raw in body[i:]:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("="):
            checksum = line[1:]
            break
        payload.append(line)
    if not payload:
        raise ValueError("armoured key has no base64 payload")

    try:
        decoded = base64.b64decode("".join(payload), validate=True)
    except Exception as e:
        raise ValueError(f"armoured key is not valid base64: {e}") from e

    # The checksum line is required by RFC 4880 and optional in RFC 9580, so a
    # key without one is accepted — but one *with* one that disagrees is
    # corrupt, and saying so here is the last chance before it travels.
    if checksum is not None:
        try:
            expected = int.from_bytes(base64.b64decode(checksum, validate=True), "big")
        except Exception as e:
            raise ValueError(f"armour checksum is not valid base64: {e}") from e
        if expected != _crc24(decoded):
            raise ValueError("armour CRC-24 checksum mismatch — the key is corrupt")
    return decoded


def looks_armoured(data: bytes) -> bool:
    return _ARMOUR_HEADER.encode() in data[:200]


def _armour_bytes(raw: bytes) -> str:
    """Whichever encoding these bytes are in, return them armoured."""
    return raw.decode("utf-8", errors="replace") if looks_armoured(raw) else enarmor(raw)


def read_key(path: Path) -> str:
    """Read a key file in either encoding and return it armoured."""
    return _armour_bytes(path.read_bytes())


def fetch_key_bytes(url: str, client: httpx.Client) -> bytes:
    """The key exactly as the server sent it."""
    from mmw.core.upstream import fetch_bytes
    return fetch_bytes(url, client)


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
    return _armour_bytes(fetch_key_bytes(url, client))


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


def _stem_for(key_url: str) -> str:
    """A filename stem for a fetched key, distinct per URL.

    The last path segment is what a vendor's own docs name, so it reads right
    — `nvidia-spark-repo.gpg`, `3bf863cc.pub`. But plenty of repositories end
    their key URL in a generic word, and two repositories sharing a `label`
    then land in one directory under one stem: the second used to be reported
    as "already present" and silently never fetched. A generic tail gets a
    short digest of the URL appended, which is ugly and unambiguous; a
    rotation of the *same* URL keeps the same stem and correctly overwrites.
    """
    tail = key_url.rstrip("/").rsplit("/", 1)[-1] or "gpgkey"
    stem = tail.rsplit(".", 1)[0] or tail
    if stem.lower() in _GENERIC_KEY_NAMES:
        digest = hashlib.sha256(key_url.encode()).hexdigest()[:8]
        return f"{stem}-{digest}"
    return stem


def sync_custom_key(mirror_name: str, key_url: str, mirror_dir: Path,
                    client: httpx.Client, stem: str | None = None) -> KeySyncResult:
    """Fetch a single GPG key URL into `mirror_dir/keys/{mirror_name}/`.

    Exactly `write_key(fetch_key(...))` — the URL door and the config door
    produce the same files. This used to write the fetched bytes verbatim
    under the URL's last path segment, which meant a key fetched by URL got
    none of the guarantees a key that came with the config gets: no
    normalization to armour, only one encoding instead of both `.asc` and
    `.gpg`, and no validation that the bytes are a key at all. It also
    returned "skipped" whenever the destination existed, so a rotated upstream
    key was never refetched.
    """
    stem = stem or _stem_for(key_url)
    try:
        raw = fetch_key_bytes(key_url, client)
        armour = _armour_bytes(raw)
    except Exception as e:
        dest_dir = mirror_dir / KEYS_DIR / mirror_name
        dest_dir.mkdir(parents=True, exist_ok=True)
        result = KeySyncResult(dest_dir=dest_dir)
        result.failed.append(f"{stem}: {e}")
        readme = dest_dir / "IMPORT.md"
        if not readme.exists():
            readme.write_text(_IMPORT_README)
        return result
    result = write_key(mirror_name, armour, mirror_dir, stem)
    result.raw = raw
    return result
