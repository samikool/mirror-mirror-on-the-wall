# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0]

The first stable release. Upgrading from 0.10: if you synced in the TUI,
run `mmw verify --deep --repair` once and then `mmw sync` (see the first
Fixed entry); a config whose network `root` overlaps the mirror directory or
another network's root is now refused at load and says which.

### Added

- `mmw verify --deep` stats every pool file and `--rehash` re-hashes it — the
  only checks that see a file damaged on disk. `--repair` drops damaged files
  so the next `mmw sync` downloads them again. Run `mmw verify --repair` once
  after upgrading if you synced in the TUI before (see the first Fixed entry).

### Changed

- The release-upgrade tool tarballs (`dist-upgrader-all/`) are mirrored only
  for a source with `dist_upgrade = true`, like `meta-release`. Every Ubuntu
  `main` mirror used to carry them; the next sync and pruning publish drop
  them elsewhere.

### Fixed

- A pool file could be stored truncated under its correct checksum, and was
  then never downloaded again: two mirrors syncing at once in the TUI (amd64
  and i386 share every `Architecture: all` package) shared one partial-download
  file, and stopping the sync — or a dropped connection — at the wrong moment
  left the half-written one in the pool. Each download now has its own.
- Dropping a suite, component or architecture from a repository kept
  publishing and bundling it, frozen at its last sync, and `mmw prune` never
  freed it. It now stops being served at once and the next prune frees it.
- Renaming a network left its snapshots and bundle history under the old
  name, and removing one left them behind: no command could reach them, their
  files could never be pruned, and a later network of the same name inherited
  the old one's bundle history. Rename now carries them along and removal
  (in the TUI) removes them. Snapshots of a network deleted from `mmw.toml` by
  hand can be listed and cleared (`mmw snapshot clean -n OLD --keep 0 --yes`),
  and `mmw prune` points them out.
- A bundle with a damaged pool file shipped without it and was recorded as
  sent, so later bundles never sent that file. Such a bundle is now refused
  whole, with what to do about it.
- The TUI's Prune button with no repositories configured deleted every pool
  file no snapshot held. Prune now refuses that everywhere.
- `mmw prune --dry-run` and its confirmation prompt counted files snapshots
  still hold, promising to free more than prune then did.
- A flat repository's `Release` was downloaded at the end of the sync but its
  `Packages` at the start, unchecked against each other, so a republish in
  between published a `Release` that did not match its index. Both are now
  read up front and checked.
- Taking a snapshot did not hold the pool against a concurrent `mmw prune`,
  which could delete key files the snapshot had just stored.
- `loong64` (and `hurd-i386` next to `i386`) index files were taken into other
  architectures' mirrors.
- `mmw.toml` and the far side's `.mmw-state` are written whole: a full disk or a
  kill mid-write no longer leaves a truncated file. `max_connections` must be
  at least 1.
- A network `root` that is, or holds, `mirror_dir` had its pool deleted by the
  first pruning publish (the stale-repository sweep removed `.pool/`), and
  removing such a network in the TUI could delete it too. Two networks with
  overlapping roots pruned each other's repositories, and a `bundle_dir` inside
  a root was served and then pruned. All of these are now refused when
  `mmw.toml` loads, and publish checks its destination again (symlinks
  resolved, `--root` included).
- Two repositories whose names derive alike — NVIDIA's per-arch flat
  repositories — shared one upstream-files directory: the second never fetched
  its own extra files, and each sync pruned the other's. Each repository now
  has its own; files under the old directory are served until the next sync.
- An upstream file's 64 MiB cap is enforced while downloading, not after.
- `mmw add`: a flat repository is named as the wizard names it (with its
  arch); a tab after `deb` is accepted; a source clash or an unwritable config
  is one line on stderr, not a traceback — and config errors go to stderr.
- A flat repository with only a `Release`, or an unsigned one with only
  `Packages`, is recognised when probed; `$(ARCH)` URLs are probed in parallel.
- `mmw manage --quiet` lists what the pool holds, so a source left at "all
  arches" no longer shows zero mirrors.
