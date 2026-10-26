"""mmw keeps itself up to date (connected side): is a newer release on PyPI,
how was this mmw installed, and run that installer's upgrade.

The check never slows mmw: it runs in a background thread with a short
timeout, its answer is remembered on this machine, and a check that hasn't
finished falls back to the last answer. The far side never comes here for a
network: its newer mmw is an installer file (`mmw.site.update`)."""
import json
import os
import subprocess
import sys
import sysconfig
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from mmw.core import disk_io

DIST = "mirror-mirror-on-the-wall"
PYPI_JSON = f"https://pypi.org/pypi/{DIST}/json"


def current_version() -> str:
    from importlib.metadata import version
    return version(DIST)


def checks_disabled(cfg=None) -> bool:
    if os.environ.get("MMW_NO_UPDATE_CHECK") == "1":
        return True
    return cfg is not None and not cfg.settings.update_check


def _http_fetch(url: str, timeout: float) -> bytes:
    import httpx
    return httpx.get(url, timeout=timeout, follow_redirects=True).raise_for_status().content


def newest_release(fetch=None, timeout: float = 2.0, current: str | None = None) -> str | None:
    """The newest release on PyPI (pre-releases only when running one), or
    None when PyPI can't be reached."""
    from packaging.version import InvalidVersion, Version
    current = current or current_version()
    try:
        data = json.loads((fetch or _http_fetch)(PYPI_JSON, timeout))
        mine_pre = Version(current).is_prerelease
        found = []
        for v, files in (data.get("releases") or {}).items():
            try:
                pv = Version(v)
            except InvalidVersion:
                continue
            if files and (mine_pre or not pv.is_prerelease):
                found.append(pv)
        return str(max(found)) if found else None
    except Exception:  # noqa: BLE001 — offline, slow, odd JSON: silence, never noise
        return None


def _cache_path() -> Path:
    from mmw.site.recents import state_dir
    return state_dir() / "update.json"


def cached() -> dict | None:
    try:
        got = json.loads(disk_io.ops.read_text(_cache_path()))
        return got if isinstance(got, dict) and got.get("newest") else None
    except (OSError, ValueError):
        return None


def remember(newest: str) -> None:
    try:
        disk_io.ops.mkdir(_cache_path().parent, parents=True, exist_ok=True)
        disk_io.ops.atomic_write(_cache_path(), json.dumps({"newest": newest, "checked_at": time.time()}).encode())
    except OSError:
        pass                                   # a convenience, never a failure


@dataclass(frozen=True)
class Offer:
    current: str
    newest: str
    major: bool

    def prompt_text(self) -> str:
        if self.major:
            return (f"mmw {self.newest} is a major update (you have {self.current}) — it may change how mmw "
                    "works (commands, settings, a mirror's layout); see its release notes. Update now?")
        return f"mmw {self.newest} is available (you have {self.current}). Update now?"

    def default_yes(self) -> bool:
        return not self.major


def offer(newest: str | None, current: str | None = None) -> Offer | None:
    from packaging.version import InvalidVersion, Version
    current = current or current_version()
    if not newest:
        return None
    try:
        new, now = Version(newest), Version(current)
    except InvalidVersion:
        return None
    if new <= now:
        return None
    return Offer(current, newest, new.major > now.major)


class Checker:
    """The check, in the background: start it, then ask for the result."""

    def __init__(self, current: str | None = None):
        self.current = current or current_version()
        self.newest: str | None = None
        self._done = threading.Event()

    def start(self, fetch=None) -> "Checker":
        def run():
            got = newest_release(fetch=fetch, current=self.current)
            if got:
                self.newest = got
                remember(got)
            self._done.set()
        threading.Thread(target=run, daemon=True, name="mmw-update-check").start()
        return self

    def result(self, wait: float = 0.0) -> Offer | None:
        self._done.wait(wait)
        newest = self.newest if self._done.is_set() and self.newest else (cached() or {}).get("newest")
        return offer(newest, self.current)


