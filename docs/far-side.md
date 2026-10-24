# mmw on the far side

For a network whose far side runs mmw (`far_side = "mmw"` on the connected
side), `mmw site` keeps **every version of every repository** it is sent,
and serves any of them again by relinking files it already has. Take Debian
back to last month's version without a new bundle, leave everything else
where it is, and move it forward when you are ready.

A network whose far side runs only `apply.py` keeps working exactly as
[airgap.md](airgap.md) describes; `mmw site` refuses its bundles, and
`apply.py` refuses an mmw network's.

---

## The shape of it

- A **root** is the directory nginx serves, laid out as usual:
  `{root}/{hostname}/{upstream path}`.
- Every served file is a hardlink into the root's **store**,
  `{root}/.mmw/store/`, one file per distinct content. Serving another
  version relinks; nothing is copied. Keep the root on one filesystem: an
  NFS export is fine.
- The site's **home** holds its state: a small database, `site.toml`, and a
  lock. It defaults to `{root}/.mmw/site`, so any machine that mounts the
  share sees the same site. None of it is ever served.

## Set it up once

```bash
mmw site init --root /mnt/oak
export MMW_SITE_HOME=/mnt/oak/.mmw/site     # or pass --home to every command
```

## Apply a bundle

```bash
mmw site apply /media/drive/oak-20261024T101500Z.tar
mmw site apply /media/drive/oak-20261024T101500Z/        # a stage directory
cat /media/drive/oak.tar | ssh server mmw site apply -   # a tar on stdin
```

```
applied 20261024T101500Z
  archive.ubuntu.com/ubuntu  v14 → v15
  deb.debian.org/debian  stored v10, still serving v6 (held)
  VS Code  v11 → v12
! nginx config changed — recreate nginx on the serving host (docker compose … up -d --force-recreate)
```

Everything arrives in the store, and is checked against the bundle's
manifest, **before anything served changes or is recorded**: a damaged copy, a truncated
one, or a bundle missing files leaves the site exactly as it was — copy it
again. A repository whose new version needs files this site never received
(it missed an earlier bundle, or deleted that version) is refused on its
own, with the connected-side command that fixes it; the rest still apply.

An older bundle, or one already applied, never moves what a repository
serves: its versions are stored, and `mmw site serve` can use them.

Files arrive in any order: a stage directory tarred with
`tar cf - -C …/oak-<stamp> .` is a bundle like any other.

## Serve another version

```bash
mmw site history debian         # every version kept here
mmw site serve debian v6        # relinks; asks first (--yes to skip)
mmw site status                 # what each repository serves, held or current
```

Anything but the newest version **holds** the repository there: later
bundles store its new versions without serving them. `mmw site apply …
--forward debian` (or `--forward-all`) moves it on as the bundle lands;
`mmw site forward debian` (or `--all`) moves it to the newest version kept
here at any time. `mmw site hold debian` holds a repository without
changing what it serves.

Switching only touches the files that differ, packages first, indexes
next, release files last, removals after — a client mid-`apt update` never
sees an index naming a file that is not there. A switch cut short (power,
Ctrl-C) is finished by the next `mmw site` command.

## Space

```bash
mmw site space                  # each version's size, and what deleting it frees
mmw site delete debian v3       # never the one served, nor a held repository's newest
mmw site settings --keep 5      # keep the newest 5 of each repository (default: all)
```

"Frees" counts only files no other kept version uses: consecutive versions
share most of theirs.

## Checking it

```bash
mmw site verify                 # hashes every served file; exit 4 on a mismatch
```

`mmw site verify --repair` puts wrong or missing files back from the store.

Never edit a served file in place: most are hardlinks into the store, and
an edit would change every copy. nginx's generated files are the exception
(they are written fresh) but are still replaced by the next bundle — keep
your own deployment copy outside the root, as [airgap.md](airgap.md) says.

## One operator at a time

Commands that change the site take a lock in its home. A second one says
who holds it; `--break-lock` clears a lock a dead run left behind.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | done |
| 1 | a bad argument (an unknown repository or version) |
| 2 | an unsafe or damaged bundle — copy it again, or bundle again |
| 3 | a repository was refused: its new version needs files this site doesn't have |
| 4 | `verify` found a mismatch |
| 5 | the lock is held, the bundle is an apply.py one, truncated or compressed |
