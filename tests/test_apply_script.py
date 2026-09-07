import ast
import subprocess
import sys
from pathlib import Path

import pytest

from mmw.resources import apply_script_bytes

SCRIPT = Path(__file__).parent.parent / "src" / "mmw" / "resources" / "apply.py"

_STDLIB = {
    "argparse", "hashlib", "io", "json", "os", "posixpath", "shutil",
    "subprocess", "sys", "tarfile", "tempfile",
}


def test_apply_script_imports_only_stdlib():
    """The far side has no pip. An import outside this set breaks every site."""
    tree = ast.parse(SCRIPT.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= _STDLIB, f"non-stdlib imports: {imported - _STDLIB}"


def test_apply_script_avoids_post_36_syntax():
    """RHEL 8 ships Python 3.6. Walrus, match and X | Y annotations all break it."""
    src = SCRIPT.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        assert not isinstance(node, ast.NamedExpr), "walrus operator is 3.8+"
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            # A `|` in an annotation context is PEP 604 (3.10+).
            assert not isinstance(node.left, ast.Name) or node.left.id[0].islower(), \
                "PEP 604 union annotation is 3.10+"
    assert "match " not in src.replace("rematch ", "")


def test_apply_script_compiles():
    subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], check=True)


def test_apply_script_bytes_returns_the_file():
    assert apply_script_bytes() == SCRIPT.read_bytes()


@pytest.mark.parametrize("bad", [
    "/etc/passwd", "../../etc/passwd", "a/../../b", "a\\b", "a\x00b", "",
])
def test_unsafe_paths_are_rejected(bad):
    ns = {}
    exec(compile(SCRIPT.read_text(), str(SCRIPT), "exec"), ns)
    assert ns["is_safe_path"](bad) is False


@pytest.mark.parametrize("good", [
    "archive.ubuntu.com/ubuntu/dists/noble/InRelease",
    "keys/nvidia/cuda.asc",
    "UPGRADE.md",
])
def test_safe_paths_are_accepted(good):
    ns = {}
    exec(compile(SCRIPT.read_text(), str(SCRIPT), "exec"), ns)
    assert ns["is_safe_path"](good) is True
