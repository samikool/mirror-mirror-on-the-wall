# Serving an airgapped network

This is the far side, end to end: how a bundle is produced, carried across,
applied, served, and verified — and what to do when one of those goes wrong.

Nothing here needs `mmw` installed on the airgapped machine. The only thing
that travels is a tarball, a small applier script, and (optionally) a checksum
file.

---

## The shape of it

```
 connected side                    airgap                 airgapped side
 ──────────────                   ────────                ──────────────
 mmw sync      → pool
 mmw verify    → is the pool complete?
 mmw bundle    → lab-<stamp>.tar  ──[ USB / disk ]──→  python3 apply.py
                 apply.py                                  → /srv/www
                 lab-<stamp>.tar.sha256                → nginx + DNS
```

Each re-bundle carries only what changed since the previous one, so the first
transfer is large and the monthly ones are not.

---

## 1. Produce the bundle

On the connected machine:

```bash
mmw sync --quiet          # fill the pool
mmw verify                # refuse to ship an incomplete mirror
mmw bundle -n lab
```

`mmw bundle` runs the completeness check itself and refuses if the pool cannot
serve everything the indexes promise. That check matters more here than
anywhere else: a missing package is invisible to the bundle (it simply is not
listed), the transfer succeeds, `apply.py --verify` passes, and the only
symptom is `apt install` failing months later on a machine nobody can reach.

It writes three files next to each other in the network's `bundle_dir`:

| File | What it is |
|---|---|
| `lab-<stamp>.tar` | the payload |
| `lab-<stamp>.tar.sha256` | `sha256sum -c` format, to check the transfer |
| `apply.py` | the applier, stdlib-only Python 3.6+ |

Useful flags:

```bash
mmw bundle -n lab --from none         # send everything, not just the delta
mmw bundle -n lab --out /media/usb    # write here instead of bundle_dir
mmw bundle -n lab --allow-incomplete  # ship anyway, having been told what is missing
```

`--from none` still computes deletions. It means "send every file", not "forget what
the far side has".

### Between any two snapshots

