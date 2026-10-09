import ast
import hashlib
import io
import json
import os
import shutil
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
    "subprocess", "sys", "tarfile", "tempfile", "threading",
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


# Constructs that parse cleanly on a modern interpreter and fail on 3.6. This
# is the riskiest compatibility claim in the project — it fails on somebody
# else's machine, months later, with no mmw installed to debug it — and the
# original check covered only three of them.
_POST_36_CALLS = {
    "removeprefix": "str.removeprefix is 3.9+",
    "removesuffix": "str.removesuffix is 3.9+",
    "cached_property": "functools.cached_property is 3.8+",
}


def test_apply_script_avoids_post_36_syntax():
    """RHEL 8 ships Python 3.6, and `apply.py` runs there with no mmw beside it."""
    src = SCRIPT.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        assert not isinstance(node, ast.NamedExpr), "walrus operator is 3.8+"
        assert not isinstance(node, ast.AnnAssign), "variable annotations are 3.6+, but avoided here"
        if isinstance(node, ast.FunctionDef):
            assert not node.args.posonlyargs, "positional-only params are 3.8+"
        if isinstance(node, ast.Attribute) and node.attr in _POST_36_CALLS:
            raise AssertionError(_POST_36_CALLS[node.attr])
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            # `dict | dict` is 3.9+; a `|` in an annotation context is PEP 604
            # (3.10+). Neither is distinguishable from a plain bitwise-or by
            # AST alone, so this stays a heuristic — the CI job that compiles
            # this file under an old interpreter is the real check.
            assert not isinstance(node.left, ast.Name) or node.left.id[0].islower(), \
                "PEP 604 union annotation is 3.10+"
    assert "match " not in src.replace("rematch ", "")
    # f-strings at all: 3.6 has them, but the project's own rule is to avoid
    # them here, and a bare `f"..."` is the easiest thing to add by reflex.
    assert not any(isinstance(n, ast.JoinedStr) for n in ast.walk(tree)), \
        "f-strings are avoided in apply.py by convention"


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


def _fake_bundle_bytes_multi(files, deletes=None, full=True, previous=None, file_count=None):
    """Like `_fake_bundle_bytes`, but the manifest may list more files than
    actually appear as tar members — for simulating a tar stream that ends
    cleanly (no exception from tarfile) but is missing content the manifest
    promises, exactly the failure mode a live spike found: a plain tar
    truncated near its end reads as 100% successful with no error at all.

    `files` is [(name, content_or_None)] — content_or_None means "listed in
    the manifest but never actually added as a tar member". `file_count`,
    when given, is `ManifestHeader.file_count` — how many real files (not
    the fixed .mmw/ control members) THIS bundle's tar declares it will
    contain; omitting it (None) simulates an older manifest written before
    that field existed.
    """
    header = {"network": "lab", "created": "20260101T000000Z",
              "previous": previous, "full": full}
    if file_count is not None:
        header["file_count"] = file_count
    lines = [json.dumps(header)]
    for name, content in files:
        checksum = hashlib.sha256(content or b"").hexdigest()
        lines.append(json.dumps({"p": name, "c": checksum}))
    manifest_jsonl = ("\n".join(lines) + "\n").encode()
    manifest_zst = zstandard.ZstdCompressor().compress(manifest_jsonl)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        ti = tarfile.TarInfo(".mmw/manifest.jsonl.zst")
        ti.size = len(manifest_zst)
        tar.addfile(ti, io.BytesIO(manifest_zst))

        if deletes:
            body = ("\n".join(deletes) + "\n").encode()
            ti = tarfile.TarInfo(".mmw/delete.list")
            ti.size = len(body)
            tar.addfile(ti, io.BytesIO(body))

        for name, content in files:
            if content is None:
                continue    # the simulated "never made it into the tar" case
            ti = tarfile.TarInfo(name)
            ti.size = len(content)
            tar.addfile(ti, io.BytesIO(content))
    return buf.getvalue()


# Completeness check: a first attempt compared `manifest` (the network's
# COMPLETE expected tree) directly against paths seen in the tar, and broke
# every delta bundle — a delta's tar deliberately contains only what changed,
# so every legitimately-unchanged file (in the manifest, never a member of
# THIS tar) looked "missing" (see test_bundle_matches_publish_across_two_rounds
# in test_bundle.py, which this broke). The corrected version compares
# `written` against a declared `file_count` in the manifest header instead —
# BundleSink.close() computes it from its own send/generate lists, which ARE
# the exact scope of the bundle that produced this manifest, so a delta
# bundle simply declares a smaller number and is never flagged.

