"""mmw's own runtime, for a far side that has no suitable Python.

One runtime per (mmw version, Python, architecture): the `uv` binary, a
python-build-standalone interpreter (the glibc 2.17 `gnu` build, which runs
on RHEL 8 and newer), and mmw with every dependency as manylinux wheels.
Built once under `{mirror_dir}/runtime/` and reused while it verifies.

Every download goes through a `Fetcher`, so tests never touch the network.
"""
import hashlib
import io
import json
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Protocol

from mmw.core import disk_io

DIST = "mirror-mirror-on-the-wall"
ARCHES = ("x86_64", "aarch64")


class NotOnIndex(Exception):
    """The requirement has no release on the index (a dev build of mmw)."""


class RuntimeUnavailable(ValueError):
    """A runtime couldn't be built: a download failed, or this mmw has no
    release to ship. The bundle can still go without one (`--no-runtime`)."""


class FetchError(RuntimeUnavailable):
    """Something a runtime needs couldn't be downloaded."""


class Fetcher(Protocol):
    def uv_binary(self, version: str, arch: str) -> bytes: ...

    def python_archive(self, python: str, arch: str) -> tuple[str, bytes]: ...

    def wheels(self, requirement: str, python: str, arch: str, dest: Path) -> list[Path]: ...


def mmw_version() -> str:
    from importlib.metadata import version
    return version(DIST)


def uv_version() -> str:
    from importlib.metadata import version
    return version("uv")


class RealFetcher:
    """PyPI for `uv` and the wheels, python-build-standalone (wherever
    `uv python list` says) for the interpreter."""

    def __init__(self, uv_exe: Path | None = None, timeout: float = 600, stop_event=None):
        if uv_exe is None:
            from uv import find_uv_bin
            uv_exe = Path(find_uv_bin())
        self.uv, self.timeout, self.stop_event = str(uv_exe), timeout, stop_event

    def _stopped(self) -> None:
        if self.stop_event is not None and self.stop_event.is_set():
            raise InterruptedError("stopped while building mmw's runtime — nothing was written")

    def _get(self, url: str) -> bytes:
        import httpx

        from mmw.core.http import retrying_client
        try:
            with retrying_client(timeout=self.timeout, follow_redirects=True) as c, c.stream("GET", url) as r:
                r.raise_for_status()
                buf = bytearray()
                for chunk in r.iter_bytes():
                    self._stopped()
                    buf += chunk
                return bytes(buf)
        except httpx.HTTPError as e:
            raise FetchError(f"couldn't fetch {url}: {e}") from e

    def _run(self, cmd: list[str]) -> subprocess.CompletedProcess:
        """`cmd`'s output, killed when Stop is pressed or it overruns."""
        import time
        deadline = time.monotonic() + self.timeout
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as p:
            while True:
                try:
                    out, err = p.communicate(timeout=0.25)
                    return subprocess.CompletedProcess(cmd, p.returncode, out, err)
                except subprocess.TimeoutExpired:
                    if (self.stop_event is not None and self.stop_event.is_set()) or time.monotonic() > deadline:
                        p.kill()
                        p.communicate()
                        self._stopped()
                        raise subprocess.TimeoutExpired(cmd, self.timeout) from None

    def uv_binary(self, version: str, arch: str) -> bytes:
        info = json.loads(self._get(f"https://pypi.org/pypi/uv/{version}/json"))
        tag = f"manylinux_2_17_{arch}"
        for f in info.get("urls", []):
            if f["filename"].endswith(".whl") and tag in f["filename"]:
                data = self._get(f["url"])
                if hashlib.sha256(data).hexdigest() != f["digests"]["sha256"]:
                    raise FetchError(f"{f['filename']} doesn't match PyPI's sha256")
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    name = next((n for n in z.namelist() if n.endswith("/scripts/uv")), None)
                    if name is None:
                        raise FetchError(f"{f['filename']} has no uv binary")
                    return z.read(name)
        raise FetchError(f"uv {version} has no {tag} wheel on PyPI")

    def python_archive(self, python: str, arch: str) -> tuple[str, bytes]:
        try:
            r = self._run([self.uv, "python", "list", "--all-platforms", "--only-downloads",
                           "--output-format", "json", python])
            if r.returncode != 0:
                raise subprocess.CalledProcessError(r.returncode, r.args, r.stdout, r.stderr)
            out = r.stdout
        except (OSError, subprocess.SubprocessError) as e:
            raise FetchError(f"couldn't list Python downloads: {e}") from e
        builds = [b for b in json.loads(out)
                  if b["implementation"] == "cpython" and b["os"] == "linux" and b["libc"] == "gnu"
                  and b["arch"] == arch and b.get("variant", "default") == "default" and b.get("url")]
        if not builds:
            raise FetchError(f"no CPython {python} build for linux {arch} (gnu)")
        best = max(builds, key=lambda b: tuple(b["version_parts"].values()))
        from urllib.parse import unquote
        return unquote(best["url"].rsplit("/", 1)[1]), self._get(best["url"])

    def wheels(self, requirement: str, python: str, arch: str, dest: Path) -> list[Path]:
        disk_io.ops.mkdir(dest, parents=True, exist_ok=True)
        platforms = [a for p in (f"manylinux_2_28_{arch}", f"manylinux_2_17_{arch}", f"manylinux2014_{arch}")
                     for a in ("--platform", p)]
        cmd = [self.uv, "run", "--no-project", "--no-config", "--python", python, "--with", "pip", "--",
               "python", "-m", "pip", "download", "--disable-pip-version-check", "--only-binary=:all:",
               *platforms, "--python-version", python, "--implementation", "cp", "--dest", str(dest), requirement]
        try:
            r = self._run(cmd)
        except (OSError, subprocess.SubprocessError) as e:
            raise FetchError(f"couldn't download wheels for {requirement}: {e}") from e
        if r.returncode != 0:
            raise pip_failure(r.stderr, requirement, python, arch)
        return sorted(disk_io.ops.glob(dest, "*.whl"))


