"""The Microsoft Marketplace gallery API, as vscodeoffline (9da2010) speaks it.

Undocumented upstream, so every request mirrors the reference: the same
criteria (Target 8 + ExcludeWithFlags 12), flags (914 latest-only, 403 when
every version is needed), headers and paging by ResultCount.
"""
import random
import time
import uuid

import httpx

GALLERY_URL = "https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery"
CLIENT_VERSION = "1.95.0"          # what the requests say they are, as vscodeoffline does
FLAGS_LATEST = 914                 # Files|VersionProperties|AssetUri|Statistics|LatestVersionOnly
FLAGS_ALL_VERSIONS = 403           # Versions|Files|VersionProperties|AssetUri|Statistics
PRERELEASE_KEY = "Microsoft.VisualStudio.Code.PreRelease"
DEPENDENCIES_KEY = "Microsoft.VisualStudio.Code.ExtensionDependencies"
PACK_KEY = "Microsoft.VisualStudio.Code.ExtensionPack"
BATCH = 50
BATCH_ALL_VERSIONS = 10
PAGE = 500
ATTEMPTS = 6
RATE_LIMIT_WAIT = 60.0             # gallery throttles can outlast exponential backoff
BACKOFF = 1.0                      # seconds, doubled per retry; tests set 0

_BASE_CRITERIA = [{"filterType": 8, "value": "Microsoft.VisualStudio.Code"},
                  {"filterType": 12, "value": "4096"}]


class MarketplaceError(RuntimeError):
    """The gallery could not answer — after retries, or with a malformed body."""


def _headers() -> dict:
    return {
        "content-type": "application/json",
        "accept": "application/json;api-version=3.0-preview.1",
        "user-agent": f"VSCode {CLIENT_VERSION}",
        "x-market-client-id": f"VSCode {CLIENT_VERSION}",
        "x-market-user-id": str(uuid.uuid4()),
    }


def ext_id(ext: dict) -> str:
    return f"{ext['publisher']['publisherName']}.{ext['extensionName']}".lower()


def query(client, criteria: list[dict], *, page_size: int, page: int = 1,
          sort_by: int = 0, sort_order: int = 0, flags: int = FLAGS_LATEST,
          on_wait=None) -> tuple[list[dict], int]:
    """One page of one query → (extensions, total).

    Retries transient 403, 429, 5xx and transport failures with jittered
    exponential backoff. Rate limits honor Retry-After (up to five minutes),
    or wait one minute without it. Reports each wait through `on_wait`;
    anything else, or exhausting the retries, raises
    `MarketplaceError` — never a raw httpx exception."""
    body = {"filters": [{"criteria": [*_BASE_CRITERIA, *criteria], "pageNumber": page,
                         "pageSize": page_size, "sortBy": sort_by, "sortOrder": sort_order}],
            "assetTypes": [], "flags": flags}
    delay = BACKOFF
    last = ""
    for attempt in range(ATTEMPTS):
        retry_after = 0.0
        try:
            resp = client.post(GALLERY_URL, json=body, headers=_headers())
        except httpx.HTTPError as e:
            last = f"{type(e).__name__}: {e}"
        else:
            if resp.status_code == 200:
                break
            last = f"HTTP {resp.status_code}"
            if resp.status_code == 429:
                try:
                    retry_after = min(300.0, max(0.0, float(resp.headers.get("retry-after", str(RATE_LIMIT_WAIT)))))
                except ValueError:
                    retry_after = RATE_LIMIT_WAIT
            # The public gallery can intermittently reject an otherwise valid
            # anonymous query with 403. Retry within the same bounded budget;
            # a persistent refusal still fails clearly.
            if resp.status_code not in (403, 429) and resp.status_code < 500:
                raise MarketplaceError(f"gallery answered {last}")
        if attempt < ATTEMPTS - 1:
            wait = max(retry_after, delay * (1 + random.random() / 2))
            if on_wait:
                on_wait(f"marketplace: {last} — retrying in {wait:.0f}s")
            time.sleep(wait)
            delay *= 2
    else:
        raise MarketplaceError(f"gallery still failing ({last}) after {ATTEMPTS} attempts")
    try:
        result = resp.json()["results"][0]
        exts = list(result["extensions"])
        total = next((m["metadataItems"][0]["count"] for m in result.get("resultMetadata") or []
                      if m.get("metadataType") == "ResultCount"), len(exts))
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise MarketplaceError(f"gallery answer had an unexpected shape: {e}") from e
    return exts, int(total)


