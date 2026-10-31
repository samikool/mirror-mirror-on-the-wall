"""A real upstream apt repository for tests: `dists/` with by-hash, signed,
served over HTTP on 127.0.0.1, and synced by the ordinary `apt` provider."""
import asyncio
import concurrent.futures
import functools
import hashlib
import http.server
import shutil
import threading
from pathlib import Path

from mmw.core import config
from mmw.core.apt_index import Entry, compressed, packages_text, read_control, release_text
from mmw.core.pool import Pool
from mmw.core.resolve import resolve_sources
from mmw.core.signing import Keyring
from mmw.core.sync_engine import KeySyncCoordinator, sync_mirror
from mmw.providers.base import ResolveContext

DATE = "Thu, 21 Apr 2022 17:16:08 UTC"


class UpstreamRepo:
    def __init__(self, tmp_path: Path, debs: dict, arches=("amd64",), *, valid_until: str | None = None):
        self.root = tmp_path / "upstream-www"
        self.keyring = Keyring(tmp_path / "upstream-keys")
        self.keyring.generate("ubuntu")
        self.arches, self.valid_until = tuple(arches), valid_until
        handler = functools.partial(_Quiet, directory=str(self.root))
        self.root.mkdir(parents=True, exist_ok=True)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/ubuntu"
        self.republish(debs)

    @property
    def key_armor(self) -> str:
        return self.keyring.public_armor("ubuntu")

    def republish(self, debs: dict, date: str = DATE) -> None:
        repo = self.root / "ubuntu"
        if repo.exists():
            shutil.rmtree(repo)
        suites: dict[str, dict[str, list[Path]]] = {}
        for (suite, comp), paths in debs.items():
            suites.setdefault(suite, {})[comp] = paths
        for suite, comps in suites.items():
            files: dict[str, bytes] = {}
            for comp, paths in comps.items():
                entries = []
                for p in paths:
                    dest = repo / "pool" / comp / p.name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(p, dest)
                    data = p.read_bytes()
                    entries.append(Entry(read_control(p), f"pool/{comp}/{p.name}", len(data),
                                         hashlib.sha256(data).hexdigest(), hashlib.md5(data).hexdigest()))
                for arch in self.arches:
                    mine = [e for e in entries if e.control["Architecture"] in (arch, "all")]
                    for name, body in compressed("Packages", packages_text(mine)).items():
                        files[f"{comp}/binary-{arch}/{name}"] = body
            dists = repo / "dists" / suite
            for rel, body in files.items():
                path = dists / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
                by_hash = path.parent / "by-hash" / "SHA256" / hashlib.sha256(body).hexdigest()
                by_hash.parent.mkdir(parents=True, exist_ok=True)
                by_hash.write_bytes(body)
            fields = {"Origin": "Ubuntu", "Label": "Ubuntu", "Suite": suite, "Codename": suite,
                      "Date": date}
            if self.valid_until:
                fields["Valid-Until"] = self.valid_until
            fields.update({"Architectures": " ".join(self.arches), "Components": " ".join(comps),
                           "Acquire-By-Hash": "yes"})
            release = release_text(fields, files)
            (dists / "Release").write_bytes(release)
            (dists / "InRelease").write_bytes(self.keyring.clearsign("ubuntu", release))
            (dists / "Release.gpg").write_bytes(self.keyring.detach_sign("ubuntu", release))

    def source(self, suites, components=("main",)) -> config.SourceConfig:
        return config.SourceConfig(url=self.url, suites=list(suites), components=list(components),
                                   arches=list(self.arches), gpg_key=self.key_armor,
                                   gpg_key_name="ubuntu-archive-keyring.gpg")

    def close(self) -> None:
        self.server.shutdown()
        self.keyring.stop_agent()


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_a):
        pass


def sync_upstream(cfg: config.Config, src: config.SourceConfig) -> list:
    """Resolve and sync one apt source into cfg's pool; its units."""
    mirror_dir = cfg.settings.mirror_dir
    units, _, errors = resolve_sources([src], context=ResolveContext.from_config(cfg))
    assert not errors, errors
    with Pool(mirror_dir) as pool, concurrent.futures.ThreadPoolExecutor(2) as ex:
        keys = KeySyncCoordinator(mirror_dir)
        for unit in units:
            summary = asyncio.run(sync_mirror(unit, pool=pool, mirror_dir=mirror_dir, max_connections=2,
                                              executor=ex, keys=keys))
            assert summary.ok, (unit.name, summary.errors, summary.metadata_errors)
    return units
