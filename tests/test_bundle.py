import hashlib
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import zstandard

from mmw.core import config
from mmw.core.pool import KIND_META, KIND_PKG, KIND_RELEASE, Pool
from mmw.core.publisher import publish_network
from mmw.core.publisher.bundle import bundle_network
from mmw.core.publisher.nginx import ROOT_PLACEHOLDER


@pytest.fixture
def seeded(tmp_path):
    """A pool holding one two-component apt mirror, refs reconciled."""
    mirror_dir = tmp_path / "mirror"
    pool = Pool(mirror_dir)
    src = "https://archive.ubuntu.com/ubuntu"
    rows = [
        ("pool/main/h/hello/hello_1.0_amd64.deb", b"hello-pkg", KIND_PKG),
        ("dists/noble/main/binary-amd64/Packages.gz", b"pkg-index", KIND_META),
        ("dists/noble/InRelease", b"signed-release", KIND_RELEASE),
    ]
    refs = []
    for repo_path, content, kind in rows:
        cs = hashlib.sha256(content).hexdigest()
        pool.add(content, cs, "sha256", Path(repo_path).name, len(content))
        refs.append((cs, repo_path, kind))
    pool.reconcile_refs("ubuntu-noble-main-amd64", refs, source=src)
    pool.record_mirror(config.MirrorConfig(
        name="ubuntu-noble-main-amd64", source=src, provider="apt",
        source_url=src, arch="amd64", suite="noble", component="main",
        label="ubuntu",
    ))
    pool.close()
    cfg = config.Config(
        settings=config.Settings(mirror_dir=mirror_dir),
        sources=[config.SourceConfig(url=src, suites=["noble"],
                                     components=["main"], arches=["amd64"])],
    )
    return mirror_dir, cfg


def _extract(bundle: Path, dest: Path) -> None:
    dctx = zstandard.ZstdDecompressor()
    dest.mkdir(parents=True, exist_ok=True)
    with open(bundle, "rb") as raw, dctx.stream_reader(raw) as zr:
        with tarfile.open(fileobj=zr, mode="r|") as tar:
            # filter="data" is the modern (3.12+) extraction filter: it
            # rejects absolute paths, `..` traversal, device files, and
            # strips owner/permission bits that could affect files outside
            # the archive. Test-only convenience — apply.py (Task 8) ships to
            # far sides as old as RHEL 8's Python 3.6, where `filter=` does
            # not exist, so it must do its own path validation rather than
            # lean on this.
            tar.extractall(dest, filter="data")


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*") if p.is_file()
    }


def test_bundle_matches_publish_exactly(seeded, tmp_path):
    """THE test. One enumeration, two sinks — the outputs must not diverge."""
    mirror_dir, cfg = seeded

    pub_root = tmp_path / "published"
    net_pub = config.NetworkConfig(name="lab", root=pub_root)
    publish_network(net_pub, cfg, mirror_dir)

    net_bun = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out")
    result = bundle_network(net_bun, cfg, mirror_dir)

    applied = tmp_path / "applied"
    _extract(result.out_path, applied)

    published = _tree(pub_root)
    bundled = {k: v for k, v in _tree(applied).items() if not k.startswith(".mmw/")}

    # nginx/ conf embeds the literal serving root, which a live publish knows
    # (`net_pub.root`) and a bundle deliberately does not — it writes
    # ROOT_PLACEHOLDER instead, to be resolved by apply.py at the far side
    # (nginx.py's own docstring; independently locked in by
    # test_nginx_conf.py::test_placeholder_root_is_left_for_apply_time).
    # Byte-identical nginx/ between the two destinations is therefore
    # impossible by design — but dropping the directory from the comparison
    # entirely would make this, the one test whose job is catching exactly
    # this kind of divergence, blind to a real BundleSink regression there
    # (wrong rel, swapped per-host content, a host silently missing). So
    # every nginx key is still required to be present on both sides, and its
    # content is asserted to differ *only* by the root substitution: undo
    # that one substitution and the bytes must match exactly.
    nginx_published = {k: v for k, v in published.items() if k.startswith("nginx/")}
    nginx_bundled = {k: v for k, v in bundled.items() if k.startswith("nginx/")}
    assert set(nginx_bundled) == set(nginx_published)
    for key, bundled_bytes in nginx_bundled.items():
        substituted = bundled_bytes.replace(
            ROOT_PLACEHOLDER.encode(), str(pub_root).encode())
        assert substituted == nginx_published[key], key

    # Everything else — the actual repository content — must match exactly.
    other_published = {k: v for k, v in published.items() if not k.startswith("nginx/")}
    other_bundled = {k: v for k, v in bundled.items() if not k.startswith("nginx/")}
    assert other_bundled == other_published


