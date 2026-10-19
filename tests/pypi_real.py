"""Real distributions for tests where pip and uv install and build for real:
wheels with correct RECORD hashes, a flit-built sdist, and the committed
flit_core wheel its build needs (tests/fixtures/pypi/real)."""
import base64
import hashlib
import io
import tarfile
import zipfile
from pathlib import Path

from pypi_fakes import FakePyPI, file_entry

REAL = Path(__file__).parent / "fixtures" / "pypi" / "real"
FLIT_CORE = "flit_core-3.9.0-py3-none-any.whl"
FLIT_CORE_SHA256 = "7aada352fb0c7f5538c4fafeddf314d3a6a92ee8e2b1de70482329e42de70301"


def _stem(name: str) -> str:
    return name.replace("-", "_")


def _record_line(path: str, body: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).rstrip(b"=").decode()
    return f"{path},sha256={digest},{len(body)}"


def real_wheel(name: str, version: str, *, requires=(), module_source: str = "") -> tuple[str, bytes, bytes]:
    """(filename, wheel bytes, METADATA bytes) — a pure py3 wheel with one module."""
    stem = _stem(name)
    dist = f"{stem}-{version}.dist-info"
    metadata = "\n".join(["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}",
                          *(f"Requires-Dist: {r}" for r in requires)]).encode() + b"\n"
    members = {
        f"{stem}.py": (module_source or f'__version__ = "{version}"\n').encode(),
        f"{dist}/METADATA": metadata,
        f"{dist}/WHEEL": b"Wheel-Version: 1.0\nGenerator: mmw-tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = [_record_line(path, body) for path, body in members.items()] + [f"{dist}/RECORD,,"]
    members[f"{dist}/RECORD"] = ("\n".join(record) + "\n").encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, body in members.items():
            info = zipfile.ZipInfo(path, date_time=(2026, 1, 1, 0, 0, 0))
            z.writestr(info, body)
    return f"{stem}-{version}-py3-none-any.whl", buf.getvalue(), metadata


def real_flit_sdist(name: str, version: str, *, requires=()) -> tuple[str, bytes]:
    """(filename, .tar.gz bytes) — an sdist only flit_core can build."""
    stem = _stem(name)
    top = f"{stem}-{version}"
    deps = ", ".join(f'"{r}"' for r in requires)
    members = {
        "pyproject.toml": ('[build-system]\nrequires = ["flit_core>=3.2,<4"]\n'
                           'build-backend = "flit_core.buildapi"\n\n'
                           f'[project]\nname = "{name}"\nversion = "{version}"\n'
                           f'description = "mmw test package"\nrequires-python = ">=3.8"\n'
                           f'dependencies = [{deps}]\n'),
        f"{stem}.py": f'"""mmw test package."""\n__version__ = "{version}"\n',
        "PKG-INFO": "\n".join(["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}",
                               *(f"Requires-Dist: {r}" for r in requires)]) + "\n",
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        for rel, text in members.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{top}/{rel}")
            # Not 0: a backend zips these into the wheel, and zip has no dates before 1980.
            info.size, info.mtime = len(data), 1767225600
            tar.addfile(info, io.BytesIO(data))
    return f"{stem}-{version}.tar.gz", buf.getvalue()


def committed_wheel(filename: str) -> tuple[str, bytes, bytes]:
    data = (REAL / filename).read_bytes()
    name, version = filename.split("-")[:2]
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        # Its own dist-info: a wheel may vendor others (flit_core carries tomli's).
        meta = z.read(f"{name}-{version}.dist-info/METADATA")
    return filename, data, meta


def fake_from_real(dists) -> FakePyPI:
    """A FakePyPI serving real bytes. `dists`: (project, filename, body, metadata-or-None)."""
    fake = FakePyPI()
    by_project: dict[str, list] = {}
    for project, filename, body, metadata in dists:
        by_project.setdefault(project, []).append(file_entry(filename, body=body, metadata=metadata))
    for project, files in by_project.items():
        fake.add(project, files)
    return fake