def test_declared_file_count_mismatch_is_detected(tmp_path):
    """The actual regression this check exists for: a tar stream tarfile
    reads as completely clean (no exception at all — proven live to be what
    a plain tar truncated right after its last complete member does) but
    that's missing a file the bundle declared it would contain."""
    ns = _load_module()
    dest = tmp_path / "far-side"

    have = "archive.ubuntu.com/ubuntu/dists/noble/InRelease"
    missing = "archive.ubuntu.com/ubuntu/pool/main/h/hello/hello.deb"
    # The bundle declares 2 files but the tar stream only actually got one —
    # simulating a truncation tarfile itself would never catch.
    bundle_bytes = _fake_bundle_bytes_multi(
        [(have, b"release content"), (missing, None)],
        file_count=2,
    )
    ns["open_stream"] = lambda path: _FakeProc(bundle_bytes)

    rc = ns["apply"]("ignored", str(dest))

    assert rc != 0
    assert (dest / have).exists(), "the file that WAS present should still land"
    assert not (dest / missing).exists()


def test_declared_file_count_matching_written_succeeds(tmp_path):
    """No false positive: a complete bundle whose declared count matches
    what was actually applied succeeds — this is what a real, healthy
    bundle (full or delta) looks like."""
    ns = _load_module()
    dest = tmp_path / "far-side"

    files = [
        ("archive.ubuntu.com/ubuntu/dists/noble/InRelease", b"release content"),
        ("archive.ubuntu.com/ubuntu/pool/main/h/hello/hello.deb", b"hello-bytes"),
    ]
    bundle_bytes = _fake_bundle_bytes_multi(files, file_count=len(files))
    ns["open_stream"] = lambda path: _FakeProc(bundle_bytes)

    rc = ns["apply"]("ignored", str(dest))

    assert rc == 0
    for name, content in files:
        assert (dest / name).read_bytes() == content


def test_a_delta_bundles_smaller_declared_count_is_not_flagged(tmp_path):
    """The exact shape of a real delta bundle: only ONE file actually
    changed and is present in the tar, declaring file_count=1 — even though
    a real manifest in this situation would separately list many more
    unchanged paths (identical to what this test's own manifest already
    does, since `files` here only ever contains what's actually present).
    Must succeed, not be treated as though 1 were "supposed to be more"."""
    ns = _load_module()
    dest = tmp_path / "far-side"

    changed = "archive.ubuntu.com/ubuntu/dists/noble/InRelease"
    bundle_bytes = _fake_bundle_bytes_multi([(changed, b"new release content")],
                                            file_count=1)
    ns["open_stream"] = lambda path: _FakeProc(bundle_bytes)

    rc = ns["apply"]("ignored", str(dest))

    assert rc == 0
    assert (dest / changed).read_bytes() == b"new release content"


def test_no_declared_file_count_skips_the_check(tmp_path):
    """A manifest written before this field existed has no `file_count` at
    all — the check must not fire on its absence (backward compatible with
    bundles already produced)."""
    ns = _load_module()
    dest = tmp_path / "far-side"

    have = "archive.ubuntu.com/ubuntu/dists/noble/InRelease"
    missing = "archive.ubuntu.com/ubuntu/pool/main/h/hello/hello.deb"
    bundle_bytes = _fake_bundle_bytes_multi(
        [(have, b"release content"), (missing, None)],
        # file_count omitted entirely
    )
    ns["open_stream"] = lambda path: _FakeProc(bundle_bytes)

    rc = ns["apply"]("ignored", str(dest))

    assert rc == 0, "no declared count means nothing to compare against"


def test_a_complete_bundle_is_not_flagged_as_missing(tmp_path):
    """A normal, complete bundle (nothing reverted touches this) must still
    apply cleanly."""
    ns = _load_module()
    dest = tmp_path / "far-side"

    files = [
        ("archive.ubuntu.com/ubuntu/dists/noble/InRelease", b"release content"),
        ("archive.ubuntu.com/ubuntu/pool/main/h/hello/hello.deb", b"hello-bytes"),
    ]
    bundle_bytes = _fake_bundle_bytes_multi(files)
    ns["open_stream"] = lambda path: _FakeProc(bundle_bytes)

    rc = ns["apply"]("ignored", str(dest))

    assert rc == 0
    for name, content in files:
        assert (dest / name).read_bytes() == content


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


def test_apply_warns_if_placeholder_survives_substitution(tmp_path, capsys):
    """The spec says an unsubstituted placeholder must never reach disk;
    nothing enforced it on the far side. `substitute_root` is a no-op here
    (standing in for a bug in that function, or some future case where it
    fails to fire) -- apply.py must at least warn rather than silently ship
    a config `nginx -t` will refuse to load with no clue why."""
    ns = _load_module()
    dest = tmp_path / "far-side"

    payload_name = "nginx/sites/example.com.conf"
    content = ("root %s/example.com;\n" % ns["ROOT_PLACEHOLDER"]).encode()
    bundle_bytes = _fake_bundle_bytes(payload_name, content)
    ns["open_stream"] = lambda path: _FakeProc(bundle_bytes)
    ns["substitute_root"] = lambda root, path: None  # never actually substitutes

    rc = ns["apply"]("ignored", str(dest))

    assert rc == 0  # a leftover placeholder warns; it does not fail the apply
    assert (dest / payload_name).read_bytes() == content  # still has it
    err = capsys.readouterr().err
    assert "still contains" in err
    assert ns["ROOT_PLACEHOLDER"] in err


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