def test_bundle_network_forwards_archive_progress(seeded, tmp_path):
    """`bundle_network` threads its `progress` callback into `BundleSink` so
    the archive-writing phase — the actual I/O, which happens entirely
    inside `close()` — is visible too, not just the fast in-memory
    enumeration phase `publish_group` reports on."""
    mirror_dir, cfg = seeded
    net = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out")

    events: list[str] = []
    bundle_network(net, cfg, mirror_dir, progress=lambda rel, status: events.append(status))

    assert any(s.startswith("archive-start:") for s in events)
    assert any(s.startswith("archive-done:") for s in events)


def test_second_bundle_of_unchanged_pool_sends_nothing(seeded, tmp_path):
    mirror_dir, cfg = seeded
    net = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out")

    first = bundle_network(net, cfg, mirror_dir)
    assert first.sent > 0
    assert first.previous is None

    second = bundle_network(net, cfg, mirror_dir)
    assert second.sent == 0
    assert second.previous == first.created

    applied = tmp_path / "applied2"
    _extract(second.out_path, applied)
    payload = [k for k in _tree(applied) if not k.startswith(".mmw/")]
    assert payload == []


def test_changed_ref_is_carried_and_manifest_stays_complete(seeded, tmp_path):
    mirror_dir, cfg = seeded
    net = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out")
    bundle_network(net, cfg, mirror_dir)

    # Republish the index with new content, as an upstream sync would.
    pool = Pool(mirror_dir)
    new = b"pkg-index-v2"
    cs_new = hashlib.sha256(new).hexdigest()
    pool.add(new, cs_new, "sha256", "Packages.gz", len(new))
    keep = [
        (hashlib.sha256(b"hello-pkg").hexdigest(),
         "pool/main/h/hello/hello_1.0_amd64.deb", KIND_PKG),
        (cs_new, "dists/noble/main/binary-amd64/Packages.gz", KIND_META),
        (hashlib.sha256(b"signed-release").hexdigest(),
         "dists/noble/InRelease", KIND_RELEASE),
    ]
    pool.reconcile_refs("ubuntu-noble-main-amd64", keep,
                        source="https://archive.ubuntu.com/ubuntu")
    pool.close()

    second = bundle_network(net, cfg, mirror_dir)
    # The changed index has two paths pointing at it: the canonical
    # Packages.gz and its by-hash/SHA256/{checksum} companion (links.py's
    # `_link_rows` always emits both for an apt index file). The checksum
    # changed, so the by-hash path is new too — a real path the far side does
    # not have yet, not a duplicate of the canonical one — and BundleSink
    # counts every accepted `link()` call, matching TreeSink's behaviour of
    # actually linking both (TreeSink just doesn't fold the by-hash call into
    # its caller-facing `linked` count — see links.py's `_link_rows`).
    assert second.sent == 2

    applied = tmp_path / "applied3"
    _extract(second.out_path, applied)
    payload = {k for k in _tree(applied) if not k.startswith(".mmw/")}
    assert payload == {
        "archive.ubuntu.com/ubuntu/dists/noble/main/binary-amd64/Packages.gz",
        "archive.ubuntu.com/ubuntu/dists/noble/main/binary-amd64/by-hash/SHA256/"
        + hashlib.sha256(b"pkg-index-v2").hexdigest(),
    }

    from mmw.core.publisher.manifest import read_manifest
    _, files = read_manifest(second.manifest_path)
    # The manifest lists the network's whole state (6 rows: pkg, meta
    # canonical + by-hash, release canonical + by-hash, and the one
    # unchanged-since-last-bundle nginx/mirror.conf) — but the tar only
    # carries what's new since the last bundle (5: the 2 changed payload
    # files plus the 3 `.mmw/` control files), since mirror.conf is identical
    # to what the far side already has from the first bundle and is never
    # re-sent. +1 is exactly that gap, not a coincidence.
    assert len(files) == len(_tree(applied)) + 1   # whole tree, not the delta


def test_full_ignores_the_ledger(seeded, tmp_path):
    mirror_dir, cfg = seeded
    net = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out")
    first = bundle_network(net, cfg, mirror_dir)
    again = bundle_network(net, cfg, mirror_dir, full=True)
    assert again.sent == first.sent
    assert again.full is True


