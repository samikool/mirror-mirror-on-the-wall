# mirror-mirror-on-the-wall (mmw)

APT repository mirror for airgapped networks — **apt** (`dists/` hierarchy) and
**apt-flat** (`Packages` at the root) repositories in one tool.

mmw mirrors upstream repositories *exactly*: metadata is copied byte-for-byte so
upstream GPG signatures stay valid, and files land on disk in the same URL
hierarchy as upstream. Point DNS for `archive.ubuntu.com` (etc.) at your mirror
server and clients work with **zero client-side configuration**.

## Highlights

- **Content-addressable pool with hardlink dedup** — a package shared by ten
  suites/components is stored once. SQLite (WAL) index; safe concurrent
  sync + publish.
- **Consistency or nothing** — every index file is verified in memory and a
  mirror's new index becomes visible in one atomic transaction. A CDN caught
  mid-update, a stopped sync, a failed download: the previous consistent
  snapshot stays in force — and you're told so.
- **Exact upstream layout** — published trees are
  `{root}/{hostname}/{path}`, matching upstream URLs byte-for-byte. DNS
  spoofing is the whole client story.
- **Publish networks** — hardlink mirrors into per-network trees for separate
  airgapped enclaves, with stale-file pruning, apt `by-hash` link maintenance,
  GPG key bundles, and optional Ubuntu `do-release-upgrade` support.
- **Two frontends, one engine** — full Textual TUI (parallel sync cards,
  publish pipeline, setup wizard) and cron-safe headless commands with
  meaningful exit codes.
- **Defensive by default** — upstream-supplied paths are validated against
  directory traversal; unknown config keys are hard errors; TLS key material
  lives outside the published tree.

## Install

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <repo> && cd mirror-mirror-on-the-wall
uv sync
uv run mmw --help
```

## Quickstart

```bash
uv run mmw init        # wizard: probe a repository URL → suites/components/arches → settings
uv run mmw sync        # TUI: parallel download with live progress
uv run mmw publish     # TUI: define a network, dry-run, publish
```

Headless equivalents for cron:

```bash
uv run mmw sync --quiet         # exit 1 on any package OR metadata failure
uv run mmw dry-run              # what would download, without downloading
uv run mmw publish --quiet      # publish all networks
uv run mmw publish --dry-run    # read-only publish estimate
uv run mmw prune                # reclaim packages no mirror's index wants
```

## Commands

| Command | What it does |
|---------|--------------|
| `mmw init` | Setup wizard (launches manage if `mmw.toml` exists; `--force` to redo) |
| `mmw manage` | TUI: sources list, detail panel, multi-select sync, add/remove, publish tab |
| `mmw sync` | TUI sync; `--quiet` headless; `--dry-run` check without downloading |
| `mmw dry-run` | Alias for `mmw sync --quiet --dry-run` |
| `mmw publish` | Publish TUI; `--quiet`/`--dry-run` headless; `-n NAME` for one network |
| `mmw add` | Headless import of an apt config file (`-f`, stdin, or `--url`); `--dry-run` prints the TOML without writing |
| `mmw prune` | Delete pool content no configured mirror's index references |
| `mmw bundle` | Headless bundle to a `.tar.zst`; `-n NAME` for one network, `--full` to send everything, `--out` to override `bundle_dir` |
| `mmw certs` | Generate/refresh TLS key + CSR for `certs = true` networks; `-n NAME` for one network |

## Configuration (`mmw.toml`)

Lives in the working directory. Unknown keys are **rejected at load** — typos
fail loudly instead of being silently ignored.

```toml
[settings]
mirror_dir = "/data/repos"     # where everything lands
max_connections = 10           # global download concurrency

# A source is a repository URI. Everything else is optional: name and label
# are derived from the URL when absent, and an empty (or missing) suites /
# components / arches list means "all of them", resolved by listing the
# repository at sync time.
[[sources]]
url = "https://deb.debian.org/debian"
suites = ["bookworm", "bookworm-updates"]
components = ["main", "contrib"]
# arches omitted — mirror every architecture this host serves
# include_sources = true            # also mirror deb-src
# gpg_key_url = "https://…"         # fetched to keys/{label}/ during sync
# [[sources.suite_overrides]]       # different components for one suite
# suite = "bookworm-updates"
# components = ["main"]

