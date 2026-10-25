"""The remote side of a root reached over ssh: `mmw site _root VERB PATH …`.

The local mmw runs this over ssh; it answers in JSON lines on stdout, every
failure as `{"event": "error", "code": N, "message": …}` with exit N. An
apply is two-phase: the members arrive framed on stdin (a JSON header line,
then exactly `size` bytes), then an end frame; the root checks everything,
says `{"event": "checked", …}`, and waits for a `commit` (or `commit
others`: another root already changed) or `abort` line — so a set checks
every root, local or remote, before any switches. A connection that drops
before `commit` changes nothing.

Never a secret: nothing here reads keys or passphrases, and the CA key never
comes near it (certificates: the root makes its key and request; only the
request leaves and only the certificate arrives)."""
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import BinaryIO

PROTOCOL = 1


# ── framing (the local side writes, the remote side reads) ────────────────────

def write_member(out: BinaryIO, name: str, kind: str, fh, size: int, linkname: str | None = None) -> None:
    head = {"name": name, "kind": kind, "size": size if kind != "link" else 0, "linkname": linkname}
    out.write((json.dumps(head) + "\n").encode())
    if kind != "link":
        left = size
        while left:
            chunk = fh.read(min(left, 1 << 20))
            if not chunk:
                raise EOFError(f"{name}: source ended {left} bytes early")
            out.write(chunk)
            left -= len(chunk)


def write_bytes_member(out: BinaryIO, name: str, data: bytes) -> None:
    import io
    write_member(out, name, "file", io.BytesIO(data), len(data))


def write_end(out: BinaryIO) -> None:
    out.write(b'{"end": true}\n')


class _Bounded:
    """Exactly `size` bytes of `fh` — a member's body."""

    def __init__(self, fh: BinaryIO, size: int):
        self._fh, self._left = fh, size

    def read(self, n: int = -1) -> bytes:
        if self._left <= 0:
            return b""
        n = self._left if n is None or n < 0 else min(n, self._left)
        data = self._fh.read(n)
        if not data:
            raise EOFError("the connection ended inside a member")
        self._left -= len(data)
        return data

    def drain(self) -> None:
        while self.read(1 << 20):
            pass


class _Member:
    def __init__(self, head: dict, fh):
        self.name, self.kind = head["name"], head["kind"]
        self.size, self.linkname, self.fh = head.get("size", 0), head.get("linkname"), fh


def read_members(stdin: BinaryIO):
    """Members until the end frame; EOFError if the stream stops first."""
    while True:
        line = stdin.readline()
        if not line:
            raise EOFError("the connection ended before the bundle did")
        head = json.loads(line)
        if head.get("end"):
            return
        body = _Bounded(stdin, head.get("size", 0)) if head["kind"] != "link" else None
        yield _Member(head, body)
        if body is not None:
            body.drain()                    # whatever the taker didn't read


# ── the verbs ─────────────────────────────────────────────────────────────────

def _emit(stdout: BinaryIO, obj) -> None:
    stdout.write((json.dumps(obj, default=_jsonable) + "\n").encode())
    stdout.flush()


def _jsonable(o):
    if is_dataclass(o):
        return asdict(o)
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if isinstance(o, Path):
        return str(o)
    raise TypeError(f"not JSON: {type(o).__name__}")


def _state(st, newest) -> dict:
    serving = ({"number": st.serving.number, "stamp": st.serving.stamp, "snap_label": st.serving.snap_label}
               if st.serving else None)
    return {"repo": st.repo, "label": st.label, "bases": st.bases, "serving": serving, "held": st.held,
            "newest": newest}