def test_the_certs_directory_never_travels(seeded, tmp_path):
    """Parity with publish, which keeps mirror.key outside network.root so a
    misconfigured server can never serve it. A certs network's bundle carries
    the signed cert and key — but only into `.mmw/tls/`, never a served path,
    and never the certs/ working directory (CSR, openssl config)."""
    mirror_dir, cfg = seeded
    _sign_lab_cert(tmp_path, mirror_dir, ["archive.ubuntu.com"])

    net = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out", certs=True)
    result = bundle_network(net, cfg, mirror_dir)

    applied = tmp_path / "applied-certs"
    _extract(result.out_path, applied)
    tree = _tree(applied)
    assert not any("certs/" in k or k.endswith(".csr") or k.endswith(".cnf") for k in tree)
    assert [k for k in tree if "mirror.key" in k] == [".mmw/tls/mirror.key"]


def test_bundle_without_destination_is_an_error(seeded, tmp_path):
    mirror_dir, cfg = seeded
    net = config.NetworkConfig(name="lab")
    with pytest.raises(ValueError, match="no bundle destination"):
        bundle_network(net, cfg, mirror_dir)


def test_apply_reproduces_the_published_tree(seeded, tmp_path):
    mirror_dir, cfg = seeded
    pub_root = tmp_path / "published"
    publish_network(config.NetworkConfig(name="lab", root=pub_root), cfg, mirror_dir)

    net = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out")
    result = bundle_network(net, cfg, mirror_dir)

    dest = tmp_path / "far-side"
    subprocess.run(
        [sys.executable, str(tmp_path / "out" / "apply.py"),
         str(result.out_path), "--root", str(dest)],
        check=True,
    )
    assert _tree(dest / "archive.ubuntu.com") == _tree(pub_root / "archive.ubuntu.com")
    assert (dest / ".mmw-state").read_text().strip() == result.created

    # The nginx root is a placeholder in the tarball and real once applied.
    conf = (dest / "nginx" / "mirror.conf").read_text()
    assert "__MMW_ROOT__" not in conf
    assert f"root {dest}/archive.ubuntu.com;" in conf


