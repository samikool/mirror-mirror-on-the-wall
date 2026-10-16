# mirror-mirror-on-the-wall (mmw)

An APT repository mirror built for **airgapped networks**: `apt` (`dists/`
hierarchy) and `apt-flat` (`Packages` at the root) repositories, mirrored
*exactly*.

Metadata is copied byte-for-byte, so upstream GPG signatures stay valid. Files
are published in the same URL hierarchy as upstream, so a client that resolves
`archive.ubuntu.com` to your mirror works with **no client-side configuration
at all** — see [How clients find the mirror](#how-clients-find-the-mirror).
(The distro archives' keys ship with the OS; a third-party repository's key —
NVIDIA, Docker, a PPA — still has to be installed on each client, exactly as it
would online.)

> **Status: 1.0.** Everything below is exercised by an offline test suite and
> has run against real upstreams. Bundling is tested end to end — a real bundle
> is applied by `apply.py` and diffed against a directly published tree — but
> report anything a physical airgap turns up. See [CHANGELOG.md](CHANGELOG.md).

## Highlights

- **Content-addressable pool with hardlink dedup** — a package shared by ten
  suites, components or architectures is stored once. SQLite (WAL) index;
  publish and verify read safely alongside a running sync.
- **Consistency or nothing** — every index file is verified in memory and a
  mirror's new index becomes visible in one atomic transaction. A CDN caught
  mid-update, a stopped sync, a failed download: the previous consistent
  snapshot stays in force — and you are told so.
- **Exact upstream layout** — published trees are `{root}/{hostname}/{path}`,
  matching upstream URLs byte-for-byte. Nothing is rewritten, so nothing
  invalidates a signature.
- **Bundling across an airgap** — one plain `.tar` per network carrying only what
  changed, plus a stdlib-only applier that runs on Python 3.6.
- **VS Code too** — the Microsoft Marketplace (top N, chosen ids, or all),
  desktop installers and Remote-SSH server builds, served offline at
  Microsoft's own hostnames by nginx and a small gallery container. Same
  pool, same networks, same bundles — see [docs/vscode.md](docs/vscode.md).
- **Two frontends, one engine** — a full Textual TUI and cron-safe headless
  commands with meaningful exit codes.
- **Defensive by default** — upstream-supplied paths are validated against
  directory traversal, unknown config keys are hard errors, and TLS private
  keys are protected in private `.mmw/tls/` deployment directories, outside
  the generated web roots.

## Requirements

- Linux, Python **3.11+**, and [uv](https://docs.astral.sh/uv/).
- A filesystem supporting **hardlinks**, with the pool and your publish roots
  ideally on the same one — otherwise publishing copies instead of linking.
- Disk. A lot of it: read [Sizing](#sizing) before you start.

## Install

```bash
git clone https://github.com/samikool/mirror-mirror-on-the-wall.git
cd mirror-mirror-on-the-wall
uv sync
uv run mmw --help
```

Or install the CLI on its own, without a checkout:

```bash
uv tool install git+https://github.com/samikool/mirror-mirror-on-the-wall.git
mmw --help
```

Everything below uses `uv run mmw`; drop the `uv run` if you installed it as a
tool.

## Quickstart

```bash
mkdir -p ~/mirrors && cd ~/mirrors          # mmw.toml is read from the CWD
uv run mmw init                             # wizard: probe a URL, pick suites/components/arches
uv run mmw manage --quiet                   # check what that expanded to
uv run mmw dry-run                          # what would download, without downloading
uv run mmw sync                             # TUI, live progress (--quiet for cron)
uv run mmw verify                           # does the pool hold everything the indexes want?
uv run mmw publish --dry-run                # read-only estimate
uv run mmw publish --quiet                  # hardlink into the network root
```

Already have the repositories written down? Skip the wizard:

```bash
uv run mmw add -f /etc/apt/sources.list.d --keys-dir ./keyrings-from-that-box
```

Next: **[Getting started](docs/getting-started.md)** walks the same path end to
end, from zero to a client running `apt update` against your mirror.

## Documentation

| Document | What it covers |
|----------|----------------|
| [Getting started](docs/getting-started.md) | Zero → first sync → first published tree → served → verified from a real client |
| [Airgapped operation](docs/airgap.md) | Bundle, transfer, `apply.py`, nginx, DNS, GPG import on the far side, troubleshooting |
| [Configuration](docs/configuration.md) | Every `mmw.toml` key and default, generated from the models |
| [CLI reference](docs/cli.md) | Every command and flag, generated from the CLI |
| [VS Code](docs/vscode.md) | Mirror the VS Code Marketplace, installers and Remote-SSH server builds |

## How clients find the mirror

This is the single most load-bearing design decision in the tool, so it is
worth stating plainly.

A published tree is laid out as:

```
{network.root}/
  archive.ubuntu.com/ubuntu/dists/noble/...
  archive.ubuntu.com/ubuntu/pool/main/...
  ports.ubuntu.com/ubuntu-ports/dists/noble/...
```

That is the upstream URL with the protocol stripped, and **nothing else
changed**. A repository on a non-standard port becomes `{host}_{port}/`, so two
repositories on one host cannot collide on disk.

**Why not rewrite paths into one tree?** Because a `Release` file is signed,
and it names the paths of every index beneath it. `apt` fetches
`dists/noble/InRelease`, checks the signature, and then fetches exactly the
paths that file lists, checking each one's hash. Rewrite a path and you have to
re-sign — with a key the clients do not trust, in a facility where you probably
cannot get one signed. Keep the layout identical and the upstream signature
keeps verifying, unmodified, forever. That is the whole trick: **the mirror is
byte-identical, so the only thing that has to change is which IP the hostname
resolves to.**

### Point DNS at the mirror

One host, quick test — `/etc/hosts` on the client:

```
10.0.0.5   archive.ubuntu.com
10.0.0.5   security.ubuntu.com
10.0.0.5   changelogs.ubuntu.com
```

Site-wide with dnsmasq (`/etc/dnsmasq.d/mirror.conf`):

```
address=/archive.ubuntu.com/10.0.0.5
address=/security.ubuntu.com/10.0.0.5
address=/ports.ubuntu.com/10.0.0.5
address=/changelogs.ubuntu.com/10.0.0.5
```

Site-wide with BIND — a "response policy zone" is cleaner than faking whole
zones, but the blunt version is one zone file per hostname:

```
zone "archive.ubuntu.com" { type master; file "/etc/bind/db.mirror"; };
```

```
; /etc/bind/db.mirror
$TTL 300
@   IN SOA  ns.lab. root.lab. ( 1 3600 600 86400 300 )
@   IN NS   ns.lab.
@   IN A    10.0.0.5
*   IN A    10.0.0.5
```

**Which hostnames do you need?** Exactly the directories in your published
tree — `ls {network.root}` answers it, and the generated
`{network.root}/nginx/mirror.conf` lists them in a comment. With
`dist_upgrade = true`, the generated `UPGRADE.md` lists them too, derived from
that network's own mirrors rather than from a hardcoded list.

### If you cannot spoof DNS

Rewrite `sources.list` on each client to point at the mirror's own hostname,
keeping the upstream path:

```
deb [signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg] \
    http://mirror.lab/archive.ubuntu.com/ubuntu noble main universe
```

This works — the signed `Release` covers paths *relative* to the suite
directory, not the host — but "zero client-side configuration" no longer
applies: every client needs editing, and every client needs re-editing if you
ever move the mirror.

Publish and bundle have the same deployable contents, including `apply.py`,
container images/configs, and TLS/signing assets when enabled. Published trees
can be copied directly to a drive. Publishing never creates a snapshot
automatically; publish snapshots are manual, while bundling current content
creates its snapshot automatically.

## Serving it

With `nginx = true`, publish and bundle include the official `nginx:latest`
Docker image for Linux amd64, `nginx/compose.yaml`, `nginx/mirror.conf`, and
setup instructions in `nginx/README.md`. Configs always use `/srv/mmw` inside
nginx; the host's folder can be anywhere.

From the network's `nginx/` directory on the serving host:

```bash
docker load -i nginx-latest-linux-amd64.tar
cp compose.yaml /path/to/oak-compose.yaml
# Edit the host paths in that deployment copy, then:
docker compose -f /path/to/oak-compose.yaml run --rm nginx nginx -t
docker compose -f /path/to/oak-compose.yaml up -d --force-recreate
```

Compose uses simple read-only bind mounts:

```yaml
volumes:
  - "/path/to/oak:/srv/mmw:ro"
  - "/path/to/oak/nginx:/etc/nginx/conf.d:ro"
```

Replace the host-side paths with your network folder; container paths stay
fixed. Keep the edited deployment copy outside the published folder so
publishes and bundle verification leave it alone. Publish and bundle carry
the same deployment scripts and private TLS assets; no external certificate
mounts or access to the original pool are needed.
Compose uses bridge networking with editable `80:80` and `443:443` port
mappings. Change the left side for other host ports, such as `8080:80`.
VS Code networks include the gallery in the same stack at `gallery:8080`,
without publishing its internal port; load its image too as described in
the generated guide. Compose never pulls on the serving host.

Publish pulls the latest image and caches exports by image ID. Bundles
include it; deltas resend image bytes only when they change, and snapshots
preserve their recorded image. If Docker is unavailable, a cached export is
used with a warning; with no cache, the guide explains how to load the image
separately. `nginx = false` omits the container setup.

Each server block has the real upstream hostname and `autoindex on`. A
`dist_upgrade` network also serves `changelogs.ubuntu.com`; repository keys
remain available at `/mmw-keys/{label}/`. See
[Airgapped operation](docs/airgap.md#4-serve-it) for setup details.

### HTTPS

Sources that say `https://` — most vendor repositories, and Ubuntu's
release-upgrade check — need the mirror to answer on 443 with a certificate
the client trusts, which in an airgap means one signed by your own CA. With
`certs = true` on the network:

```bash
uv run mmw certs -n lab --ca site-ca.crt --ca-key site-ca.key
```

signs a certificate for every hostname the network serves. Both publish and
bundle deployments carry the key, signing request, certificate when available,
and signing tools in private `.mmw/tls/` (key 0600, directory 0700). No
generated web root serves that directory. The same container config enables
HTTPS when the certificate fits the key. A published tree can be copied
directly to a drive and signed offline using its included `sign.py` and
`apply.py --tls`, just like an applied bundle. Source key material is kept in
`{mirror_dir}/certs/`. See [Airgapped operation](docs/airgap.md#https).

## Recommended operational order

```
sync  →  verify  →  publish   (or bundle)
```

`verify` is the step people skip. Publish and bundle enumerate `refs ⋈
packages` — a package the index wants but the pool does not hold is simply
absent from both, with no error. The result applies cleanly, `apply.py --verify`
passes, and the gap only surfaces as a 404 during `apt install` on the far side
of an airgap. `mmw verify` is the read-only check that catches it while you can
still fix it. (`publish` and `bundle` run it themselves too, but running it
separately tells you *before* you start a multi-hour transfer.)

Those checks read the database, not the files. `mmw verify --deep` also stats
every pool file and `--rehash` re-hashes it — slow, so never implicit — and
`--repair` drops any that are damaged so the next `mmw sync` downloads them
again. A bundle refuses to write while the pool holds a damaged file.

For a VS Code mirror with a few missing assets, use
`mmw retry-failed --network lab` to retry its persisted list of failed
downloads. Sync saves only failures, and recovery deletes each entry when
its file is recovered and referenced successfully. Subsequent retries read
only that small list, without scanning every file reference or metadata JSON.
An older mirror without a failure list needs one local discovery scan to
initialize it; a dry run reports gaps but does not save the initial list.
Recovery skips marketplace discovery and leaves existing metadata and other
files intact. Omit `--network` to repair all
previously synced VS Code units; add `--dry-run` to count gaps without fetching.
It uses the normal download retries and server backoffs. A first sync must
have saved usable metadata; an interrupted sync that saved none still needs
a normal sync. Persistent upstream failures return a nonzero exit code.
In the TUI, tick the VS Code sources in Sources and choose **Retry missing**
(or press **r**). With nothing ticked, it retries all saved VS Code units.
The Sync page also has **Retry missing** for the previous session's selection.

To publish or bundle despite missing files, the existing headless
`--allow-incomplete` option reports the gaps and proceeds. Those files will
remain unavailable to clients until repaired.
In the TUI, an incomplete publish or bundle shows the missing-file report
and a **Publish anyway** or **Bundle anyway** button. Proceeding keeps a
warning in the transfer log and completion status. This applies to current
content; snapshot integrity checks still apply when replaying a snapshot.

Every headless command exits non-zero on failure, so cron is the intended
frontend:

```cron
# m  h  dom mon dow   command
  15 2   *   *   *    cd /srv/mirrors && uv run mmw sync --quiet
  30 5   *   *   *    cd /srv/mirrors && uv run mmw verify && uv run mmw publish --quiet
   0 6   *   *   0    cd /srv/mirrors && uv run mmw bundle -n lab
   0 4   1   *   *    cd /srv/mirrors && uv run mmw prune
```

Failures go to stderr and successes to stdout, so a cron job that mails only
stderr tells you what broke and nothing else. See
[the CLI reference](docs/cli.md#exit-codes) for what each code means.

## Sizing

**Read this before leaving `suites` blank.** An empty `suites`, `components` or
`arches` list means *everything upstream serves*, not "a sensible default", and
`archive.ubuntu.com` publishes **55 suites**. A wizard run with everything
ticked is a multi-terabyte instruction.

Order-of-magnitude figures only — they drift constantly, and these are
**estimates**, not measurements. Check `mmw dry-run` for the real number before
committing disk:

| What | Rough size |
|------|-----------|
| Ubuntu noble `main`, amd64, release pocket only | ~100 GB |
| Ubuntu noble `main` + `universe`, amd64, release pocket only | ~1 TB |
| …plus `-updates` and `-security` for that suite | add roughly 30–60% over a release cycle, and it keeps growing |
| …plus a second architecture | add roughly 60–90% (arch-independent packages dedup, arch-specific ones do not) |
| `ports.ubuntu.com` — all five non-x86 architectures | several TB for the same suite/component selection |
| Debian bookworm `main`, amd64 | ~150 GB |
| A vendor repository (Microsoft, NVIDIA, a PPA) | usually 1–50 GB |

Multipliers compound, and they compound *multiplicatively*: suites × components
× architectures. Two suites of main+universe on two architectures is not twice
a terabyte, it is closer to six.

What actually helps:

- **Name your suites.** One release and its `-updates`/`-security` is almost
  always what you want, not all 55.
- **Name your architectures.** `arches = ["amd64"]` instead of blank.
- **Use `suite_overrides`** to take `main` only on `-updates` while taking
  everything on the base suite.
- **Leave `include_sources` off** unless you genuinely need `deb-src` — source
  packages roughly double a suite.
- **Run `mmw dry-run` first.** It reports what "all" expanded to and what it
  would fetch, without downloading a byte.
- **Deduplication is real but it is not magic.** The pool stores each unique
  blob once across every suite, component, architecture and network, and
  publishing costs directory entries rather than bytes. It does not make a
  second architecture's binaries free.

`mmw prune` reclaims pool content no configured mirror's index references any
more — superseded package versions are not kept forever. Narrowing a
repository (dropping a suite, component or architecture from it) stops it
being published right away, and the next prune frees what only it used.

## Moving a mirror to the airgapped side

**Bundling is the supported route.** `mmw bundle` writes one plain `.tar` per
network containing exactly what `publish` would have written, plus a manifest
and a stdlib-only `apply.py` that runs on the far side with no `mmw` install
(Python 3.6+, nothing else). Each re-bundle carries only what changed since
the last one, and `apply.py` refuses to apply bundles out of order.

```bash
uv run mmw bundle -n lab                    # delta since the last bundle
uv run mmw bundle -n lab --full             # send everything
# ...carry lab-20260906T143000Z.tar across...
tar -xf lab-20260906T143000Z.tar .mmw/apply.py   # every bundle carries its own applier
python3 .mmw/apply.py lab-20260906T143000Z.tar --root /srv/www
```

Use `apply.py` rather than plain `tar -x`: it stages each file and renames it
into place, refuses a truncated archive, and records which bundle the tree is
at, so the next delta applies. An interrupted plain extraction leaves a
partial tree and no record of where it stopped.

Full walkthrough, including the far side's nginx, DNS, GPG import and a
troubleshooting table: **[docs/airgap.md](docs/airgap.md)**.

If you copy a published tree by hand instead, **use `rsync -aH --delete`**. A
published tree is entirely hardlinks into the pool — the same package appears
once per suite, component and network that wants it, all pointing at one inode.
`cp -a`, or `rsync` without `-H`, expands every one of those into a separate
full copy, which can multiply the transfer several times over and fill the
destination. `-H` preserves the links; `--delete` gives you the same
stale-file removal that `prune = true` gives a real publish.

## How the pool works

Sync stores everything — packages *and* repository metadata — once,
content-addressed:

```
{mirror_dir}/
  .pool/packages/{xx}/{sha256}-{name}   ← the only copies (packages + indexes)
  .pool/mmw.db                          ← SQLite: blobs + per-mirror desired state
  keys/                                 ← collected GPG keys
  certs/                                ← TLS material, never inside a served tree

{network.root}/                         ← built by publish, the only tree
  deb.debian.org/debian/pool/...        ← hardlinks into the pool
  archive.ubuntu.com/ubuntu/pool/...    ← hardlinks into the pool
```

Syncing records *what each mirror's index wants* (refs) and fills the pool with
*what you have* (blobs). The index flip is one atomic transaction, so a sync
interrupted at any point leaves the previous consistent snapshot in force.
Publishing materializes exactly the selected mirrors as hardlinks — packages
first, release files last, which is safe for a destination being served live.
Re-syncing an unchanged mirror is a metadata fetch plus a database diff:
seconds, no downloads.

There is deliberately **no browsable mirror tree** under `mirror_dir`. Use
`mmw manage` for statistics; trees exist only at publish destinations.

## Architectures, briefly

`arches` is a list, and each entry becomes its own sync unit with the
architecture in the mirror name (`ubuntu-archive-noble-main-arm64`), so
architectures can never collide in the pool. An empty list means "every
architecture this host actually serves", discovered by listing the repository
at sync time.

Ubuntu splits its archive by architecture: `archive.ubuntu.com` serves amd64
and i386, `ports.ubuntu.com` serves arm64, armhf, ppc64el, riscv64 and s390x.
Both ship a *byte-identical* `InRelease` advertising all seven, which is why
mmw lists `binary-*` directories rather than trusting that field. Add both
hosts as sources to mirror everything; each is its own repository, identified
by its own URI. Debian serves every architecture from one host and stays a
single source.

## Development

```bash
uv sync
uv run pytest -n auto            # offline suite, no network, ~30s parallel (~6 min serial)
uv run ruff check src tests
```

Note for anyone poking around: `mirror/` in a working copy may be a **live
multi-terabyte mirror tree**. Do not point recursive tools at the repo root
(pytest and ruff are already scoped away from it).

`src/mmw/resources/apply.py` runs on the far side of the airgap and must stay
stdlib-only and Python 3.6 compatible; `tests/test_apply_script.py` and CI
enforce it.

## License

MIT — see [LICENSE](LICENSE).