def serve_root_command(argv: list[str], stdin: BinaryIO, stdout: BinaryIO) -> int:
    from mmw.site.apply import ApplyError
    from mmw.site.home import read_root
    from mmw.site.lock import LockHeld
    from mmw.site.mset import LocalRoot
    if len(argv) < 2:
        _emit(stdout, {"event": "error", "code": 1, "message": "usage: mmw site _root VERB PATH …"})
        return 1
    verb, path, args = argv[0], argv[1], argv[2:]
    if verb in ("init", "free"):
        return _pre_root(verb, path, args, stdout)
    info = read_root(path)
    if info is None:
        _emit(stdout, {"event": "error", "code": 1, "message": f"no mirror at {path}"})
        return 1
    root = LocalRoot(info)
    if verb not in _VERBS:
        _emit(stdout, {"event": "error", "code": 1, "message": f"unknown verb {verb!r} (protocol {PROTOCOL})"})
        return 1
    try:
        return _VERBS[verb](root, args, stdin, stdout)
    except ApplyError as e:
        code = e.code
        _emit(stdout, {"event": "error", "code": code, "message": str(e).strip("'\"")})
        return code
    except LockHeld as e:
        _emit(stdout, {"event": "error", "code": 5, "message": str(e)})
        return 5
    except (EOFError, ConnectionError) as e:
        _emit(stdout, {"event": "error", "code": 5, "message": f"the connection dropped — nothing changed ({e})"})
        return 5
    except (ValueError, LookupError, FileNotFoundError, OSError) as e:
        _emit(stdout, {"event": "error", "code": 1, "message": str(e).strip("'\"")})
        return 1


def _pre_root(verb: str, path: str, args: list[str], stdout) -> int:
    """Verbs for a root that may not exist yet: make one, or how much room."""
    from mmw.core import disk_io
    from mmw.site.home import init_root
    try:
        if verb == "init":
            name, set_id, network = args
            init_root(Path(path), name=name, set_id=set_id, network=network)
            _emit(stdout, {"ok": True})
        else:
            parent = Path(path)
            while not disk_io.ops.exists(parent):
                parent = parent.parent
            _emit(stdout, {"free": disk_io.ops.disk_usage(parent).free})
        return 0
    except (ValueError, OSError) as e:
        _emit(stdout, {"event": "error", "code": 1, "message": str(e)})
        return 1


def _info(root, args, stdin, stdout) -> int:
    from mmw.core.runtime import mmw_version
    i = root.info
    _emit(stdout, {"name": i.name, "set_id": i.set_id, "network": i.network, "members": list(i.members),
                   "hosts": root.hosts_map(), "hosts_at": root.hosts_at(), "hostnames": sorted(root.hostnames()), "free": root.free_bytes(),
                   "path": str(i.path), "mmw": mmw_version(), "protocol": PROTOCOL})
    return 0


def _status(root, args, stdin, stdout) -> int:
    _emit(stdout, [_state(st, root.newest(st.repo)) for st in root.status()])
    return 0


def _has(root, args, stdin, stdout) -> int:
    wanted = {line.strip() for line in stdin.read().decode().splitlines() if line.strip()}
    _emit(stdout, sorted(root.has(wanted)))
    return 0


def _simple(fn):
    def verb(root, args, stdin, stdout) -> int:
        _emit(stdout, fn(root, args, stdin))
        return 0
    return verb


def _apply(root, args, stdin, stdout) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="apply")
    p.add_argument("--forward", action="append", default=[])
    p.add_argument("--forward-all", action="store_true")
    p.add_argument("--only", action="append", default=None)
    o = p.parse_args(args)
    from mmw.resources.apply import MANIFEST_MEMBER

    def progress(event, **data):
        if event in ("check", "switch", "done"):
            _emit(stdout, {"event": event, **data})
    with root.applier(forward=set(o.forward), forward_all=o.forward_all,
                      only=set(o.only) if o.only is not None else None, progress=progress,
                      say=lambda *a: None) as ra:
        for m in read_members(stdin):
            if m.name == MANIFEST_MEMBER:
                ra.begin(m.fh.read())
            elif m.kind == "blob":
                # Content another root of the set received: hashed into the store.
                checksum = m.linkname
                root.import_blob(m.fh, checksum)
                ra.got[m.name] = (checksum, m.size)
            else:
                ra.take(m)
        checked = ra.check()
        _emit(stdout, {"event": "checked", "result": checked, "first_time": not ra.db.applied()})
        control = stdin.readline().decode().strip().split()
        if not control or control[0] != "commit":
            _emit(stdout, {"event": "aborted"})
            return 0
        if "stopped" in control:
            import threading
            ra.stop_event = threading.Event()
            ra.stop_event.set()                    # another root's stop: store and hold the rest here
        result = ra.commit(others_changed="others" in control)
        _emit(stdout, {"event": "result", "result": result, "first_time": ra.first_time})
    return 0


