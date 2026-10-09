# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### 1.0 is gated on a real hardware round trip

The bundling path — bundle, transfer, `apply.py`, serve — is covered end to end
by the test suite (a real bundle is produced, `apply.py` runs as a subprocess,
and the result is diffed against a directly-published tree, across a delta),
but it has never run against a genuinely airgapped site. 1.0 is tagged after
that, not before.

## [0.9.0]

First public release. Prior history was not versioned.

### Added

- `mmw certs --ca CA.crt --ca-key CA.key [--days N]` signs each network's CSR
  with your CA and writes `mirror.crt` (an intermediate CA is appended as the
  chain). An encrypted CA key is prompted for, or read from
  `MMW_CA_PASSPHRASE`.
- HTTPS in the generated nginx config: once a network has a signed
  certificate, every server block also listens on 443. A certificate that
  does not match `mirror.key` is never wired in — nginx would refuse to start.
- `bundle_tls = true` (requires `certs`) carries the signed certificate and
  private key in every bundle; `apply.py` installs them 0600 under
  `{root}/.mmw/tls/` and the bundle's nginx config points at them. A
  `bundle_tls` bundle without a usable certificate refuses to start.
- `mmw certs` (and every `certs = true` publish) checks the signed certificate
  and reports, on stderr, names it no longer covers, a key mismatch, or expiry
  within 30 days.
- Repository signing keys reach clients from the mirror: every host serves
  `{root}/keys/` at `/mmw-keys/`, and a key fetched from a `gpg_key_url` is
  also published byte-for-byte at that URL's own path (a host that serves
  only a key gets a server block and a certificate name). A source with both
  an inline `gpg_key` and a `gpg_key_url` now fetches the URL too.
- The TUI edits every `mmw.toml` setting: a Settings screen (`,`) for
  `mirror_dir` and `max_connections`; repository URL, type, key group, key
  URL and per-suite components in the repository editor; and network rename
  (its certs and bundle ledger move with it) and `bundle_tls` in the network
  editor and add wizard.
- `mmw bundle`, `mmw certs` and `mmw verify` as headless commands, completing
  the verb surface — every verb now has both a TUI and a cron-safe door.
- `mmw --version`, and a global `--config PATH` so the config need not be in
  the working directory.
- `mmw prune --dry-run` and `--yes`; an interactive prune now asks before
  deleting.
- `--allow-incomplete` on `publish` and `bundle`, for knowingly shipping a
  mirror the pool cannot fully serve.
- `mmw sync --profile` now works with `--quiet` (it was silently a no-op on the
  headless run, which is the one worth profiling).
- Resumable package downloads: a retry continues with a `Range:` request rather
  than starting a multi-gigabyte file again.
- A free-space check before the download phase.
- A `.sha256` sidecar beside every bundle, so the far side can tell a bad
  transfer from a bad bundle without `mmw`.
- Cross-process pool locking: `prune` and `wipe` refuse while a sync is
  running. The lock is process-wide and acquired off the event loop — taking
  it per mirror, inline in `download_mirror`, froze the sync TUI outright.
- A help screen (`?`) and find-as-you-type (`/`) in the manage TUI.
- User documentation: getting started, airgap, configuration, CLI reference.
- CI, and a `py_compile` job keeping `apply.py` runnable on old interpreters.

### Fixed

- A `dist_upgrade` network published `changelogs.ubuntu.com/` but the nginx
  config had no server block for it, so it was never served.
- Editing a network in the TUI rebuilt it from the fields on screen, dropping
  any it does not show.
- The generated `CERTS.md` told you to hand-edit nginx files that are
  regenerated on every publish, named a site-specific CA file, and gave the
  wrong OpenSSL version for `-copy_extensions` (3.0, not 1.1.1).
- **Security: a repository could write outside the pool.** A checksum from an
  upstream `Release` file was used verbatim as a path component; only its
  *size* field had ever been validated. Checksums are now required to be hex
  digests.
- **An empty metadata set erased a mirror's index.** A CDN 503 on both
  InRelease and Release produced an empty list, which counted as a clean index
  flip and reconciled the mirror's refs down to packages alone — at exit 0. The
  Release fetch now reports failure instead of returning a short list, and an
  empty set can no longer commit.
