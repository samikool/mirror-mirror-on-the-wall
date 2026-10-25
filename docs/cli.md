# CLI reference

Every command and option, generated from the typer app itself.

> **Generated file — do not hand-edit.** Regenerate after any CLI change:
> `uv run python docs/tools/gen_cli_docs.py`
>
> Run any command with `--help` for the same information in the terminal.

## Global options

These come before the command (`mmw --config /etc/mmw verify`):

| Option | What it does |
|--------|--------------|
| `--config DIR`, `-c` | Directory holding `mmw.toml`. Defaults to the working directory. |
| `--version`, `-V` | Print the version and exit. The far side of an airgap has no `git log`, so this is how a bug report identifies a build. |
| `--help` | Show help and exit. |

Without `--config`, every command reads `mmw.toml` from the **current working
directory** — so a cron entry either passes `--config` or `cd`s first.

## Exit codes

`mmw` is built to be run from cron, so the exit code is the contract — a
command that quietly did nothing is worse than one that fails.

| Code | Meaning |
|------|---------|
| `0` | Success. For `sync`, every mirror's packages *and* metadata landed. |
| `1` | Something failed: a download or metadata error, a repository that could not be listed, an incomplete mirror, an unwritable destination. |
| `2` | Usage error (bad flag, missing argument) — typer's own. |

