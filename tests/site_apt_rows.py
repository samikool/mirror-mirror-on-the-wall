"""Synthetic APT repository file sets for far-side timeline tests: rows
(path → sha256) plus the blobs behind them, with real Release files."""
import gzip
import hashlib
import lzma


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clearsign(release: bytes) -> bytes:
    """A clearsigned InRelease around `release` (a fake signature)."""
    body = b"\n".join((b"- " + ln if ln.startswith(b"-") else ln) for ln in release.rstrip(b"\n").split(b"\n"))
    return (b"-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\n" + body +
            b"\n-----BEGIN PGP SIGNATURE-----\n\nZmFrZQ==\n-----END PGP SIGNATURE-----\n")


class Repo:
    """Builds one repository's rows. Paths are relative to the tree root."""

    def __init__(self, base: str = "ex.org/debian"):
        self.base = base
        self.rows: dict[str, str] = {}
        self.blobs: dict[str, bytes] = {}

    def put(self, rel: str, data: bytes) -> str:
        path = f"{self.base}/{rel}" if rel else self.base
        cs = sha(data)
        self.rows[path] = cs
        self.blobs[cs] = data
        return path

    def deb(self, rel: str, data: bytes | None = None) -> str:
        self.put(rel, data if data is not None else rel.encode() * 3)
        return rel

    def suite(self, suite: str, date: str | None, packages: list[str], *, flat: bool = False,
              compress: str = "xz", sources: dict[str, list[str]] | None = None,
              inrelease: bool = True, release: bool = True, extra: str = "") -> bytes:
        """dists/<suite>/ (or the base, `flat`) with a Packages index naming
        `packages` (paths relative to the base), and a Release. Returns the
        Release bytes."""
        d = "" if flat else f"dists/{suite}/"
        comp = "" if flat else "main/binary-amd64/"
        text = "".join(f"Package: p{i}\nVersion: 1\nFilename: {p}\n\n" for i, p in enumerate(packages)).encode()
        name = f"{comp}Packages"
        data = {"xz": lzma.compress, "gz": gzip.compress, "": lambda b: b}[compress](text)
        idx = name + (f".{compress}" if compress else "")
        self.put(f"{d}{idx}", data)
        listed = [(idx, data)]
        if sources:
            src = "".join(f"Package: s{i}\nDirectory: {directory}\nFiles:\n" +
                          "".join(f" 0123 1 {f}\n" for f in files) + "\n"
                          for i, (directory, files) in enumerate(sources.items())).encode()
            self.put(f"{d}main/source/Sources", src)
            listed.append(("main/source/Sources", src))
        head = f"Origin: Debian\nLabel: Debian\nSuite: {suite}\nCodename: {suite}\n"
        if date is not None:
            head += f"Date: {date}\n"
        head += extra
        rel = (head + "SHA256:\n" + "".join(f" {sha(b)} {len(b)} {n}\n" for n, b in listed)).encode()
        if release:
            self.put(f"{d}Release", rel)
        if inrelease:
            self.put(f"{d}InRelease", clearsign(rel))
        return rel

    def read(self, path: str) -> bytes | None:
        cs = self.rows.get(path)
        return self.blobs.get(cs) if cs else None