- **`mmw prune` with no configured sources deleted the whole pool.** Refused
  outright: an empty source list cannot prove anything is unwanted.
- **`mmw sync --quiet` and `mmw dry-run` exited 0 when every mirror failed.**
- **Headless publish and bundle skipped the completeness check the TUI ran**,
  so cron could ship a bundle whose missing package only surfaced as a 404
  during `apt install` on the far side of the airgap. For publish it also meant
  pruning a live tree against an under-complete keep set.
- **`merge_sources()` narrowed "all" to whatever was imported.** Importing one
  `.sources` file into a repository mirroring every suite silently reduced it
  to one.
- **`config.save()` could write a file `config.load()` rejects**, locking the
  user out of every command.
- **A mixed `sources.list.d/` imported only the deb822 files**, silently
  dropping every one-line `.list` repository.
- Several apt-parser defects: `./` and `stable/` flat repositories parsed as
  suites, a trailing `#` comment became components, `signed-by=` was discarded
  from one-line entries, `$(ARCH)` was not resolved in deb822 URIs, and a
  separate `deb-src` stanza did not set `include_sources`.
- **`mmw bundle --out <existing file>` spun forever** at 100% CPU.
- **By-hash metadata was pooled without verifying it**, permanently, against
  the checksum already in hand.
- **A mirror's package list and its published index could come from different
  upstream snapshots.** `AptProvider` enumerated packages from the canonical
  `Packages.xz` while verifying metadata against `InRelease`, so a republish
  between the two GETs published an index listing packages that were never
  enumerated, never downloaded, and 404 for every client. Both apt providers
  now override `fetch_all` to read the index once, by hash, and reuse those
  bytes — which also stops the largest metadata file in a mirror being
  downloaded twice on every sync.
- **A pre-fetched index could be filed as a trust root.** The release/index
  split was inferred from which fields happened to be set; it is now declared
  (`MetadataFile.is_release`), so `Packages.xz` cannot be published last
  alongside `InRelease`.
- Deferred pool-writer errors were collected and never read, so a failed commit
  silently dropped up to 500 package rows.
- Dropping a repository from a network never pruned its subtree, its nginx site
  file or its keys — all three were served indefinitely.
- Duplicate network names were accepted and then crashed the Networks tab with
  a widget-ID collision.
- `EditSourceScreen` silently dropped a custom `label`.
- Wipe Pool ran `shutil.rmtree` on the event loop and told the user their data
  was being kept.
- Decompression is now bounded, so a crafted index cannot exhaust memory.
- **Eight places did blocking disk I/O on the Textual event loop**, found by
  profiling a sync that locked up and then sweeping for the pattern: the pool
  lock (per mirror, the freeze itself), `Pool()` construction in
  `SyncApp.on_mount`, `_run_pre_publish` and the prune action, `config.save`
  and `config.delete`, `keys.read_key` in the key modal, `shutil.rmtree` of a
  bundle ledger, and the last-bundled lookup on every network highlight. A test
  now fails on any direct blocking call from an event-loop handler.

### Changed

- nginx config is one self-contained `nginx/mirror.conf` instead of a
  `mirror.conf` plus a `sites/` folder. Debian's `include sites-enabled/*`
  matched the folder and nginx refused to start ("is a directory"). A pruning
  publish or bundle removes the old `sites/` files.
- A `dist_upgrade` network's certificate covers `changelogs.ubuntu.com`.
- `requires-python` lowered from 3.13 to **3.11** — nothing in the source
  needed more, and 3.13 excluded Ubuntu 24.04 and RHEL 9, the distributions
  this tool exists to mirror.
- `textual` and `pydantic` are now upper-bounded.
- Publish asks for confirmation before deleting files from a destination.
- `mmw publish` and `mmw bundle` now agree on skip semantics: naming a network
  with `-n` that has no destination is an error; meeting one while iterating
  every network is not.
- Errors go to stderr across every command.
- Destructive TUI keys moved behind modifiers; `?` and `/` gained bindings.
- `design-notes.md` moved to `docs/internals/design-notes.md`.

### Removed

- `prune_old_mirrors.py`, an obsolete migration script that against a current
  config proposed `rm -rf` on every top-level directory in `mirror_dir`.
