"""A published PyPI tree is a working index: the real uv resolves from it
using only the pages and PEP 658 metadata — through JSON and through HTML."""
import contextlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mmw.core.pool import Pool
from mmw.core.publisher.results import NetworkPublishResult
from mmw.providers import get_provider
from mmw.providers.pypi import uv as uvmod
from mmw.providers.pypi.targets import Floors, Target
from test_pypi_publish import _bump_pysocks, _cfg, _publish, _src, _sync

JSON = "application/vnd.pypi.simple.v1+json"


@contextlib.contextmanager
def serve_tree(root, *, html_only=False):
    """What the generated nginx does, for a test: negotiate /simple/ pages,
    serve files; page URLs are pointed at this server's /files/."""
    seen, holder = [], {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append(self.path)
            if self.path.startswith("/files/"):
                target, ctype = root / "files.pythonhosted.org" / self.path[len("/files/"):], "application/octet-stream"
            else:
                wants_json = JSON in self.headers.get("Accept", "") and not html_only
                target = root / "pypi.org" / self.path.lstrip("/")
                if target.is_dir():
                    target = target / ("index.json" if wants_json else "index.html")
                ctype = JSON if target.suffix == ".json" else "text/html"
            if not target.is_file():
                self.send_response(404)
                self.end_headers()
                return
            body = target.read_bytes().replace(b"https://files.pythonhosted.org", f"{holder['base']}/files".encode())
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    holder["base"] = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield holder["base"], seen
    finally:
        server.shutdown()
        server.server_close()


def _resolve_from(root, tmp_path, **kw):
    with serve_tree(root, **kw) as (base, seen):
        got = uvmod.compile_for(["requests[socks]"], Target("3.12", "linux-glibc", "amd64"), Floors(),
                                index_url=f"{base}/simple", cache_dir=tmp_path / "uvc")
    return got, seen


def test_uv_resolves_from_the_published_tree(tmp_path, up):
    cfg = _cfg(tmp_path, _src())
    _sync(cfg)
    got, seen = _resolve_from(_publish(cfg), tmp_path)
    assert got.error is None, got.error
    assert {"requests", "urllib3", "pysocks"} <= {name for name, _ in got.pins}
    assert any(p.endswith(".whl.metadata") for p in seen)
    assert not any(p.endswith(".whl") for p in seen)          # metadata only, no wheel downloads


def test_html_only_clients_resolve_too(tmp_path, up):
    cfg = _cfg(tmp_path, _src())
    _sync(cfg)
    root = _publish(cfg)
    json_pins, _ = _resolve_from(root, tmp_path)
    html_pins, _ = _resolve_from(root, tmp_path / "h", html_only=True)
    assert html_pins.error is None and sorted(html_pins.pins) == sorted(json_pins.pins)


def test_dry_run_reports_retained_files(tmp_path, up):
    cfg = _cfg(tmp_path, _src(), retention="forever")
    _sync(cfg)
    _bump_pysocks(up)
    _sync(cfg)
    result = NetworkPublishResult(network_name="lab", dry_run=True)
    with Pool(cfg.settings.mirror_dir, read_only=True) as pool:
        get_provider("pypi").preview(cfg.networks[0], cfg, pool, cfg.settings.mirror_dir, result)
    assert any(w.startswith("pypi: ") and "retention (forever)" in w for w in result.warnings), result.warnings