`apply.py`, which runs on the far side of an airgap, has its own finer-grained
codes — see [airgap.md](airgap.md#exit-codes).

## Commands

| Command | What it does |
|---------|--------------|
| [`mmw add`](#mmw-add) | Add sources from a sources.list or deb822 .sources file — or the VS Code source |
| [`mmw bundle`](#mmw-bundle) | Write a network's mirrors to a transferable bundle — between any two snapshots (see `mmw snapshot list`) |
| [`mmw stage`](#mmw-stage) | Lay a bundle out as a directory of hardlinks, for networks whose far side runs mmw |
| [`mmw site`](#mmw-site) | mmw on the far side: apply bundles, serve any kept version |
| [`mmw certs`](#mmw-certs) | Generate (or refresh) TLS key/CSR material for TLS networks (the default), and sign it with your CA |
| [`mmw dry-run`](#mmw-dry-run) | Show what would be downloaded without fetching anything |
| [`mmw init`](#mmw-init) | Setup wizard |
| [`mmw manage`](#mmw-manage) | Manage configured mirrors (add, remove) |
| [`mmw move-database`](#mmw-move-database) | Move the pool database (settings.database) and point mmw.toml at it |
| [`mmw prune`](#mmw-prune) | Remove orphaned pool packages not referenced by any mirror |
| [`mmw publish`](#mmw-publish) | Publish mirrors to network targets |
| [`mmw retry-failed`](#mmw-retry-failed) | Retry missing VS Code files from saved metadata, without a full sync |
| [`mmw snapshot`](#mmw-snapshot) | Network snapshots: a network's exact state, kept to bundle to or publish again |
| [`mmw sync`](#mmw-sync) | Sync configured mirrors |
| [`mmw verify`](#mmw-verify) | Check the pool holds every blob its mirrors' refs want (read-only unless --repair) |

### `mmw add`

Add sources from a sources.list or deb822 .sources file — or the VS Code source.

Everything is taken from the file — no discovery, no prompts:

  mmw add -f /etc/apt/sources.list.d/ubuntu.sources
  cat ubuntu.sources | mmw add
  mmw add --url https://example.com/repo.list --dry-run
  mmw add -f /etc/apt/sources.list.d --keys-dir /path/to/keyrings
  mmw add vscode --kind extensions --top-n 200 --platform linux-x64
  mmw add pypi --python 3.12 --platform linux-glibc --arch amd64 --all

| Option | Default | Description |
|--------|---------|-------------|
| `WHAT` |  | `vscode` or `pypi` to add (or replace) that source; omit for apt files. |
| `--file`, `-f` |  | apt config file(s) or a directory of them ('-' for stdin). Repeatable; joined with blank lines so records never run together. Defaults to stdin. |
| `--url` |  | Fetch the config file from a URL. |
| `--label` |  | Override the key-grouping label (default: the host's domain name). |
| `--arch` |  | apt: the architecture to use when the file does not state one (default: leave unset — every architecture upstream serves). pypi: amd64 or arm64 (repeatable; default both). |
| `--keys-dir` |  | Directory of keyrings (searched recursively). A `Signed-By:` path is matched by basename; .gpg and .asc are both accepted. |
| `--dry-run` |  | Print the resulting [[sources]] TOML without writing. |
| `--kind` |  | vscode: extensions, desktop, server (repeatable; default all). |
| `--platform` |  | vscode: target platform, e.g. linux-x64; pypi: linux-glibc, linux-musl, windows, macos (repeatable; default all). |
| `--quality` |  | vscode: stable or insider (repeatable; default both). |
| `--top-n` |  | vscode: the N most-installed extensions. |
| `--id` |  | vscode: an extension id, publisher.name (repeatable). |
| `--ids-file` |  | vscode: a file of extension ids, one per line. |
| `--all` |  | vscode: every extension on the marketplace; pypi: every project, latest + dependencies (both very large). |
| `--prerelease` |  | vscode: allow prerelease versions. |
| `--python` |  | pypi: a Python version, e.g. 3.12 (repeatable; default all). |
| `--glibc` |  | pypi: oldest far-side glibc (default 2.31). |
| `--musl` |  | pypi: oldest far-side musl (default 1.2). |
| `--macos` |  | pypi: oldest far-side macOS (default 11.0). |
| `--sdists` |  | pypi: also mirror every kept release's sdist. |
| `--req` |  | pypi: a requirement to resolve with the others, e.g. 'numpy<2' (repeatable). |
| `--requirements-file` |  | pypi: a requirements.txt-style file, relative to mmw.toml. |

### `mmw bundle`

Write a network's mirrors to a transferable bundle — between any two
snapshots (see `mmw snapshot list`). By default: from where the last
bundle went, to a new snapshot of now.

| Option | Default | Description |
|--------|---------|-------------|
| `--network`, `-n` |  | Bundle one network by name |
| `--out`, `-o` |  | Write here instead of the network's bundle_dir. `-` streams the tar to stdout (needs `-n`). |
| `--allow-incomplete` |  | Bundle even if the pool cannot serve everything the indexes promise. The gap is invisible in the bundle and surfaces as a 404 during `apt install` on the far side, so you are told exactly what is missing. |
| `--from` |  | Snapshot the far side is at (stamp, label or id), or 'none' for a full bundle. Default: where the last bundle went. |
| `--to` |  | Snapshot to bring it to (stamp, label or id). Default: a new snapshot of now. |
| `--repo` |  | Only this repository (URL or name; repeatable). Needs `-n` and `far_side = "mmw"`. |
| `--installer` |  | Include mmw installer: write `mmw-<version>-<arch>.tar` beside it, for a far side to install mmw from. |
| `--runtime-wheel` |  | The mmw wheel the installer ships (default: this checkout's own build, or PyPI's for a release). |

### `mmw stage`

Lay a bundle out as a directory of hardlinks to the pool — the same members
as `mmw bundle`, almost no disk — to tar, rsync or inspect without mmw. For
networks whose far side runs mmw (`far_side = "mmw"`).

| Option | Default | Description |
|--------|---------|-------------|
| `--network`, `-n` |  | The network to stage (required) |
| `--out`, `-o` |  | Directory to stage into instead of the network's bundle_dir |
| `--allow-incomplete` |  | Stage even if the pool cannot serve everything the indexes promise. |
| `--from` |  | Snapshot the far side is at (stamp, label or id), or 'none' for everything. Default: what was last sent of each repository. |
| `--to` |  | Snapshot to bring it to. Default: a new snapshot of now. |
| `--repo` |  | Only this repository (URL or name; repeatable). |
| `--installer` |  | Include mmw installer: write `mmw-<version>-<arch>.tar` beside it, for a far side to install mmw from. |
| `--runtime-wheel` |  | The mmw wheel the installer ships (default: this checkout's own build, or PyPI's for a release). |

### `mmw installer`

Write mmw's installer on its own: one `mmw-<version>-<arch>.tar` per
architecture. On the far side, `tar -xf mmw-….tar && sh mmw-…/install.sh`
puts mmw on the path (`~/.local/bin`, or `--prefix DIR`), offline. From a
source checkout it ships that checkout's own build, never PyPI's.

| Option | Short | Description |
|---|---|---|
| `--out` | `-o` | Directory to write the installer(s) into (required). |
| `--network` | `-n` | Take the architectures and Python from this network's `runtime_arches` and `runtime_python`. |
| `--runtime-wheel` |  | The mmw wheel to ship. |

### `mmw site`

mmw on the far side of the airgap, for networks with `far_side = "mmw"`.
Every command takes `--root PATH` (any root of the mirror; by default the
mirror this machine has opened); the ones that change a root take
`--break-lock`. See [far-side.md](far-side.md).

| Command | What it does |
|---|---|
| `mmw site apply SRC [--root HOST=PATH]… [--forward REPO]… [--forward-all] [--dry-run]` | Apply a bundle (`.tar`, stage directory, or `-` for stdin); a new hostname goes to the root at PATH (`'*'=PATH`: every new one) |
| `mmw site open PATH` / `mmw site roots` | Remember a root on this machine / the mirror's roots, their hostnames, or MISSING |
| `mmw site manage` | The far-side TUI: repositories, their history, applying bundles |
| `mmw site status` | What each repository serves, the newest kept, held or current |
| `mmw site history REPO` | A repository's kept versions, sizes, what each would free |
| `mmw site serve REPO VERSION [--yes]` | Serve any kept version; anything but the newest holds it |
| `mmw site hold REPO` / `mmw site forward [REPO] [--all]` | Hold a repository / move it to its newest version |
| `mmw site move REPO ROOTNAME [--yes]` | Move a repository, every kept version, to another root of the mirror |
| `mmw site tidy` | Clear moved repositories' old copies, once nginx has been recreated |
| `mmw site ca load --cert C --key K` / `ca make --out DIR [--name N] [--encrypt]` / `ca show` | The CA, recorded by path only, on this machine |
| `mmw site certs [--days N] [--force]` | Sign every root's certificate with the site's CA; HTTPS on |
| `mmw site space` | Each version's size and what deleting it frees; the root's free space |
| `mmw site delete REPO VERSION [--yes]` | Delete a kept version (never the served one, nor a held repository's newest) |
| `mmw site settings [--keep N \| --keep-all]` | How many versions of each repository to keep |
| `mmw site verify [--repair]` | Hash every served file; exit 4 on a mismatch; `--repair` puts them back first |

### `mmw certs`

Generate (or refresh) TLS key/CSR material for TLS networks (every network unless `certs = false`),
and sign it with your CA.

  mmw certs                                  # key + CSR, and check any signed cert
  mmw certs -n lab --ca ca.crt --ca-key ca.key

| Option | Default | Description |
|--------|---------|-------------|
| `--network`, `-n` |  | Generate certs for a specific network by name |
| `--dry-run` |  | Report what would be generated without writing any key material. |
| `--ca` |  | CA certificate to sign with (PEM). With --ca-key, writes mirror.crt; the next publish/bundle then serves HTTPS. |
| `--ca-key` |  | The CA's private key (PEM). Prompted for its passphrase if encrypted, or set MMW_CA_PASSPHRASE. |
| `--days` | `730` | Validity of the signed certificate. |

### `mmw dry-run`

Show what would be downloaded without fetching anything.

Takes no options.

### `mmw init`

Setup wizard. Launches manage if config already exists.

| Option | Default | Description |
|--------|---------|-------------|
| `--mirror-dir` | `mirror` | Mirror storage directory |
| `--max-connections` | `10` | Concurrent download connections |
| `--force` |  | Re-initialize even if config exists |

### `mmw manage`

Manage configured mirrors (add, remove).

| Option | Default | Description |
|--------|---------|-------------|
| `--quiet`, `-q` |  | Print mirror list to stdout, no TUI |
| `--profile` |  | Sample every thread's stack and CPU use; write a report on exit. In-process, so it needs no ptrace/root. Covers the whole session (publish, bundle, sync-from-manage), not just one action. |

### `mmw move-database`

Move the pool database (settings.database) and point mmw.toml at it.

| Option | Default | Description |
|--------|---------|-------------|
| `PATH` |  | New path for the pool database file, e.g. on a fast disk *(required)* |

### `mmw prune`

Remove orphaned pool packages not referenced by any mirror.

| Option | Default | Description |
|--------|---------|-------------|
| `--dry-run` |  | Report what would be removed without deleting anything |
| `--yes`, `-y` |  | Skip the confirmation prompt (implied when not a TTY) |

### `mmw publish`

Publish mirrors to network targets. Without --quiet opens the TUI
(--snapshot / --root / --recheck / --take-snapshot always run headless).

| Option | Default | Description |
|--------|---------|-------------|
| `--quiet`, `-q` |  | Headless publish — no TUI |
| `--network`, `-n` |  | Publish a specific network by name |
| `--dry-run` |  | Show what would be linked without writing |
| `--allow-incomplete` |  | Publish even if the pool cannot serve everything the indexes promise (you are still told what is missing). |
| `--snapshot`, `-s` |  | Publish this snapshot (stamp, label or id — see `mmw snapshot list`) instead of the current state; 'live' is the current state. Needs -n. |
| `--root` |  | Publish into this directory instead of the network's root. Its nginx config points there with the same hostnames, so serve it on its own address. Needs -n. |
| `--recheck` |  | Ignore the record of the last publish and recheck every published file. |
| `--take-snapshot` |  | Take a snapshot of the network first. |

### `mmw retry-failed`

Retry missing VS Code files from saved metadata, without a full sync.

Runs headless. Uses the existing snapshot and HTTP retry/backoff policy;
does not refresh the marketplace or update metadata.

| Option | Default | Description |
|--------|---------|-------------|
| `--network`, `-n` |  | Limit recovery to this network |
| `--dry-run` |  | Count missing files without downloading |

### `mmw snapshot`

Network snapshots: a network's exact state, kept to bundle to or publish again.

Takes no options.

### `mmw sync`

Sync configured mirrors. Use --quiet for cron jobs.

| Option | Default | Description |
|--------|---------|-------------|
| `--quiet`, `-q` |  | No TUI — plain output for cron/headless use |
| `--dry-run` |  | Show what would be downloaded without fetching |
| `--profile` |  | Sample every thread's stack and CPU use; write a report on exit. In-process, so it needs no ptrace/root. |

### `mmw verify`

Check the pool holds every blob its mirrors' refs want (read-only unless --repair).

Results include phase timings. VS Code verification separates bulk pool lookup,
metadata reads, and asset checks; all providers report reference checks and,
when requested, physical blob checks. Sync separately reports package pool
lookup and classification, metadata lookup, verification and writes, and its
final pool reference commit. These sync stages are independent of the
pre-publish completeness verifier.

The final sync reference commit retains unchanged rows and writes only added,
removed, or changed references. It still reads the mirror's reference set and
waits for queued blob writes and other writer work. Sync finishes only after
the transaction commits, so publish and bundle see a complete old or new state.
First syncs and full replacements still require substantial database writes.

| Option | Default | Description |
|--------|---------|-------------|
| `--network`, `-n` |  | Verify only this network's mirrors |
| `--stat` |  | Also stat every blob (slow: one stat per file) |
| `--hash` |  | Also re-hash every blob (slowest: reads the whole pool) |
| `--repair` |  | Drop damaged blobs so the next sync downloads them again (implies --stat) |
