"""No raw disk I/O outside `mmw.core.disk_io`.

Every module under the scanned trees is parsed; a direct filesystem call in a
function not on `disk_io_allowlist.txt` fails, and so does a listed function
that no longer makes one — so no new raw I/O can be added, and the list can
only shrink. Each line is `<path under src/mmw>:<qualname>  # <reason>`.

Matching is by name on calls, so a non-filesystem method with a listed name
(`.stat()` on something else) is absorbed by the allowlist with its reason.
`replace` is too common on strings to flag on any receiver: it counts only
on a receiver that looks like a path. `gzip.open(fileobj=…)`-style calls wrap
an open stream and are not disk.
"""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "mmw"
ALLOWLIST = Path(__file__).resolve().parent / "disk_io_allowlist.txt"
SCANNED = ("core", "providers", "commands", "tui")
EXEMPT = ("core/disk_io/",)

OS_CALLS = {"link", "unlink", "remove", "replace", "rename", "stat", "lstat", "walk", "scandir",
            "listdir", "makedirs", "mkdir", "rmdir", "open", "fsync", "chmod", "utime", "symlink",
            "readlink"}
SHUTIL_CALLS = {"rmtree", "move"}            # plus every `copy*`
PATH_METHODS = {"read_bytes", "write_bytes", "read_text", "write_text", "hardlink_to", "iterdir",
                "rglob", "glob", "unlink", "rmdir", "mkdir", "exists", "is_file", "is_dir", "stat",
                "open", "touch", "rename", "replace", "chmod", "is_symlink", "symlink_to",
                "readlink", "samefile", "lstat"}
PATH_ONLY = {"replace"}                      # flagged only on a path-looking receiver
PATHISH_SUFFIXES = ("path", "dir", "root", "tmp", "dst", "src", "dest", "target", "file", "fh")


def _pathish(node: ast.AST) -> bool:
    if isinstance(node, ast.Call):
        return isinstance(node.func, ast.Name) and node.func.id == "Path"
    if isinstance(node, ast.BinOp):
        return isinstance(node.op, ast.Div)
    if isinstance(node, ast.Attribute):
        return node.attr == "parent" or node.attr.lower().endswith(PATHISH_SUFFIXES)
    if isinstance(node, ast.Name):
        return node.id.lower().endswith(PATHISH_SUFFIXES)
    return False


def _layer(node: ast.AST) -> bool:
    """`disk_io.…`, `ops.…`, `job.…`: the layer's own API (a `DiskJob` is
    conventionally `job` / `self.job`)."""
    while isinstance(node, ast.Attribute):
        if node.attr in ("disk_io", "ops", "job"):
            return True
        node = node.value
    return isinstance(node, ast.Name) and node.id in ("disk_io", "ops", "job")


# Modules whose `.open(fileobj=…)` wraps an already-open stream — not disk.
# The same modules' `.open(path)` IS disk and is flagged.
STREAM_MODULES = {"tarfile", "gzip", "zipfile", "lzma", "bz2"}


def _flagged(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return "open" if func.id == "open" else None
    if not isinstance(func, ast.Attribute):
        return None
    receiver = func.value
    if _layer(receiver):
        return None                          # a call into the layer itself
    if isinstance(receiver, ast.Name) and receiver.id == "os":
        return f"os.{func.attr}" if func.attr in OS_CALLS else None
    if isinstance(receiver, ast.Name) and receiver.id == "shutil":
        hit = func.attr in SHUTIL_CALLS or func.attr.startswith("copy")
        return f"shutil.{func.attr}" if hit else None
    if (func.attr == "open" and isinstance(receiver, ast.Name) and receiver.id in STREAM_MODULES
            and any(k.arg == "fileobj" for k in call.keywords)):
        return None
    if func.attr in PATH_METHODS and (func.attr not in PATH_ONLY or _pathish(receiver)):
        return f".{func.attr}"
    return None


class _Scan(ast.NodeVisitor):
    def __init__(self, rel: str):
        self.rel = rel
        self.stack: list[str] = []
        self.hits: list[tuple[str, int, str]] = []    # (key, line, call)
        self._called: set[int] = set()                 # funcs already hit as calls

    def _scope(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = _scope

    def visit_Call(self, node: ast.Call):
        call = _flagged(node)
        if call:
            self._hit(node, call)
            self._called.add(id(node.func))
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute):
        # `Thread(target=shutil.rmtree, …)`: a reference, called elsewhere.
        if id(node) not in self._called and isinstance(node.value, ast.Name) and (
                (node.value.id == "os" and node.attr in OS_CALLS)
                or (node.value.id == "shutil" and (node.attr in SHUTIL_CALLS
                                                   or node.attr.startswith("copy")))):
            self._hit(node, f"{node.value.id}.{node.attr}")
        self.generic_visit(node)

    def _hit(self, node, call: str) -> None:
        key = f"{self.rel}:{'.'.join(self.stack) or '<module>'}"
        self.hits.append((key, node.lineno, call))


def scan(scanned=SCANNED) -> list[tuple[str, int, str]]:
    hits = []
    for top in scanned:
        for path in sorted((SRC / top).rglob("*.py")):
            rel = path.relative_to(SRC).as_posix()
            if rel.startswith(EXEMPT):
                continue
            visitor = _Scan(rel)
            visitor.visit(ast.parse(path.read_text(), filename=str(path)))
            hits += visitor.hits
    return hits


def _allowlist() -> dict[str, str]:
    out = {}
    for line in ALLOWLIST.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, _, reason = line.partition("#")
        out[key.strip()] = reason.strip()
    return out


def test_no_unlisted_raw_disk_io():
    allowed = _allowlist()
    bad = [f"src/mmw/{key.split(':')[0]}:{line} {key.split(':')[1]} {call}"
           for key, line, call in scan() if key not in allowed]
    assert not bad, ("raw disk I/O outside mmw.core.disk_io — route it through the layer "
                     "(or list it with a reason):\n  " + "\n  ".join(bad))


def test_allowlist_only_names_live_raw_io():
    live = {key for key, _, _ in scan()}
    stale = sorted(set(_allowlist()) - live)
    assert not stale, ("these allowlist entries no longer make a raw disk call — delete "
                       "them:\n  " + "\n  ".join(stale))


def test_allowlist_entries_have_reasons():
    missing = sorted(k for k, reason in _allowlist().items() if not reason)
    assert not missing, f"allowlist entries without a `# reason`: {missing}"


def _hits(code: str) -> list[str]:
    visitor = _Scan("x.py")
    visitor.visit(ast.parse(code))
    return [call for _key, _line, call in visitor.hits]


def test_guard_rules():
    assert _hits("gzip.open(path, 'rb')") == [".open"]
    assert _hits("gzip.open(fileobj=buf, mode='rb')") == []
    assert _hits("tarfile.open(fileobj=raw, mode='w|')") == []
    assert _hits("tar.open('rb')") == [".open"]                 # any receiver's .open
    assert _hits("os.fsync(fd)") == ["os.fsync"]
    assert _hits("os.chmod(p, 0o600)") == ["os.chmod"]
    assert _hits("p.is_symlink()") == [".is_symlink"]
    assert _hits("Thread(target=shutil.rmtree, args=(d,))") == ["shutil.rmtree"]
    assert _hits("name.replace('-', '_')") == []                 # a str, not a path
    assert _hits("dst.replace(target)") == [".replace"]
    assert _hits("disk_io.ops.unlink(p)") == [] and _hits("self.job.stat(paths)") == []
