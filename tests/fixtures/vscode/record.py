"""Record the real upstream answers the VS Code tests replay. Run by hand,
once, with network access — never from the suite:

    uv run python tests/fixtures/vscode/record.py

Queries are the ones vscodeoffline (9da2010) makes: latest-only (flags 914),
Target + ExcludeWithFlags criteria, its headers. Responses are saved decoded,
pretty-printed, so a re-record diffs readably.
"""
import json
from pathlib import Path

import httpx

HERE = Path(__file__).parent
GALLERY = "https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery"
UPDATE = "https://update.code.visualstudio.com/api/update/{id}/{quality}/{commit}"
OLD_COMMIT = "7c4205b5c6e52a53b81c69d2b2dc8a627abaa0ba"
HEADERS = {
    "content-type": "application/json",
    "accept": "application/json;api-version=3.0-preview.1",
    "user-agent": "VSCode 1.95.0",
    "x-market-client-id": "VSCode 1.95.0",
}


def query(criteria, *, page_size, sort_by=0, sort_order=0, flags=914) -> dict:
    return {"filters": [{"criteria": [{"filterType": 8, "value": "Microsoft.VisualStudio.Code"},
                                      {"filterType": 12, "value": "4096"}, *criteria],
                         "pageNumber": 1, "pageSize": page_size,
                         "sortBy": sort_by, "sortOrder": sort_order}],
            "assetTypes": [], "flags": flags}


def names(*ids):
    return [{"filterType": 7, "value": i} for i in ids]


def save(name: str, data) -> None:
    (HERE / name).write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    print("wrote", name)


def main() -> None:
    with httpx.Client(timeout=60, follow_redirects=True) as c:
        post = lambda body: c.post(GALLERY, json=body, headers=HEADERS).raise_for_status().json()  # noqa: E731
        # No SearchText criterion at all — an empty one matches nothing
        # (vscodeoffline's _query_filter only adds it when non-empty).
        save("top3.json", post(query([], page_size=3, sort_by=4, sort_order=2)))
        save("names-python.json", post(query(names("ms-python.python"), page_size=1)))
        # Every version (flags 403), trimmed to the newest release and the
        # prereleases above it: a latest-only answer can hold no release at
        # all (ms-python.python ships prereleases continuously), which is the
        # case the provider re-queries for.
        full = post(query(names("ms-python.python"), page_size=1, flags=403))
        ext = full["results"][0]["extensions"][0]
        pre = "Microsoft.VisualStudio.Code.PreRelease"
        is_pre = lambda v: any(p["key"] == pre and p["value"] == "true"  # noqa: E731
                               for p in v.get("properties", []))
        release = next(v["version"] for v in ext["versions"] if not is_pre(v))
        cut = max(i for i, v in enumerate(ext["versions"]) if v["version"] == release)
        ext["versions"] = ext["versions"][:cut + 1]
        save("names-python-all.json", full)
        py = json.loads((HERE / "names-python.json").read_text())
        deps = set()
        for v in py["results"][0]["extensions"][0]["versions"]:
            for p in v.get("properties", []):
                if p["key"] in ("Microsoft.VisualStudio.Code.ExtensionDependencies",
                                "Microsoft.VisualStudio.Code.ExtensionPack"):
                    deps |= {d.strip() for d in p["value"].split(",") if d.strip()}
        save("names-deps.json", post(query(names(*sorted(deps)), page_size=max(1, len(deps)))))
        save("names-missing.json", post(query(names("nobody.no-such-extension"), page_size=1)))
        for ident in ("linux-x64", "server-linux-x64", "cli-alpine-x64"):
            r = c.get(UPDATE.format(id=ident, quality="stable", commit=OLD_COMMIT))
            r.raise_for_status()
            save(f"update-{ident}-stable.json", r.json())
        save("malicious.json",
             c.get("https://main.vscode-cdn.net/extensions/marketplace.json").raise_for_status().json())


if __name__ == "__main__":
    main()