# ── optional bundle compression: real open_stream/_is_zstd, not mocked ──────
# Everything above overrides `open_stream` directly to inject fake tar bytes.
# These exercise the real function — the actual magic-byte sniffing and the
# real `zstd` binary / plain-file dispatch — since that's the part unique to
# supporting an uncompressed bundle.

def test_is_zstd_detects_real_compressed_and_plain_files(tmp_path):
    ns = _load_module()

    compressed = tmp_path / "c.tar.zst"
    compressed.write_bytes(zstandard.ZstdCompressor().compress(b"hello"))
    assert ns["_is_zstd"](str(compressed)) is True

    plain = tmp_path / "p.tar"
    plain.write_bytes(b"not zstd at all")
    assert ns["_is_zstd"](str(plain)) is False


def test_open_stream_reads_a_real_plain_tar_without_zstd(tmp_path):
    """The actual regression this feature is for: apply() must be able to
    read a bundle mmw produced with compression turned off, via the real
    (unmocked) open_stream — no `zstd` binary involved for the outer stream."""
    ns = _load_module()
    dest = tmp_path / "far-side"

    payload_name = "archive.ubuntu.com/ubuntu/pool/main/h/hello/hello.deb"
    content = b"hello-package-content"
    bundle_bytes = _fake_bundle_bytes(payload_name, content)
    bundle_path = tmp_path / "lab.tar"   # no .zst — a genuinely plain tar
    bundle_path.write_bytes(bundle_bytes)

    rc = ns["apply"](str(bundle_path), str(dest))

    assert rc == 0
    assert (dest / payload_name).read_bytes() == content


@pytest.mark.skipif(shutil.which("zstd") is None, reason="requires the zstd binary")
def test_open_stream_reads_a_real_compressed_bundle_via_zstd(tmp_path):
    """The other half of the same dispatch: a genuinely zstd-compressed
    bundle still goes through the real `zstd -dc` subprocess, unmocked —
    proving detection didn't just remove the compressed path."""
    ns = _load_module()
    dest = tmp_path / "far-side"

    payload_name = "archive.ubuntu.com/ubuntu/pool/main/h/hello/hello.deb"
    content = b"hello-package-content"
    bundle_bytes = _fake_bundle_bytes(payload_name, content)
    bundle_path = tmp_path / "lab.tar.zst"
    bundle_path.write_bytes(zstandard.ZstdCompressor().compress(bundle_bytes))

    rc = ns["apply"](str(bundle_path), str(dest))

    assert rc == 0
    assert (dest / payload_name).read_bytes() == content


def test_a_delete_list_cannot_escape_the_root(tmp_path):
    """Path safety was tested for every *extraction* entry and for hardlink
    targets, but never for the delete list — and a hostile bundle that can
    unlink arbitrary files on the far side is the same class of bug as one
    that can write them. It was in fact guarded; it just failed silently,
    which is indistinguishable from not being guarded at all."""
    ns = _load_module()
    dest = tmp_path / "far-side"
    outsider = tmp_path / "precious.conf"
    outsider.write_text("do not delete me")

    bundle_bytes = _fake_bundle_bytes_multi(
        [("archive.ubuntu.com/ubuntu/dists/noble/InRelease", b"rel")],
        deletes=["../precious.conf"],
        file_count=1,
    )
    ns["open_stream"] = lambda path: _FakeProc(bundle_bytes)

    rc = ns["apply"]("ignored", str(dest))

    assert rc == ns["EXIT_UNSAFE"], f"expected EXIT_UNSAFE, got {rc}"
    assert outsider.exists(), "a delete list escaped --root"
    assert outsider.read_text() == "do not delete me"


def test_deleting_the_last_file_prunes_the_empty_directory(tmp_path):
    """The generated nginx sets `autoindex on`, so a retired suite that leaves
    its directory skeleton behind is browsable forever and accumulates with
    every rotation."""
    ns = _load_module()
    dest = tmp_path / "far-side"
    stale = dest / "archive.ubuntu.com/ubuntu/dists/oldsuite/main"
    stale.mkdir(parents=True)
    (stale / "Packages").write_text("old")

    bundle_bytes = _fake_bundle_bytes_multi(
        [("archive.ubuntu.com/ubuntu/dists/noble/InRelease", b"rel")],
        deletes=["archive.ubuntu.com/ubuntu/dists/oldsuite/main/Packages"],
        file_count=1,
    )
    ns["open_stream"] = lambda path: _FakeProc(bundle_bytes)

    rc = ns["apply"]("ignored", str(dest))

    assert rc == 0, rc
    assert not stale.exists(), "empty directory left behind after its last file went"
    assert not stale.parent.exists(), "and its now-empty parent too"
    # ...but never above the root, and never the tree that is still in use.
    assert (dest / "archive.ubuntu.com/ubuntu/dists/noble/InRelease").exists()
    assert dest.exists()