def test_apply_refuses_out_of_order(seeded, tmp_path):
    mirror_dir, cfg = seeded
    net = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out")
    bundle_network(net, cfg, mirror_dir)
    # Change something so the second bundle is a real delta.
    (tmp_path / "out" / "sentinel").write_text("x")
    second = bundle_network(net, cfg, mirror_dir, full=False)

    dest = tmp_path / "far-side"
    proc = subprocess.run(
        [sys.executable, str(tmp_path / "out" / "apply.py"),
         str(second.out_path), "--root", str(dest)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert proc.returncode != 0
    assert b"expects" in proc.stderr or b"out of order" in proc.stderr
    # The message must recover the operator, not name a capability no
    # user-facing surface can invoke (`_execute_bundle` hardcodes
    # `full=False`).
    assert b"--full" not in proc.stderr


def test_apply_verify_succeeds_immediately_after_apply(seeded, tmp_path):
    """CRITICAL 2 (whole-branch review) -- `--verify` is the far side's only
    way to distinguish a good transfer from a corrupt one. Before the fix,
    every nginx/ file failed verification on every healthy bundle (nginx
    defaults to True): BundleSink hashes the ROOT_PLACEHOLDER bytes it wrote
    into the manifest, and apply() then rewrites those exact files in place
    with the real --root before verify_tree hashes them off disk -- a
    guaranteed mismatch ("0 missing, 2 wrong", exit 4). No test invoked
    --verify at all before this one."""
    mirror_dir, cfg = seeded
    net = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out")
    result = bundle_network(net, cfg, mirror_dir)
    assert result.errors == []

    dest = tmp_path / "far-side"
    proc = subprocess.run(
        [sys.executable, str(tmp_path / "out" / "apply.py"),
         str(result.out_path), "--root", str(dest), "--verify"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    assert b"verify FAILED" not in proc.stdout
    assert b"verified" in proc.stdout


def test_full_bundle_still_carries_deletions(seeded, tmp_path):
    """IMPORTANT 4 -- `--full` means "send everything", never "forget what
    the far side has". The original implementation set `prev = {}` for
    `full=True`, so a full bundle always computed zero deletions -- and
    permanently: the new manifest it writes doesn't list the stale path
    either, so no future *delta* bundle could ever propose deleting it
    either."""
    mirror_dir, cfg = seeded
    net = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out")
    bundle_network(net, cfg, mirror_dir)

    # The package falls out of the upstream index, as a real re-sync would
    # produce -- only the meta/release refs remain.
    pool = Pool(mirror_dir)
    pool.reconcile_refs("ubuntu-noble-main-amd64", [
        (hashlib.sha256(b"pkg-index").hexdigest(),
         "dists/noble/main/binary-amd64/Packages.gz", KIND_META),
        (hashlib.sha256(b"signed-release").hexdigest(),
         "dists/noble/InRelease", KIND_RELEASE),
    ], source="https://archive.ubuntu.com/ubuntu")
    pool.close()

    full = bundle_network(net, cfg, mirror_dir, full=True)
    assert full.full is True
    assert full.deleted == 1

    applied = tmp_path / "applied-full"
    _extract(full.out_path, applied)
    deletes = (applied / ".mmw" / "delete.list").read_text().split()
    assert deletes == [
        "archive.ubuntu.com/ubuntu/pool/main/h/hello/hello_1.0_amd64.deb"
    ]


def test_two_bundles_in_the_same_second_get_distinct_stamps(seeded, tmp_path, monkeypatch):
    """IMPORTANT 5 -- now_stamp() has 1-second resolution; two bundles run
    back-to-back (well under a second for a near-empty delta, which is
    exactly what an operator does when unsure a first run worked) used to
    collide: the second run's tmp-then-rename silently replaced the first
    run's tarball AND its ledger manifest, destroying the record of what the
    far side was actually sent, and made `previous == created` -- a
    self-referential header."""
    from mmw.core.publisher import bundle as bundle_mod

    mirror_dir, cfg = seeded
    net = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out")

    # Force the collision deterministically rather than hoping two real
    # calls land in the same wall-clock second.
    monkeypatch.setattr(bundle_mod, "now_stamp", lambda: "20260906T143000Z")

    first = bundle_network(net, cfg, mirror_dir)
    second = bundle_network(net, cfg, mirror_dir)

    assert first.created == "20260906T143000Z"
    assert second.created == "20260906T143001Z"   # bumped forward exactly 1s
    assert first.out_path != second.out_path
    assert first.out_path.exists()
    assert second.out_path.exists()
    assert first.manifest_path.exists()
    assert second.manifest_path.exists()


def test_bundle_matches_publish_across_two_rounds(seeded, tmp_path):
    """Extends `test_bundle_matches_publish_exactly`, which only ever
    exercises ONE round -- no prune, no by-hash retirement, no delta -- so
    the "one prune contract" this whole pipeline claims to have was never
    actually tested end to end. Round 2 drops a package and republishes a
    changed index, then diffs the applied-from-bundle tree against the
    published-directly tree.

    The only allowed divergence is a stale by-hash generation `TreeSink`
    deliberately keeps for in-flight clients (`_BY_HASH_KEEP_STALE = 2`,
    sinks.py) that `BundleSink` -- having no directory to read mtimes from --
    retires the instant a checksum stops being live (also documented in
    sinks.py). Anything else diverging here would be a real bundle/publish
    parity bug.
    """
    mirror_dir, cfg = seeded

    pub_root = tmp_path / "published"
    net_pub = config.NetworkConfig(name="lab", root=pub_root)
    publish_network(net_pub, cfg, mirror_dir)

    net_bun = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out")
    first = bundle_network(net_bun, cfg, mirror_dir)

    dest = tmp_path / "far-side"
    subprocess.run(
        [sys.executable, str(tmp_path / "out" / "apply.py"),
         str(first.out_path), "--root", str(dest)],
        check=True,
    )
    assert _tree(dest / "archive.ubuntu.com") == _tree(pub_root / "archive.ubuntu.com")

    # ── Round 2: change an index, drop a package ───────────────────────────
    pool = Pool(mirror_dir)
    new_index = b"pkg-index-v2"
    cs_new = hashlib.sha256(new_index).hexdigest()
    pool.add(new_index, cs_new, "sha256", "Packages.gz", len(new_index))
    pool.reconcile_refs("ubuntu-noble-main-amd64", [
        (cs_new, "dists/noble/main/binary-amd64/Packages.gz", KIND_META),
        (hashlib.sha256(b"signed-release").hexdigest(),
         "dists/noble/InRelease", KIND_RELEASE),
        # hello_1.0_amd64.deb is gone -- dropped from the upstream index.
    ], source="https://archive.ubuntu.com/ubuntu")
    pool.close()

    publish_network(net_pub, cfg, mirror_dir)
    second = bundle_network(net_bun, cfg, mirror_dir)
    subprocess.run(
        [sys.executable, str(tmp_path / "out" / "apply.py"),
         str(second.out_path), "--root", str(dest)],
        check=True,
    )

    published = _tree(pub_root)
    applied = {k: v for k, v in _tree(dest).items() if not k.startswith(".mmw")}

    # nginx/ is already substituted in place by apply.py on both sides, but
    # with two DIFFERENT real roots (pub_root vs dest) -- normalize each back
    # to a common placeholder before comparing, same idea as the
    # single-round test's ROOT_PLACEHOLDER undo.
    nginx_published = {k: v for k, v in published.items() if k.startswith("nginx/")}
    nginx_applied = {k: v for k, v in applied.items() if k.startswith("nginx/")}
    assert set(nginx_applied) == set(nginx_published)
    for key, applied_bytes in nginx_applied.items():
        norm_applied = applied_bytes.replace(str(dest).encode(), b"<ROOT>")
        norm_published = nginx_published[key].replace(str(pub_root).encode(), b"<ROOT>")
        assert norm_applied == norm_published, key

    other_published = {k: v for k, v in published.items() if not k.startswith("nginx/")}
    other_applied = {k: v for k, v in applied.items() if not k.startswith("nginx/")}

    # Everything present on both sides must be byte-identical.
    common = set(other_applied) & set(other_published)
    for k in common:
        assert other_applied[k] == other_published[k], k

    # Published-only files must be exactly the stale-by-hash-retention gap
    # documented above -- nothing else may diverge.
    only_published = set(other_published) - set(other_applied)
    assert all("by-hash" in p for p in only_published), only_published
    # Nothing may exist only on the applied side -- a bundle never invents
    # content publish doesn't also have.
    assert set(other_applied) - set(other_published) == set()

    # The dropped package must actually be gone from both trees.
    assert not any("hello_1.0_amd64.deb" in k for k in other_published)
    assert not any("hello_1.0_amd64.deb" in k for k in other_applied)


# --- TLS: publish serves it, a certs network's bundles carry it ------------

def _sign_lab_cert(tmp_path: Path, mirror_dir: Path, hostnames: list[str]) -> Path:
    """A throwaway CA signs a `lab` network key — EC, so it is fast."""
    from datetime import datetime, timedelta, timezone

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    from mmw.core import certs

    def pem_key(k):
        return k.private_bytes(serialization.Encoding.PEM,
                               serialization.PrivateFormat.PKCS8,
                               serialization.NoEncryption())

    ca_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Lab CA")])
    now = datetime.now(timezone.utc)
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
          .public_key(ca_key.public_key()).serial_number(1)
          .not_valid_before(now - timedelta(days=1))
          .not_valid_after(now + timedelta(days=365))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .sign(ca_key, hashes.SHA256()))
    (tmp_path / "ca.crt").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "ca.key").write_bytes(pem_key(ca_key))

    out = mirror_dir / "certs" / "lab"
    out.mkdir(parents=True)
    key = ec.generate_private_key(ec.SECP256R1())
    (out / "mirror.key").write_bytes(pem_key(key))
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostnames[0])]))
           .add_extension(x509.SubjectAlternativeName(
               [x509.DNSName(h) for h in hostnames]), critical=False)
           .sign(key, hashes.SHA256()))
    (out / "mirror.csr").write_bytes(csr.public_bytes(serialization.Encoding.PEM))
    return certs.sign_csr(out / "mirror.csr", tmp_path / "ca.crt", tmp_path / "ca.key")


