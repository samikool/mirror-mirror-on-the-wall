"""Replay recorded VS Code upstream answers through httpx.MockTransport."""
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx

FIX = Path(__file__).parent / "fixtures" / "vscode"


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def _names_in(body: dict) -> list[str]:
    return [c["value"].lower() for c in body["filters"][0]["criteria"] if c["filterType"] == 7]


def fake_asset_bytes(url: str) -> bytes:
    """Deterministic stand-in for a VSIX / asset / build: its own URL."""
    return f"bytes of {url}".encode()


class FakeUpstream:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.update_overrides: dict[str, dict | int] = {}
        self.status_queue: list[int] = []
        catalog = {}
        for f in ("top3.json", "names-python.json", "names-deps.json"):
            for ext in load(f)["results"][0]["extensions"]:
                ident = f"{ext['publisher']['publisherName']}.{ext['extensionName']}".lower()
                catalog[ident] = ext
        self.catalog = catalog
        # What a flags-403 (every version) query answers, where recorded.
        self.all_versions = {}
        for ext in load("names-python-all.json")["results"][0]["extensions"]:
            ident = f"{ext['publisher']['publisherName']}.{ext['extensionName']}".lower()
            self.all_versions[ident] = ext

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status_queue:
            return httpx.Response(self.status_queue.pop(0))
        url = urlsplit(str(request.url))
        if url.path.endswith("/extensionquery"):
            body = json.loads(request.content)
            wanted = _names_in(body)
            if wanted:
                pool = {**self.catalog, **self.all_versions} if body.get("flags") == 403 \
                    else self.catalog
                exts = [pool[n] for n in wanted if n in pool]
            else:
                size = body["filters"][0]["pageSize"]
                exts = load("top3.json")["results"][0]["extensions"][:size]
            total = len(exts)
            return httpx.Response(200, json={"results": [{
                "extensions": exts,
                "resultMetadata": [{"metadataType": "ResultCount",
                                    "metadataItems": [{"name": "TotalCount", "count": total}]}]}]})
        if url.netloc == "update.code.visualstudio.com" and url.path.startswith("/api/update/"):
            ident, quality = url.path.split("/")[3:5]
            override = self.update_overrides.get(ident)
            if isinstance(override, int):
                return httpx.Response(override)
            if override is not None:
                return httpx.Response(200, json=override)
            fixture = FIX / f"update-{ident}-{quality}.json"
            if not fixture.exists():
                return httpx.Response(404)
            data = json.loads(fixture.read_text())
            # The recorded hash is of the real build; this fake serves a
            # stand-in, so answer with the stand-in's hash — as upstream would
            # for the bytes it serves.
            data["sha256hash"] = hashlib.sha256(fake_asset_bytes(data["url"])).hexdigest()
            return httpx.Response(200, json=data)
        if url.netloc == "main.vscode-cdn.net":
            return httpx.Response(200, json=load("malicious.json"))
        return httpx.Response(200, content=fake_asset_bytes(str(request.url)))

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self), follow_redirects=True)
