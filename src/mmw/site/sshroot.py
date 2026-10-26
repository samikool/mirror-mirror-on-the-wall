"""A root reached over ssh: the same interface as `mset.LocalRoot`, each
call answered by `mmw site _root VERB PATH …` on the remote host (see
`remote.py`). Every ssh goes through `ssh.py`: key auth only, never a prompt,
never a secret."""
import json
import queue
import threading
from pathlib import Path
from types import SimpleNamespace

from mmw.site import remote, ssh
from mmw.site.home import RootInfo


def _ns(obj):
    """JSON → attribute access, all the way down (what the CLI and TUI read)."""
    if isinstance(obj, dict):
        return SimpleNamespace(**{k: _ns(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_ns(v) for v in obj]
    return obj


def _version(d):
    from mmw.site.db import VersionInfo
    return VersionInfo(d.get("id", 0), d.get("repo", ""), d["number"], d.get("label", ""), d.get("digest", ""),
                       d.get("stamp", ""), d.get("snap_label"))


def _call(host: str, verb: str, path: str, *args: str, input: bytes | None = None):
    try:
        out = ssh.run(host, ssh.mmw_argv("site", "_root", verb, path, *args), input=input)
    except ssh.RemoteFailed as e:
        last = [json.loads(x) for x in e.stdout.decode().splitlines() if x.strip()][-1:] or [{}]
        raise _remote_error(last[0], e.code) from None
    lines = [x for x in out.decode().splitlines() if x.strip()]
    return json.loads(lines[-1]) if lines else None


def _remote_error(event: dict, code: int):
    from mmw.site.apply import ApplyError
    message = event.get("message") or f"the remote mmw failed ({code})"
    if code in (2, 3, 5):
        return ApplyError(code, message)
    return ValueError(message)


def remote_info(location: str) -> dict | None:
    """The root at an ssh location (`_root info`), or None: no mirror there."""
    _kind, host, path = ssh.parse_location(location)
    ssh.check_remote_mmw(host)
    try:
        info = _call(host, "info", path)
    except ValueError as e:
        if "no mirror at" in str(e):
            return None
        raise
    from mmw.site.remote import PROTOCOL
    if info.get("protocol") != PROTOCOL:
        reason = f"mmw on {host} speaks protocol {info.get('protocol')}, this one {PROTOCOL}"
        raise ssh.RemoteMmw(f"{reason} — install the same mmw on both", reason=reason, installable=True)
    return info


def init_remote(location: str, *, name: str, set_id: str, network: str) -> None:
    _kind, host, path = ssh.parse_location(location)
    _call(host, "init", path, name, set_id, network)


def empty_at(location: str) -> bool:
    _kind, host, path = ssh.parse_location(location)
    return bool(_call(host, "empty", path)["empty"])


def free_at(location: str) -> int:
    _kind, host, path = ssh.parse_location(location)
    return int(_call(host, "free", path)["free"])


class SshRoot:
    """A root on another machine, reached with plain ssh and key auth."""

    def __init__(self, location: str, info: dict):
        self._kind, self.host, self.path = ssh.parse_location(location)
        self._info = info
        self.info = RootInfo(location, Path(info["path"]), Path(info["path"]) / ".mmw" / "site", info["name"],
                             info["set_id"], info["network"], tuple(info["members"]))

    def _call(self, verb: str, *args: str, input: bytes | None = None):
        return _call(self.host, verb, self.path, *args, input=input)

    def __repr__(self) -> str:
        return f"SshRoot({self.info.name!r} at {self.info.location!r})"

    def hostnames(self) -> set[str]:
        return set(self._info["hostnames"])

    def arch(self) -> str:
        """The remote machine's architecture — from its mmw, or (an older
        mmw that doesn't say) `uname -m`; never assumed."""
        if not self._info.get("arch"):
            m = ssh.run(self.host, ["uname", "-m"]).decode().strip()
            self._info["arch"] = {"arm64": "aarch64"}.get(m, m)
        return self._info["arch"]

    def free_bytes(self) -> int:
        return int(self._call("info")["free"])

    def status(self) -> list:
        from mmw.site.db import RepoState
        rows = self._call("status")
        self._newest = {r["repo"]: r["newest"] for r in rows}
        return [RepoState(r["repo"], r["label"], r["bases"], _version({**r["serving"], "repo": r["repo"]})
                          if r["serving"] else None, r["held"]) for r in rows]

    def newest(self, repo: str) -> int | None:
        if not hasattr(self, "_newest"):
            self.status()
        return self._newest.get(repo)

    def repo_key(self, ref: str) -> str | None:
        return self._call("repo-key", ref)["key"]

    def labels(self) -> list[str]:
        return self._call("labels")

    def has(self, checksums: set[str]) -> set[str]:
        return set(self._call("has", input="\n".join(sorted(checksums)).encode()))

    def hosts_map(self) -> dict[str, str]:
        return dict(self._call("info")["hosts"])

    def hosts_at(self) -> str:
        return self._call("info").get("hosts_at", "")

    def set_identity(self, *, members=None, hosts=None) -> None:
        self._call("set-identity", input=json.dumps({k: v for k, v in (("members", members), ("hosts", hosts))
                                                      if v is not None}).encode())

    def keep_versions(self) -> int | None:
        return self._call("keep-versions")["keep"]

    def history(self, key: str):
        return [(_version(r["version"]), r["total"], r["frees"], r["serving"]) for r in self._call("history", key)]

    def deleted(self, key: str) -> list[str]:
        try:
            return self._call("deleted", key)["stamps"]
        except ValueError as e:
            if "unknown verb" in str(e):    # an older mmw there: nothing recorded
                return []
            raise

    def serve_preview(self, key: str, number: int):
        return _ns(self._call("serve-preview", key, str(number)))

    def serve(self, key: str, number: int):
        return _ns(self._call("serve", key, str(number)))

    def forward(self, key: str | None) -> list[str]:
        return self._call("forward", *([key] if key else []))

    def hold(self, key: str) -> str:
        return self._call("hold", key)["label"]

    def delete(self, key: str, number: int) -> int:
        return self._call("delete", key, str(number))["freed"]

    def space(self):
        return _ns(self._call("space"))

    def verify(self, repair: bool = False):
        return self._call("verify", *(["--repair"] if repair else []))

    def keep(self, keep: int | None) -> None:
        self._call("keep", "all" if keep is None else str(keep))

    def break_lock(self) -> bool:
        return self._call("break-lock")["removed"]

    def tidy(self) -> list[str]:
        return self._call("tidy")

    def details(self) -> dict:
        return self._call("details")

    def tls_notes(self) -> list[str]:
        return self._call("tls-notes")

    def move_out_info(self, key: str) -> dict:
        return self._call("move-out-info", key)

    def move_in(self, key: str, info: dict) -> bool:
        return self._call("move-in", key, input=json.dumps(info).encode())["changed"]

    def move_out_finish(self, key: str, to: str, info: dict) -> bool:
        return self._call("move-out-finish", key, to, input=json.dumps(info).encode())["changed"]

    def set_moving_in(self, key: str, src: str) -> None:
        self._call("set-moving-in", key, src)

    def export_blob(self, checksum: str):
        proc = ssh.popen(self.host, ssh.mmw_argv("site", "_root", "export", self.path, checksum))
        proc.stdin.close()
        return _ProcReader(proc, self.host)

    def import_blob(self, fh, checksum: str) -> None:
        proc = ssh.popen(self.host, ssh.mmw_argv("site", "_root", "import", self.path, checksum))
        try:
            while chunk := fh.read(1 << 20):
                proc.stdin.write(chunk)
            proc.stdin.close()
        except BrokenPipeError:
            pass
        out = proc.stdout.read()
        proc.wait()
        if proc.returncode == 255:
            raise ssh.SshError(ssh.UNREACHABLE.format(host=self.host))
        if proc.returncode != 0:
            from mmw.site.store import ChecksumMismatch
            raise ChecksumMismatch(f"{checksum[:12]}… didn't arrive whole on {self.host}: {out[-200:]!r}")

    def applier(self, **opts):
        return RemoteApply(self, **opts)

    def sign(self, *, password, days: int, force: bool):
        from mmw.site.certs import sign_remote
        return sign_remote(self, password=password, days=days, force=force)


class _ProcReader:
    """A blob streamed from the remote: read it, then close (exit checked)."""

    def __init__(self, proc, host: str):
        self._proc, self._host = proc, host

    def read(self, n: int = -1) -> bytes:
        return self._proc.stdout.read(n)

    def __enter__(self):
        head = self._proc.stdout.readline()
        try:
            event = json.loads(head) if head else {}
        except ValueError:
            event = {}
        if not event.get("ok"):
            self._proc.stdout.close()
            self._proc.wait()
            if self._proc.returncode == 255:
                raise ssh.SshError(ssh.UNREACHABLE.format(host=self._host))
            raise ValueError(event.get("message") or f"{self._host} couldn't send the blob")
        return self

    def __exit__(self, *exc) -> None:
        self._proc.stdout.close()
        self._proc.wait()
        if self._proc.returncode not in (0, None) and exc[0] is None:
            raise ssh.SshError(ssh.UNREACHABLE.format(host=self._host))


class RemoteApply:
    """One ssh root's part of an apply: members framed down the pipe, the
    remote checks, then commits (or aborts) when the set says so."""

    def __init__(self, root: SshRoot, *, forward=None, forward_all=False, only=None, progress=None,
                 stop_event=None, say=print, dry_run=False):
        self.root, self.progress, self.stop_event = root, progress, stop_event
        self.opts = {"forward": sorted(forward or ()), "forward_all": forward_all,
                     "only": sorted(only) if only is not None else None}
        self.got: dict[str, tuple[str, int]] = {}
        self.links: dict[str, str] = {}
        self.tls: dict[str, bytes] = {}
        self.dry_run = dry_run
        self.expected: dict[str, str] | None = None
        self.first_time = False
        self._events: "queue.Queue[dict]" = queue.Queue()
        self._done = False

    def __enter__(self) -> "RemoteApply":
        args = ssh.mmw_argv("site", "_root", "apply", self.root.path)
        for k in self.opts["forward"]:
            args += ["--forward", k]
        if self.opts["forward_all"]:
            args.append("--forward-all")
        for k in self.opts["only"] or []:
            args += ["--only", k]
        self._proc = ssh.popen(self.root.host, args)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        return self

    def _read(self) -> None:
        for line in self._proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("event") in ("check", "switch", "done"):
                if self.progress:                 # progress, never a reply
                    self.progress(event["event"], **{k: v for k, v in event.items() if k != "event"})
            else:
                self._events.put(event)
        self._events.put({"event": "eof"})

    def _send(self, write) -> None:
        try:
            write(self._proc.stdin)
        except (BrokenPipeError, OSError):
            raise self._failure() from None

    def _failure(self):
        from mmw.site.apply import ApplyError
        try:
            self._proc.stdin.close()
        except OSError:
            pass
        self._proc.wait()
        self._reader.join(timeout=5)
        while not self._events.empty():               # the remote's own error, if it said one
            event = self._events.get_nowait()
            if event.get("event") == "error":
                return _remote_error(event, event.get("code", 1))
        if self._proc.returncode == 255:
            return ssh.SshError(ssh.UNREACHABLE.format(host=self.root.host))
        name = self.root.info.name
        if getattr(self, "_committing", False):
            return ApplyError(5, f"{name}: the connection to {self.root.host} dropped after commit — {name} "
                                 "may have changed; check mmw site status")
        return ApplyError(5, f"{name}: the connection to {self.root.host} dropped — nothing changed there")

    def _wait(self, want: str) -> dict:
        event = self._events.get()
        if event.get("event") == want:
            return event
        if event.get("event") == "error":
            raise _remote_error(event, event.get("code", 1))
        raise self._failure()

    def begin(self, manifest: bytes) -> None:
        from mmw.resources.apply import MANIFEST_MEMBER
        self._send(lambda out: remote.write_bytes_member(out, MANIFEST_MEMBER, manifest))
        if self.expected is None:
            rows = [json.loads(x) for x in manifest.decode().splitlines()[1:] if x.strip()]
            self.expected = {r["p"]: r["c"] for r in rows}

    def take(self, m) -> None:
        from mmw.resources.apply import TLS_PREFIX
        if m.name.startswith(TLS_PREFIX):
            self.tls[m.name[len(TLS_PREFIX):]] = m.fh.read()
        elif m.name.startswith(".mmw/"):
            return                                  # control members: only the manifest crosses
        elif m.kind == "link":
            self.links[m.name] = m.linkname
        else:
            self._send(lambda out: remote.write_member(out, m.name, "file", m.fh, m.size))
            self.got[m.name] = ((self.expected or {}).get(m.name, ""), m.size)

    def take_blob(self, name: str, checksum: str, size: int, from_root) -> None:
        if from_root is not None:
            with from_root.export_blob(checksum) as fh:
                self._send(lambda out: remote.write_member(out, name, "blob", fh, size, linkname=checksum))
        self.got[name] = (checksum, size)

    def check(self):
        from mmw.resources.apply import TLS_PREFIX
        from mmw.site.apply import ApplyResult

        def tail(out):
            for name, target in self.links.items():
                remote.write_member(out, name, "link", None, 0, linkname=target)
            for name, data in sorted(self.tls.items()):
                remote.write_bytes_member(out, TLS_PREFIX + name, data)
            remote.write_end(out)
            out.flush()
        self._send(tail)
        event = self._wait("checked")
        return ApplyResult(**event["result"])

    def commit(self, *, others_changed: bool = False):
        from mmw.site.apply import ApplyError, ApplyResult
        stopped = self.stop_event is not None and self.stop_event.is_set()
        if stopped and not others_changed:
            self._send(lambda out: (out.write(b"abort\n"), out.flush()))
            raise ApplyError(5, "stopped before anything changed — what arrived is stored")
        control = "commit" + (" others" if others_changed else "") + (" stopped" if stopped else "")
        self._committing = True
        self._send(lambda out: (out.write(control.encode() + b"\n"), out.flush(), out.close()))
        event = self._wait("result")
        self.first_time = bool(event.get("first_time"))
        self._done = True
        return ApplyResult(**event["result"])

    def __exit__(self, *exc) -> None:
        try:
            if not self._done and not self._proc.stdin.closed:
                self._proc.stdin.write(b"abort\n")
                self._proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        self._proc.wait()
        self._reader.join(timeout=5)