def _paged(client, criteria, *, limit: int | None, on_wait=None, **kw) -> list[dict]:
    """Every page of a query, in order. The page size never changes between
    pages: the gallery's offset is (pageNumber − 1) × pageSize, so shrinking
    it re-reads earlier rows. The result is trimmed to `limit`."""
    size = PAGE if limit is None else min(PAGE, limit)
    out: dict[str, dict] = {}
    page = 1
    while True:
        exts, total = query(client, criteria, page_size=size, page=page, on_wait=on_wait, **kw)
        for e in exts:
            out.setdefault(ext_id(e), e)
        if on_wait:
            on_wait(f"marketplace: listed {len(out):,} of {total:,} extensions (page {page})")
        if not exts or page * size >= total or (limit is not None and len(out) >= limit):
            break
        page += 1
    found = list(out.values())
    return found if limit is None else found[:limit]


def top_n(client, n: int, *, on_wait=None) -> list[dict]:
    """The n most-installed extensions."""
    # No SearchText criterion: an empty one matches nothing (vscodeoffline
    # adds it only when non-empty).
    return _paged(client, [], limit=n, sort_by=4, sort_order=2, on_wait=on_wait)


def search_all(client, *, on_wait=None) -> list[dict]:
    """Every extension the gallery lists."""
    # Install counts change during enumeration and ties can reorder pages,
    # skipping entries. Oldest published first keeps new entries at the end.
    # VS Code gallery manifest: PublishedDate=10, Ascending=1.
    return _paged(client, [], limit=None, sort_by=10, sort_order=1, on_wait=on_wait)


def by_names(client, ids: list[str], *, flags: int = FLAGS_LATEST,
             on_wait=None) -> dict[str, dict]:
    """`publisher.name` → extension, for those that exist (case-insensitive).
    Every-version queries carry whole histories, so they go in smaller batches."""
    found: dict[str, dict] = {}
    wanted = sorted({i.lower() for i in ids})
    step = BATCH_ALL_VERSIONS if flags == FLAGS_ALL_VERSIONS else BATCH
    for i in range(0, len(wanted), step):
        batch = wanted[i:i + step]
        exts, _ = query(client, [{"filterType": 7, "value": n} for n in batch],
                        page_size=len(batch), flags=flags, on_wait=on_wait)
        for e in exts:
            found[ext_id(e)] = e
        if on_wait:
            on_wait(f"marketplace: resolved {min(i + step, len(wanted)):,} of {len(wanted):,} extension IDs")
    return found


def _props(version: dict) -> dict[str, str]:
    return {p.get("key"): p.get("value", "") for p in version.get("properties") or []}


def is_prerelease(version: dict) -> bool:
    return _props(version).get(PRERELEASE_KEY) == "true"


def pick_versions(ext: dict, platform: str, *, prerelease: bool) -> list[dict]:
    """The version entries a `platform` client should get: the newest version
    (newest first, as the gallery lists them) that is not a prerelease unless
    asked, with its build for `platform` — or its universal build."""
    candidates = [v for v in ext.get("versions", []) if prerelease or not is_prerelease(v)]
    for v in candidates:
        target = v.get("targetPlatform")
        if target in (None, "universal", platform):
            version = v["version"]
            same = [c for c in candidates if c["version"] == version]
            exact = [c for c in same if c.get("targetPlatform") == platform]
            if exact:
                return exact
            return [c for c in same if c.get("targetPlatform") in (None, "universal")]
    return []


def dependencies(ext: dict, *, prerelease: bool) -> set[str]:
    """`extensionDependencies` + `extensionPack` of the release that would be
    mirrored (any platform's build of it declares the same)."""
    for v in ext.get("versions", []):
        if prerelease or not is_prerelease(v):
            props = _props(v)
            ids = ",".join(props.get(k, "") for k in (DEPENDENCIES_KEY, PACK_KEY))
            return {i.strip().lower() for i in ids.split(",") if i.strip()}
    return set()