# ── how this mmw was installed, and how it updates ────────────────────────────

@dataclass(frozen=True)
class InstallKind:
    name: str                  # uv-tool | pipx | venv | system | installer | checkout
    argv: list[str] | None     # what to run; None: mmw doesn't run it itself
    note: str


def _uv_tool_dir() -> Path | None:
    try:
        out = subprocess.run([sys.executable, "-m", "uv", "tool", "dir"], capture_output=True, text=True, timeout=10)
        return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None
    except (OSError, subprocess.SubprocessError):
        return None


def _pipx_venvs() -> Path | None:
    home = os.environ.get("PIPX_HOME")
    if home:
        return Path(home) / "venvs"
    for p in (Path.home() / ".local" / "share" / "pipx" / "venvs", Path.home() / ".local" / "pipx" / "venvs"):
        if disk_io.ops.exists(p):
            return p
    return None


def _checkout() -> Path | None:
    from mmw.core.runtime import source_checkout
    return source_checkout()


def _within(child: Path, parent: Path | None) -> bool:
    if parent is None:
        return False
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def installer_prefix() -> Path | None:
    """PREFIX for an mmw our install.sh put in PREFIX/share/mmw/<name>/venv."""
    venv = Path(sys.prefix)
    share = venv.parent.parent
    if venv.name == "venv" and share.name == "mmw" and disk_io.ops.exists(share / "CURRENT"):
        return share.parent.parent
    return None


def detect_install() -> InstallKind:
    prefix = Path(sys.prefix)
    if _checkout() is not None:
        return InstallKind("checkout", None, "this mmw runs from a source checkout — update it with git pull")
    if installer_prefix() is not None:
        return InstallKind("installer", None, "installed by mmw's installer — updated from a newer installer")
    if _within(prefix, _uv_tool_dir()):
        return InstallKind("uv-tool", ["uv", "tool", "upgrade", DIST], "installed with uv tool")
    if _within(prefix, _pipx_venvs()):
        return InstallKind("pipx", ["pipx", "upgrade", DIST], "installed with pipx")
    if sys.prefix != sys.base_prefix:
        return InstallKind("venv", [sys.executable, "-m", "pip", "install", "-U", DIST], "installed with pip in a virtualenv")
    stdlib = Path(sysconfig.get_path("stdlib"))
    command = f"{sys.executable} -m pip install -U {DIST}"
    if disk_io.ops.exists(stdlib / "EXTERNALLY-MANAGED"):
        return InstallKind("system", None, f"this Python is managed by the system — update mmw yourself: {command} "
                                           "(or move to uv tool install / pipx)")
    return InstallKind("system", [sys.executable, "-m", "pip", "install", "-U", DIST], "installed with pip")


def run_update(kind: InstallKind, progress=print) -> tuple[bool, str]:
    """Run the upgrade, its output lines to `progress`; (ok, last error line)."""
    if kind.argv is None:
        return False, kind.note
    try:
        proc = subprocess.Popen(kind.argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError as e:
        return False, f"{kind.argv[0]}: {e}"
    out, err = proc.communicate()
    for line in (out + err).splitlines():
        if line.strip():
            progress(line)
    last = next((x for x in reversed(err.splitlines()) if x.strip()), "") or \
        next((x for x in reversed(out.splitlines()) if x.strip()), "")
    return proc.returncode == 0, ("" if proc.returncode == 0 else last.strip())


def busy_elsewhere(mirror_dir: Path | None) -> str | None:
    """Another mmw holding the pool (a sync, a publish): never updated under it."""
    if mirror_dir is None:
        return None
    from mmw.core.pool import pool_is_busy
    try:
        if pool_is_busy(Path(mirror_dir)):
            return "another mmw is running (it holds the pool) — update once it's done"
    except OSError:
        return None
    return None
