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
 mmw bundle    → lab-<stamp>.tar.zst  ──[ USB / disk ]──→  python3 apply.py
                 apply.py                                  → /srv/www
                 lab-<stamp>.tar.zst.sha256                → nginx + DNS
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
| `lab-<stamp>.tar.zst` | the payload |
| `lab-<stamp>.tar.zst.sha256` | `sha256sum -c` format, to check the transfer |
| `apply.py` | the applier, stdlib-only Python 3.6+ |

Useful flags:

```bash
mmw bundle -n lab --full              # send everything, not just the delta
mmw bundle -n lab --out /media/usb    # write here instead of bundle_dir
mmw bundle -n lab --allow-incomplete  # ship anyway, having been told what is missing
```

`--full` still computes deletions. It means "send every file", not "forget what
the far side has".

---

## 2. Carry it across

Copy **the tarball, the `.sha256`, and `apply.py`** to your transfer media.
Forgot `apply.py`? Every bundle carries its own copy as its third member:
`tar -xf lab-20260906T143000Z.tar.zst .mmw/apply.py`.

On arrival, check the transfer before spending an hour on anything else:

```bash
sha256sum -c lab-20260906T143000Z.tar.zst.sha256
```

A tarball truncated on a member boundary still extracts cleanly, so this is the
cheapest way to tell a bad copy from a bad bundle.

---

## 3. Apply it

```bash
python3 apply.py lab-20260906T143000Z.tar.zst --root /srv/www
# 20260901T090000Z -> 20260906T143000Z
# 12,481 applied  3,902 removed
```

Requirements on the far side: Python 3.6 or newer and the `zstd` binary on
`PATH`. Nothing else — no `mmw`, no pip install, no network.

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
| 5 | tooling problem (`zstd` missing, truncated archive) | check `zstd`, re-check the `.sha256` |

---

## 4. Serve it

Every bundle carries its own nginx configuration, with the real `--root`
already substituted in:

```bash
ln -s /srv/www/nginx/mirror.conf /etc/nginx/conf.d/mmw-mirror.conf
nginx -t && systemctl reload nginx
```

`mirror.conf` is one self-contained file with a server block per host, so it
works linked into `conf.d/` or `sites-enabled/` alike — link the file, never
the `nginx/` folder. (Bundles from before 1.0 shipped a `sites/` folder beside
it; Debian's `include sites-enabled/*` matches that folder too, and nginx
refuses to start on "is a directory". Applying a newer bundle deletes it.)

With `bundle_tls = true` (see [HTTPS](#https) below) every host also listens
on 443. Set `nginx = false` on the network if you would rather write your own.

---

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
this works when every client trusts **your** CA.

On the connected side, with `certs = true` on the network:

```bash
mmw certs -n lab --ca site-ca.crt --ca-key site-ca.key   # writes mirror.crt
```

Then set `bundle_tls = true` on the network. Every bundle from then on
carries `mirror.crt` and `mirror.key`; `apply.py` installs them in
`{root}/.mmw/tls/` (key 0600, directory 0700 — no server block serves it) and
the bundle's `nginx/mirror.conf` already points at them:

```
TLS: mirror.crt, mirror.key -> /srv/www/.mmw/tls
```

They ride **every** bundle, delta or full, near the front of the archive, so a
re-signed certificate reaches the far side with the next ordinary delta.

`bundle_tls` is off by default because it puts the private key on your
transfer media. Without it, the far side is served over HTTP only.

`mmw certs` re-checks the certificate on each run: a network that gained a
hostname since signing (with `dist_upgrade`, `changelogs.ubuntu.com` is one),
a key that no longer matches, or an expiry within 30 days is reported on
stderr. Re-run it with `--ca` to re-sign, then bundle.

If you do not run a CA, turn off the client checks that use HTTPS instead —
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
you skipped, or simply take the next ordinary delta — a failed apply never
advanced `.mmw-state`, so the next bundle produced on the connected side
already covers what was missed. `--full` also works and bypasses the check.

**`apply.py --verify` reports mismatches.**
If it names files under `nginx/`, check that `--root` is the same directory you
applied to previously — those files have the root substituted into them, and
verifying against a different root reports every one of them as wrong.
Otherwise, re-check the `.sha256` and re-transfer.

**`apt install` 404s on a package `apt update` listed.**
The bundle shipped an index without its package. Run `mmw verify` on the
connected side — that is exactly what it detects — then re-sync and re-bundle.

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