- A mirror short only of index files or damaged blobs is described as such,
  not as "0 of N missing". `apply.py` replaces by-hash links atomically.
- Changing a repository's URL in the TUI left every network's selection of
  it pointing at the old URL: the network silently stopped publishing it, and
  a pruning publish then deleted its tree. The selections follow the URL now.
- Adding one URL during Setup as both a `dists/` and a flat repository closed
  the app; it is a notice now.
- A bundle's progress total counted files that ship as hardlinks to an
  identical file as full copies, so it announced the logical tree size (5.1 TB
  for a 4.65 TB tar), the bar stalled near 91% and the ETA ran long. The
  `sent` size in `mmw bundle`'s summary was overstated the same way.

## [0.10.1]

### Added

- Network snapshots: a network's exact published state, kept in the pool —
  every package, index, key and mirrored upstream file it served (keys and
  upstream files frozen into the pool). `mmw snapshot take|list|rename|delete|
  clean --keep N -n NET`; `mmw prune` never deletes a file a snapshot lists and
  frees it once the snapshot is deleted. Bundle history from before snapshots
  is imported as history-only snapshots.
- Bundles go between any two snapshots — forwards, backwards (5 → 3) or
  skipping (1 → 5): `mmw bundle -n NET --from <snapshot|none> --to <snapshot>`.
  By default from where the last bundle went to a new snapshot of now. The
  delta is exact; a full bundle repairs a far side of unknown state.
  `apply.py` prints both ends with their labels, `apply.py --status --root DIR`
  says which snapshot a mirror is at, and an out-of-order bundle names the
  exact `mmw bundle --from … --to …` to ask for.
- Publish a snapshot: `mmw publish -n NET --snapshot <ref|live> [--root DIR]`
  lays it out exactly — older packages come back, newer ones go — into the
  network's root or another directory (its nginx config points there, same
  hostnames). The pool records what each directory serves; a served snapshot
  cannot be deleted, deleting the one the last bundle went to needs `--yes`,
  and `clean` keeps both.
- Snapshots in the TUI: a Snapshots button (or `n`) on a network opens its
  Snapshots page — take (labelled, in the background), rename, delete, clean
  up (keep N, with what a prune then frees). `p` asks what to publish (live or
  a snapshot) and where; `b` asks From and To and estimates what the bundle
  sends, replacing the delta/full question. The network detail shows how many
  snapshots, what the root serves and the last bundle's ends.
- `sign.py`, shipped with every bundle of a certs network: signs every
  mirror's certificate request on the airgapped side, where the CA lives, in
  one go — mirror roots, a directory of them, or `.csr` files. Certificates
  land where the generated nginx config looks; `--out DIR` also collects
  `NAME.crt`, `NAME.key` and `ca.crt` for Nginx Proxy Manager. Needs Python 3.6+
  and `openssl` 1.1.1 or 3.x.

### Changed

- A certs network bundles without a certificate signed on the connected side;
  nothing has to come back across the airgap. Bundles carry the key, the
  signing request and `sign.py`. The generated nginx config includes
  `{root}/.mmw/tls/https*.conf`, which `apply.py` writes once a certificate
  fits the key (`apply.py --tls` right after signing), so HTTP keeps working
  until then and HTTPS turns on by itself.
- Bundles are plain `.tar` files — compression is gone, not optional. The
  payload is packages, already compressed. The manifest inside is plain too, so
  `apply.py` needs only Python: no `zstd` binary on the far side (mmw itself no
  longer needs it either). Compress a bundle yourself for transfer if you
  like; `apply.py` asks for it decompressed first. `bundle_compress` in
  `mmw.toml` is ignored and dropped on the next save; older bundles apply with
  the `apply.py` that came with them.

## [0.10.0]

### Added

- A repository can be added from the URL of a sources file — NVIDIA's install
  docs point at `…/nvidia-container-toolkit.list`. Probe URL in the TUI reads a
  `.list` / `.sources` URL like a pasted config (`$(ARCH)` included), and both
  it and `mmw add --url` keep the file as an extra file of every repository it
  names, so the vendor's own `curl …/foo.list` works against the mirror.
