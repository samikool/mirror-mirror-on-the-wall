# CLI reference

Every command and option, generated from the typer app itself.

> **Generated file — do not hand-edit.**
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
| [`mmw add`](#mmw-add) | Add sources from a sources.list or deb822 .sources file |
| [`mmw bundle`](#mmw-bundle) | Write a network's mirrors to a transferable bundle |
| [`mmw certs`](#mmw-certs) | Generate (or refresh) TLS key/CSR material for `certs = true` networks, and sign it with your CA |
| [`mmw dry-run`](#mmw-dry-run) | Show what would be downloaded without fetching anything |
| [`mmw init`](#mmw-init) | Setup wizard |
| [`mmw manage`](#mmw-manage) | Manage configured mirrors (add, remove) |
| [`mmw prune`](#mmw-prune) | Remove orphaned pool packages not referenced by any mirror |
| [`mmw publish`](#mmw-publish) | Publish mirrors to network targets |
| [`mmw sync`](#mmw-sync) | Sync configured mirrors |
| [`mmw verify`](#mmw-verify) | Check the pool holds every blob its mirrors' refs want (read-only) |

### `mmw add`

Add sources from a sources.list or deb822 .sources file.

Everything is taken from the file — no discovery, no prompts:

  mmw add -f /etc/apt/sources.list.d/ubuntu.sources
  cat ubuntu.sources | mmw add
  mmw add --url https://example.com/repo.list --dry-run
  mmw add -f /etc/apt/sources.list.d --keys-dir /path/to/keyrings

| Option | Default | Description |
|--------|---------|-------------|
| `--file`, `-f` |  | apt config file(s) or a directory of them ('-' for stdin). Repeatable; joined with blank lines so records never run together. Defaults to stdin. |
| `--url` |  | Fetch the config file from a URL. |
| `--label` |  | Override the key-grouping label (default: the host's domain name). |
| `--arch` |  | Architecture to use when the file does not state one (default: leave unset — every architecture upstream serves). |
| `--keys-dir` |  | Directory of keyrings (searched recursively). A `Signed-By:` path is matched by basename; .gpg and .asc are both accepted. |
| `--dry-run` |  | Print the resulting [[sources]] TOML without writing. |

### `mmw bundle`

Write a network's mirrors to a transferable bundle.

| Option | Default | Description |
|--------|---------|-------------|
| `--network`, `-n` |  | Bundle one network by name |
| `--full` |  | Send the whole network, not just the delta |
| `--out`, `-o` |  | Write here instead of the network's bundle_dir |
| `--allow-incomplete` |  | Bundle even if the pool cannot serve everything the indexes promise. The gap is invisible in the bundle and surfaces as a 404 during `apt install` on the far side, so you are told exactly what is missing. |

### `mmw certs`

Generate (or refresh) TLS key/CSR material for `certs = true` networks,
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

### `mmw prune`

Remove orphaned pool packages not referenced by any mirror.

| Option | Default | Description |
|--------|---------|-------------|
| `--dry-run` |  | Report what would be removed without deleting anything |
| `--yes`, `-y` |  | Skip the confirmation prompt (implied when not a TTY) |

### `mmw publish`

Publish mirrors to network targets. Without --quiet opens the TUI.

| Option | Default | Description |
|--------|---------|-------------|
| `--quiet`, `-q` |  | Headless publish — no TUI |
| `--network`, `-n` |  | Publish a specific network by name |
| `--dry-run` |  | Show what would be linked without writing |
| `--allow-incomplete` |  | Publish even if the pool cannot serve everything the indexes promise (you are still told what is missing). |

### `mmw sync`

Sync configured mirrors. Use --quiet for cron jobs.

| Option | Default | Description |
|--------|---------|-------------|
| `--quiet`, `-q` |  | No TUI — plain output for cron/headless use |
| `--dry-run` |  | Show what would be downloaded without fetching |
| `--profile` |  | Sample every thread's stack and CPU use; write a report on exit. In-process, so it needs no ptrace/root. |

### `mmw verify`

Check the pool holds every blob its mirrors' refs want (read-only).

| Option | Default | Description |
|--------|---------|-------------|
| `--network`, `-n` |  | Verify only this network's mirrors |
