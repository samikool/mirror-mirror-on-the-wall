"""Opt-in: stock pip and uv → HTTPS nginx at PyPI's own hostnames, offline.

MMW_PYPI_E2E=1 enables this test; ordinary pytest never starts Docker.
See tests/e2e/README.md for tooling preparation.
"""
import asyncio
import concurrent.futures
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

import pytest
from uv import find_uv_bin

from mmw.core import certs, config, container_image, downloader
from mmw.core.pool import Pool
from mmw.core.publisher import bundle_network, nginx_container, publish_network
from mmw.core.resolve import resolve_sources
from mmw.core.sync_engine import sync_mirror
from mmw.providers.base import ResolveContext
from mmw.providers.pypi import index
from mmw.providers.pypi import uv as uvmod
from pypi_real import FLIT_CORE, committed_wheel, fake_from_real, real_flit_sdist, real_wheel
from pypi_server import serve
from e2e_stack import ComposeStack, _run
from test_vscode_client_e2e import _ca

pytestmark = pytest.mark.skipif(os.environ.get("MMW_PYPI_E2E") != "1",
                                reason="opt-in real Docker pip/uv clients: MMW_PYPI_E2E=1")
CHECKS = Path(__file__).parent / "e2e"


class PypiStack(ComposeStack):
    """PyPI on the shared harness: a stock Python client with mmw's bundled uv
    mounted read-only, trusting the CA the way PYPI.md says (system store;
    UV_NATIVE_TLS for uv), with no index flags."""
    PREFIX = "mmw-pypi-e2e-"
    ALIASES = ("pypi.org", "files.pythonhosted.org", "uncovered.fixture.invalid")
    CLIENT_IMAGE = ("MMW_PYPI_CLIENT_IMAGE", "python:3.13-slim")
    NGINX_IMAGE = ("MMW_PYPI_NGINX_IMAGE", "nginx:latest")
    CLIENT_ENV = {"UV_NATIVE_TLS": "1", "UV_PYTHON_DOWNLOADS": "never"}

    def __init__(self, scratch, root, ca, network):
        super().__init__(scratch, root, ca, network)
        self.state.chmod(0o777)     # the client writes its probe and project here

    def client_volumes(self):
        return [{"type": "bind", "source": str(CHECKS.resolve()), "target": "/scripts", "read_only": True},
                {"type": "bind", "source": str(Path(find_uv_bin()).resolve()), "target": "/opt/uv/uv",
                 "read_only": True}]

    def sh(self, script):
        return self.compose("exec", "-T", "client", "bash", "-c", "set -euo pipefail\n" + script)

    def hashed_install(self):
        self.sh("python3 -m venv /tmp/h && /tmp/h/bin/pip install -q --no-cache-dir --require-hashes "
                "-r /state/reqs.txt")

    def locked_sync(self):
        """`uv sync --frozen` from a fresh copy and an empty cache: every wheel
        is fetched through the lock's own files.pythonhosted.org URLs."""
        self.sh("rm -rf /tmp/proj2 && cp -r /state/proj /tmp/proj2 && rm -rf /tmp/proj2/.venv && cd /tmp/proj2 && "
                "UV_NO_CACHE=1 /opt/uv/uv sync -q --frozen && .venv/bin/python -c 'import mmw_a, mmw_b'")

    def probe(self):
        self.compose("exec", "-T", "client", "python3", "/scripts/pypi-client-probe.py")
        probe = json.loads((self.state / "probe.json").read_text())
        assert probe["tls"] == "verified" and probe["negotiation"] == "ok"
        assert probe["uncovered"].startswith("refused"), probe

    def installs(self):
        """(a) dependency pinned below the newest, (b) an sdist built offline
        with mirrored build tools — by pip and by uv, with no index flags."""
        tag = uuid.uuid4().hex[:6]
        shown = self.sh(f"python3 -m venv /tmp/p{tag} && /tmp/p{tag}/bin/pip install -q mmw-a && "
                        f"/tmp/p{tag}/bin/pip show mmw-b")
        assert "Version: 1.0" in shown, shown
        self.sh(f"python3 -m venv /tmp/q{tag} && /tmp/q{tag}/bin/pip install -q mmw-c && "
                f"/tmp/q{tag}/bin/python -c 'import mmw_c; assert mmw_c.__version__ == \"1.0\"'")
        self.sh(f"/opt/uv/uv venv -q /tmp/u{tag} && /opt/uv/uv pip install -q --python /tmp/u{tag} mmw-c && "
                f"/tmp/u{tag}/bin/python -c 'import mmw_c'")


def _sync(cfg):
    units, _, errors = resolve_sources(cfg.sources, context=ResolveContext.from_config(cfg))
    assert not errors, errors
    ex = concurrent.futures.ThreadPoolExecutor(4)
    mirror_dir = cfg.settings.mirror_dir

    async def go():
        with Pool(mirror_dir) as pool:
            return [await sync_mirror(u, pool=pool, mirror_dir=mirror_dir, max_connections=4, executor=ex)
                    for u in units]
    try:
        assert all(s.ok for s in asyncio.run(go()))
    finally:
        ex.shutdown()