- Adding a single repository (probe a URL, or a pasted config naming one) has
  a Key… button: the same key page as editing — scan a directory of keyrings,
  fetch from a URL (kept as the key URL, so it is mirrored at its own path), or
  paste. Before, a key could only be set by editing the repository afterwards.

### Changed

- The TUI is one app. Repositories, Networks, Sync, Publish and Settings are
  sections in a sidebar (keys 1–5); editing happens on pages that slide in over
  the section instead of in pop-up boxes, and Esc goes back. `mmw manage`,
  `mmw publish`, `mmw sync` and `mmw init` all open it at the matching section.
  First-run setup and re-initialising (Settings → Re-initialize…, or
  `mmw init --force`) also happen inside the app; `mmw init --force` keeps the
  old `mmw.toml` until the new setup is finished.
- Sync and publish run inside the app while you browse the other sections.
  `s` on Repositories starts a sync; `p` publish, `r` dry run and `b` bundle on
  a network start the job in the Publish section. The sidebar shows their
  progress, every outcome is also shown as a notification, `x` stops either
  (it asks first), and quitting asks while one runs. The publish progress view
  is the same, now flat in its section; preparing, the dry-run estimate (with
  Publish now / Discard) and "incomplete mirrors" appear there instead of
  pop-ups. A sync and a publish can run at the same time.
- Themes: every built-in Textual theme plus your own `themes/*.toml`, chosen
  in Settings (or `theme =` in `[settings]`) and saved as you switch.
- `?` lists the keys of the page you are on. The footer shows only that
  page's own keys and fits in an 80-column terminal; `q` quits as before.
- A network's TLS certs now go wherever the network does: with `certs = true`,
  every bundle carries the signed certificate and its private key, and the far
  side serves HTTPS. The separate `bundle_tls` setting is gone; an old config's
  `bundle_tls` line is ignored and dropped on the next save. A certs network
  refuses to bundle until its certificate is signed. Keep `certs` off for a
  network whose transfer media must not hold the key.
- Sizes are shown in decimal units (1 TB = 10¹² bytes), matching drive labels
  and `df -H`. They were divided by 1024 but labelled "GB"/"TB", so every size
  read 7–9% low.

### Fixed

- A network's dry run overstated its size: files several mirrors share (an
  `Architecture: all` package is in every arch; `-security` packages are in
  `-updates`) were counted once per mirror, so a 4.5 TB network read 7.9 TB.
  Totals now count each file once — in the TUI dry run, `mmw publish
  --dry-run`, and the network detail pane's "Published" size.
- The Networks detail pane sat on "Loading…" for a published network: reading
  its published indexes takes ~23 s for a large network, and opening Networks
  started two such reads (more with every cursor move), none cancellable. Now
  the mirror list shows at once with a running count for the sizes, a read for
  a network you moved off stops, and a revisit is instant unless something was
  republished.
- A GPG key fetched from a URL in the TUI kept only the key text and dropped
  the URL, so the key was never mirrored at its own path and a vendor's
  `curl …/gpgkey` install line failed against the mirror. The URL is now kept
  as the repository's key URL. Clearing a key now clears its key URL too (the
  next sync used to fetch it straight back), and a key set only by URL no
  longer shows as "no key".
## [0.9.1]

### Added

- `extra_files` on a repository: any upstream URLs a vendor's install
  instructions fetch (a sources `.list`, a key outside the repository) are
  fetched each sync and published byte-for-byte at their own path, so the
  instructions work unchanged against the mirror. The key URL is published
  the same way. Files are kept per repository, so a network gets only its own
  repositories' files; a failed fetch keeps the last good copy, and a removed
  URL's copy is deleted. Editable in the TUI repository editor.

### Fixed

- A file beside a repository on its own host (Microsoft's
  `packages.microsoft.com/keys/`, next to `repos/code`) was removed again by
  the stale-directory sweep right after every pruning publish.

### Changed

- Releases are cut by CI from `vX.Y.Z` tags: the full test matrix runs, then the
  wheel and sdist are attached to the GitHub release.

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
