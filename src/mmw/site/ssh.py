"""Roots over ssh: the one way mmw talks to another machine.

Only the system `ssh`, always non-interactive (`BatchMode=yes`) and never
accepting a host key on anyone's behalf (`StrictHostKeyChecking=yes`).
mmw never stores, copies, reads, logs or shows a key, a passphrase or a
password: keys live in the user's agent and ssh config. A host that won't
let mmw in without a prompt is a hard failure, said in fixed words — ssh's
own stderr is never echoed."""
import re
import shlex
import subprocess
from pathlib import Path

KEY_AUTH = "{host} needs key-based ssh access for mmw (a security requirement) — configure it and try again"
HOST_KEY = ("{host}'s host key isn't known (or changed) — connect once with plain ssh to check it; "
            "mmw never accepts one for you")
UNREACHABLE = "can't reach {host}"
OPTIONS = ("-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15",
           # A connection that dies silently is noticed within a minute, never a hang.
           "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4")


class SshError(Exception):
    """An ssh root mmw can't use; the message is always one of the fixed ones."""


def parse_location(loc: str) -> tuple[str, str | None, str]:
    """("ssh", host, path) for `[user@]host:/path`, else ("local", None, loc)."""
    m = re.match(r"^([^/\\:]+):(.+)$", loc)
    if m and not re.match(r"^[A-Za-z]$", m.group(1)) and not loc.startswith((".", "/", "-")):
        return "ssh", m.group(1), m.group(2)
    return "local", None, loc


def ssh_argv(host: str, remote: list[str]) -> list[str]:
    if host.startswith("-"):
        raise SshError(f"{host!r} isn't a host")      # never an ssh option in disguise
    return ["ssh", *OPTIONS, "--", host, " ".join(shlex.quote(a) for a in remote)]


def _failure(host: str, returncode: int, stderr: bytes) -> SshError:
    text = stderr.decode("utf-8", "replace")
    if "Host key verification failed" in text or "REMOTE HOST IDENTIFICATION HAS CHANGED" in text:
        return SshError(HOST_KEY.format(host=host))
    if "Permission denied" in text or "publickey" in text:
        return SshError(KEY_AUTH.format(host=host))
    return SshError(UNREACHABLE.format(host=host))


def check_access(host: str) -> None:
    """Can mmw get in without a prompt? A fixed-message `SshError` if not."""
    r = subprocess.run(ssh_argv(host, ["true"]), capture_output=True, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise _failure(host, r.returncode, r.stderr)


class RemoteFailed(Exception):
    """The remote command ran and failed (exit code, its stdout) — not ssh."""

    def __init__(self, code: int, stdout: bytes):
        super().__init__(f"remote command failed ({code})")
        self.code, self.stdout = code, stdout


def run(host: str, remote: list[str], *, input: bytes | None = None, timeout: float | None = None) -> bytes:
    """Run `remote` on `host`; its stdout. ssh failing (255) is an `SshError`
    in fixed words; the remote command failing is `RemoteFailed`."""
    r = subprocess.run(ssh_argv(host, remote), input=input, capture_output=True, timeout=timeout,
                       stdin=None if input is not None else subprocess.DEVNULL)
    if r.returncode == 255:
        raise _failure(host, r.returncode, r.stderr)
    if r.returncode != 0:
        raise RemoteFailed(r.returncode, r.stdout)
    return r.stdout


def popen(host: str, remote: list[str]) -> subprocess.Popen:
    """`remote` on `host` with stdin and stdout as pipes (a stream); stderr is
    kept from the terminal and never shown."""
    return subprocess.Popen(ssh_argv(host, remote), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)   # never read, so never a pipe that fills


# ── the mmw on the other end ─────────────────────────────────────────────────

# A non-interactive ssh session rarely has ~/.local/bin on its PATH: the
# installer's default home for mmw is put first.
REMOTE_MMW = 'PATH="$HOME/.local/bin:$PATH"; exec mmw "$@"'
INSTALL = 'd=$(mktemp -d) && tar -xf - -C "$d" && sh "$d"/mmw-*/install.sh && rm -rf "$d"'


class RemoteMmw(ValueError):
    """The remote host has no mmw, or one too old to talk to."""


def mmw_argv(*args: str) -> list[str]:
    return ["sh", "-c", REMOTE_MMW, "mmw", *args]


_checked: set[str] = set()


def forget_checked() -> None:
    _checked.clear()


def check_remote_mmw(host: str) -> None:
    """The remote's mmw is there and no older than this one (checked once per
    host); a `RemoteMmw` that says how to install it otherwise."""
    if host in _checked:
        return
    from packaging.version import InvalidVersion, Version

    from mmw.core.runtime import mmw_version
    here = mmw_version()
    hint = (f"— carry mmw-{here}-<arch>.tar across and: mmw site open {host}:/PATH --install "
            f"mmw-{here}-<arch>.tar (it is pushed over this same key-based ssh)")
    try:
        out = run(host, mmw_argv("--version")).decode().strip()
    except RemoteFailed:
        raise RemoteMmw(f"mmw isn't installed on {host} {hint}") from None
    there = out.split()[-1] if out else ""
    try:
        older = Version(there) < Version(here)
    except InvalidVersion:
        raise RemoteMmw(f"mmw on {host} says {out!r}, not a version {hint}") from None
    if older:
        raise RemoteMmw(f"mmw on {host} is older ({there} < {here}) {hint}")
    _checked.add(host)


def install_remote(host: str, installer) -> None:
    """Stream an installer tar (`mmw-<version>-<arch>.tar`) to `host` and run
    its install.sh there — over the same key-based ssh, nothing copied first."""
    from mmw.core import disk_io
    proc = popen(host, ["sh", "-c", INSTALL])
    try:
        with disk_io.ops.open_rb(installer) as fh:
            while chunk := fh.read(1 << 20):
                proc.stdin.write(chunk)
        proc.stdin.close()
    except BrokenPipeError:
        pass
    proc.stdout.read()
    proc.wait()
    if proc.returncode == 255:
        raise SshError(UNREACHABLE.format(host=host))
    if proc.returncode != 0:
        raise RemoteMmw(f"installing mmw on {host} failed (exit {proc.returncode}) — "
                        f"run the installer there by hand: tar -xf {Path(str(installer)).name} && sh mmw-*/install.sh")
    _checked.discard(host)
