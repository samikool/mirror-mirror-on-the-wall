"""Build small, real APT trees on disk for adoption tests: a Release (signed
or not) naming its indexes, Packages naming pool files, all with sha256."""
import gzip
import hashlib
from pathlib import Path


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def make_tree(root: Path, suite: str = "bookworm", day: int = 1, packages: dict[str, bytes] | None = None, *,
              sign_with=None, inrelease: bool = True, flat: bool = False, origin: str = "Debian",
              extra: dict[str, bytes] | None = None, arch: str = "amd64") -> Path:
    """An archive root at `root`. `sign_with`: (Keyring, key name) — clearsigned
    InRelease (`inrelease`) or a detached Release.gpg. `extra`: more files
    by path under the root (written as given, after the rest)."""
    root = Path(root)
    packages = packages if packages is not None else {"hello_1.0_amd64.deb": b"hello" * 50}
    suite_dir = root if flat else root / "dists" / suite
    index_dir = suite_dir if flat else suite_dir / "main" / f"binary-{arch}"
    stanzas = []
    for name, data in sorted(packages.items()):
        rel = name if flat else f"pool/main/{name[0]}/{name.split('_')[0]}/{name}"
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        stanzas.append(f"Package: {name.split('_')[0]}\nVersion: 1.0\nArchitecture: {arch}\n"
                       f"Filename: {'./' if flat else ''}{rel}\nSize: {len(data)}\nSHA256: {_sha(data)}\n")
    index = "\n".join(stanzas).encode()
    index_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / "Packages").write_bytes(index)
    (index_dir / "Packages.gz").write_bytes(gzip.compress(index, mtime=0))
    listed = {}
    for name in ("Packages", "Packages.gz"):
        data = (index_dir / name).read_bytes()
        listed[(index_dir / name).relative_to(suite_dir).as_posix()] = data
    date = f"{['Fri', 'Sat', 'Sun', 'Mon', 'Tue', 'Wed', 'Thu'][(day - 1) % 7]}, {day:02d} Nov 2026 00:00:00 UTC"
    head = (f"Origin: {origin}\nLabel: {origin}\nSuite: {'stable' if not flat else suite}\nCodename: {suite}\n"
            f"Date: {date}\nArchitectures: {arch}\n" + ("" if flat else "Components: main\n"))
    release = (head + "SHA256:\n" + "".join(f" {_sha(d)} {len(d)} {n}\n" for n, d in sorted(listed.items()))).encode()
    (suite_dir / "Release").write_bytes(release)
    if sign_with is not None:
        keyring, name = sign_with
        if inrelease:
            (suite_dir / "InRelease").write_bytes(keyring.clearsign(name, release))
        else:
            (suite_dir / "Release.gpg").write_bytes(keyring.detach_sign(name, release))
    for rel, data in (extra or {}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root