_OFFLINE = ("NewConnectionError", "Failed to establish a new connection", "Temporary failure in name resolution",
            "Name or service not known", "Network is unreachable", "ConnectTimeoutError", "ProxyError")


def pip_failure(stderr: str, requirement: str, python: str, arch: str) -> Exception:
    """What a failed `pip download` means. pip says "No matching
    distribution" both when the release doesn't exist and when it couldn't
    look: only the first is a dev build that needs a local wheel."""
    if any(s in stderr for s in _OFFLINE):
        return FetchError(f"couldn't reach the index for {requirement} ({arch}) — is this side online?")
    if "require a different python version" in stderr:
        return RuntimeUnavailable(f"{requirement} doesn't run on Python {python} — "
                                  "pick another runtime_python for the network")
    if f"No matching distribution found for {requirement}" in stderr:
        return NotOnIndex(requirement)
    return FetchError(f"couldn't download wheels for {requirement} ({arch}): "
                      f"{(stderr.strip().splitlines() or ['pip failed'])[-1]}")


def _sha256(path: Path) -> str:
    with disk_io.ops.open_rb(path) as fh:
        return disk_io.ops.hash_fh(fh)[0]


def verify(d: Path) -> dict | None:
    """The runtime's RUNTIME.json, if every file it lists is there and
    matches; None otherwise."""
    try:
        meta = json.loads(disk_io.ops.read_text(d / "RUNTIME.json"))
        for rel, sha in meta["files"].items():
            if _sha256(d / rel) != sha:
                return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return meta


def _reap_abandoned(cache: Path, name: str, older_than: float = 86400) -> None:
    """Builds of `name` a crash left behind (no build runs for a day)."""
    import time
    for p in disk_io.ops.glob(cache, f"{name}.tmp-*"):
        try:
            if disk_io.ops.stat(p).st_mtime < time.time() - older_than:
                disk_io.ops.rmtree(p, ignore_errors=True)
        except OSError:
            pass


def build_runtime(cache: Path, *, arch: str, python: str, mmw_version: str, fetcher: Fetcher,
                  wheel: Path | None = None, progress=None, stop_event=None) -> Path:
    """`{cache}/{mmw_version}-py{python}-{arch}/`, built if it isn't there
    (or doesn't verify). `wheel` is a local mmw wheel used instead of
    PyPI's — a dev build has none there. `progress(message)` before each
    download and when done; a set `stop_event` stops between them, leaving
    nothing behind."""
    def step(message: str) -> None:
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError("stopped while building mmw's runtime — nothing was written")
        if progress:
            progress(message)
    if arch not in ARCHES:
        raise ValueError(f"no runtime for {arch} — one of {', '.join(ARCHES)}")
    # A local wheel's version may stay the same across rebuilds: its hash
    # tells the builds apart.
    build = f"-{_sha256(wheel)[:12]}" if wheel is not None else ""
    d = cache / f"{mmw_version}-py{python}-{arch}{build}"
    if verify(d) is not None:
        return d
    disk_io.ops.mkdir(cache, parents=True, exist_ok=True)
    _reap_abandoned(cache, d.name)
    if disk_io.ops.exists(d) and verify(d) is None:
        disk_io.ops.rmtree(d)                         # damaged: built again
    # Each build in its own directory, renamed into place whole: two bundles
    # building the same runtime at once never write into each other's.
    tmp = Path(tempfile.mkdtemp(dir=cache, prefix=f"{d.name}.tmp-"))
    try:
        uv = uv_version()
        step(f"uv {uv} for {arch}…")
        disk_io.ops.write_bytes(tmp / "uv", fetcher.uv_binary(uv, arch))
        disk_io.ops.chmod(tmp / "uv", 0o755)
        step(f"Python {python} for {arch}…")
        name, data = fetcher.python_archive(python, arch)
        disk_io.ops.write_bytes(tmp / name, data)
        requirement = str(wheel) if wheel is not None else f"{DIST}=={mmw_version}"
        step(f"wheels for mmw {mmw_version} on Python {python}, {arch}…")
        try:
            fetcher.wheels(requirement, python, arch, tmp / "wheels")
        except NotOnIndex:
            raise RuntimeUnavailable(f"mmw {mmw_version} isn't on PyPI — pass --runtime-wheel PATH "
                             "(uv build makes one)") from None
        files = {str(p.relative_to(tmp)): _sha256(p)
                 for p in sorted(disk_io.ops.rglob(tmp, "*")) if disk_io.ops.is_file(p)}
        meta = {"mmw": mmw_version, "python": python, "python_archive": name, "uv": uv,
                "arch": arch, "files": files}
        disk_io.ops.write_text(tmp / "RUNTIME.json", json.dumps(meta, indent=2, sort_keys=True) + "\n")
        step("done")
        try:
            disk_io.ops.rename(tmp, d)
        except OSError:
            if verify(d) is None:
                raise
            disk_io.ops.rmtree(tmp)                   # another build got there first
    except BaseException:
        disk_io.ops.rmtree(tmp, ignore_errors=True)
        raise
    return d


