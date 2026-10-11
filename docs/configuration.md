# Configuration

`mmw` reads **`mmw.toml` from the current working directory**. There is no
system-wide config, no `~/.config` lookup and no `--config` flag: the working
directory *is* the selector, so two mirrors on one machine are two directories.

> **Generated file — do not hand-edit.** The key tables below are built from the
> pydantic models in `src/mmw/core/config.py`:
> `uv run python docs/tools/gen_config_docs.py`

**Unknown keys are a hard error.** Every model is `extra="forbid"`, so a typo
(`arch = ` instead of `arches = `) fails the load with a message instead of
being silently ignored — and by the same token an *undocumented* key is
undiscoverable, which is why this file is generated from the models rather than
written by hand. The pre-1.0 `[[mirrors]]` format is explicitly rejected; use
`[[sources]]`.

**Empty means all.** An empty or omitted `suites` / `components` / `arches` list
means "everything upstream serves", resolved by listing the repository at sync
time. It never means "nothing". Read
[Sizing](../README.md#sizing) before leaving `suites` blank on a
distribution archive.

## A complete example

```toml
[settings]
mirror_dir = "/data/repos"
max_connections = 16

# Ubuntu's amd64/i386 archive.
[[sources]]
url = "http://archive.ubuntu.com/ubuntu"
provider = "apt"
suites = ["noble", "noble-updates", "noble-security"]
components = ["main", "universe"]
arches = ["amd64"]
dist_upgrade = true               # mirror changelogs.ubuntu.com meta-release too

  [[sources.suite_overrides]]     # -updates: main only
  suite = "noble-updates"
  components = ["main"]

# The other five architectures live on a different host, so they are a
# different repository — identity is the URI.
[[sources]]
url = "http://ports.ubuntu.com/ubuntu-ports"
suites = ["noble", "noble-updates"]
components = ["main"]
arches = ["arm64"]

# A vendor repository with a key we can fetch now and carry across later.
[[sources]]
url = "https://packages.microsoft.com/repos/code"
suites = ["stable"]
components = ["main"]
arches = ["amd64"]
gpg_key_url = "https://packages.microsoft.com/keys/microsoft.asc"

# A flat repository: Packages at the root, architecture baked into the URL.
[[sources]]
url = "https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/sbsa"
provider = "apt-flat"

[[networks]]
name = "lab"
root = "/srv/lab"                 # publish's destination
bundle_dir = "/media/transfer"    # bundle's destination — independent of root
prune = true
dist_upgrade = true
certs = true
# nginx = false                   # default true: generate the site config

  # amd64 only for this site, every component we mirrored.
  [[networks.select]]
  source = "http://archive.ubuntu.com/ubuntu"
  suites = ["noble", "noble-updates"]
  arches = ["amd64"]

  # No select entry for a repository means it is not published to this
  # network at all — unless there is no `select` block anywhere, in which
  # case every mirrored repository is published.
  [[networks.select]]
  source = "https://packages.microsoft.com/repos/code"
```

## `[settings]`

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `mirror_dir` | path | **required** | Where everything lands: `.pool/` (blobs + the SQLite index), `keys/`, and `certs/` for networks with `certs = true`. **No browsable mirror tree is created here** — trees exist only at publish destinations. Needs to be on a filesystem that supports hardlinks, and ideally the same filesystem as your publish roots, or publishing copies instead of linking. |
| `max_connections` | int | `os.cpu_count()`, or `10` if that is unavailable | Global cap on simultaneous HTTP downloads — across *all* mirrors, not per mirror. Also the size of the single thread pool every network operation (metadata, packages, GPG keys) runs on. Raise it for a fast link and many small files; lower it if you are being rate-limited or the disk is the bottleneck. |
| `theme` | string | `'mmw'` | Colour theme for the TUI: `mmw`, `mmw-light`, any built-in Textual theme (`nord`, `gruvbox`, `tokyo-night`, `catppuccin-mocha`, …), or the name of a custom theme file in `themes/` beside `mmw.toml`. Changed from the Settings page or the command palette (Ctrl+P); an unknown name falls back to `mmw` with a warning. See [themes.md](themes.md). |


## `[[sources]]`

One table per upstream repository. The only required key is `url`.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `url` | string | **required** | **The identity of the repository** — host plus path, the thing you would put after `deb ` in a sources.list. Everything else about a source is optional. Normalized before comparison (host lowercased, trailing slash and a default port folded), so `HTTPS://Deb.Debian.org/debian/` and `https://deb.debian.org/debian` are one repository, not two. The *path* is compared as written — a webserver may serve `/Ubuntu` and `/ubuntu` differently. Two entries with the same identity are merged, never duplicated. |
| `provider` | `apt` \| `apt-flat` | `'apt'` | `apt` for a normal repository with a `dists/` hierarchy; `apt-flat` for one that serves `Packages` at its root (suite `/`). Nothing else exists. A flat repository is never arch-expanded — its architecture is baked into the URL. |
| `name` | string | derived from the URL | Cosmetic handle used in the TUI, in log lines and as the `refs` key. Derived from the URL when absent (`archive.ubuntu.com/ubuntu` → `ubuntu-archive`) and omitted from a saved file when it equals what would be derived. Changing it renames the mirror's rows; it is **not** identity. |
| `label` | string | derived from the URL | Grouping for GPG keys only: keys land in `{mirror_dir}/keys/{label}/`. Derived from the host's domain, so `us.archive.ubuntu.com` and `archive.ubuntu.com` both give `ubuntu` and a country mirror's keys sit beside the primary's. Never identity. |
| `arches` | list of string | `[]` — *all* | One sync unit per architecture, with the arch in the mirror name. **Empty means every architecture this host actually serves**, discovered by listing `binary-*` directories at sync time (not by trusting the `Architectures:` field in `Release`, which Ubuntu ships identically on archive and ports while each host serves only some of them). Each architecture multiplies the download. |
| `suites` | list of string | `[]` — *all* | Which releases to mirror (`noble`, `noble-updates`, `bookworm-security`, …). **Empty means every suite the repository publishes** — `archive.ubuntu.com` publishes 55 of them, so leaving this blank there is a multi-terabyte instruction. See [Sizing](../README.md#sizing). |
| `components` | list of string | `[]` — *all* | `main`, `universe`, `contrib`, … **Empty means every component the repository publishes.** Overridable per suite with `suite_overrides`. |
| `suite_overrides` | table array | *(unset)* | A list of `{suite, components}` tables replacing `components` for one suite — for `main` only on `-updates` while taking everything on the base suite. Mirroring adds to these, never narrows them. |
| `include_sources` | bool | `false` | Also mirror `deb-src` source indexes and the `source/` index files. Off by default because source packages roughly double a suite's size and almost no airgapped client wants them. Set automatically when an imported apt config carried a `deb-src` line. |
| `gpg_key_url` | string | *(unset)* | Fetched during sync and written to `{mirror_dir}/keys/{label}/` in both encodings (`.asc` armour and `.gpg` keyring), served at `/mmw-keys/` — unless `gpg_key` is set, which fills that instead. Like an `extra_files` entry, the exact bytes are also published at the URL's own path. There is no built-in catalog of well-known key locations — if you do not state one, no key is fetched. A fetch failure is reported, never swallowed. |
| `gpg_key` | string | *(unset)* | The armoured key itself, inline. This is what an imported deb822 stanza with an inline `Signed-By:` block becomes. It is kept in `mmw.toml` rather than only on disk on purpose: an airgapped network is exactly where a key *URL* cannot be reached, so the config alone has to be able to reproduce the mirror, and `keys/` is an output directory that gets rebuilt. |
| `gpg_key_name` | string | *(unset)* | Filename stem for the emitted key files — `nvidia-spark-repo` gives `keys/nvidia/nvidia-spark-repo.asc` and `.gpg`. Taken from the basename of a `Signed-By:` *path* when a config named a keyring file mmw does not have, which is also the handle `mmw add --keys-dir` matches against to supply it later. |
| `extra_files` | list of string | `[]` | Any other upstream URLs a vendor's install instructions fetch — a sources `.list`, a key on another path or host, a keyring `.deb`. Each is fetched every sync and published byte-for-byte at its own path (`https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list` → `{root}/nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list`), so the vendor's commands work unchanged once DNS points at the mirror. A host that only serves such a file gets a server block and a certificate name. A failed fetch keeps the last good copy; removing a URL removes its copy. Must name a file, with no query string; 64 MiB at most. |
| `dist_upgrade` | bool | `false` | Ubuntu only. Also mirror `changelogs.ubuntu.com/meta-release*`, which `do-release-upgrade` reads to discover releases. A **declared** opt-in — it is never inferred from a distro name. Pair it with `dist_upgrade` on the network that should publish those files. |


### `[[sources.suite_overrides]]`

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `suite` | string | **required** | The suite this override applies to. Must be one of the source's `suites`. |
| `components` | list of string | **required** | Components for that suite, replacing the source-level `components` entirely. |


### What a source expands into

Each source becomes one **sync unit** per suite × component × architecture, named
`{name}-{suite}-{component}-{arch}`. That is why the architecture is part of
the name: two sync units sharing a name would share a `refs` key, and the second
one's index reconcile would silently delete the first one's. Duplicate source
*and* mirror names are rejected at load time.

A flat (`apt-flat`) source is a single sync unit and is never arch-expanded.

## `[[networks]]`

A network is a named downselect of the mirror — what one site gets. `publish`
and `bundle` are verbs over it, each with its own optional destination. A
network with neither `root` nor `bundle_dir` is a valid config; the verb whose
destination is unset says so rather than inventing one.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `name` | string | **required** | Identifies the network in `-n` flags, in `{mirror_dir}/certs/{name}/`, in the bundle ledger at `{mirror_dir}/bundles/{name}/`, and in bundle filenames. Removing a network clears its bundle ledger, so reusing a name later starts clean rather than inheriting delta history the far side never received. |
| `root` | path | *(unset)* | The `publish` verb's destination. Trees are materialized at `{root}/{hostname}/{upstream_path}/` as hardlinks into the pool. A network with no `root` is skipped by publish with a message — it is a valid config, not an error. |
| `bundle_dir` | path | *(unset)* | The `bundle` verb's destination. Independent of `root`: a network may have neither, one, or both. `mmw bundle --out` overrides it for one run. |
| `select` | table array | `[]` — every mirrored repository | A list of `[[networks.select]]` tables — what this network publishes, **by dimension, never by mirror name**. Omitting `select` entirely publishes every mirrored repository. See the table below. |
| `prune` | bool | `true` | Delete files at the destination that the current refs set no longer wants. **This is what makes a published tree exactly match the mirror**, and it is why a network that selects one architecture of a shared-base repository removes the other architecture's files from *that tree* (the pool keeps them). `by-hash/` is swept separately, keeping the newest two stale generations for clients that are mid-`apt update`. |
| `dist_upgrade` | bool | `false` | Publish `changelogs.ubuntu.com/meta-release*` to the network root and write an `UPGRADE.md` with the DNS entries this network actually needs (derived from its own mirrors) and the LTS upgrade paths parsed out of the mirrored meta-release file. Needs `dist_upgrade = true` on the Ubuntu source too, or there is nothing to publish. |
| `certs` | bool | `false` | Generate a 4096-bit TLS key and a CSR for this network's hostnames (plus `changelogs.ubuntu.com` with `dist_upgrade`) at `{mirror_dir}/certs/{name}/`, **deliberately outside `root`** so a misconfigured web server can never serve the private key. An existing `mirror.key` is never overwritten (that would invalidate an already-signed certificate); only the CSR is refreshed. Sign it with `mmw certs --ca CA.crt --ca-key CA.key`; once `mirror.crt` exists, publish serves every host over HTTPS as well as HTTP. Every bundle carries the signed certificate **and its private key** to `{root}/.mmw/tls/` on the far side (0600, in a 0700 directory no generated server block serves) and points the bundle's nginx config at them; a bundle refuses to start until `mirror.crt` is signed rather than ship HTTP-only. Keep it off for a network whose transfer media must not hold the key. (The old `bundle_tls` key is accepted and ignored.) |
| `nginx` | bool | `true` | Write one `{root}/nginx/mirror.conf` with a server block per mirrored hostname, `server_name` derived from the real upstream hostname and `root` pointing at the published directory — plus `listen 443 ssl` once the network has a signed certificate. One file, so it works included, or dropped into `conf.d/` or `sites-enabled/`. Set `false` if you maintain your own configuration. See [How clients find the mirror](../README.md#how-clients-find-the-mirror). |


### `[[networks.select]]`

Selection is **by dimension, never by mirror name**. A frozen list of names
silently stops publishing a new architecture the moment one appears upstream;
a dimension filter keeps following the repository.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `source` | string | **required** | The repository URI this selection applies to — must match a `[[sources]]` `url`. Normalized before comparison, so a trailing slash or an upper-case host still matches. |
| `suites` | list of string | `[]` — *all* | Suites of that repository to publish. Empty = every suite mirrored for it. |
| `components` | list of string | `[]` — *all* | Components to publish. Empty = every component mirrored for it. |
| `arches` | list of string | `[]` — *all* | Architectures to publish. Empty = every architecture mirrored for it, **and it keeps meaning that** as new architectures appear upstream — which is the whole reason selection is by dimension rather than by a frozen list of mirror names. A flat repository whose URL names no architecture matches any filter. |


## Where things live

| Path | What |
|------|------|
| `{mirror_dir}/.pool/packages/{xx}/{sha256}-{name}` | Every blob — packages *and* index files — stored once |
| `{mirror_dir}/.pool/mmw.db` | SQLite (WAL): what each mirror's index wants, and what the pool holds |
| `{mirror_dir}/keys/{label}/` | Collected GPG keys, `.asc` and `.gpg`, plus `IMPORT.md` |
| `{mirror_dir}/certs/{network}/` | TLS key + CSR — outside every published tree, on purpose |
| `{mirror_dir}/bundles/{network}/` | Bundle ledger: one manifest per bundle ever written |
| `{network.root}/{hostname}/{upstream_path}/` | The published tree — hardlinks into the pool |
| `{network.root}/nginx/` | Generated site configuration |
| `{network.root}/keys/` | The same keys, published for clients |

`mirror_dir` deliberately holds **no browsable mirror tree**. Trees exist only
at publish destinations.

## See also

- [CLI reference](cli.md)
- [Getting started](getting-started.md)
- [Airgapped operation](airgap.md)
