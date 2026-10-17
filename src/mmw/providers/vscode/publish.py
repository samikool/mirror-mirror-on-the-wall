"""What a VS Code network publishes besides the payloads themselves: the
gallery's data (catalog, update index), its script, and the DNS / TLS guide —
all under `vscode-gallery/` in the network root. The gallery container itself
is a service in the network's `nginx/compose.yaml`."""
import gzip
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

GALLERY_DIR = "vscode-gallery"
META_CACHE = "vscode-meta-cache"     # under mirror_dir: what is derived from the metadata
STATIC_HOSTS = ("vscode.download.prss.microsoft.com", "main.vscode-cdn.net")
# code.visualstudio.com: Linux's "Download Update" opens it, and its
# /sha/download links are how scripts fetch a build.
PROXIED_HOSTS = ("marketplace.visualstudio.com", "update.code.visualstudio.com",
                 "code.visualstudio.com")
WILDCARDS = ("*.gallerycdn.vsassets.io", "*.gallery.vsassets.io")
HOSTS = (*PROXIED_HOSTS, *STATIC_HOSTS, *WILDCARDS)
EXACT_HOSTS = tuple(h for h in HOSTS if not h.startswith("*."))
_NUMBERS = dict(enumerate(("no", "one", "two", "three", "four", "five", "six", "seven")))


def _read_json(pool, unit_name: str, rel: str):
    path = pool.resolve(unit_name, rel)
    try:
        return json.loads(path.read_bytes()) if path is not None else None
    except (OSError, ValueError):
        return None


def _version_key(v: dict):
    parts = re.findall(r"\d+", str(v.get("version", "")))
    return tuple(int(p) for p in parts)


def _extension_meta(pool, units) -> list[tuple[str, str, str, str]]:
    """(unit, rel, pool path, checksum) of every extension metadata JSON, in
    catalog order."""
    rows = []
    for unit in units:
        if unit.dims.get("kind") != "extensions":
            continue
        held = sorted((rel, path, checksum) for rel, path, _, _, checksum
                      in pool.mirror_files([unit.name], prefix=".vscode-meta/extensions/"))
        rows += [(unit.name, rel, path, checksum) for rel, path, checksum in held]
    return rows


def _catalog_from(rows, bodies) -> list[dict]:
    merged: dict[str, dict] = {}
    for _, _, path, _ in rows:
        try:
            obj = json.loads(bodies[path])
        except (TypeError, ValueError):     # an OSError in place of bytes, or bad JSON
            continue
        if not obj:
            continue
        key = f"{obj['publisher']['publisherName']}.{obj['extensionName']}".lower()
        cur = merged.setdefault(key, {**obj, "versions": []})
        seen = {(v.get("version"), v.get("targetPlatform")) for v in cur["versions"]}
        for v in obj.get("versions", []):
            if (v.get("version"), v.get("targetPlatform")) not in seen:
                cur["versions"].append(v)
    for obj in merged.values():
        obj["versions"].sort(key=_version_key, reverse=True)
    return [merged[k] for k in sorted(merged)]


def catalog(pool, units) -> list[dict]:
    """One gallery object per extension: the universal unit's builds plus every
    selected platform's, newest first, each (version, platform) once."""
    from mmw.core.bulk_read import read_all
    rows = _extension_meta(pool, units)
    return _catalog_from(rows, read_all(path for _, _, path, _ in rows))


# A catalog is a function of its metadata's checksums and of this file's code,
# so one built before is reused while neither changed — a mirror of 120k
# extensions otherwise reads every JSON again on each publish.
_CODE = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
CATALOGS_KEPT = 2           # networks selecting different extensions alternate
DESCRIPTIONS = "descriptions.gz"    # checksum → that metadata JSON's bytes


def _load_descriptions(path: Path) -> dict[str, bytes]:
    """The remembered bodies. A body is keyed by its own checksum, so a stale
    one cannot exist; anything unreadable is simply read from the pool again."""
    found: dict[str, bytes] = {}
    try:
        with gzip.open(path, "rb") as f:
            while header := f.readline():
                checksum, size = header.split()
                body = f.read(int(size))
                if len(body) != int(size):
                    break                   # truncated: keep what was whole
                found[checksum.decode()] = body
    except (OSError, ValueError, EOFError):
        pass
    return found


def _save_descriptions(path: Path, bodies: dict[str, bytes]) -> None:
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wb", compresslevel=1) as f:
        for checksum, body in bodies.items():
            f.write(b"%s %d\n" % (checksum.encode(), len(body)))
            f.write(body)
    tmp.replace(path)


