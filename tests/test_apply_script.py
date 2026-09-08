import ast
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import zstandard

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


def _load_module():
    """Exec apply.py into a fresh namespace so its functions can be called
    (and, in the tests below, selectively monkeypatched) directly — no
    subprocess, no real zstd needed for the outer stream."""
    ns = {}
    exec(compile(SCRIPT.read_text(), str(SCRIPT), "exec"), ns)
    return ns


def _fake_bundle_bytes(payload_name, payload_content, full=True, previous=None):
    """A raw (uncompressed) tar byte stream shaped like a real mmw bundle:
    the manifest member first, then one payload file. Uncompressed is fine
    for the outer stream in these tests — `open_stream` is replaced, so
    apply() never shells out to zstd to decompress it. The manifest member
    itself still goes through a real zstd round-trip, matching what a real
    bundle carries (`read_manifest_bytes` always shells out for it).
    """
    header = {"network": "lab", "created": "20260101T000000Z",
              "previous": previous, "full": full}
    checksum = hashlib.sha256(payload_content).hexdigest()
    lines = [json.dumps(header), json.dumps({"p": payload_name, "c": checksum})]
    manifest_jsonl = ("\n".join(lines) + "\n").encode()
    manifest_zst = zstandard.ZstdCompressor().compress(manifest_jsonl)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        ti = tarfile.TarInfo(".mmw/manifest.jsonl.zst")
        ti.size = len(manifest_zst)
        tar.addfile(ti, io.BytesIO(manifest_zst))

        ti = tarfile.TarInfo(payload_name)
        ti.size = len(payload_content)
        tar.addfile(ti, io.BytesIO(payload_content))
    return buf.getvalue()


class _FakeProc:
    """Stands in for the subprocess.Popen `open_stream` normally returns —
    `.stdout` just needs to support .read(), exactly like a real pipe."""

    def __init__(self, data):
        self.stdout = io.BytesIO(data)

    def wait(self):
        return 0


def test_interrupted_write_leaves_nothing_servable(tmp_path, monkeypatch):
    """A crash between 'temp file written' and 'renamed into place' — the
    real shape of a killed process or a full disk — must not leave a
    partial file anywhere nginx (autoindex on) could list and serve it.

    Simulated by making os.rename fail for exactly the one destination this
    apply is writing, which is what a real interruption at that exact point
    looks like from apply()'s perspective, without needing to reproduce
    byte-precise stream truncation to get there.
    """
    ns = _load_module()
    dest = tmp_path / "far-side"

    payload_name = "archive.ubuntu.com/ubuntu/pool/main/h/hello/hello.deb"
    content = b"hello-package-content"
    bundle_bytes = _fake_bundle_bytes(payload_name, content)
    ns["open_stream"] = lambda path: _FakeProc(bundle_bytes)

    os_mod = ns["os"]  # the real os module — apply.py did a plain `import os`
    real_rename = os_mod.rename
    final_dest = os_mod.path.join(str(dest), payload_name)

    def flaky_rename(src, dst):
        if dst == final_dest:
            raise OSError("simulated crash before rename completes")
        return real_rename(src, dst)

    with monkeypatch.context() as m:
        m.setattr(os_mod, "rename", flaky_rename)
        rc = ns["apply"]("ignored", str(dest))

    assert rc != 0

    # Nothing at the real destination — the rename never completed.
    assert not (dest / payload_name).exists()

    # Its parent directory may exist (os.makedirs runs before the write),
    # but nothing inside it is servable — no partial file, and no
    # {dest}.mmw-tmp sibling from the old naming scheme.
    parent = (dest / payload_name).parent
    assert not parent.exists() or list(parent.iterdir()) == []

    # The content that was written is confined to staging, off any served
    # path (see _tmp_dir / _sweep_tmp docstrings).
    staged = list((dest / ".mmw" / "tmp").iterdir())
    assert len(staged) == 1
    assert staged[0].read_bytes() == content