def _export(root, args, stdin, stdout) -> int:
    if not root.has({args[0]}):
        raise FileNotFoundError(f"no blob {args[0][:12]}… in {root.info.name}")
    _emit(stdout, {"ok": True})                    # the header: an error line instead is never bytes
    with root.export_blob(args[0]) as fh:
        while chunk := fh.read(1 << 20):
            stdout.write(chunk)
    stdout.flush()
    return 0


def _import(root, args, stdin, stdout) -> int:
    root.import_blob(stdin, args[0])
    _emit(stdout, {"ok": True})
    return 0


def _json_in(stdin) -> dict:
    return json.loads(stdin.read().decode() or "{}")


def _history(root, args, stdin):
    return [{"version": v, "total": total, "frees": frees, "serving": serving}
            for v, total, frees, serving in root.history(args[0])]


def _break_lock(root, args, stdin):
    return {"removed": root.break_lock()}


def _csr(root, args, stdin):
    """The root's key stays here; the request goes back. stdin: the CA's
    certificate (public), to tell whether the one here still fits."""
    from cryptography import x509

    from mmw.site.certs import prepare_csr
    ca_cert = x509.load_pem_x509_certificate(stdin.read())
    hosts, notes, csr, action = prepare_csr(root.info, ca_cert, force="--force" in args)
    return {"hosts": hosts, "notes": notes, "csr": csr.decode() if csr else None, "action": action}


def _cert(root, args, stdin):
    from mmw.site.certs import install_cert
    return {"notes": install_cert(root.info, stdin.read())}


_VERBS = {
    "info": _info,
    "status": _status,
    "has": _has,
    "apply": _apply,
    "export": _export,
    "import": _import,
    "repo-key": _simple(lambda r, a, i: {"key": r.repo_key(a[0])}),
    "labels": _simple(lambda r, a, i: r.labels()),
    "set-identity": _simple(lambda r, a, i: r.set_identity(**_json_in(i)) or {"ok": True}),
    "keep-versions": _simple(lambda r, a, i: {"keep": r.keep_versions()}),
    "history": _simple(_history),
    "serve-preview": _simple(lambda r, a, i: r.serve_preview(a[0], int(a[1]))),
    "serve": _simple(lambda r, a, i: r.serve(a[0], int(a[1]))),
    "forward": _simple(lambda r, a, i: r.forward(a[0] if a else None)),
    "hold": _simple(lambda r, a, i: {"label": r.hold(a[0])}),
    "delete": _simple(lambda r, a, i: {"freed": r.delete(a[0], int(a[1]))}),
    "space": _simple(lambda r, a, i: r.space()),
    "verify": _simple(lambda r, a, i: r.verify(bool(a and a[0] == "--repair"))),
    "keep": _simple(lambda r, a, i: r.keep(None if a[0] == "all" else int(a[0])) or {"ok": True}),
    "break-lock": _simple(_break_lock),
    "tidy": _simple(lambda r, a, i: r.tidy()),
    "tls-notes": _simple(lambda r, a, i: r.tls_notes()),
    "csr": _simple(_csr),
    "cert": _simple(_cert),
    "move-out-info": _simple(lambda r, a, i: r.move_out_info(a[0])),
    "move-in": _simple(lambda r, a, i: {"changed": r.move_in(a[0], _json_in(i))}),
    "move-out-finish": _simple(lambda r, a, i: {"changed": r.move_out_finish(a[0], a[1], _json_in(i))}),
    "set-moving-in": _simple(lambda r, a, i: r.set_moving_in(a[0], a[1] if len(a) > 1 else "") or {"ok": True}),
}
