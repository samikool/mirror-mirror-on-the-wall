# Upgrading to timelines

This release changes how history is kept, on both sides. The connected side
records a **point** per suite at every sync, and bundles carry points. A far
side running mmw keeps a **timeline** per suite, serves the newest complete
point, and serves any other on request. `apply.py`, `sign.py` and
`make-ca.py` are gone: the far side runs mmw itself.

Nothing needs converting by hand. Below is what happens on its own and what
to do yourself, by where you start from. Do the connected side first.

## The connected side

Install the new mmw. Existing retained points become available without another
sync: opening history, opening a publish/transfer date picker, or previewing a
transfer finishes any pending history upgrade. This uses retained data only;
it does not fetch repositories or invent a new point for today's state.
The upgrade, or the first bundle or publish, as needed, handles each network:

- **Turns its snapshots into points**, once, each at its own date. One that
  can't be split is named and skipped (it stays a snapshot).
- **Seeds its recordings from those snapshots**, so `mmw publish --as-of` reaches
  back past the upgrade. A date before the upgrade is published as the
  snapshot before it when only legacy history is available: what synced between
  that snapshot and the upgrade was not recorded.
- **Carries over what it has sent**, so the first bundle afterwards is still
  a delta. A full bundle a forced snapshot delete had queued is sent full,
  once.

Then:

- **`far_side = "apply"` in `mmw.toml` is retired.** mmw says so once per
  run and otherwise ignores it. Remove the line.
- **Manual network snapshots are retired.** Use `mmw history` (or Sources /
  Networks → More… → History) to inspect repository points. Publish with
  `--as-of DATE`; transfer with `--since DATE`, `--until DATE` and `--full`.
  The old `snapshot` commands, `publish --snapshot` / `--take-snapshot`, and
  connected-side bundle / stage `--from` / `--to` options are removed.
  Existing snapshot-derived history and old bundles remain readable.
- **APT repository history is independent of networks.** Creating or editing
  a network does not need a resync or a history import. Its current selection
  can publish or transfer retained repository points, including earlier ones.
  PyPI, VS Code and layers retain their compatibility history adapters.
- **Repository history is kept in full by default:** `[settings] keep_points`
  is `all` unless you set it (a pool saved with the old default of 3 now keeps
  everything), overridden in Sources with `keep_points` / `keep_suites`.
  Networks no longer have their own retention — `keep_points` / `keep_suites`
  on a network are ignored with a warning. A point an actual publish
  directory serves stays protected until that directory advances.
  `mmw prune --dry-run --costs` previews retention before deleting anything.
  Older publish records without point IDs retain prior history conservatively;
  `mmw history` explains when republishing that directory can replace this with
  an exact hold. No resync or new airgap transfer is needed.
- **Bundles need mmw on the far side.** Tick **Include mmw installer** (or
  `mmw bundle --installer`) for the first bundle to a far side that doesn't
  have this version yet.

## A far side already running mmw

Install the new mmw there (`sh mmw-<version>/install.sh` from the installer
file) on every machine that holds one of its roots: a root over ssh needs
the same version. Converting is one-way. On the first `mmw site` command
that converts its old history, mmw:

- **Backs up the control state first**, copying each root's `.mmw/site/`
  to `.mmw/site.before-timelines/`. This keeps the database and settings,
  never the store or the active lock. An existing backup is left alone.
  A failed copy stops conversion so you can fix the problem and retry.
- **Says once that it converted**, with the backup's path. The far-side TUI
  shows the same notice when opening the mirror; no approval is needed.
- **Splits every kept version into timelines**, once. A held repository is
  pinned to what it serves.
- **Reads `keep_versions = N`** in its settings as `keep = N` with
  `auto_clean = true`, the same behaviour under the new names. See `mmw site
  settings`.

Opening the far-side TUI also performs this conversion and shows the retained
points immediately. You can upgrade directly from an older release; another
bundle apply or an intermediate mmw installation is not required. The served
files, retained versions, labels, and held repository's selected point stay in
place.

`mmw site tidy` lists the conversion backup as removable, in the TUI too,
but leaves it in place. Remove `.mmw/site.before-timelines/` yourself once
you are satisfied with the converted mirror.

Bundles made before the upgrade still apply. Applying one made for
`apply.py` is refused with exit code 5: bundle again with the new mmw.

## A far side `apply.py` manages

Install mmw there, then `mmw site adopt /srv/www --yes`: the tree becomes
an mmw root where it stands (every file checked against its
`.mmw/manifest.jsonl`; nothing served changes), and the network's next
bundle names its repositories. See [far-side.md](far-side.md#a-far-side-applypy-manages).

In the far-side TUI's first screen, choose **Upgrade an older mirror…**, select
the existing folder, then **Look** and **Adopt**. This uses the same in-place
conversion as the CLI. Close the result to open the converted mirror and its
points; no replacement transfer is needed.

Stop the old mirror/apply jobs before starting. **Look** is read-only and checks
the files against the recorded manifest, including older compressed manifests.
It reports damaged and missing repository files before creating any mmw state;
restore those files and Look again. Unchanged hash results are reused by Adopt.

If adoption is interrupted, its progress is retained. Fix the reported problem,
then repeat Look and Adopt on the same folder, even after restarting mmw. The
original manifest remains available until conversion finishes, and an unfinished
conversion is not exposed as a ready mirror. If the previous process died and
left a lock, select **Clear a stale lock** only after confirming it is gone
(`--break-lock` in the CLI); Look never clears a lock.

## A tree mmw didn't make

An APT mirror kept by other tools (apt-mirror, debmirror, rsync):

```bash
mmw site adopt /srv/mirror --as deb.debian.org/debian --key debian-archive.gpg --dry-run
```

Each suite is checked against its signature (the key you give, or the keys
under the root's `keys/`), its Release and its indexes.

- **The report says which key verified each suite**, and lists what isn't
  adopted: pool files no index names, by-hash files of earlier Releases,
  entries listed with MD5 only, and damaged files.
- **Installer images are checked against their `SHA256SUMS`.**
- **With no key, `--unverified` adopts anyway.** Those points are marked
  until `mmw site check-signatures` checks them.

## A published tree copied to a drive

A tree `mmw publish` wrote carries `.mmw/manifest.jsonl` like an `apply.py`
tree does, so the same applies: `mmw site adopt PATH` makes it a far side.
It carries the connected side's TLS key. `mmw site certs` gives the far
side a key of its own.

## After any of these

- `mmw site status` shows what each repository serves, any dated URLs
  (`HOST/PATH/@YYYY-MM-DD/`), and any dated tree past `Valid-Until`.
- `mmw site history REPO` lists each suite's points, what each covers, and
  what is served.
- `mmw site settings` shows what is kept; `--costs` adds what each setting
  would free.
