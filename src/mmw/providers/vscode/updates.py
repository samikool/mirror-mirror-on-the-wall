"""The VS Code update API: the latest build per identity, and which server
commits to keep.

`GET /api/update/{identity}/{quality}/{commit}` answers 204 when `commit` is
current and otherwise the latest build's JSON (url, version = commit,
sha256hash, timestamp, …). Asking with a long-gone commit always yields the
latest — vscodeoffline's trick.
"""
import json
import random
import time

import httpx

UPDATE_URL = "https://update.code.visualstudio.com/api/update/{ident}/{quality}/{commit}"
OLD_COMMIT = "7c4205b5c6e52a53b81c69d2b2dc8a627abaa0ba"
RETAIN_PREVIOUS = 2
ATTEMPTS = 6
BACKOFF = 1.0                      # seconds, doubled per retry; tests set 0
_REQUIRED = ("url", "version", "sha256hash")


def meta_path(ident: str, quality: str, commit: str) -> str:
    return f".vscode-meta/update/{ident}/{quality}/{commit}.json"


def latest(client, ident: str, quality: str, *, on_wait=None) -> dict | None:
    """The latest build's JSON, or None when the API serves no such build.

    429, 5xx and transport failures are retried with jittered backoff (each
    wait reported through `on_wait`); persistent failure raises RuntimeError."""
    url = UPDATE_URL.format(ident=ident, quality=quality, commit=OLD_COMMIT)
    delay = BACKOFF
    last = ""
    for attempt in range(ATTEMPTS):
        try:
            resp = client.get(url)
        except httpx.HTTPError as e:
            last = f"{type(e).__name__}: {e}"
        else:
            if resp.status_code in (204, 404):
                return None
            if resp.status_code == 200:
                break
            last = f"HTTP {resp.status_code}"
            if resp.status_code != 429 and resp.status_code < 500:
                raise RuntimeError(f"update API answered {last} for {ident}/{quality}")
        if attempt < ATTEMPTS - 1:
            wait = delay * (1 + random.random() / 2)
            if on_wait:
                on_wait(f"update API: {last} for {ident} — retrying in {wait:.0f}s")
            time.sleep(wait)
            delay *= 2
    else:
        raise RuntimeError(f"update API still failing ({last}) for {ident}/{quality} "
                           f"after {ATTEMPTS} attempts")
    data = resp.json()
    missing = [k for k in _REQUIRED if not data.get(k)]
    if missing:
        raise ValueError(f"update API answer for {ident}/{quality} lacks {', '.join(missing)}")
    return data


def retained(current: dict, history, ident: str, quality: str) -> list[dict]:
    """`current` first, then up to RETAIN_PREVIOUS earlier commits this unit
    already holds, newest first."""
    keep = [current]
    previous = []
    for path in history.paths(f".vscode-meta/update/{ident}/{quality}/"):
        raw = history.read(path)
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = None
        if data and data.get("version") != current["version"] and \
                all(data.get(k) for k in _REQUIRED):
            previous.append(data)
    previous.sort(key=lambda d: d.get("timestamp") or 0, reverse=True)
    return keep + previous[:RETAIN_PREVIOUS]