def test_stock_clients_live_publish_and_applied_bundle(tmp_path, monkeypatch):
    scratch = tmp_path.resolve()
    scratch.chmod(0o755)        # nginx workers must traverse the disposable published tree
    a = real_wheel("mmw-a", "1.0", requires=["mmw-b<2"])
    b1, b2 = real_wheel("mmw-b", "1.0"), real_wheel("mmw-b", "2.0")
    c = real_flit_sdist("mmw-c", "1.0")
    flit = committed_wheel(FLIT_CORE)
    fake = fake_from_real([("mmw-a", *a), ("mmw-b", *b1), ("mmw-b", *b2), ("mmw-c", c[0], c[1], None),
                           ("flit-core", *flit)])
    monkeypatch.setattr(index, "_client", fake.client)
    monkeypatch.setattr(downloader, "_thread_client", fake.client)
    monkeypatch.setattr(nginx_container, "DOCKER", "docker")   # explicit opt-in; ordinary tests never use Docker
    original_run = container_image._run
    monkeypatch.setattr(container_image, "_run", lambda docker, *args, **kw:
                        "" if args[0] == "pull" else original_run(docker, *args, **kw))

    src = config.PypiSource(provider="pypi", pythons=["3.13"], platforms=["linux-glibc"], arches=["amd64"],
                            all=True, requirements=["mmw-c"])
    net = config.NetworkConfig(name="e2e-" + uuid.uuid4().hex[:12], root=scratch / "published", certs=True,
                               bundle_dir=scratch / "bundles", retention="90d")
    cfg = config.Config(settings=config.Settings(mirror_dir=scratch / "mirror"), sources=[src], networks=[net])
    with serve(fake) as base:
        monkeypatch.setattr(uvmod, "INDEX_URL", f"{base}/simple")
        _sync(cfg)
    # The straight mirror carries these too; this proves the requirements list
    # resolved mmw-c *and its build tool* on its own (spec §4.4).
    with Pool(cfg.settings.mirror_dir, read_only=True) as pool:
        req_files = {p.rsplit("/", 1)[1] for p in pool.ref_map("pypi-req-universal")}
    assert {c[0], FLIT_CORE} <= req_files, req_files
    ca, key = _ca(scratch)
    _, _, csr = certs.generate_key_and_csr(certs.collect_hostnames(net, cfg), certs.cert_dir(net, cfg))
    certs.sign_csr(csr, ca, key)
    published = publish_network(net, cfg, cfg.settings.mirror_dir)
    assert not published.errors, published.errors

    with PypiStack(scratch, net.root, ca, net.name) as stack:
        stack.probe()
        stack.installs()
        # (c) hashes survive the mirror
        hashes = {name: hashlib.sha256(body).hexdigest() for name, body, _ in (a, b1)}
        reqs = (f"mmw-a==1.0 --hash=sha256:{hashes[a[0]]}\n"
                f"mmw-b==1.0 --hash=sha256:{hashes[b1[0]]}\n")
        (stack.state / "reqs.txt").write_text(reqs)
        stack.hashed_install()
        # (d) a uv.lock written against the mirror names PyPI's own URLs and re-syncs fresh
        proj = stack.state / "proj"
        proj.mkdir()
        (proj / "pyproject.toml").write_text('[project]\nname = "proj"\nversion = "0"\n'
                                             'requires-python = ">=3.13"\ndependencies = ["mmw-a"]\n')
        stack.sh("cd /state/proj && /opt/uv/uv lock -q")
        lock = (proj / "uv.lock").read_text()
        assert "https://files.pythonhosted.org/packages/" in lock and 'name = "mmw-b"' in lock
        assert f"sha256:{hashes[b1[0]]}" in lock          # upstream hashes, as PyPI publishes them
        stack.locked_sync()
        lockfile, requirements = lock, reqs

    # (e) the far side renders its own pages from the bundle's shards
    bundled = bundle_network(net, cfg, cfg.settings.mirror_dir)
    assert not bundled.errors, bundled.errors
    far = scratch / "applied"
    far.mkdir()
    assert not (far / "pypi.org").exists()
    _run([sys.executable, bundled.out_path.parent / "apply.py", bundled.out_path, "--root", far],
         scratch / "apply.log")
    assert (far / "pypi.org" / "simple" / "mmw-a" / "index.html").is_file()
    with PypiStack(scratch, far, ca, net.name) as stack:
        stack.probe()
        stack.installs()
        # The real airgap flow: a lock and a hashed requirements file made on
        # the connected side, used unchanged against the far side's mirror.
        (stack.state / "reqs.txt").write_text(requirements)
        (stack.state / "proj").mkdir()
        (stack.state / "proj" / "pyproject.toml").write_text((proj / "pyproject.toml").read_text())
        (stack.state / "proj" / "uv.lock").write_text(lockfile)
        stack.hashed_install()
        stack.locked_sync()