Every bundle goes from one [snapshot](getting-started.md#snapshots-roll-a-network-back)
of the network to another. By default that is from where the last bundle went
to a new snapshot of now, but any two work — forwards, backwards, or skipping
some:

```bash
mmw snapshot list -n lab                        # stamps, labels, what each holds
mmw bundle -n lab --from 20260901T090000Z --to pre-upgrade
mmw bundle -n lab --from none --to pre-upgrade  # full, to an older state
```

Backwards (bringing the far side back to an older state) works because a snapshot keeps every file
it lists in the pool until the snapshot is deleted. In the TUI, `b` on a network
asks From and To and estimates what the bundle sends.

On the far side, `python3 apply.py --status --root /srv/www` says which
snapshot a mirror is at, and a refused bundle names the exact
`mmw bundle --from … --to …` to ask for.

---

## 2. Carry it across

Copy **the tarball, the `.sha256`, and `apply.py`** to your transfer media.
Forgot `apply.py`? Every bundle carries its own copy as its third member:
`tar -xf lab-20260906T143000Z.tar .mmw/apply.py`.

On arrival, check the transfer before spending an hour on anything else:

```bash
sha256sum -c lab-20260906T143000Z.tar.sha256
```

A tarball truncated on a member boundary still extracts cleanly, so this is the
cheapest way to tell a bad copy from a bad bundle.

---

## 3. Apply it

```bash
python3 apply.py lab-20260906T143000Z.tar --root /srv/www
# 20260901T090000Z -> 20260906T143000Z
# 12,481 applied  3,902 removed
```

Requirements on the far side: Python 3.6 or newer. Nothing else — no `mmw`,
no pip install, no network. Bundles are plain tars: the payload is packages,
which are already compressed. If your transfer medium wants compression,
`zstd` the tar yourself and `zstd -d` it before applying — `apply.py` refuses a
compressed file with exactly that message.

| Flag | Effect |
|---|---|
| `--root DIR` | where the tree is written (required) |
| `--dry-run` | report what would change; touch nothing |
| `--verify` | after applying, hash every file the manifest names |

**Bundles must be applied in order.** `apply.py` records the last-applied stamp
in `{root}/.mmw-state` and refuses a bundle whose `previous` does not match.
Applying 1 then 3 would silently leave the tree missing everything 2 carried.

**Deletions are applied last**, after every payload file has been written, so a
machine running `apt update` mid-apply never sees a release file whose indexes
have already been removed.

**A failed apply never advances `.mmw-state`**, so it is always safe to retry.

### Exit codes

| Code | Meaning | What to do |
|---|---|---|
| 0 | applied cleanly | — |
| 2 | unsafe or malformed bundle | do not retry; re-bundle on the connected side |
| 3 | out of order | apply the missing bundle, or just take the next ordinary delta |
| 4 | `--verify` found a mismatch | see below |
| 5 | tooling problem (a compressed or truncated archive, an older bundle's manifest) | decompress it, re-check the `.sha256`, or use the `apply.py` that came with that bundle |

---

## 4. Serve it

A directly copied publish tree has the same deployment files and setup as an
applied bundle; there is no apply step for files already copied into place.
Publishing snapshots remain manual; bundling current content snapshots it
automatically.

Every nginx-enabled network includes `nginx:latest` for Linux amd64, a Compose
file, the generated `mirror.conf`, and a guide in `nginx/README.md`. The config
always uses `/srv/mmw` inside the container, regardless of the host folder.

After applying the bundle, run from its `nginx/` directory:

```bash
docker load -i nginx-latest-linux-amd64.tar
cp compose.yaml /path/to/oak-compose.yaml
# Edit host-side mount paths in that deployment copy, then:
docker compose -f /path/to/oak-compose.yaml run --rm nginx nginx -t
docker compose -f /path/to/oak-compose.yaml up -d --force-recreate
```

The Compose file uses simple bind mounts such as
`/path/to/oak:/srv/mmw:ro` and
`/path/to/oak/nginx:/etc/nginx/conf.d:ro`.
Replace the host sources with the actual network folder; keep container
targets fixed. Keep your edited deployment copy outside the published folder
so later publishes do not overwrite it and manifest verification still passes.

Compose uses bridge networking with `80:80` and `443:443` port mappings.
Change the left side to use other host ports, for example `8080:80` and
`8443:443`. Clients must use the selected ports. VS Code networks include
the gallery in this same stack; nginx reaches it at `gallery:8080` without
publishing the gallery port. Load the gallery image too, as shown in the
generated guide. Do not also start its separate Compose file. Point client
DNS at this host and trust the network CA for HTTPS.

After applying another bundle, load any changed image and recreate the
container using your deployment copy. Image bytes travel in a delta
only when they change; replaying an existing snapshot uses its frozen image.
On the connected side, publish/bundle pulls the latest image and caches exports
by image ID in `{mirror_dir}/nginx-image/`. If Docker or the registry is
unavailable, a cached export is used with a warning. With no cache, a warning
identifies the missing image; load `nginx:latest` (linux/amd64) separately on
the far side. `nginx = false` omits the container setup and generated config.

## 5. Point DNS at it

Clients need **no repository configuration** — that is the whole design
(third-party signing keys are the one exception; see step 6). The tree
is laid out as `{root}/{hostname}/{upstream path}`, byte for byte as upstream
serves it, so a client asking `archive.ubuntu.com` for a file gets the same
path it always asked for. You only have to make that name resolve to the mirror.

For a small lab, `/etc/hosts` on each client:

```
10.0.0.10   archive.ubuntu.com security.ubuntu.com changelogs.ubuntu.com
```

For a real network, a dnsmasq stanza on the resolver:

```
address=/archive.ubuntu.com/10.0.0.10
address=/security.ubuntu.com/10.0.0.10
address=/changelogs.ubuntu.com/10.0.0.10
```

The hostnames you need are exactly the directory names at the top of `--root`.
If the network has `dist_upgrade = true`, `UPGRADE.md` in the root lists them
for you.

---

## 6. Import the GPG keys

Mirrored metadata is copied byte for byte, so upstream signatures stay valid —
which means the client still has to trust the upstream key.

Keys travel in the bundle under `{root}/keys/{label}/` — they are the **last**
members of the archive, so an extraction that stopped early has none. Both
encodings are shipped:

- `*.asc` — armoured, for `gpg --import`
- `*.gpg` — binary keyring, for `signed-by=` in a sources.list entry

Clients get them from the mirror itself, two ways:

- **At the vendor's own URL.** A key the source fetched from a URL
  (`gpg_key_url`) is published at that exact path, and so is anything else the
  vendor's instructions fetch, listed in `extra_files` — see
  [Vendor install instructions](#vendor-install-instructions) below.

- **From `/mmw-keys/` on any mirrored host** — every key, both encodings, by
  label. Useful for a key that came inline from a `Signed-By:` block and has
  no URL:

  ```bash
  sudo curl -fsSLo /usr/share/keyrings/cuda-archive-keyring.gpg \
    http://developer.download.nvidia.com/mmw-keys/nvidia/cuda_debian_prod.gpg
  # deb [signed-by=/usr/share/keyrings/cuda-archive-keyring.gpg] https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/ /
  ```

The distro archives' keys (Ubuntu, Debian) are already on a stock client; it
is third-party repositories — NVIDIA, Docker, PPAs — whose keys need this step.

An airgapped client with no trusted key is the most common first failure. It
shows up in `apt update` as `NO_PUBKEY <key id>`; match the ID with
`gpg --show-keys {root}/keys/*/*.asc`.

### Vendor install instructions

A vendor's setup steps fetch files from the vendor's own servers: a key, a
ready-made sources file. Name each such URL on the repository and the mirror
serves it byte-for-byte at the same path, so the steps run unchanged on the
airgapped side — given DNS for the vendor's host, and [HTTPS](#https) for an
`https://` URL.

**NVIDIA Container Toolkit**:

```toml
[[sources]]
url = "https://nvidia.github.io/libnvidia-container/stable/deb/amd64"
provider = "apt-flat"
gpg_key_url = "https://nvidia.github.io/libnvidia-container/gpgkey"
extra_files = ["https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list"]
```

after which NVIDIA's own commands just work:

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg \
  && curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
```

(The `.list` says `stable/deb/$(ARCH)`; apt fills in the client's
architecture, so mirror each architecture your clients use.)

**VS Code** — the key lives beside the repository, not in it:

```toml
[[sources]]
url = "https://packages.microsoft.com/repos/code"
suites = ["stable"]
components = ["main"]
gpg_key_url = "https://packages.microsoft.com/keys/microsoft.asc"
```

after which Microsoft's `wget -qO- https://packages.microsoft.com/keys/microsoft.asc
| sudo gpg --dearmor -o /usr/share/keyrings/microsoft.gpg` and their
`vscode.sources` file work as written.

A URL works for this when it names a file and has no query string; a fetch
that fails keeps the last good copy, and removing a URL removes its copy on
the next sync. Both fields are in the repository editor in the TUI.

### HTTPS

Clients whose sources say `https://` — vendor repositories like NVIDIA's, and
a stock Ubuntu client's release-upgrade check against
`https://changelogs.ubuntu.com` — need the mirror to answer on 443 with a
certificate they trust. No server-side trick can fake a public certificate;
this works when every client trusts **your** CA, which on an airgapped network
lives on the airgapped side. Nothing has to come back across.

With `certs = true`, both published trees and bundles carry the private key,
certificate signing request, OpenSSL config and signing tools. Publish installs
them directly; `apply.py` installs a bundle's copies in the same
`{root}/.mmw/tls/` layout (key 0600, directory 0700; outside every web root).
Both include top-level `apply.py`, `sign.py` and `make-ca.py`, so a published tree copied
to a drive can be signed and deployed without the original mirror pool.
Until there is a signed certificate, TLS setup reports:

```
HTTPS waits for a signed certificate: python3 sign.py --ca CA.crt --ca-key CA.key /srv/www/lab
```

No CA yet? Make one once, where it will live — `make-ca.py` sits beside
`sign.py`, and prints how to trust `ca.crt` on each kind of client:

```bash
python3 make-ca.py --out ~/site-ca --name "Lab CA"    # --encrypt asks for a passphrase
```

It never overwrites an existing CA. Keep `ca.key` private and backed up:
losing it means re-trusting a new CA on every client.

On the airgapped side, sign every mirror at once — name mirror roots, a
directory of them, or `.csr` files; `sign.py` sits beside each bundle (and in
`{root}/.mmw/tls/`):

```bash
python3 sign.py --ca site-ca.crt --ca-key site-ca.key /srv/www   # every root under it
# lab  signed (no certificate yet): 7 hostname(s), until Oct 11 14:10:00 2028 GMT
# oak  signed (no certificate yet): 7 hostname(s), until Oct 11 14:10:00 2028 GMT
python3 apply.py --tls --root /srv/www/lab                       # then recreate nginx
```

Each certificate lands beside its request as `mirror.crt`. `apply.py --tls`
(and every later apply) turns HTTPS on in the generated config: each server
block includes `/srv/mmw/.mmw/tls/https*.conf`, a wildcard nginx ignores while
nothing matches, and `apply.py` writes `https.conf` — `listen 443 ssl` and the
certificate paths — only once the certificate fits the key. So the config
loads before, during and after; plain HTTP never stops. Later bundles never
carry a certificate they do not have, so the far side's survives every apply.

Using Nginx Proxy Manager or your own config instead? `--out DIR` also collects
`NAME.crt`, `NAME.key` and `ca.crt` per mirror to load from there.

`sign.py` needs Python 3.6+ and the `openssl` binary (1.1.1 or 3.x). It asks
for an encrypted CA key's passphrase once, refuses a CA key that is not the
CA certificate's, and leaves alone a certificate that still fits its key,
covers every hostname and is not expiring within 30 days (`--force` re-signs).
When a new repository brings a hostname, the next apply says the certificate
does not cover it; run `sign.py` again.

This puts the private key on your transfer media. A network whose media must
not hold it keeps `certs` off, and its far side is served over HTTP only.

If your CA is reachable from the connected side instead,
`mmw certs -n lab --ca site-ca.crt --ca-key site-ca.key` signs there and the
certificate rides every bundle too.

If you would rather not run a CA at all, turn off the client checks that use HTTPS instead —
for the release-upgrade check, `Prompt=never` in
`/etc/update-manager/release-upgrades`.

---

## 7. `Valid-Until`

Debian and Ubuntu `Release` files carry a `Valid-Until` date. Once a mirror is
older than that, `apt update` rejects it — which on a slow transfer cycle is
routine rather than exceptional.

```
Acquire::Check-Valid-Until "false";
```

in `/etc/apt/apt.conf.d/99mmw-airgap` on the clients. The generated `IMPORT.md`
says the same thing.

---

## Troubleshooting

**`apply.py` exits 3 (out of order).**
The far side is not where this bundle expects it to be. Either apply the bundle
you skipped, or ask for one from where the far side actually is — the refusal
prints the exact `mmw bundle -n … --from … --to …` to run on the connected
side. `--from none` also works and bypasses the check.

**`apply.py --verify` reports mismatches.**
Keep generated files unchanged; edit Compose mount sources in a deployment
copy outside the published folder. Re-check the `.sha256` and re-transfer damaged
files. Older bundles used host-root config substitution, so verify those
against the same `--root` you originally applied to.

**`apt install` 404s on a package `apt update` listed.**
The bundle shipped an index without its package. Run `mmw verify` on the
connected side — that is exactly what it detects — then re-sync and re-bundle.

**`mmw bundle` says files are damaged in the pool.**
A pool file no longer matches what was recorded for it, so nothing was
bundled. `mmw verify --repair` drops the damaged files, `mmw sync` downloads
them again, and the same `mmw bundle` command then works.

**`apt update` reports a missing `Release` file.**
The suite was published without its metadata. Check `mmw verify` on the
connected side, and confirm DNS points at the mirror for that specific host.

**A host you removed is still being served.**
Publishing and bundling only prune when `prune = true` on the network. With it
off, files the network no longer selects are left in place deliberately.

**Everything is served but `apt` complains about signatures.**
The key is not imported on the client — see step 6.

---

## See also

- [Getting started](getting-started.md) — the connected side, from zero
- [Configuration](configuration.md) — every `mmw.toml` key
- [CLI reference](cli.md) — every command and flag
