#!/usr/bin/env python3
"""mmw's offline VS Code Marketplace — the dynamic half of a published network.

Run by the stock python image (docker compose, see compose.yaml beside it);
nginx proxies to it the paths a static tree cannot answer:

  POST /_apis/public/gallery/extensionquery          extension search
  OPTIONS (any path)                                 the workbench's CORS preflight
  GET  /api/update/{identity}/{quality}/{commit}     "is there a newer VS Code?"
  GET  /commit:{sha}/{identity}/{quality}            Remote-SSH server download
  GET  /latest/{identity}/{quality}                  newest build (a direct link)
  GET  /sha/download?build={quality}&os={identity}   the same, code.visualstudio.com's form
  GET  /  /Download  on code.visualstudio.com         download page: every newest build —
                                                     where Linux's "Download Update" goes
  GET  /  on marketplace.visualstudio.com            every extension, linked to its VSIX
  GET  /_apis/public/gallery/publisher/…/assetbyname/{type}   fallback asset URL
  GET  /healthz

Everything else — VSIX, installers, server builds — is a static file nginx
serves from the published tree at Microsoft's own host/path. Answers carry
Microsoft's own JSON: nothing is rewritten. Query handling follows
vscodeoffline (9da2010), which the clients this serves are known to accept.

Stdlib only. Reads {data}/catalog.json and {data}/updates.json, re-reading a
file when its mtime changes (a new publish or bundle replaces them).
"""
import argparse
import gzip
import html
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

FILTER_TAG, FILTER_ID, FILTER_CATEGORY, FILTER_NAME = 1, 4, 5, 7
FILTER_FEATURED, FILTER_SEARCH = 9, 10
SORT_UPDATED, SORT_TITLE, SORT_PUBLISHER, SORT_INSTALLS = 1, 2, 3, 4
SORT_PUBLISHED, SORT_RATING, SORT_WEIGHTED = 5, 6, 12
ORDER_ASC, ORDER_DESC = 1, 2

QUERY_PATH = "/_apis/public/gallery/extensionquery"
_UPDATE = re.compile(r"^/api/update/([^/]+)/([^/]+)/([0-9a-fA-F]+)$")
_COMMIT = re.compile(r"^/commit:([0-9a-fA-F]{40})/([^/]+)/([^/]+)$")
_LATEST = re.compile(r"^/latest/([^/]+)/([^/]+)$")
DOWNLOAD_HOST = "code.visualstudio.com"
DOWNLOAD_PAGES = ("/", "/Download", "/download")
LIST_HOST = "marketplace.visualstudio.com"
VSIX = "Microsoft.VisualStudio.Services.VSIXPackage"
_ASSET = re.compile(r"^/_apis/public/gallery/publisher/([^/]+)/extension/([^/]+)/"
                    r"([^/]+)/assetbyname/([^/]+)$")


def log(msg: str) -> None:
    sys.stderr.write(f"gallery: {msg}\n")


def ident(ext: dict) -> str:
    return f"{ext['publisher']['publisherName']}.{ext['extensionName']}".lower()


def _stat(ext: dict, name: str) -> float:
    for s in ext.get("statistics") or []:
        if s.get("statisticName") == name:
            return float(s.get("value") or 0)
    return 0.0


class Store:
    """The catalog and update index, reloaded when their files change. A file
    that fails to parse keeps the last good copy in service."""

    FILES = ("catalog.json", "updates.json")

    def __init__(self, data_dir: str):
        self.dir = data_dir
        self.extensions: list = []
        self.updates: dict = {}
        self.healthy = False
        self._mtimes: dict = {}
        self._lock = threading.Lock()
        self.refresh()

    def _mtime(self, name):
        try:
            return os.path.getmtime(os.path.join(self.dir, name))
        except OSError:
            return None

    def refresh(self) -> None:
        with self._lock:
            ok = True
            for name in self.FILES:
                m = self._mtime(name)
                if name in self._mtimes and self._mtimes[name] == m:
                    continue
                default = [] if name == "catalog.json" else {}
                try:
                    if m is None:
                        data = default
                    else:
                        with open(os.path.join(self.dir, name), encoding="utf-8") as f:
                            data = json.load(f)
                except (OSError, ValueError) as e:
                    log(f"cannot read {name} ({e}); keeping the previous copy")
                    ok = ok and name in self._mtimes
                    continue
                if name == "catalog.json":
                    self.extensions = data
                else:
                    self.updates = data
                self._mtimes[name] = m
            # A missing file loads as empty, but is not healthy: a wrong mount
            # would otherwise pass the healthcheck serving an empty Marketplace.
            self.healthy = ok and all(self._mtimes.get(n) is not None for n in self.FILES)


