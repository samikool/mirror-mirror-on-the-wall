"""A FakePyPI on a real local HTTP port, for the real uv binary.

`{base}/simple/<name>/` serves the fake's PEP 691 pages with every
files.pythonhosted.org URL rewritten to `{base}/files/…`, which serves the
bodies and core metadata. Nothing leaves 127.0.0.1.
"""
import contextlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

SIMPLE_JSON = "application/vnd.pypi.simple.v1+json"


@contextlib.contextmanager
def serve(fake):
    holder = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            base = holder["base"]
            if self.path.startswith("/files/"):
                url = "https://files.pythonhosted.org" + self.path[len("/files"):]
            else:
                url = "https://pypi.org" + self.path
            resp = fake(httpx.Request("GET", url, headers={"Accept": self.headers.get("Accept", "*/*")}))
            body = resp.content
            ctype = resp.headers.get("content-type", "application/octet-stream")
            if self.path.startswith("/simple/") and resp.status_code == 200:
                body = body.replace(b"https://files.pythonhosted.org", f"{base}/files".encode())
                ctype = SIMPLE_JSON
            self.send_response(resp.status_code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    holder["base"] = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield holder["base"]
    finally:
        server.shutdown()
        server.server_close()