def default_fetcher(stop_event=None) -> Fetcher:
    return RealFetcher(stop_event=stop_event)


def wanted(network, wheel: Path | None = None) -> dict:
    """What an `mmw` network's far side should be running: {mmw, python,
    arches} — the record a bundle that carries it leaves in the ledger."""
    version, built = mmw_version(), {}
    if wheel is not None:
        from packaging.utils import parse_wheel_filename
        version = str(parse_wheel_filename(wheel.name)[1])
        built = {"wheel": _sha256(wheel)}            # a rebuild of the same version is another runtime
    return {"mmw": version, "python": network.runtime_python, "arches": sorted(network.runtime_arches), **built}


def runtime_files(network, cfg, mirror_dir: Path, *, fetcher: Fetcher,
                  wheel: Path | None = None, progress=None, stop_event=None) -> dict[str, bytes | Path]:
    """The `.mmw/runtime/` members of a bundle: `bootstrap.sh`, a top
    `RUNTIME.json` ({mmw, python, arches}), and each architecture's built
    runtime under `{arch}/`. Builds what isn't cached; raises before
    anything is written if a download fails."""
    from mmw.resources import BOOTSTRAP
    want = wanted(network, wheel)
    out: dict[str, bytes | Path] = {
        # Bytes, never the file: a stage hardlinks files, and a link to the
        # installed package's own copy would let an edit reach it.
        ".mmw/runtime/bootstrap.sh": disk_io.ops.read_bytes(BOOTSTRAP),
        ".mmw/runtime/RUNTIME.json": (json.dumps(want, sort_keys=True) + "\n").encode(),
    }
    for arch in want["arches"]:
        d = build_runtime(Path(mirror_dir) / "runtime", arch=arch, python=want["python"],
                          mmw_version=want["mmw"], fetcher=fetcher, wheel=wheel,
                          progress=(lambda m: progress("", f"runtime:{m}")) if progress else None,
                          stop_event=stop_event)
        for p in sorted(disk_io.ops.rglob(d, "*")):
            if disk_io.ops.is_file(p):
                out[f".mmw/runtime/{arch}/{p.relative_to(d)}"] = p
    return out


def pending(network, mirror_dir: Path, *, from_nothing: bool) -> str | None:
    """What the next bundle of `network` adds for mmw's runtime, said for an
    estimate: None when it carries none (an apply network, or what was last
    sent is current and the bundle doesn't start from nothing)."""
    if network.far_side != "mmw":
        return None
    from mmw.core.format import fmt_size
    from mmw.core.pool import Pool
    want = wanted(network)
    if not from_nothing:
        try:
            with Pool(mirror_dir, read_only=True) as pool:
                if pool.runtime_sent(network.name) == want:
                    return None
        except Exception:  # noqa: BLE001 — no pool yet: nothing was sent
            pass
    size = 0
    for arch in want["arches"]:
        d = Path(mirror_dir) / "runtime" / f"{want['mmw']}-py{want['python']}-{arch}"
        if verify(d) is None:
            return "mmw's runtime (downloaded when it runs, about 100 MB per architecture)"
        size += sum(disk_io.ops.stat(p).st_size for p in disk_io.ops.rglob(d, "*") if disk_io.ops.is_file(p))
    return f"mmw's runtime ({fmt_size(size)})"