# Publish/bundle target — root and bundle_dir are independent optional
# destinations; a network may have either, both, or neither.
[[networks]]
name = "lab"
root = "/srv/lab"
# bundle_dir = "/media/usb"  # destination for the Networks tab's Bundle button
prune = true           # remove destination files no longer in the mirror
# dist_upgrade = true  # Ubuntu do-release-upgrade support
# certs = true         # manage a TLS key + CSR for this network

  # What this network publishes, by dimension — never by mirror name. No
  # [[networks.select]] at all means every mirrored repository; an empty
  # dimension inside one means all of that dimension.
  [[networks.select]]
  source = "https://deb.debian.org/debian"
  suites = ["bookworm"]
  arches = ["amd64"]
  # components = []    # omitted = all mirrored components
```

### Architectures

`arches` is a list — every entry becomes its own sync unit, and the arch is part
of the mirror name (`debian-bookworm-main-arm64`), so arches never collide in
the pool. An empty (or omitted) `arches` means "every architecture this
repository serves", discovered by listing it at sync time — the wizard reports
what that expanded to before anything downloads.

Ubuntu splits its archive by arch: `archive.ubuntu.com` serves amd64 and i386,
while `ports.ubuntu.com` serves everything else (arm64, armhf, ppc64el,
riscv64, s390x) — including their `-security` suites — as two entirely
separate repositories. Add both as sources to mirror every architecture; each
is identified by its own URI and publishes to its own hostname tree. Debian
and Kali serve every arch from one host and stay a single source.

Providers: `apt` (`dists/` hierarchy) and `apt-flat` (`Packages` at the root,
suite `/`) — nothing else exists. The wizard's **Probe URL** / **Paste config**
flow can probe any repository URL, fetch a sources.list or deb822 `.sources`
file by URL, or parse pasted deb822 / one-line sources.list text.

There is no built-in distro catalog. A source is a repository URI (host +
path); `mmw` detects the apt shape and lists what that repository actually
publishes — pasting a repository URL is a first-class path, not a fallback.

## Serving an airgapped network

1. **Sync** on a connected machine — this fills the pool only.
2. **Publish** to a network root: every selected mirror is materialized at
   `{root}/{hostname}/{upstream_path}` as hardlinks — publishing costs
   directory entries, not disk space. Transfer the network root (or the whole
   `mirror_dir` + republish) to the airgapped side.
3. **Serve** with any web server. One nginx block covers every hostname:

   ```nginx
   server {
       listen 80;
       root /srv/lab/$host;
       autoindex on;
   }
   ```

4. **DNS** — point the upstream hostnames (`archive.ubuntu.com`,
   `security.ubuntu.com`, …) at the mirror server. Clients need no changes.
5. **GPG keys** are collected to `{root}/keys/{label}/`, each with an
   `IMPORT.md` walkthrough for first-time import.

Notes for airgapped apt clients: Ubuntu/Debian `Release` files carry a
`Valid-Until` date; once stale, use
`Acquire::Check-Valid-Until "false"` (documented in the generated `IMPORT.md`).
With `dist_upgrade = true`, the publish step also serves
`changelogs.ubuntu.com` meta-release files and writes an `UPGRADE.md` listing
detected LTS upgrade paths — `do-release-upgrade` works fully offline.

TLS: networks with `certs = true` get a 4096-bit key + CSR generated under
`{mirror_dir}/certs/{network}/` (never inside the served tree), with a
`CERTS.md` walking through signing on your CA and the matching nginx config.
Headless `mmw publish --quiet` generates certs for a `certs = true` network
too, after a clean publish — the same thing the TUI's Publish button does, so
a cron-driven publish no longer needs a human at the keyboard to get TLS
material. `mmw certs` runs the same step standalone, for a first cert or a
refresh without a full publish.

## Bundling — moving a network across an airgap

Transferring a network by copying its published tree by hand doesn't scale
past the first sync, and a second transfer a month later has no way to carry
only what changed. `bundle` is the alternative: it packages a network into
**one `.tar.zst` file** containing exactly what `publish` would have written
— packages, indexes, GPG keys, nginx config — plus a manifest and a small
applier script, and each re-bundle carries only the files that changed since
the last one.

There are two doors onto the same pipeline. Headless, for cron:

```bash
uv run mmw bundle                     # bundle every network with a bundle_dir
uv run mmw bundle -n lab              # one network by name
uv run mmw bundle -n lab --full       # send everything, not just the delta
uv run mmw bundle -n lab --out /media/usb   # write here instead of bundle_dir
```

A network with neither `--out` nor a configured `bundle_dir` is skipped with a
message explaining why, rather than aborting the whole run — the same
"keep processing the rest" behaviour `mmw publish` already has for a network
with no `root`.

Or from `mmw manage`'s **Networks** tab, TUI-first:

1. Give the network a **Bundle directory** in the network editor (a second,
   independent destination alongside its publish root — a network can have
   either, both, or neither).
2. Press **Bundle**. A progress modal reports files sent and any deletions,
   and writes `{network}-{timestamp}.tar.zst` plus a plain `apply.py` copy
   next to it in the bundle directory.
3. Copy both files to your transfer media and carry them across the airgap.

On the airgapped side, no `mmw` install is required — `apply.py` is a single
stdlib-only Python 3 script (Python 3.6+, so it works on older RHEL too). It
needs the `zstd` binary on `PATH` and nothing else:

```bash
python3 apply.py lab-20260906T143000Z.tar.zst --root /srv/www
# 20260901T090000Z -> 20260906T143000Z
# 12,481 applied  3,902 removed
```

Run it again with each new bundle as it arrives — `apply.py` refuses to apply
one out of order (it tracks the last-applied bundle in `.mmw-state` at
`--root`), so a skipped transfer is caught rather than silently leaving files
missing. `--dry-run` reports what would change without touching disk;
`--verify` hashes every file the bundle's manifest describes against what's
on disk afterward, so you can confirm a transfer landed intact.

Every bundle carries its own nginx configuration (`nginx/mirror.conf` plus one
file per hostname under `nginx/sites/`) — `apply.py` fills in the destination
path automatically, so serving the result is:

```bash
# after apply.py has written into /srv/www:
ln -s /srv/www/nginx/mirror.conf /etc/nginx/conf.d/mmw-mirror.conf
nginx -t && systemctl reload nginx
```

Then point DNS for each mirrored hostname (`archive.ubuntu.com`,
`security.ubuntu.com`, …) at the serving machine, same as a live publish.
TLS certificates are **never** included in a bundle — `certs = true`
networks keep their key and CSR outside the published tree on the connected
side, and that boundary holds across the airgap too; provision TLS on the far
side separately if you need it.

## How the pool works

Sync stores everything — packages *and* repo metadata — once, content-addressed:

```
{mirror_dir}/
  .pool/packages/{xx}/{sha256}-{name}   ← the only copies (packages + indexes)
  .pool/mmw.db                          ← SQLite: blobs + per-mirror desired state
  keys/                                 ← collected GPG keys

{network.root}/                         ← built by publish, the only tree
  deb.debian.org/debian/pool/...        ← hardlinks into the pool
  archive.ubuntu.com/ubuntu/pool/...    ← hardlinks into the pool
```

Syncing records *what each mirror's index wants* (refs) and fills the pool with
*what you have* (blobs); the index flip is one atomic transaction, so a sync
interrupted at any point leaves the previous consistent snapshot in force.
Publishing materializes exactly the selected mirrors as hardlinks (packages
first, release files last — safe for live-served destinations). Re-syncing an
unchanged mirror is a metadata fetch plus a database diff: seconds, no
downloads, no link storms. `mmw prune` deletes pool content no configured
mirror's index references anymore — superseded versions are reclaimed
automatically.

## Development

```bash
uv run pytest              # offline test suite, no network
uv run ruff check src tests
```

`design-notes.md` documents the architecture in depth (sync engine, publish
pipeline, metadata-ordering invariants, threading model). Note for anyone
poking around: `mirror/` in a working copy may be a **live multi-terabyte
mirror tree** — don't point recursive tools at the repo root (pytest and ruff
are already scoped).

## License

MIT
