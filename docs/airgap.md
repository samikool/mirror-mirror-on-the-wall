# Serving an airgapped network

This is the far side, end to end: how a bundle is produced, carried across,
applied, served, and verified — and what to do when one of those goes wrong.

The far side runs mmw too (`mmw site`): it keeps every version, serves any
of them, and can take one repository back without a new bundle — see
[far-side.md](far-side.md) for everything it does. Installing it needs no
network: the first bundle carries mmw's installer beside it. A far side that
still runs the old `apply.py` converts in place with `mmw site adopt`
([far-side.md](far-side.md#a-far-side-applypy-manages)).

---

## The shape of it

```
 connected side                    airgap                 airgapped side
 ──────────────                   ────────                ──────────────
 mmw sync      → pool
 mmw verify    → is the pool complete?
 mmw bundle    → lab-<stamp>.tar  ──[ USB / disk ]──→  mmw site apply
                 lab-<stamp>.tar.sha256                    → /srv/www
                 (mmw-<version>-<arch>.tar)            → nginx + DNS
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
listed), the transfer succeeds, the far side applies it, and the only
symptom is `apt install` failing months later on a machine nobody can reach.

It writes these next to each other in the network's `bundle_dir`:

| File | What it is |
|---|---|
| `lab-<stamp>.tar` | the payload |
| `lab-<stamp>.tar.sha256` | `sha256sum -c` format, to check the transfer |
| `mmw-<version>-<arch>.tar` | mmw's installer — the first time, and whenever this mmw is newer (`--installer`) |

Useful flags:

```bash
mmw bundle -n lab --full              # send everything, not just the delta
mmw bundle -n lab --out /media/usb    # write here instead of bundle_dir
mmw bundle -n lab --allow-incomplete  # ship anyway, having been told what is missing
mmw bundle -n lab --out -             # stream the tar to stdout (see below)
```

### Straight onto a drive, over ssh

`--out -` writes the bundle to stdout and nothing to disk, so a machine
with the drive attached can pull it from the mirror host:

```bash
ssh mirrorhost mmw bundle -n lab --out - > /media/drive/lab.tar
```

Everything else mmw prints goes to stderr, ending with a `sha256 <digest>`
line. Write it beside the tar under the name you chose, in `sha256sum -c`
format, to check the copy later:

```bash
echo "<digest>  lab.tar" > /media/drive/lab.tar.sha256
``` The
bundle counts as sent only once the last byte is written: if the pipe
breaks, nothing is recorded and the same command starts it again.
Streaming runs at the disk's speed, about the same as `tar cf -` over the
same files.

### Stage instead of bundle

`mmw stage` lays the bundle out as a directory instead of a tar: the same members,
with every package hardlinked from the pool, so it costs almost no disk.
It exists without mmw, so tar it over ssh, rsync it or look inside it with
whatever you like:

```bash
mmw stage -n lab --out /srv/stage          # /srv/stage/lab-<stamp>/
ssh mirrorhost tar cf - -C /srv/stage/lab-<stamp> . > /media/drive/lab.tar
```

Tar the directory's contents (`-C …/lab-<stamp> .`), not the directory
itself, so `.mmw/manifest.jsonl` sits at the top of the archive. Any such
tar, in any order, is a valid bundle for the far-side mmw. Keep
the stage directory on the pool's filesystem: elsewhere mmw warns and copies
the files instead. It counts as sent once the directory is complete; delete
it yourself when it has gone across.

### Lost the connected side?

A kept bundle records its network settings and the APT units it carried.
You can rebuild `mmw.toml` without contacting the far side:

```bash
# Install mmw on the replacement connected side, then inspect the recovery.
mmw init --from-bundle /media/kept/lab-20261101T120000Z.tar \
  --mirror-dir /srv/mirror --dry-run

# Write mmw.toml once you have reviewed the output.
mmw init --from-bundle /media/kept/lab-20261101T120000Z.tar \
  --mirror-dir /srv/mirror
mmw sync --quiet
mmw bundle -n lab --since 2026-11-01 --bundles /media/kept
```

Use the day or stamp of the last bundle that **arrived**, rather than a later
one that was lost. Keep these bundles on the connected side; nothing needs
to come back across the airgap.

`--from-bundle PATH` is repeatable: supply each network's bundle to recover
several networks. If several supplied bundles describe one network, its
newest manifest wins. Both mmw 1.4 format-2 and current format-3 manifests
work, including ledger `.manifest.jsonl` and legacy `.manifest.jsonl.zst`
files. Recovery reads the tar's first manifest member; it does not unpack
packages or recreate the pool.

Recovery refuses to overwrite an existing `mmw.toml`, even with `--force`.
`--dry-run` works when one exists and prints only TOML to stdout, with the
recovery notes on stderr, so you can redirect the preview to another file.
Review the restored publish and bundle destinations before using them.

The manifest cannot recover your old mirror directory, global connection
and disk settings, sources outside that network's selection, or networks
never bundled. You choose `--mirror-dir` (default `./mirror`); other global
settings use defaults. Recovered APT sources preserve recorded suites,
components, architectures, key fields and flags. Only recorded selections
can be recovered; an original “all upstream suites” choice becomes the
explicit suites found in the bundle.

VS Code, PyPI and apt-local sources need adding again. Their recorded units
omit essential source settings: VS Code's dimensions and extension choices,
PyPI's targets and requirements, and apt-local's folder and signing-key
name. Recovery names each omitted source and what was not recorded, rather
than constructing a source with guessed defaults.

In the TUI, the first Setup screen has **Start from a bundle…**. Choose
again to add another network's bundle. Choose the mirror directory as
usual, then review the Sources step. **Review sources and networks…** opens
an editable TOML preview with validation. Nothing is written until the
wizard's final **Write config** step. Setup can finish with no sources,
including when only network settings could be recovered; add the missing
sources afterwards from Repositories or with `mmw add`.

### A past state from Debian's or Ubuntu's archive

When no kept bundle covers the day you need, or a build must be reproduced
against the archive as it was, mmw can record a Debian or Ubuntu
repository's past state as points, from
[snapshot.debian.org](https://snapshot.debian.org) or
[snapshot.ubuntu.com](https://snapshot.ubuntu.com):

```bash
mmw sync --quiet --as-of 2026-10-15 --repo Debian     # its state at the end of that day
mmw sync --quiet --as-of 2026-10-15 --repo https://archive.ubuntu.com/ubuntu
mmw sync --quiet --from-url https://snapshot.ubuntu.com/ubuntu/20261015T013713Z/
```

A date needs `--repo` (a name or URL); the archive follows from the
repository's Release. A snapshot URL finds its repositories itself: each
apt repository whose path matches is compared with the snapshot on its
Release `Origin`, `Label` and `Codename`. An Ubuntu archive and security
repository can both match; each gets its own suites' points. `--repo`
overrides the match. Without `--quiet` the TUI opens on **Add a past
point**. In Sources, highlight a repository and choose **More… → Add a past point…** (or press `t`).

Ubuntu imports use the public service's documented
`https://snapshot.ubuntu.com/ubuntu/TIMESTAMP/` URLs for both metadata and
packages, including security pockets. No Ubuntu account, Pro token or local
APT configuration is needed. **Look** checks the snapshot metadata before
enabling **Add point**. An HTTP 401 or 403 means the service refused access;
mmw reports the URL and stops, rather than treating that suite as missing.
Check [the service](https://snapshot.ubuntu.com/) and retry later. Imports
never substitute today's archive for an unavailable historical state.

The fetch uses all configured units of that repository, through the same
code as a sync, so files already in the pool are not downloaded again.
No network is required: history belongs to the repository. Points are dated by their Releases, and what each network is on now
does not change: a later `mmw bundle --history` carries them to the far
side. Nothing is recorded unless every file arrived; a re-run fetches only
what is missing.

Transfer previews read retained repository points even if the network was
created after the sync. You do not need to sync again just to give that
network its own recording. Imported past points can also be transferred
without replacing the live repository's files or sync references.

- A selected suite the archive didn't have yet on that date (a pocket
  created later) is skipped with a warning; the others still get points.
- snapshot.ubuntu.com starts on 2023-03-01, and its ports archive (arm64 and
  the other non-x86 architectures) is not public.
- Third-party repositories have no archive.
- A network with its own debs layered over the repository is skipped and
  named; layered repositories aren't supported yet.
- A past Release whose `Valid-Until` has passed is flagged: clients need
  `Check-Valid-Until=false` (see [§7](#7-valid-until)).

### Repository history and new networks

Every successful APT sync records the repository's accumulated state per
suite, even without a network. Architectures may sync separately: a point
retains each unit's last successfully synced files and its own Release metadata. Networks
select their slice; an update to an unselected unit does not create another
network point or transfer unchanged files. Publish and transfer use the
last complete repository point, even if a later failed sync left different
live refs. Creating or editing a network does not require another sync.

A network can use all retained points of its selected repositories directly.
Inspect them without changing any state:

```bash
mmw history -n lab --since 2026-10-01
mmw history --repo Debian
```

In Sources or Networks, choose **More… → History**. There is no Add step:
the transfer's From and To menus select retained dates directly. `--history`
carries intermediate retained points. A network layer keeps its own rewritten
history; upstream history cannot stand in for that layer's generated files.

Repository history is kept in full by default (`[settings] keep_points =
"all"`); pruning it is manual. Set a count or an age there, or override
`keep_points` and `keep_suites` on a source. Networks have no retention of
their own. Points currently published to a
directory remain protected until it advances. Use Settings or Sources, or:

```bash
mmw keep 10
mmw keep all --repo Debian
mmw keep 90d --repo Debian --suite bookworm
mmw keep default --repo Debian                # inherit Settings again
mmw prune --costs                            # repository and network costs
```

Existing history is kept as legacy slices during upgrade. VS Code and PyPI
still use their existing network-owned history because their provider-wide
publish layouts generate files across units; repository-owned recording for
those layouts is deferred. The far side and bundle format are unchanged.

### Choose a transfer range

Transfer defaults to each repository's last successful local transfer through
its latest recorded points. The TUI's From and To controls show the actual
resolved dates per suite, retained date spans, point counts and an approximate
payload. No snapshot is made. Choose an earlier From date to recover a lost
transfer; choose an earlier To date to carry older retained points. Both menus
list retained repository point dates, newest first, including points synced
before the network existed. Choose **A date** to enter another cutoff.

```bash
mmw bundle -n lab --until 2026-11-10 --dry-run
mmw bundle -n lab --since 2026-11-01 --until 2026-11-10 --history
mmw stage -n lab --until 2026-11-10 --full
```

A To date (`--until`) selects the newest retained point at or before that date,
independently for every selected suite. Repositories need not share dates.
A selected suite with no eligible point, or whose target was pruned, is named
and refused; it is never silently dropped. History includes retained
intermediate points through the end date and warns about pruned history.
With the default From point, `--history` also carries older history imported
or explicitly included since the last transfer. Explicit dates bound the range.
`--repo` limits the repositories; a partial transfer advances only their local
transfer records. `--dry-run` uses the same range planner without writing a
bundle or advancing those records. These options also work with `mmw stage`.
Legacy snapshot `--to` remains available, but cannot be combined with `--until`.

Date-targeted APT transfers use the network's current selection and
configuration to slice retained repository points. They do not need a
network-specific recording or unrelated live refs. A network created after
the syncs can select those earlier points immediately.

An APT X→Z transfer is cumulative across the retained points in that range.
A site at Y, with X ≤ Y ≤ Z, can reach Z using the bundle and its existing
Y files. A file that disappears after X and returns by Z is carried again;
having sent it in an earlier transfer is not grounds to omit it. The last
transfer supplies the default From point, not an exact-base requirement.
Network configuration and keys travel with the range. A failed removal of
an obsolete package warns after the target is installed rather than blocking
the update; files required by Z must still be present and verified.

The far-side review shows each arriving suite's date span. Importing an older
point does **not** roll back a newer served point or remove retained points:
the site serves its newest complete points unless held or pinned. Rollback and
cleanup remain separate site actions; automatic cleanup runs only if explicitly
enabled in the site's settings. A local transfer record confirms bundle
creation, not that the drive arrived across the airgap.

### What a bundle carries

A bundle carries **points**, not a jump from one snapshot to another. Every sync records a
point per suite that changed; a bundle lists each chosen repository's
points, and carries only the files the network was never sent — each file
once, however many points hold it. The far side adds the points to its
timeline and serves each suite's newest.

```bash
mmw bundle -n lab              # each suite's newest point (the default)
mmw bundle -n lab --history    # every point synced since the last bundle too
mmw bundle -n lab --newest     # just the newest, whatever the network's setting
mmw bundle -n lab --full       # carry every file again, sent or not
```

`--history` lets the far side serve an older point (`mmw site serve`)
without asking for it; set `history = true` on the network to make it the
default. The summary names each suite's dates:

```
  Debian  bookworm 2026-11-01 → 2026-11-15
  VS Code  stable new → 2026-11-15
```

Manual network snapshots and snapshot endpoints have been removed. Use
`--since DATE`, `--until DATE` and `--full` instead. Existing snapshot-derived
history remains readable through compatibility code; upgrading does not erase it.

A bundle counts as sent once it is written: mmw cannot know whether it ever
reached the far side. If one never arrived (the drive was lost, the stage
directory deleted), the next bundle leaves out the files it carried. Send
what is missing with `--since` and the day of the last bundle that arrived:

```bash
mmw bundle -n lab --since 2026-11-01
mmw bundle -n lab --since 20261101T120000Z --bundles /media/kept
mmw bundle -n lab --since 2026-11-01 --history  # recover intermediate points too
```

A day means its end in UTC; a full stamp selects an exact cutoff. mmw first
looks for this network's newest kept manifest on or before that cutoff, in
its `bundle_dir`, the local bundle ledger, and any extra `--bundles DIR`
(repeatable). Only the first tar member is read. Unreadable bundles are named
and skipped. Format 2 bundles from mmw 1.4 and current format 3 both work.

With repository points, the baseline is this network's retained points dated
on or before the cutoff, the newest per track, and a kept manifest limits it
to the units that bundle carried: an architecture, component or suite added
to the network since then is sent whole, and the preview says so
(`ubuntu: arches i386 sent whole (not in <bundle>.tar's selection)`). Without
repository points, the manifest's files are the baseline. If no manifest is
old enough, the points alone are an assumption about what arrived, whereas a
manifest records what was bundled; the summary names the source. If neither
exists, mmw refuses and offers `--full`.

When no kept bundle survives, say what the far side never had with
`--whole` (repeatable): `[REPO:]DIMENSION=VALUE[,VALUE]`, DIMENSION one of
`arches`, `components` or `suites`, REPO a repository's name or URL
(default: every repository).

```bash
# Ubuntu went over as amd64 only; the network now selects i386 too
mmw bundle -n lab --since 2026-11-01 --whole ubuntu:arches=i386 --dry-run
```

amd64 goes as the change since 1 November; every i386 file goes, except
those amd64 already carried (`Architecture: all` packages), which the far
side has. A selector that matches nothing the network selects is refused.
`--whole` also works without `--since`, from the last transfer, and cannot
be combined with `--full`.

`--since` replaces the sent record for this bundle, including generated
network files. With `--history`, retained points first seen after the cutoff
travel even if previously marked sent. After success, the baseline and
carried files count as sent, so the next plain bundle is a delta again.
`--since` cannot be combined with `--from` or `--full`.

This also works after rebuilding a lost connected-side pool: keep the last
arrived bundle somewhere on the connected side, sync again, and use its date
with `--bundles` pointing at that folder. Nothing needs to come back across
the airgap. If no baseline remains, use `mmw bundle -n lab --full`.

In the TUI, choose **A date** under **From**, enter the date and optionally
choose a **Kept bundles** folder. **Send whole** takes the same selectors as
`--whole`, space-separated. The preview names the source and what goes whole;
if no state can be found, the transfer offers to send everything instead.

The far side checks every point it is given: a point whose files are missing
is stored incomplete and not served — applying says which, with how many
files are missing, and `mmw site history` marks it.
In the TUI, the transfer page's **Points** choice is
the same switch.

---

## 2. Carry it across

Copy **the tarball and the `.sha256`** to your transfer media — and mmw's
installer, when one was written beside them.

On arrival, check the transfer before spending an hour on anything else:

```bash
sha256sum -c lab-20260906T143000Z.tar.sha256
```

A tarball truncated on a member boundary still extracts cleanly, so this is the
cheapest way to tell a bad copy from a bad bundle.

---

## 3. Apply it

```bash
mmw site apply /media/drive/lab-20260906T143000Z.tar --root '*'=/srv/www   # the first time
mmw site apply /media/drive/lab-20261006T143000Z.tar                       # after that
```

Everything arrives and is checked against the bundle's manifest before
anything served changes; bundles can come in any order, from this network or
another; a point whose files aren't all here is stored and said. The details
— roots, holding a repository back, serving an older day, exit codes — are in
[far-side.md](far-side.md#apply-a-bundle).

---

## 4. Serve it

A tree `mmw site apply` lays out serves the same files as a publish of the
same network; a copied publish tree becomes a far side with
`mmw site adopt PATH`. Neither publishing nor bundling takes a snapshot.

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

The far side makes its own key and signs it with a CA held there; a bundle
never carries a private key. Make the CA once (or load one you have), then
sign every root of the mirror:

```bash
mmw site ca make --out ~/site-ca --name "Lab CA"    # --encrypt asks for a passphrase
mmw site certs                                      # every root; then recreate nginx
```

It never overwrites an existing CA. Keep `ca.key` private and backed up:
losing it means re-trusting a new CA on every client. Each server block of
the generated config includes `/srv/mmw/.mmw/tls/https*.conf`, a wildcard
nginx ignores while nothing matches; `https.conf` — `listen 443 ssl` and the
certificate paths — is written only once the certificate fits the key, so
the config loads before, during and after, and plain HTTP never stops. A
certificate that still fits, covers every hostname and isn't expiring
within 30 days is left alone (`--force` re-signs); when a new repository
brings a hostname, `mmw site status` says the certificate doesn't cover it.

Using Nginx Proxy Manager or your own config instead? `mmw site certs --out
DIR` also collects each root's `NETWORK-ROOT.crt`, `.key` and `ca.crt` there.

A published tree (served on the connected side) carries the connected
side's key, request and config in `{root}/.mmw/tls/` (key 0600, directory
0700; outside every web root) and is signed with `mmw certs`.

If your CA is reachable from the connected side instead,
`mmw certs -n lab --ca site-ca.crt --ca-key site-ca.key` signs a published
tree there.

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

**`mmw site apply` stores a point "incomplete".**
The far side lacks files that point needs (it missed a bundle). The line
names what to ask for — usually `mmw bundle -n lab --repo 'LABEL' --full` on
the connected side; the rest of the bundle still applies.

**`mmw site verify` reports mismatches.**
Keep generated files unchanged; edit Compose mount sources in a deployment
copy outside the served folder. Re-check the `.sha256` and re-transfer a
damaged bundle; `mmw site verify --repair` puts back what the store still holds.

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
