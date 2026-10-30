# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Your own packages: folder repositories.** A folder of `.deb` files becomes
  a signed apt repository served at an address you choose — `mmw add local
  --folder ./debs --url https://tools.lan/apt`, or Sources → Add → Local
  folder. Sync scans the folder (unchanged debs are not read again), builds
  the indexes and signs them; it publishes and bundles like any other
  repository. Flat by default, or `--structured` for `dists/{suite}/{component}/`;
  `--contents` adds `Contents` files for apt-file. `mmw sources-file NAME`
  (or `f` on the repository) prints the client's sources file, deb822 or
  one-line, which is also published beside the repository.
- **Signing keys** (`mmw signing-key generate|import|export|list|remove`,
  Settings → Signing keys): the keys mmw signs your repositories with, kept in
  its own keyring under the pool directory. Needs `gpg`.
- **Force delete a snapshot** (`D` on the Snapshots page, `mmw snapshot
  delete --force`): forgets it outright, with every transfer that went to it
  and its ledger manifest — like a force push. The far side is not touched:
  if it may be at the forgotten snapshot, the next transfer sends everything,
  and the dialog (or the command) says so before you confirm.

### Changed

- **Failed downloads say why.** Each failed file on a sync card names its
  reason (`http 404 (not on the server)`, `checksum mismatch`, `503 Service
  Unavailable`…), and a card that finishes with failures sums them up —
  `Failed packages: 12 — 10 × http 404 (not on the server), 2 × checksum
  mismatch`. `mmw retry-failed` ends each mirror with the same `Why:` line.
- **PyPI resolving says what it is doing.** `wave 3, 4,512 releases` is now
  `dependencies, round 3 — reading the requirements of 4,512 release(s);
  9,870 kept so far`, the metadata fetch between rounds counts as it goes
  (`1,200/4,512`), and each target ends with how many releases it kept
  after how many rounds.
- **Retry missing says what it leaves out.** It covers VS Code and PyPI
  mirrors; apt mirrors in the selection used to vanish silently (or read as
  "nothing selected could be resolved"). It now names them and says a normal
  sync fetches what they are missing.
- **A new network's far side defaults to mmw.** The network editor offers mmw
  first; existing networks keep what they have (no `far_side` in the config
  still means `apply`).
- **Deleting a snapshot a transfer went to keeps it as history only.** Its
  files go with the next prune, but it stays listed as where the next transfer
  can start from — so `mmw snapshot delete` no longer needs `--yes` for the
  one the last bundle went to (it is still accepted), and Clean up no longer
  keeps snapshots back for that. A history-only snapshot holds no files, and
  picking one as From uses the repository versions it kept.

### Fixed

- **Deleted snapshots came back.** Every bundle leaves its manifest in the
  ledger, and listing snapshots imported it again — the snapshot reappeared
  as history only, still holding every file, so prune freed nothing.
  Snapshots already history only stop holding their files too: the first
  prune after upgrading may free more than usual.

## [1.3.0]

mmw comes to the far side. A network set to `far_side = "mmw"` is applied by
mmw itself: every bundle is kept as a version you can serve, hold, roll back
to or delete, across one or more roots — local disks or other machines over
key-based ssh — from `mmw site` or a full-screen TUI. mmw installs there from
its own small file, offline, and keeps itself up to date on both sides.

Upgrading from 1.2.0: nothing to do. Networks default to `far_side =
"apply"`, the same `apply.py` as before. To move one to mmw on the far side,
set `far_side = "mmw"` (or Far side on its Edit page), transfer with the mmw
installer included, install it there, and `mmw site apply` the bundle.

### Added