def test_stale_tmp_is_swept_on_next_apply(tmp_path):
    """Debris left by a previous crashed/killed apply must not survive into
    the next one — nothing else would ever clean it up (a --full recovery
    bundle only removes paths its manifest names, and this file's real path
    is never even mentioned by one)."""
    ns = _load_module()
    dest = tmp_path / "far-side"

    tmp_dir = dest / ".mmw" / "tmp"
    tmp_dir.mkdir(parents=True)
    orphan = tmp_dir / "orphan-from-a-dead-run"
    orphan.write_bytes(b"leftover")

    payload_name = "archive.ubuntu.com/ubuntu/pool/main/h/hello/hello.deb"
    content = b"hello-package-content"
    bundle_bytes = _fake_bundle_bytes(payload_name, content)
    ns["open_stream"] = lambda path: _FakeProc(bundle_bytes)

    rc = ns["apply"]("ignored", str(dest))

    assert rc == 0
    assert not orphan.exists()
    assert (dest / payload_name).read_bytes() == content


def test_truncated_manifest_is_detected(tmp_path):
    """A truncated manifest that lands on a line boundary produces valid
    JSON lines but is incomplete — a manifest is 200k+ lines, truncating to
    100 lines is a valid JSONL file. Only the zstd return code can detect
    this. On a garbage blob or truncated frame, zstd exits 1."""
    ns = _load_module()
    dest = tmp_path / "far-side"

    # A real manifest-like structure, but truncated before the zstd frame ends.
    # We can't do byte-precise truncation (zstd compression is non-deterministic),
    # so we test by providing garbage to the zstd decompression inside
    # read_manifest_bytes — that is reached via the manifest member extraction.
    payload_name = "archive.ubuntu.com/ubuntu/dists/noble/InRelease"
    content = b"a" * 1000

    # Create a tar with manifest first, but the manifest blob will be garbage.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        # Add a manifest member with garbage zstd data (not actually valid zstd).
        ti = tarfile.TarInfo(".mmw/manifest.jsonl.zst")
        garbage = b"not valid zstd data at all"
        ti.size = len(garbage)
        tar.addfile(ti, io.BytesIO(garbage))

        ti = tarfile.TarInfo(payload_name)
        ti.size = len(content)
        tar.addfile(ti, io.BytesIO(content))

    bundle_bytes = buf.getvalue()
    ns["open_stream"] = lambda path: _FakeProc(bundle_bytes)

    rc = ns["apply"]("ignored", str(dest))

    # Should fail because the manifest is corrupt (zstd will exit 1).
    assert rc != 0
    # Nothing should be written.
    assert not (dest / payload_name).exists()


def test_truncated_manifest_on_line_boundary(tmp_path):
    """A manifest truncated exactly on a line boundary still has valid JSON
    lines but is missing the tail. This should be detected by the zstd
    return code. Simulate this by creating a valid manifest and truncating
    the compressed form."""
    ns = _load_module()
    dest = tmp_path / "far-side"

    # Build a multi-line manifest, compress it with zstd, then truncate it.
    header = {"network": "lab", "created": "20260101T000000Z",
              "previous": None, "full": True}
    lines = [json.dumps(header)]
    # Add enough lines to create a multi-block zstd stream.
    for i in range(5000):
        lines.append(json.dumps({"p": "archive.ubuntu.com/pool/p%d" % i,
                                  "c": "c" * 64}))
    manifest_jsonl = ("\n".join(lines) + "\n").encode()
    full_compressed = zstandard.ZstdCompressor().compress(manifest_jsonl)

    # Truncate the compressed manifest — remove last 100 bytes.
    truncated = full_compressed[:-100]

    payload_name = "archive.ubuntu.com/ubuntu/dists/noble/InRelease"
    content = b"content"

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        ti = tarfile.TarInfo(".mmw/manifest.jsonl.zst")
        ti.size = len(truncated)
        tar.addfile(ti, io.BytesIO(truncated))

        ti = tarfile.TarInfo(payload_name)
        ti.size = len(content)
        tar.addfile(ti, io.BytesIO(content))

    bundle_bytes = buf.getvalue()
    ns["open_stream"] = lambda path: _FakeProc(bundle_bytes)

    rc = ns["apply"]("ignored", str(dest))

    # Should fail because zstd will exit 1 on truncated frame.
    assert rc != 0
    assert not (dest / payload_name).exists()