def catalog_files(pool, units, cache_dir: Path, *, full: bool = False,
                  say=None) -> tuple[bytes, list[str]]:
    """catalog.json's bytes and the publisher hosts it names — rebuilt only
    when the metadata changed (or `full`), else the copy in `cache_dir`."""
    from mmw.core.bulk_read import read_all
    if say:
        say("checking the extension catalog…")
    rows = _extension_meta(pool, units)
    digest = hashlib.sha256(_CODE.encode())
    for unit_name, rel, _, checksum in rows:
        digest.update(f"{unit_name}\0{rel}\0{checksum}\n".encode())
    key = digest.hexdigest()[:32]
    data_path, hosts_path = cache_dir / f"catalog-{key}.json", cache_dir / f"catalog-{key}.hosts"
    if not full and data_path.exists() and hosts_path.exists():
        if say:
            say(f"extension catalog unchanged ({len(rows):,} descriptions) — reusing it…")
        try:
            return data_path.read_bytes(), hosts_path.read_text().split()
        except OSError:
            pass
    # A sync that updates one extension changes the key above, so the bodies
    # are remembered too, by checksum: only the descriptions not seen by an
    # earlier publish are read from the pool.
    known = {} if full else _load_descriptions(cache_dir / DESCRIPTIONS)
    unread = [path for _, _, path, checksum in rows if checksum not in known]
    if say:
        say(f"reading {len(unread):,} of {len(rows):,} extension descriptions…")
    bodies = read_all(unread)
    by_checksum = {}
    for _, _, path, checksum in rows:
        body = known.get(checksum, bodies.get(path))
        if isinstance(body, bytes):
            by_checksum[checksum] = body
    catalogue = _catalog_from(rows, {path: by_checksum.get(checksum)
                                     for _, _, path, checksum in rows})
    data, hosts = json.dumps(catalogue, sort_keys=True).encode(), publisher_hosts(catalogue)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        if unread or len(by_checksum) != len(known):
            _save_descriptions(cache_dir / DESCRIPTIONS, by_checksum)
        for path, body in ((data_path, data), (hosts_path, "\n".join(hosts).encode())):
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(body)
            tmp.replace(path)
        kept = sorted(cache_dir.glob("catalog-*.json"), key=lambda p: p.stat().st_mtime,
                      reverse=True)
        for old in kept[CATALOGS_KEPT:]:
            old.unlink(missing_ok=True)
            old.with_suffix(".hosts").unlink(missing_ok=True)
    except OSError:
        pass                    # the cache is an optimisation; the catalog stands
    return data, hosts


def updates(pool, units) -> dict:
    """{identity: {quality: {"latest": commit, "builds": {commit: json}}}}."""
    out: dict = {}
    for unit in units:
        if unit.dims.get("kind") not in ("desktop", "server"):
            continue
        for rel in sorted(pool.ref_map(unit.name)):
            parts = rel.split("/")
            if len(parts) != 5 or parts[:2] != [".vscode-meta", "update"]:
                continue
            build = _read_json(pool, unit.name, rel)
            if not build:
                continue
            ident, quality = parts[2], parts[3]
            entry = out.setdefault(ident, {}).setdefault(quality, {"latest": "", "builds": {}})
            entry["builds"][build["version"]] = build
    for qualities in out.values():
        for entry in qualities.values():
            entry["latest"] = max(entry["builds"].values(),
                                  key=lambda b: b.get("timestamp") or 0)["version"]
    return out


def _check(updates: dict) -> str:
    """A check that works for what this network serves: the update check
    for a build it holds, else an extension search."""
    for ident in sorted(updates, key=lambda i: (i.startswith("cli-"), i)):   # a CLI build last
        for quality in sorted(updates[ident]):
            return f"""\
```
curl -s -o /dev/null -w '%{{http_code}}\\n' \\
  https://update.code.visualstudio.com/api/update/{ident}/{quality}/{"0" * 40}
```

prints `200` (an update is available) — not `000` (DNS or certificate), not
`502` (the container is not running)."""
    return """\
```
curl -s -o /dev/null -w '%{http_code}\\n' -H 'Content-Type: application/json' \\
  -d '{"filters":[{"criteria":[],"pageSize":1}],"flags":0}' \\
  https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery
```

prints `200` — not `000` (DNS or certificate), not `502` (the container is
not running)."""