- **mmw on the far side** (`far_side = "mmw"`). See [docs/far-side.md](https://github.com/samikool/mirror-mirror-on-the-wall/blob/master/docs/far-side.md).
  - `mmw site apply` stores a bundle (a `.tar`, a stage folder, or stdin),
    checks everything, then switches each repository; a cut-short switch
    finishes on the next command. Per-repository deltas and partial bundles
    (`--repo`) carry only what changed.
  - Every applied bundle is a version: `serve`, `hold`, `forward`,
    `delete`, `history`, `space`, with retention (`settings --keep N`). A
    version can be named by its bundle's date, stamp or label as well as
    `v3`, and `mmw site serve-bundle 28oct` serves the whole mirror as it
    was after one bundle.
  - **Several roots**, each describing itself (name, network, members) and
    holding its own hostnames: new hostnames are placed on a root at apply
    time, `mmw site move` moves a repository (with every repository sharing
    its hostname) to another root, and `tidy` clears the old copy. nginx is
    generated per root, with the exact steps after each change.
  - **Roots over ssh** — key auth only, never a stored key or password. An
    ssh root behaves like a local one; mmw can push its installer there
    (`mmw site open HOST:/PATH --install …`, or offered by the TUI).
  - **HTTPS on the far side**: each root's key is made where it lives and
    only the request and certificate travel; `mmw site ca load`/`ca make`
    record the site's CA on that machine only, and `mmw site certs` signs.
- **The far-side TUI** (`mmw site manage`): a card per repository with its
  detail and versions beside it; Apply bundle (review, then a progress page
  per repository), Add root, As of (a bundle), Move, Certs and Tidy;
  Serve, Forward, Hold and Delete under the versions. Long work runs on one
  progress page with Stop where it's safe; ssh never holds the screen.
- **mmw's installer**: `mmw-<version>-<arch>.tar` installs mmw offline
  (`tar -xf … && sh mmw-<version>/install.sh`, into `~/.local/bin` or
  `--prefix`). Written by `mmw installer --out DIR`, `--installer` on
  `mmw bundle`/`mmw stage`, or "Include mmw installer" on the Transfer page —
  on by default whenever this mmw is newer than the last installer sent.
- **mmw keeps itself up to date.** On the connected side every command and
  the TUI check PyPI (cheaply, never slowing a start) and offer the update —
  `[Y/n]` at the end of a command, a dialog when the TUI opens — following
  how mmw was installed (uv tool, pipx, pip in a venv, the installer). The
  TUI comes back updated. A new major version warns and defaults to No.
  `mmw update` does it on demand; `MMW_NO_UPDATE_CHECK=1` turns it off. On
  the far side the installer next to a bundle offers the same, updating
  every ssh root with this machine, or none.
- **`mmw stage`**: a bundle laid out as hardlinks in a folder, for media
  that is mounted rather than copied; `--out -` streams a bundle to stdout.
- Python 3.10 is supported.

### Changed

- **Bundle becomes Transfer** in the TUI: a bundle (`.tar`) or a stage
  (folder), with the repositories to send picked there and the
  repositories each snapshot changed shown.
- Bundles no longer carry mmw's runtime; one made before this applies as
  usual, with a note. `--no-runtime` and `mmw site apply --allow-downgrade`
  are gone.
- Sync downloads the largest files first, so one slow file overlaps the rest
  instead of trailing alone.
- The bundle's `.sha256` is computed while the tar is written — no second
  read of the whole bundle.
- A long status message wraps onto up to three lines instead of being cut
  off.

### Fixed

- The selected sync card has its own look, never the finished green.
- An action's message in the TUI is no longer overwritten by the reload that
  follows it.

## [1.2.0]

PyPI joins APT and VS Code: mirror the whole index or a requirements list for
the Pythons, OSes and arches you choose, and stock `pip` and `uv` install from
it at pypi.org with no index flags. mmw itself is now on PyPI, every disk
operation goes through one layer that makes verify and publish markedly
faster, and TLS is on for every network by default.

Upgrading from 1.1.2: a network that never set `certs` now has TLS on. Its
next publish writes a TLS key and signing request into the tree, and its
bundles carry the private key. If transfer media must not carry a key, set
`certs = false` on that network (or turn TLS off on its Edit page) before
publishing or bundling.

### Added

- **PyPI mirroring.** A `pypi` source mirrors pypi.org filtered to targets
  (Python × platform × arch, with glibc/musl/macOS floors): either the whole
  index — every project's latest release a client at the floor can install,
  plus the older releases its dependencies require — or a requirements list
  (`requirements` inline or `requirements_file`), resolved together per
  target by the bundled uv, with build tools mirrored for anything that has
  to build from source. Anything that cannot be guaranteed lands in a
  per-unit report instead of going missing. See [docs/pypi.md](https://github.com/samikool/mirror-mirror-on-the-wall/blob/master/docs/pypi.md).
- **PyPI served at its own hostnames.** Publish lays out
  files.pythonhosted.org and pypi.org with PEP 503 HTML and PEP 691/700 JSON
  simple pages (negotiated by `Accept`), PEP 658 metadata and
  `/pypi/<name>/json`. Pages are rendered from ≤256 shards after a publish, a
  snapshot replay, or `apply.py` on the far side, so snapshots and bundles
  never carry millions of pages.
- **Network `retention`** (`current`, `forever` or `<N>d`, default `90d`):
  how long a release that left the keep set stays published.
- **PyPI in `mmw manage`:** add and edit a source, and **Check** resolves the
  unsaved requirements list for each target, showing uv's own explanation of
  any conflict. `mmw add pypi` on the command line.
- **On PyPI.** `uv tool install mirror-mirror-on-the-wall` (or `pipx install`)
  installs the `mmw` command. Every release tag uploads to PyPI the same
  files the GitHub release carries, through trusted publishing; 0.9.0–1.1.2
  are there too.
- Settings `disk_write_threads`, `disk_read_threads` and `disk_readahead`
  (8 / 16 / 16, tuned on one spinning disk; 1 / 1 / 0 is the old serial
  behaviour), editable on the Settings page.

### Changed

- **TLS is on by default** for every network (see the upgrade note above).
  An unsigned network still serves plain HTTP; `certs = false` opts out.
- **One disk I/O layer.** Linking, pruning, stats, small reads, hashing and
  the bundle archive all go through it. Measured cold on a real pool:
  `verify --stat` 2.2× and snapshot publish 1.8× faster. The bundle
  pre-flight, verify, pool prune and the VS Code reads now show progress
  and honour Stop.
- PyPI pages render on that layer's write threads, and the publish screen
  shows "PyPI pages: N/256 shards rendered" while they do. A stopped render
  finishes the interrupted shard on the next publish.
- The publish summary counts packages, as the progress bar does, with
  metadata files after: "691,244 linked, 0 already current (+ 674,243
  metadata files)". The CLI and the publish record keep the full totals.
- Faster all-of-PyPI resolve and metadata sync: parsed dependencies are
  cached per metadata body, and metadata is fetched and pooled a slice at
  a time.

### Fixed

- A PyPI or VS Code publish ended at about 51 % on the publish screen: each
  host's total counted its metadata files, the bar did not.
- The last step's line ("saving the publish record…") stayed on screen after
  a publish finished.
- A network whose sources were all dropped failed its publish with "No
  hostnames found" once TLS was on; it now skips the TLS files.

## [1.1.2]

The check flags get one name each, and pool listings are faster. Upgrading
from 1.1.1: the old flag names are removed, not aliased — a cron entry or
script that passes `--deep`, `--rehash` or `--full` now fails as an unknown
option, so update it first (see Changed).

### Changed

- **Check flags renamed, old names removed.** `mmw verify --deep` is now
  `--stat` and `--rehash` is `--hash`; `mmw publish --full` is `--recheck`.
  `mmw bundle --full` is gone — `--from none` already did the same thing.
  `--full` had meant two different things; each name now means one.
- Pool listings (publish, bundle, verify) dedupe and sort in Python rather
  than in SQLite: a 1M-file VS Code listing takes 3.7 s instead of 9.8 s.

## [1.1.1]

VS Code mirrors behave like the real Marketplace in more places, and a publish
after a sync is minutes faster. Upgrading from 1.1.0: the first VS Code sync
rewrites every extension description once (their install counts move into
one file per unit, see Fixed), and republishing afterwards updates nginx and
the gallery — restart the network's Compose stack to load them.

### Changed

- A publish or bundle that finds incomplete mirrors asks in a popup, once
  every mirror is checked: it lists them all, incomplete first with what each
  is missing, and offers **Publish anyway** / **Bundle anyway** or **Cancel**.
  It replaces the button under the report.
- Verifying mirrors before a publish or bundle shows what a long check is
  doing (listing references, reading new descriptions, checking files), so a
  VS Code mirror's minute-long check no longer looks stuck at "1 of 4".
- After a sync's last package, each mirror's card says it is saving to the
  pool database, with a spinner and how long it has taken, instead of a
  static "Committing pool references..." that looked frozen.

### Fixed

- A VS Code sync rewrote most extension descriptions every time, because each
  carried its install counts, ratings and trending scores, which move daily
  for any extension with activity. Those now live in one statistics file per
  unit (`.vscode-meta/statistics/`), and descriptions change only when the
  extension does — far fewer pool writes per sync and far fewer re-reads at
  publish. The gallery still sorts and shows install counts as before.
- A VS Code publish after a sync rebuilt the gallery catalog from scratch —
  every description re-read, and every file in the mirror listed to find
  them. On a 120k-extension mirror the catalog step went from about 5 minutes
  to 18 seconds after a sync, and from about a minute to 3 seconds when
  nothing changed. Descriptions are remembered in
  `{mirror_dir}/vscode-meta-cache/descriptions.gz`.
- VS Code's check for a newer version of each installed extension
  (`marketplace.visualstudio.com/_apis/public/gallery/vscode/{publisher}/{name}/latest`)
  is answered by the gallery. It used to fail twice per extension ("Failed to
  fetch") before VS Code fell back to a search.
- The stable desktop build also mirrors the workbench's chat registry,
  profile templates and MCP server list from `main.vscode-cdn.net`, which
  were logged as 404s at startup.
- A VS Code publish's progress ran past 100 % (e.g. 937,588 of 895,934): the
  total counted distinct file contents, while publish places one file per
  path, and VS Code serves the same bytes at many paths.
- Clicking a notification just as it timed out crashed the TUI, stopping any
  sync or publish running in it.
- Pool listings (publish, bundle, snapshot publish) build each blob's path as
  a plain string: about 8 s less per million files.

## [1.1.0]

VS Code mirrors, and a pool that stays fast at a million files. Upgrading
from 1.0: the first writable open of the pool builds new indexes once, and
the first publish of each network does one full pass before repeat
publishes become incremental. A `[[networks.select]]` key that is not a
dimension of its source's provider is now a load error (see Fixed).

### Added

- VS Code sources: `[[sources]] provider = "vscode"` mirrors Microsoft
  Marketplace extensions (top N, listed ids, or all — with dependencies),
  desktop builds and VS Code Server/CLI builds (latest + 2 previous commits).
  `mmw add vscode`, `mmw dry-run` and `mmw verify` know them; Sources and
  first-run setup in the TUI can add and edit them. Guide: `docs/vscode.md`.
- VS Code networks publish and bundle a working offline Marketplace for stock
  VS Code: payloads at Microsoft's own hostnames, a gallery service in the
  network's nginx Compose stack, nginx for seven hostnames (two wildcards),
  wildcard-capable certificates, and a `VSCODE.md` guide per network.
  Extension search, install and update work offline; so does Linux's
  **Download Update** (`code.visualstudio.com` and Microsoft's direct
  download links redirect to the mirrored builds), and VS Code's startup
  alerts are mirrored with the stable desktop build.
  `https://marketplace.visualstudio.com/` and `code.visualstudio.com` list
  what the mirror holds.
- `mmw retry-failed [--network N] [--dry-run]` retries only a VS Code
  mirror's saved list of failed downloads, without Marketplace discovery
  (TUI: **Retry missing** on Sources and Sync).
- Nginx-enabled networks include the official `nginx:latest` amd64 Docker
  image, an offline Compose setup, and network-specific instructions in
  publish outputs and bundles. Container paths are fixed at `/srv/mmw`;
  image exports are cached and frozen in snapshots.
- `make-ca.py` ships beside `sign.py`: it makes the site CA once and prints
  how to trust it on each kind of client.
- `settings.database` places the pool database (default: where it has always
  been, `{mirror_dir}/.pool/mmw.db`). Move it from Settings → **Move…** or
  `mmw move-database PATH` — e.g. onto a fast disk while the pool stays on a
  big slow one. mmw refuses to start over an existing pool whose configured
  database is missing, rather than starting an empty one.
- `mmw publish --full` rechecks every published file (the same word as
  `mmw bundle --full`); `mmw publish --take-snapshot` snapshots the network
  first. Both are toggles on the TUI's publish page, off by default.
- The TUI offers **Publish anyway** / **Bundle anyway** after the
  missing-file report, like the headless `--allow-incomplete`.

### Changed

- Repeat publishes touch only what changed: each published tree keeps a
  record (`.mmw/manifest.jsonl`, the bundle manifest's own format; `apply.py`
  writes it into applied trees too) and trusts it — no per-file checks, no
  tree walk to prune. The first publish after upgrading does one full pass.
- Pool listings read through covering indexes in checksum order: on a
  spinning disk a 1M-file listing no longer does a random read per file.
  The first writable open after upgrading builds the indexes once.
- Sync, publish, bundle and verify first read the pool database into the page
  cache when it fits in available memory (up to 90 %; reclaimable cache only).
- Repeat syncs write only the pool references that changed, and metadata work
  is batched — much less database I/O on large mirrors.
- Published trees and bundles carry the same deployable files, including
  top-level apply/signing scripts and portable private TLS assets. Publish
  snapshots remain manual; bundling current content still snapshots it
  automatically.
- Sync progress counts logical items (packages, extensions, builds) rather
  than individual files.
- The Repositories section is now called **Sources**, and Add starts by
  asking what you are adding (APT repository, VS Code, or detect from a URL /
  pasted config). Same key (`1`), same behaviour once past that first step.
- The transfer log writes at most 50 per-file lines per refresh (the counters
  keep the totals), and the transfer panel says what each step after a
  publish's files is doing.

### Fixed

- Parallel mirror units share downloads of the same file, and a restarted
  sync reuses files an interrupted one already completed.
- Initial index fetches are retried, and backoff after a server error is
  shared by every request to that server.
- An apt suite that lists a component with no indexes (a skeleton
  component) no longer fails the sync.
- `mmw -c DIR add …` now reads and writes `DIR/mmw.toml`; it used the working
  directory regardless of `-c`.
- A `[[networks.select]]` key that is not a dimension of that source's
  provider (e.g. `components = ["main"]` on an apt-flat repository) is now a
  load error. It used to load and silently publish nothing from that
  repository. Empty lists (`suites = []`) are still accepted.
- Quitting the TUI right after opening a page no longer crashes on exit.

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