def query(extensions: list, body: dict) -> dict:
    """Answer an extensionquery body the way vscodeoffline does."""
    flt = body["filters"][0]
    criteria = flt.get("criteria") or []
    narrowed = False
    found: dict = {}
    for c in criteria:
        ft, val = c.get("filterType"), str(c.get("value", "")).lower()
        if ft == FILTER_ID:
            hits = [e for e in extensions if str(e.get("extensionId", "")).lower() == val]
        elif ft == FILTER_NAME:
            hits = [e for e in extensions if ident(e) == val]
        elif ft == FILTER_SEARCH:
            if not val:
                continue
            hits = [e for e in extensions
                    if val in ident(e) or val in str(e.get("displayName", "")).lower()
                    or val in str(e.get("shortDescription", "")).lower()]
        elif ft in (FILTER_TAG, FILTER_CATEGORY, FILTER_FEATURED):
            log(f"filter type {ft} is not supported; matching nothing")
            hits = []
        else:
            continue                        # product target, exclude-with-flags, unknown
        narrowed = True
        for e in hits:
            found.setdefault(ident(e), e)
    result = list(found.values()) if narrowed else list(extensions)

    sort_by = flt.get("sortBy") or SORT_INSTALLS
    order = flt.get("sortOrder") or 0
    keys = {
        SORT_INSTALLS: lambda e: _stat(e, "install"),
        SORT_RATING: lambda e: _stat(e, "averagerating"),
        SORT_WEIGHTED: lambda e: _stat(e, "weightedRating"),
        SORT_UPDATED: lambda e: str(e.get("lastUpdated", "")),
        SORT_PUBLISHED: lambda e: str(e.get("publishedDate", "")),
        SORT_PUBLISHER: lambda e: e["publisher"]["publisherName"].lower(),
        SORT_TITLE: lambda e: str(e.get("displayName") or e.get("extensionName")).lower(),
    }
    key = keys.get(sort_by, keys[SORT_INSTALLS])
    textual = sort_by in (SORT_TITLE, SORT_PUBLISHER)
    descending = order == ORDER_DESC or (order != ORDER_ASC and not textual)
    result.sort(key=key, reverse=descending)

    total = len(result)
    size = int(flt.get("pageSize") or total or 1)
    page = max(1, int(flt.get("pageNumber") or 1))
    page_items = result[(page - 1) * size: page * size]
    return {"results": [{"extensions": page_items, "pagingToken": None, "resultMetadata": [
        {"metadataType": "ResultCount", "metadataItems": [{"name": "TotalCount", "count": total}]}]}]}


def newest(updates: dict, identity: str, quality: str):
    """The newest build of `identity` in `quality`, or None."""
    entry = updates.get(identity, {}).get(quality) or {}
    return (entry.get("builds") or {}).get(entry.get("latest", ""))


def listing(title: str, entries) -> bytes:
    """A bare page in the style of nginx's autoindex: each (name, url) one
    line, the name a link to the file, A–Z."""
    lines = "".join(f'<a href="{html.escape(url)}">{html.escape(name)}</a>\n'
                    for name, url in sorted(entries, key=lambda e: e[0].lower()))
    t = html.escape(title)
    return (f"<html>\n<head><title>{t}</title></head>\n<body>\n<h1>{t}</h1><hr><pre>\n"
            f"{lines}</pre><hr></body>\n</html>\n").encode()


def download_page(updates: dict) -> bytes:
    """Every identity's newest build per quality, by file name."""
    entries = []
    for identity in updates:
        for quality in updates[identity]:
            url = (newest(updates, identity, quality) or {}).get("url")
            if url:
                entries.append((url.rsplit("/", 1)[-1], url))
    return listing("VS Code downloads", entries)


def extension_list(extensions: list) -> bytes:
    """Every extension's newest version, one line per platform build
    (`publisher.name@linux-x64`; a universal build is the bare name)."""
    entries = []
    for e in extensions:
        versions = e.get("versions") or []
        latest = versions[0].get("version") if versions else None
        for v in versions:
            if v.get("version") != latest:
                continue
            platform = v.get("targetPlatform")
            name = ident(e) + (f"@{platform}" if platform not in (None, "", "universal") else "")
            for f in v.get("files", []):
                if f.get("assetType") == VSIX:
                    entries.append((name, f.get("source", "")))
    return listing("VS Code extensions", entries)