def publisher_hosts(catalogue) -> list[str]:
    """Every per-publisher gallery host the catalogue's files are served from."""
    # A URL's host is fixed by what comes before its third "/", and a
    # catalogue's million URLs share a few hundred thousand of those: each is
    # parsed once.
    origins = set()
    for extension in catalogue:
        for version in extension.get('versions', []):
            urls = [version.get('assetUri', ''), version.get('fallbackAssetUri', ''),
                    *(file.get('source', '') for file in version.get('files', []))]
            origins.update('/'.join(url.split('/', 3)[:3]) for url in urls)
    hosts = set()
    for origin in origins:
        host = urlsplit(origin).hostname or ''
        if re.fullmatch(r'[a-z0-9-]+\.(?:gallerycdn|gallery)\.vsassets\.io', host):
            hosts.add(host)
    return sorted(hosts)


def _publisher_dns(hosts) -> str:
    if not hosts:
        return ''
    publishers = {}
    for host in sorted(hosts):
        publishers.setdefault(host.split('.', 1)[0], []).append(host)
    lines = ['MIRROR_IP ' + ' '.join(names) for names in publishers.values()]
    return '\n'.join([
        'For a site without wildcard DNS, these hosts-file entries cover the',
        'publishers in this network (also add the '
        f'{_NUMBERS[len(EXACT_HOSTS)]} exact hostnames above):',
        '', '```', *lines, '```', '',
        'Regenerate these entries when the selection gains a publisher.', ''])


def _start(network, tar_name: str) -> str:
    if not network.nginx:
        return f"""\
## 3. Start the gallery

This network has `nginx = false`, so no `nginx/compose.yaml` was generated
and nothing here starts the gallery. Turn `nginx` on for this network, or run
`gallery.py` (stock `python:3.13-slim`, image in `{tar_name}`) behind your own
nginx: proxy `update.code.visualstudio.com`, `*.gallery.vsassets.io` and
marketplace's `/_apis/public/gallery/extensionquery` to it, and serve the
other hostnames statically from this tree.
"""
    return f"""\
## 3. Start nginx and the gallery

The gallery is a service in this network's `nginx/compose.yaml`, beside
nginx. Follow `nginx/README.md`: it loads both images (this one is
`vscode-gallery/{tar_name}`) and starts the stack. A publish or bundle swaps
the catalog and the gallery notices without a restart.
"""


def vscode_md(network, tar_name: str, updates: dict, *, catalogue=(), hosts=None) -> bytes:
    """`hosts`: the catalogue's publisher hosts, when already known."""
    hosts = publisher_hosts(catalogue) if hosts is None else hosts
    exact = EXACT_HOSTS
    return f"""\
# VS Code on network `{network.name}`

Generated by mmw on every publish and bundle.

## 1. DNS

Point these at this server (the two wildcards need a DNS server that supports
wildcard records — BIND, dnsmasq, Unbound, Windows DNS…; a hosts file cannot):

{chr(10).join(f"- `{h}`" for h in HOSTS)}

dnsmasq (`/etc/dnsmasq.d/vscode.conf`) — each line also covers subdomains:

```
{chr(10).join(f"address=/{h.removeprefix('*.')}/MIRROR_IP" for h in (*exact, *WILDCARDS))}
```

BIND: one zone per name above (`*` records for the two wildcard zones), each
with `@` and `*` pointing at the mirror.

{_publisher_dns(hosts)}
## 2. TLS

VS Code only talks HTTPS. Sign a certificate covering every name above
(`mmw certs` / `sign.py` include both wildcards) with a CA the clients trust
(no CA yet? `python3 make-ca.py`, beside `sign.py`, makes one),
and install that CA on every client (Windows: Local Machine → Trusted Root;
macOS: System keychain, Always Trust; Linux: the system store — VS Code uses it).

For a CLI whose bundled Node runtime does not read that system store, trust
the same CA explicitly when running it:

```
NODE_EXTRA_CA_CERTS=/path/to/site-ca.crt code --install-extension publisher.name
```

{_start(network, tar_name)}
## 4. Check

{_check(updates)}

Then search in VS Code's Extensions view and install something.

## Remote-SSH

The server build must match the client's exact commit. The mirror keeps the
latest and the two previous commits, so a client up to two releases behind
can still connect; older clients should take the mirrored update first.

Extensions are licensed for use with Microsoft's VS Code products; this
mirror is for internal use.
""".encode()
