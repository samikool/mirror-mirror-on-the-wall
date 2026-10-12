"""Phase 6: shared sync engine."""
import asyncio
import concurrent.futures
import hashlib


from mmw.core import sync_engine
from mmw.core.config import MirrorConfig
from mmw.core.pool import Pool
from mmw.core.sync_engine import DryRunStats, KeySyncCoordinator, classify_mirror
from mmw.providers.base import PackageInfo


def _mirror(name="m", label="centos", **kw) -> MirrorConfig:
    return MirrorConfig(name=name, provider="apt", label=label,
                        source_url="https://example.com/repo/", **kw)


def test_key_coordinator_reports_failures(monkeypatch, tmp_path):
    def boom(mirror_name, key_url, mirror_dir, client):
        raise ConnectionError("offline")

    monkeypatch.setattr(sync_engine, "sync_custom_key", boom)
    coord = KeySyncCoordinator(tmp_path)
    mirror = _mirror("a")
    mirror.gpg_key_url = "https://example.com/key.asc"
    failures = coord.sync_for(mirror)
    assert failures and "offline" in failures[0]


def test_key_coordinator_fetches_a_shared_key_once(monkeypatch, tmp_path):
    """Sources sharing a key URL must not each re-fetch it."""
    calls = []

    def record(mirror_name, key_url, mirror_dir, client):
        calls.append(key_url)
        return sync_engine.KeySyncResult(dest_dir=tmp_path)

    monkeypatch.setattr(sync_engine, "sync_custom_key", record)
    coord = KeySyncCoordinator(tmp_path)
    for name in ("a", "b", "c"):
        m = _mirror(name)
        m.gpg_key_url = "https://example.com/key.asc"
        coord.sync_for(m)
    assert calls == ["https://example.com/key.asc"]


def test_key_coordinator_is_a_noop_without_a_key_url(tmp_path):
    assert KeySyncCoordinator(tmp_path).sync_for(_mirror("a")) == []


def test_classify_mirror(monkeypatch, tmp_path):
    pool = Pool(tmp_path / "mirror")
    try:
        # seed the pool with one package
        content = b"pooled-bytes"
        checksum = hashlib.sha256(content).hexdigest()
        pool.add(content, checksum, "sha256", "a.deb", len(content))

        pkgs = [
            PackageInfo(name="a", url="u", checksum=checksum, checksum_type="sha256",
                        size=len(content), repo_path="a.deb", filename="a.deb"),
            PackageInfo(name="b", url="u", checksum="0" * 64, checksum_type="sha256",
                        size=500, repo_path="b.deb", filename="b.deb"),
            PackageInfo(name="legacy", url="u", checksum="", checksum_type="sha256",
                        size=0, repo_path="c.deb", filename="c.deb", checksum_known=False),
        ]
        monkeypatch.setattr(sync_engine, "fetch_packages_threaded",
                            lambda provider, mirror: pkgs)

        async def run():
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
                return await classify_mirror(_mirror(), pool, ex)

        stats = asyncio.run(run())
        assert (stats.total, stats.in_pool, stats.to_fetch, stats.fetch_size,
                stats.has_unknown) == (3, 1, 2, 500, True)
        assert isinstance(stats, DryRunStats)
    finally:
        pool.close()


# ── inline GPG keys need no fetch ────────────────────────────────────────────

ARMOUR = "-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nmQINBGXf\n-----END PGP PUBLIC KEY BLOCK-----"


def test_inline_key_is_written_without_any_network(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise AssertionError("an inline key must not trigger a fetch")

    monkeypatch.setattr(sync_engine, "sync_custom_key", boom)
    coord = KeySyncCoordinator(tmp_path)
    m = _mirror("a")
    m.gpg_key = ARMOUR
    assert coord.sync_for(m) == []

    # Grouped by **label**, not by expanded mirror name: ubuntu-ports alone
    # expands to 16 mirrors, which wrote 16 byte-identical copies of one key.
    written = list((tmp_path / "keys" / "centos").glob("*.asc"))
    assert len(written) == 1
    assert written[0].read_text().startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----")


def test_writing_a_key_emits_both_encodings(tmp_path):
    """`.asc` for `gpg --import`, `.gpg` for a `signed-by=` keyring path.

    Which one a client wants is not knowable here, and guessing wrong on an
    airgapped machine means a key that cannot be imported at all.
    """
    from mmw.core.keys import dearmor, write_key

    result = write_key("m", ARMOUR, tmp_path)
    assert result.fetched == 2 and not result.failed
    d = tmp_path / "keys" / "m"
    assert (d / "m.asc").read_text().startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----")
    assert (d / "m.gpg").read_bytes() == dearmor(ARMOUR)


def test_a_key_is_written_under_the_name_signed_by_asked_for(tmp_path):
    from mmw.core.keys import write_key

    write_key("m", ARMOUR, tmp_path, stem="cuda_debian_prod.gpg")
    d = tmp_path / "keys" / "m"
    assert (d / "cuda_debian_prod.asc").exists()
    assert (d / "cuda_debian_prod.gpg").exists()


def test_writing_a_key_twice_is_idempotent(tmp_path):
    from mmw.core.keys import write_key

    first = write_key("m", ARMOUR, tmp_path)
    second = write_key("m", ARMOUR, tmp_path)
    assert (first.fetched, first.skipped) == (2, 0)
    assert (second.fetched, second.skipped) == (0, 2)


def test_a_changed_key_is_rewritten(tmp_path):
    from mmw.core.keys import write_key

    write_key("m", ARMOUR, tmp_path)
    result = write_key("m", ARMOUR.replace("mQINBGXf", "DIFFEREN"), tmp_path)
    assert result.fetched == 2
    assert "DIFFEREN" in (tmp_path / "keys" / "m" / "m.asc").read_text()


def test_one_key_is_written_once_for_every_mirror_of_a_repository(tmp_path):
    """The DGX symptom: `ubuntu-ports` expands to 16 mirrors, and the key was
    written under each expanded name — 16 identical files in 16 directories,
    none of which publish could find."""
    coord = KeySyncCoordinator(tmp_path)
    written = []
    for suite in ("noble", "noble-updates"):
        for comp in ("main", "universe"):
            m = _mirror(f"ports-{suite}-{comp}-arm64", label="ubuntu",
                        suite=suite, component=comp)
            m.gpg_key = ARMOUR
            m.gpg_key_name = "ubuntu-archive-keyring.gpg"
            written.append(coord.sync_for(m))

    assert all(w == [] for w in written)
    files = sorted(p.name for p in (tmp_path / "keys" / "ubuntu").iterdir() if p.is_file())
    assert files == ["IMPORT.md", "ubuntu-archive-keyring.asc",
                     "ubuntu-archive-keyring.gpg"], files
    assert not (tmp_path / "keys" / "ports-noble-main-arm64").exists()


def test_repositories_sharing_a_label_keep_separate_keys(tmp_path):
    """`repo.download.nvidia.com` serves several repositories with *different*
    keys — grouping by label must not collapse them onto one file."""
    coord = KeySyncCoordinator(tmp_path)
    for stem in ("dgx_debian_prod.gpg", "nvidia-spark-repo.gpg"):
        m = _mirror(f"nvidia-{stem}", label="nvidia")
        m.gpg_key = ARMOUR
        m.gpg_key_name = stem
        coord.sync_for(m)

    files = sorted(p.name for p in (tmp_path / "keys" / "nvidia").iterdir() if p.is_file())
    assert "dgx_debian_prod.asc" in files and "nvidia-spark-repo.asc" in files
