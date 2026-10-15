# VS Code

mmw mirrors the Microsoft VS Code Marketplace and serves it offline at
Microsoft's own hostnames: clients keep their stock settings, and extension
search, install, auto-update, the editor's own updates and Remote-SSH all go
to your server instead. This page is the whole path — what to mirror, how a
network carries it, and what the far side runs.

---

## What it mirrors

One `vscode` source covers three kinds of thing:

| Kind | What | Lands at |
|------|------|----------|
| `extensions` | The selected extensions' VSIX packages and their assets (icons, README, manifest), newest release per target platform | `*.gallerycdn.vsassets.io` |
| `desktop` | VS Code installers and archives (`.exe`, `.zip`, `.tar.gz`, `.deb`, `.rpm`) | `vscode.download.prss.microsoft.com` |
| `server` | VS Code Server and CLI builds — what Remote-SSH installs on a host. The latest commit and the two previous are kept | `vscode.download.prss.microsoft.com` |

Each is filtered by target platform (`linux-x64`, `win32-x64`, `darwin-arm64`,
… — see [Configuration](configuration.md#vs-code-provider--vscode)) and, for
builds, by quality (`stable`, `insider`). Platform-neutral extensions are
always included.

Which extensions: the **top N** most-installed, an explicit **list of ids**
(their dependencies and extension-pack members come along), or **all** of
them (over 100,000 — run `mmw dry-run` first). Latest release only;
prereleases only if you ask.

Only the Microsoft Marketplace. Copilot, Settings Sync and telemetry are not
mirrored, and extensions are licensed for use with Microsoft's VS Code
products — this is for internal use.

## Add it

```bash
mmw add vscode --kind extensions --kind server --platform linux-x64 \
               --quality stable --top-n 200 --id golang.go
```

or, in `mmw.toml`:

```toml
[[sources]]
provider  = "vscode"
kinds     = ["extensions", "server"]
platforms = ["linux-x64"]
qualities = ["stable"]

[sources.extensions]
top_n = 200
ids   = ["golang.go", "ms-python.python"]
```

In `mmw manage`, open **Sources → Add → VS Code**. The same form is
available through **Edit** on an existing VS Code source and during first-run
setup. Set the name, label, kinds, platforms and qualities; an empty dimension
means all of its choices. Add starts with no extensions selected. Choose
Top N, enter extension IDs (one per line, with optional `#` comments), or
enable All. These extension choices form a union, including dependencies and
extension-pack members. Add and Save validate and store the policy immediately;
Marketplace enumeration runs when you sync.

**Look up IDs** shows display names and missing IDs for the entered list.
Edit and adding the same source again replace its selection; they preserve
networks' references to the source. Cancel
keeps the saved configuration unchanged.

`mmw sync` then fills the pool like any other source: one sync unit per
kind × platform (× quality), e.g. `vscode-extensions-linux-x64`,
`vscode-server-linux-x64-stable`. `mmw dry-run` shows the size first.
Interactive sync shows source resolution and Marketplace retry messages in
the Sync section before the unit cards appear. Resolution failures stay visible
and mark a partial sync incomplete. Stop discards a pending
resolution result; an upstream request already in progress finishes first.

Sync progress counts extensions, not individual assets. An extension finishes
when all its selected assets are downloaded or already pooled; a failed asset
makes that extension fail. Session totals count each extension once across
platform units. Dry-run uses the same grouping. Transferred bytes and speed
still measure file transfers, and the pool's storage count is labeled files.

## A network with VS Code

A network publishes the VS Code source unless its `select` leaves it out — so
a network without `select` gets it, and a network that selects can narrow it
by dimension:

```toml
[[networks]]
name  = "lab"
root  = "/srv/www/lab"
certs = true                   # VS Code only speaks HTTPS — see TLS below

[[networks.select]]
source    = "https://marketplace.visualstudio.com"
kinds     = ["extensions", "server"]
platforms = ["linux-x64"]

# gallery_port = 8765          # change only if two networks share a host
```

One network can carry apt repositories and VS Code together: one tree, one
bundle, one nginx file, one certificate.
In network Add/Edit, **Gallery port** appears when VS Code is selected.
Deselecting VS Code hides the field and preserves its saved value. Network
details show the served hostnames, including both Marketplace wildcards.

## What publish and bundle produce

Under the network `root`:

| Path | What |
|------|------|
| `{publisher}.gallerycdn.vsassets.io/…` | Extension packages and assets, at their upstream paths |
| `vscode.download.prss.microsoft.com/…` | Desktop and server builds |
| `main.vscode-cdn.net/…` | The Marketplace's extension-control file |
| `vscode-gallery/catalog.json`, `updates.json` | What the gallery answers from — regenerated every publish |
| `vscode-gallery/gallery.py`, `compose.yaml` | The gallery service and how to run it |
| `vscode-gallery/python-3.13-slim-linux-amd64.tar` | The container image, for a far side with no registry |
| `vscode-gallery/VSCODE.md` | This network's far-side steps, with its own port |
| `nginx/mirror.conf` | Server blocks for every hostname, VS Code's six included |

`mmw bundle` carries the same tree. A delta bundle carries only what changed —
the image tar travels again only when the image itself changed — and
`apply.py` lays it out on the far side exactly as publish does here. A pruning
publish removes everything VS Code once the source leaves the network.

Publishing needs Docker on the connected machine the first time, to pull
`python:3.13-slim` (linux/amd64) and save it. The saved tar is cached in
`{mirror_dir}/vscode-gallery-image/`; without Docker, the cached tar is used,
and with neither, publish reports an error and still publishes everything else
— the far side then needs `python:3.13-slim` loaded some other way.
The Publish/Bundle view shows **Gallery image** progress while pulling,
inspecting, saving or using the cached tar. Stop is checked between Docker
operations; a Docker command already running finishes within its timeout.

The gallery is the only moving part: extension search, the update check and
"commit" redirects are dynamic, so `gallery.py` (stdlib Python, no
dependencies) answers them from `catalog.json` and `updates.json`. nginx
serves every file itself and proxies only those paths to the gallery.

## The far side

### 1. DNS

Point these names at the mirror:

- `marketplace.visualstudio.com`
- `update.code.visualstudio.com`
- `vscode.download.prss.microsoft.com`
- `main.vscode-cdn.net`
- `*.gallerycdn.vsassets.io`
- `*.gallery.vsassets.io`

The last two are wildcards — every publisher has its own subdomain — so they
need a DNS server with wildcard records. A hosts file cannot do it.

dnsmasq (`/etc/dnsmasq.d/vscode.conf`) — each line also covers subdomains:

```
address=/marketplace.visualstudio.com/MIRROR_IP
address=/update.code.visualstudio.com/MIRROR_IP
address=/vscode.download.prss.microsoft.com/MIRROR_IP
address=/main.vscode-cdn.net/MIRROR_IP
address=/gallerycdn.vsassets.io/MIRROR_IP
address=/gallery.vsassets.io/MIRROR_IP
```

BIND: one zone per name, with `@` and `*` records pointing at the mirror for
the two wildcard zones.

### 2. TLS

VS Code talks HTTPS only, so the network needs `certs = true`. The
certificate mmw generates covers all six names, wildcards included:
`mmw certs -n lab --ca site-ca.crt --ca-key site-ca.key` signs it here, or
`sign.py` signs it on the far side (see [Airgapped operation](airgap.md#https)).
Install the CA on every client: Windows — Local Machine → Trusted Root
Certification Authorities; macOS — the System keychain, Always Trust; Linux —
the system store, which VS Code reads.

If `code --install-extension` reports a certificate error despite system
trust, its bundled Node runtime may need the same CA explicitly:

```bash
NODE_EXTRA_CA_CERTS=/path/to/site-ca.crt code --install-extension publisher.name
```

This adds trusted CA certificates while keeping TLS verification enabled.
See [Node's CA option](https://nodejs.org/api/cli.html#node_extra_ca_certsfile).
The generated `VSCODE.md` also lists per-publisher hosts-file entries for
sites without wildcard DNS; regenerate those entries as publishers are added.

### 3. The gallery container

```bash
cd /srv/www/lab/vscode-gallery
docker load -i python-3.13-slim-linux-amd64.tar     # first time, and when a bundle brings a new one
docker compose -f compose.yaml up -d
```

It mounts the published root read-only and listens on
`127.0.0.1:{gallery_port}`. A later publish or `apply.py` swaps the catalog;
the gallery notices by itself — no restart.

### 4. nginx

The same include as any mmw network:

```bash
ln -s /srv/www/lab/nginx/mirror.conf /etc/nginx/conf.d/mmw-mirror.conf
nginx -t && systemctl reload nginx
```

### 5. Check it

```bash
curl -I https://update.code.visualstudio.com/api/update/linux-x64/stable/0000000000000000000000000000000000000000
```

answers `200` (an update is available) or `204` (up to date) — not a
certificate error, not `502`. Then search in VS Code's Extensions view and
install something.

## Remote-SSH

Remote-SSH installs a server build that matches the client's exact commit.
The mirror keeps the latest stable commit and the two before it, so a client
up to two releases behind still connects. An older client should take the
mirrored update first.

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| Extensions view search is empty or errors | The gallery container is not running (`docker compose ps` in `vscode-gallery/`), or nginx was not reloaded after the first publish |
| `502 Bad Gateway` on the update check | Same — nginx is up, the gallery behind it is not; check `gallery_port` matches |
| Certificate errors in VS Code | The CA is not trusted on the client, or the certificate predates the network's VS Code source — re-sign so it covers the wildcards |
| A name resolves to Microsoft | DNS lacks a record — usually one of the wildcards |
| Remote-SSH fails to download the server | The client's commit is older than the three kept; update the client from the mirror first |
| An extension is missing | It is not in the selection — add its id, or raise `top_n` — or it has no build for the client's platform |

For maintainers, the [real-client test](../tests/e2e/README.md) runs a stock
Microsoft CLI, nginx and gallery on an isolated offline network, including
signed extension installation/update and bundle replay.
