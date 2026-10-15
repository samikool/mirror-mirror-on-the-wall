# Getting started

From nothing to a client machine running `apt update` against your mirror.

Everything here is on the **connected** machine unless it says otherwise. For
moving the result across an actual airgap, continue with
[airgap.md](airgap.md).

Before you begin, read [Sizing](../README.md#sizing) in the README. The most
common first mistake is leaving `suites` blank on a distribution archive and
filling a disk.

---

## 0. Install and choose a working directory

```bash
uv tool install git+https://github.com/samikool/mirror-mirror-on-the-wall.git
mkdir -p /srv/mirrors && cd /srv/mirrors
```

`mmw` reads **`mmw.toml` from the current working directory**. There is no
global config: the directory you are standing in *is* which mirror you are
operating on. Every command below assumes you are in `/srv/mirrors`, and so
should every cron entry.

---

## 1. Add your first repository

Two doors. Both produce the same `mmw.toml`.

### The wizard

```bash
uv run mmw init
```

It asks for a mirror directory and a connection limit, then asks what you are
adding — choose **APT repository** and press Enter. Paste a repository URL — `http://archive.ubuntu.com/ubuntu` —
and press **Probe URL**. mmw fetches the repository, works out that it is an
apt repository with a `dists/` hierarchy, and lists what it actually publishes.

- **Suites** open in their own modal, because `archive.ubuntu.com` publishes 55
  of them. **Nothing is preselected** — that is deliberate; 55 preselected
  suites would turn one keypress into a multi-terabyte sync. Type to jump,
  space to toggle, shift+space or shift+click for a range, `ctrl+a` for all.
- **Components** and **architectures** *are* preselected, because the
  repository serves them and the config is a filter you narrow rather than one
  you widen.

Pick `noble`, `noble-updates`, `noble-security`; components `main` and
`universe`; architecture `amd64`. Confirm.

If the URL you paste is not itself a repository — `http://archive.ubuntu.com/`
— mmw offers its subdirectories instead and you descend one level at a time. It
never crawls.

### Or import an apt config you already have

If you have the machine's own apt configuration, no discovery is needed at all
— a `sources.list` or deb822 `.sources` file already states the URL, suites,
components and architecture:

```bash
uv run mmw add -f /etc/apt/sources.list.d/ubuntu.sources
uv run mmw add -f /etc/apt/sources.list.d --keys-dir ~/keyrings   # a whole directory
cat ubuntu.sources | uv run mmw add                              # or stdin
uv run mmw add -f ubuntu.sources --dry-run                       # print the TOML, write nothing
```

Pass a **directory** rather than `cat`-ing files together: mmw joins them with
blank lines itself, which is the one way the deb822 record boundary cannot go
wrong. A file with no trailing newline glued onto the next one has been
measured parsing ten repositories as one.

`--keys-dir` solves the problem the config itself cannot: a deb822 stanza's
`Signed-By:` often names a *path* to a keyring on the machine the config came
from. mmw remembers the basename and matches it against the directory you
point at, recursively, accepting `.gpg`, `.asc`, `.key` and `.pub`. Copy the
keyrings off that machine alongside its `.sources` files and both halves
arrive.

Re-running `mmw add` on the same file is a no-op (`0 added, 0 updated`) —
sources are keyed on the repository URI, so importing twice merges rather than
duplicating.

### Check what you got

```bash
uv run mmw manage --quiet
```

```
Mirror dir : /srv/mirrors/mirror
Connections: 16
Mirrors    : 6

  ubuntu-archive-noble-main-amd64  (apt)
  ubuntu-archive-noble-universe-amd64  (apt)
  ...
```

Six sync units from one source: three suites × two components × one
architecture. That multiplication is the thing to watch.

Full key reference: [configuration.md](configuration.md).

---

## 2. Look before you download

```bash
uv run mmw dry-run
```

This fetches and parses every index — no packages — and reports what it would
download. It also prints what any blank dimension expanded to:

```
  ubuntu-archive: all components → main, multiverse, restricted, universe
  ubuntu-archive: all arches → amd64, i386
```

If that line surprises you, fix the config now. A repository that cannot be
listed prints a `⚠` and makes `dry-run` exit 1 — a sync that silently did
nothing is worse than one that fails.

---

## 3. Sync

```bash
uv run mmw sync            # TUI with a card per mirror, live speed and progress
uv run mmw sync --quiet    # headless, for cron
```

The TUI shows one focusable card per mirror; Enter expands it into a live
detail panel. Card borders tell you the outcome at a glance: green is clean,
yellow means stopped or "metadata was not updated" (with reasons in the
expanded log), red is an error.

Parallel units share package downloads by checksum. When another unit is
already fetching the same content, the duplicate request waits without using
a download slot. After verification, each unit records its own reference;
the downloading unit counts it as downloaded and the others count it as
pooled, with zero transferred bytes. Without an upstream checksum, only
requests for the exact same URL share a download. A queued download rechecks
the pool before fetching, so content added since the index was read is reused.

The footer's **stored** size is the pool's current content size, not a measure
of avoided downloads. **Transferred** measures network bytes, including
retries; pooled requests do not inflate it.

This fills **only the pool**. Nothing browsable appears on disk:

```
/srv/mirrors/mirror/
  .pool/packages/{xx}/{sha256}-{filename}    every blob, stored once
  .pool/mmw.db                               what each mirror's index wants
  keys/{label}/                              collected GPG keys + IMPORT.md
```

Interrupting a sync is safe. New blobs enter the pool invisibly and the index
only flips in a single transaction at the end, so an aborted run leaves the
previous consistent snapshot in force and the next run picks up where it left
off. Re-syncing an unchanged mirror is a metadata fetch and a database diff:
seconds.

Slow? `mmw sync --profile` samples every thread and writes a report on exit. It
is in-process, so it needs no ptrace or root.

---

## 4. Verify

```bash
uv run mmw verify
```

Read-only, safe alongside a running sync, creates nothing. It asks: does the
pool physically hold every blob its mirrors' indexes reference?

Run it. Publish and bundle enumerate `refs ⋈ packages` — a ref whose blob is
missing is invisible to both, so it lands in neither the published tree nor the
bundle, with no error anywhere. The gap only shows up as a 404 during
`apt install`, possibly on the far side of an airgap months later. `verify`
exits 1 if any mirror is incomplete; the fix is another `mmw sync`.

---

## 5. Define a network and publish

A **network** is a named downselect of the mirror — what one site gets.
`publish` and `bundle` are verbs over it.

Add one by hand in `mmw.toml`:

```toml
[[networks]]
name = "lab"
root = "/srv/lab"
prune = true

  [[networks.select]]
  source = "http://archive.ubuntu.com/ubuntu"
  suites = ["noble", "noble-updates", "noble-security"]
  arches = ["amd64"]
```

…or from `mmw manage`'s **Networks** tab, which walks Path & Name →
Repositories → Dimensions → Confirm.

Omitting `select` entirely publishes every mirrored repository. Selection is by
**dimension, never by mirror name**: a frozen list of names silently stops
publishing a new architecture the moment one appears upstream.

Then:

```bash
uv run mmw publish --dry-run     # read-only: what would be linked, and how much
uv run mmw publish --quiet       # do it
```

You now have a tree:

```
/srv/lab/
  archive.ubuntu.com/ubuntu/dists/noble/...
  archive.ubuntu.com/ubuntu/pool/main/...
  keys/ubuntu/ubuntu-archive-keyring.asc   (+ .gpg, + IMPORT.md)
  nginx/mirror.conf                         (one file, a server block per host)
```

Every package file there is a **hardlink** into the pool, so this cost
directory entries rather than disk space. It also means you must use
`rsync -aH` if you ever copy this tree by hand — see
[the README](../README.md#moving-a-mirror-to-the-airgapped-side).

`prune = true` deletes destination files the current index no longer wants, so
the tree keeps matching the mirror exactly instead of accumulating every
package version ever published.

---

## 6. Serve it

`publish` already wrote the nginx configuration. Hook it up:

```bash
sudo apt install nginx
echo 'include /srv/lab/nginx/mirror.conf;' | sudo tee /etc/nginx/conf.d/mmw.conf
sudo nginx -t && sudo systemctl reload nginx
```

`mirror.conf` is one file with a server block per hostname, each with the
correct `server_name`, the correct `root`, and `autoindex on`. Its header
comment lists the hostnames this network serves — you will need them in the
next step. For HTTPS, see [the README](../README.md#https).

Check it locally before involving DNS:

```bash
curl -s -H 'Host: archive.ubuntu.com' \
  http://localhost/ubuntu/dists/noble/InRelease | head -5
```

Prefer your own configuration? Set `nginx = false` on the network. If you do,
remember to serve `{root}/keys/` and (with `dist_upgrade`) `{root}/UPGRADE.md`
— they live at the network root, not under any hostname directory.

---

## 7. Point a client at it

On the **client**, make the upstream hostnames resolve to the mirror. For one
machine, `/etc/hosts` is enough:

```
10.0.0.5   archive.ubuntu.com
10.0.0.5   security.ubuntu.com
```

Site-wide, use dnsmasq or BIND — worked examples in
[the README](../README.md#point-dns-at-the-mirror).

Nothing about the client's `sources.list` changes. The published tree is
byte-identical to upstream, so the signed `Release` still describes exactly the
paths that exist, and the upstream signature still verifies.

```bash
sudo apt update
```

The keys are already trusted on a normal Ubuntu or Debian install. On a machine
that does not have a vendor's key, take it from `{root}/keys/{label}/` — each
directory ships both encodings and an `IMPORT.md`:

```bash
sudo cp ubuntu-archive-keyring.gpg /usr/share/keyrings/
# then reference it: deb [signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg] ...
```

### If `apt update` complains the release is not valid yet / expired

`Release` files carry a `Valid-Until` date, and an airgapped mirror eventually
passes it:

```bash
echo 'Acquire::Check-Valid-Until "false";' | \
  sudo tee /etc/apt/apt.conf.d/99airgap
```

This is documented in every generated `IMPORT.md` too. It disables one specific
freshness check; the GPG signature is still verified.

### If a package 404s

The index knows about a package the tree does not hold. Run `mmw verify` on the
connected side — that is exactly what it catches — then sync and publish again.
If it reports the pool complete but a client still gets a bad file,
`mmw verify --deep --repair` finds pool files that are damaged on disk and drops
them, and the next sync downloads them again.

---

## 8. Keep it fresh

```cron
  15 2  * * *   cd /srv/mirrors && uv run mmw sync --quiet
  30 5  * * *   cd /srv/mirrors && uv run mmw verify && uv run mmw publish --quiet
   0 4  1 * *   cd /srv/mirrors && uv run mmw prune
```

`sync → verify → publish` is the order that matters. Every command exits
non-zero on failure and writes failures to stderr, so a cron job mailing only
stderr tells you what broke.

`mmw prune` deletes pool content no configured mirror's index references any
more. Published trees keep their own hardlinks, so pruning does not pull the
rug out from under a served tree.

### Snapshots: roll a network back

A snapshot is a network's exact published state, kept in the pool: every
package, index, key and mirrored file it served. Take one before anything you
might want to undo:

```bash
mmw snapshot take -n lab --label pre-upgrade
mmw snapshot list -n lab
mmw publish -n lab --snapshot pre-upgrade         # serve that state again, exactly
mmw publish -n lab --snapshot live                # back to the current state
mmw publish -n lab --snapshot pre-upgrade --root /srv/www-old   # beside the live one
```

Publishing into another directory writes an nginx config for that directory
with the same hostnames, so serve it on its own address. Every bundle also
takes one (see [airgap](airgap.md#between-any-two-snapshots)).

`mmw prune` never deletes a file a snapshot lists; `mmw snapshot delete` /
`clean --keep N` let it go (the next prune frees it). A snapshot some directory
serves cannot be deleted. Renaming a network carries its snapshots along, and
removing it in the TUI removes them; a network deleted from `mmw.toml` by hand
leaves them behind — `mmw prune` names it, and
`mmw snapshot clean -n OLD --keep 0 --yes` clears them. In the TUI: `n` on a network opens its snapshots, and
`p` asks what to publish and where.

---

## Next

- **[Airgapped operation](airgap.md)** — bundling, transfer, `apply.py`, and
  the far side end to end.
- **[Configuration](configuration.md)** — every key, generated from the models.
- **[CLI reference](cli.md)** — every command and flag.