def _asset(extensions: list, want: str, version: str, asset_type: str, platform: str):
    """The file `asset_type` of `want`@`version` for `platform`: that
    platform's build, else a universal one. With no platform asked for, a
    universal build first, then any. Never another platform's build when a
    platform was asked for — that VSIX would install and not work."""
    builds = [v for e in extensions if ident(e) == want
              for v in e.get("versions", []) if v.get("version") == version]

    def neutral(v):
        return v.get("targetPlatform") in (None, "", "universal")
    if platform:
        ranked = [v for v in builds if v.get("targetPlatform") == platform] + \
                 [v for v in builds if neutral(v)]
    else:
        ranked = [v for v in builds if neutral(v)] + [v for v in builds if not neutral(v)]
    for v in ranked:
        for f in v.get("files", []):
            if f.get("assetType") == asset_type:
                return f
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "mmw-gallery"

    def log_message(self, fmt, *args):
        log(f"{self.address_string()} {fmt % args}")

    def _send(self, status: int, body: bytes = b"", ctype="application/json", headers=None):
        self.send_response(status)
        # The workbench calls from its own origin (vscode-file://), as it does
        # Microsoft's marketplace; nothing here is per-user, so any origin.
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if body:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data) -> None:
        self._send(status, json.dumps(data).encode())

    @property
    def store(self) -> Store:
        return self.server.store

    def do_POST(self):
        self.store.refresh()
        if urlsplit(self.path).path != QUERY_PATH:
            return self._send(404)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0:
                raise ValueError("negative Content-Length")
            body = json.loads(self.rfile.read(length) or b"null")
            answer = query(self.store.extensions, body)
        except (ValueError, KeyError, IndexError, TypeError, AttributeError,
                OverflowError, RecursionError) as e:
            log(f"bad query ({e})")
            return self._send(400)
        self._json(200, answer)

    def _extension_list(self):
        # Built once per catalog (120k extensions is tens of MB of HTML) and
        # kept gzipped too: nginx does not compress what it proxies.
        exts = self.store.extensions
        cached = getattr(self.server, "front_page", None)
        if cached is None or cached[0] is not exts:
            page = extension_list(exts)
            cached = self.server.front_page = (exts, page, gzip.compress(page, 6))
        ctype = "text/html; charset=utf-8"
        if "gzip" in (self.headers.get("Accept-Encoding") or ""):
            return self._send(200, cached[2], ctype,
                              {"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
        self._send(200, cached[1], ctype, {"Vary": "Accept-Encoding"})

    def do_OPTIONS(self):
        self._send(204, headers={
            "Access-Control-Allow-Methods": "GET, HEAD, POST, OPTIONS",
            "Access-Control-Allow-Headers":
                self.headers.get("Access-Control-Request-Headers") or "content-type",
            "Access-Control-Max-Age": "86400"})

    def do_HEAD(self):
        self.do_GET()                       # `_send` leaves the body out

    def do_GET(self):
        self.store.refresh()
        url = urlsplit(self.path)
        path = url.path
        if path == "/healthz":
            return self._send(200 if self.store.healthy else 503)
        m = _UPDATE.match(path)
        if m:
            entry = self.store.updates.get(m.group(1), {}).get(m.group(2))
            if not entry:
                return self._send(404)
            if m.group(3).lower() == entry["latest"].lower():
                return self._send(204)
            return self._json(200, entry["builds"][entry["latest"]])
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if host == LIST_HOST and path == "/":
            return self._extension_list()
        if host == DOWNLOAD_HOST and path in DOWNLOAD_PAGES:
            return self._send(200, download_page(self.store.updates), "text/html; charset=utf-8")
        m = _LATEST.match(path)
        if m or path == "/sha/download":
            if m:
                identity, quality = m.group(1), m.group(2)
            else:
                q = parse_qs(url.query)
                identity, quality = (q.get("os") or [""])[0], (q.get("build") or ["stable"])[0]
            b = newest(self.store.updates, identity, quality)
            if not b:
                return self._send(404)
            return self._send(302, headers={"Location": b["url"]})
        m = _COMMIT.match(path)
        if m:
            entry = self.store.updates.get(m.group(2), {}).get(m.group(3)) or {}
            build = (entry.get("builds") or {}).get(m.group(1).lower())
            if not build:
                return self._send(404)
            return self._send(302, headers={"Location": build["url"]})
        m = _ASSET.match(path)
        if m:
            platform = (parse_qs(url.query).get("targetPlatform") or [""])[0]
            f = _asset(self.store.extensions, f"{m.group(1)}.{m.group(2)}".lower(),
                       m.group(3), m.group(4), platform)
            if f is None:
                return self._send(404)
            return self._send(302, headers={"Location": f["source"]})
        self._send(404)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # A client that hangs up mid-answer — VS Code cancelling a search as
        # the next keystroke sends another, a browser leaving the long list —
        # is routine, not a fault worth a traceback.
        if isinstance(sys.exc_info()[1], ConnectionError):
            log(f"{client_address[0]} hung up before the answer was sent")
            return
        super().handle_error(request, client_address)


def make_server(store: Store, bind: str, port: int) -> ThreadingHTTPServer:
    server = Server((bind, port), Handler)
    server.store = store
    return server


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--bind", default="0.0.0.0")
    args = ap.parse_args(argv)
    store = Store(args.data)
    log(f"serving {len(store.extensions)} extensions from {args.data} on {args.bind}:{args.port}")
    make_server(store, args.bind, args.port).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