def test_publish_serves_https_once_signed(seeded, tmp_path):
    mirror_dir, cfg = seeded
    crt = _sign_lab_cert(tmp_path, mirror_dir, ["archive.ubuntu.com"])
    root = tmp_path / "published"
    publish_network(config.NetworkConfig(name="lab", root=root, certs=True), cfg, mirror_dir)
    conf = (root / "nginx" / "mirror.conf").read_text()
    assert "listen 443 ssl;" in conf
    assert f"ssl_certificate {crt};" in conf
    assert f"ssl_certificate_key {crt.parent / 'mirror.key'};" in conf
    # The key is referenced where it lives — never copied into the served tree.
    assert not any(p.name == "mirror.key" for p in root.rglob("*"))


def test_publish_without_a_signed_cert_stays_http(seeded, tmp_path):
    mirror_dir, cfg = seeded
    root = tmp_path / "published"
    publish_network(config.NetworkConfig(name="lab", root=root, certs=True), cfg, mirror_dir)
    assert "443" not in (root / "nginx" / "mirror.conf").read_text()


def test_publish_never_wires_a_mismatched_pair(seeded, tmp_path):
    """nginx refuses to start on a cert/key mismatch — the whole server, not
    one site. Keep serving HTTP rather than take everything down."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    mirror_dir, cfg = seeded
    crt = _sign_lab_cert(tmp_path, mirror_dir, ["archive.ubuntu.com"])
    (crt.parent / "mirror.key").write_bytes(
        ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
    root = tmp_path / "published"
    publish_network(config.NetworkConfig(name="lab", root=root, certs=True), cfg, mirror_dir)
    assert "443" not in (root / "nginx" / "mirror.conf").read_text()


def test_dist_upgrade_network_serves_changelogs(seeded, tmp_path):
    mirror_dir, cfg = seeded
    root = tmp_path / "published"
    publish_network(config.NetworkConfig(name="lab", root=root, dist_upgrade=True),
                    cfg, mirror_dir)
    conf = (root / "nginx" / "mirror.conf").read_text()
    assert "server_name changelogs.ubuntu.com;" in conf
    assert f"root {root}/changelogs.ubuntu.com;" in conf


def test_certs_without_a_signed_cert_refuses_to_bundle_up_front(seeded, tmp_path):
    """Not a silently HTTP-only bundle the far side's clients would refuse."""
    mirror_dir, cfg = seeded
    net = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out", certs=True)
    with pytest.raises(ValueError, match="no usable signed certificate"):
        bundle_network(net, cfg, mirror_dir)
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").glob("*.tar*"))