def test_missing_manifest_header_field(tmp_path):
    """A manifest with valid JSON but missing a required field should be
    rejected. This catches a partially-truncated manifest that still has a
    header line."""
    ns = _load_module()
    dest = tmp_path / "far-side"

    # Header missing the 'full' field.
    header = {"network": "lab", "created": "20260101T000000Z",
              "previous": None}
    lines = [json.dumps(header)]
    lines.append(json.dumps({"p": "path", "c": "c" * 64}))
    manifest_jsonl = ("\n".join(lines) + "\n").encode()
    manifest_zst = zstandard.ZstdCompressor().compress(manifest_jsonl)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        ti = tarfile.TarInfo(".mmw/manifest.jsonl.zst")
        ti.size = len(manifest_zst)
        tar.addfile(ti, io.BytesIO(manifest_zst))

    bundle_bytes = buf.getvalue()
    ns["open_stream"] = lambda path: _FakeProc(bundle_bytes)

    rc = ns["apply"]("ignored", str(dest))

    # Should fail because header is missing 'full' field.
    assert rc != 0


def test_permission_error_creating_tmp_dir(tmp_path):
    """A PermissionError when creating .mmw/tmp should be caught and
    reported, not produce a raw traceback."""
    ns = _load_module()
    dest = tmp_path / "far-side"
    dest.mkdir()

    # Make the root read-only so makedirs fails with PermissionError.
    os.chmod(str(dest), 0o500)

    try:
        payload_name = "archive.ubuntu.com/ubuntu/pool/main/h/hello/hello.deb"
        content = b"hello-package-content"
        bundle_bytes = _fake_bundle_bytes(payload_name, content)
        ns["open_stream"] = lambda path: _FakeProc(bundle_bytes)

        rc = ns["apply"]("ignored", str(dest))

        # Should fail with EXIT_UNSAFE or EXIT_TOOLING, not a traceback.
        assert rc != 0
    finally:
        # Restore permissions so cleanup works.
        os.chmod(str(dest), 0o755)


def test_symlink_in_mmw_tmp_is_refused(tmp_path):
    """A symlink at {root}/.mmw/tmp would allow _sweep_tmp to delete
    outside --root. Refuse it outright."""
    ns = _load_module()
    dest = tmp_path / "far-side"
    dest.mkdir()

    # Create a symlink pretending to be the tmp directory, pointing outside.
    outside = tmp_path / "outside"
    outside.mkdir()
    mmw_dir = dest / ".mmw"
    mmw_dir.mkdir()
    tmp_link = mmw_dir / "tmp"
    tmp_link.symlink_to(str(outside))

    payload_name = "archive.ubuntu.com/ubuntu/pool/main/h/hello/hello.deb"
    content = b"hello-package-content"
    bundle_bytes = _fake_bundle_bytes(payload_name, content)
    ns["open_stream"] = lambda path: _FakeProc(bundle_bytes)

    rc = ns["apply"]("ignored", str(dest))

    # Should fail because tmp is a symlink.
    assert rc != 0
    # Nothing should be written to the outside directory.
    assert list(outside.iterdir()) == []


def test_symlink_at_mmw_is_refused(tmp_path):
    """A symlink at {root}/.mmw would allow writes outside --root. Refuse it
    before attempting to create tmp beneath it."""
    ns = _load_module()
    dest = tmp_path / "far-side"
    dest.mkdir()

    # Create a symlink pretending to be .mmw, pointing outside.
    outside = tmp_path / "outside"
    outside.mkdir()
    mmw_link = dest / ".mmw"
    mmw_link.symlink_to(str(outside))

    payload_name = "archive.ubuntu.com/ubuntu/pool/main/h/hello/hello.deb"
    content = b"hello-package-content"
    bundle_bytes = _fake_bundle_bytes(payload_name, content)
    ns["open_stream"] = lambda path: _FakeProc(bundle_bytes)

    rc = ns["apply"]("ignored", str(dest))

    # Should fail because .mmw is a symlink.
    assert rc != 0
    # Nothing should be written to the outside directory.
    assert list(outside.iterdir()) == []