def test_certs_bundle_round_trip(seeded, tmp_path):
    """Signed here, carried in the bundle, installed 0600 on the far side,
    and named by the far side's own nginx config with the real root."""
    import os
    import stat
    mirror_dir, cfg = seeded
    crt = _sign_lab_cert(tmp_path, mirror_dir, ["archive.ubuntu.com"])
    net = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out", certs=True)
    result = bundle_network(net, cfg, mirror_dir)
    assert not result.errors

    dest = tmp_path / "far-side"
    proc = subprocess.run(
        [sys.executable, str(tmp_path / "out" / "apply.py"),
         str(result.out_path), "--root", str(dest), "--verify"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "TLS: mirror.crt, mirror.key" in proc.stdout

    tls = dest / ".mmw" / "tls"
    assert (tls / "mirror.crt").read_bytes() == crt.read_bytes()
    assert (tls / "mirror.key").read_bytes() == (crt.parent / "mirror.key").read_bytes()
    assert stat.S_IMODE(os.stat(tls / "mirror.key").st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tls).st_mode) == 0o700

    conf = (dest / "nginx" / "mirror.conf").read_text()
    assert f"ssl_certificate {tls}/mirror.crt;" in conf
    assert f"ssl_certificate_key {tls}/mirror.key;" in conf
    assert "__MMW_ROOT__" not in conf


def test_certs_ride_every_delta(seeded, tmp_path):
    """A delta that changes nothing still carries the TLS files — a re-signed
    certificate must reach the far side without a --full bundle."""
    mirror_dir, cfg = seeded
    _sign_lab_cert(tmp_path, mirror_dir, ["archive.ubuntu.com"])
    net = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out", certs=True)
    bundle_network(net, cfg, mirror_dir)
    second = bundle_network(net, cfg, mirror_dir)
    assert second.sent == 0
    applied = tmp_path / "second"
    _extract(second.out_path, applied)
    assert (applied / ".mmw" / "tls" / "mirror.key").exists()


def test_without_certs_a_bundle_is_http_only_and_carries_no_key(seeded, tmp_path):
    """A signed cert exists here, but the network has certs off — no key on
    the media, and the far side's nginx points at no TLS files."""
    mirror_dir, cfg = seeded
    _sign_lab_cert(tmp_path, mirror_dir, ["archive.ubuntu.com"])
    net = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out", certs=False)
    result = bundle_network(net, cfg, mirror_dir)
    applied = tmp_path / "applied"
    _extract(result.out_path, applied)
    assert "443" not in (applied / "nginx" / "mirror.conf").read_text()
    assert not (applied / ".mmw" / "tls").exists()
