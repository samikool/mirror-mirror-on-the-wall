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

## First contact: installing mmw

The far side needs no Python of its own and no network. An mmw network's
first bundle (and any bundle whose mmw is newer than the last one sent)
carries mmw's own runtime: the `uv` binary, a standalone Python (the glibc
2.17 build, so RHEL 8 and newer), and mmw with every dependency as wheels,
for each architecture the network lists. Install it with one command, from
the bundle itself:

```bash
tar -xf /media/drive/oak-20261024T101500Z.tar .mmw/runtime
sh .mmw/runtime/bootstrap.sh --home /mnt/oak/.mmw/site
export PATH=/mnt/oak/.mmw/site/bin:$PATH
```

```
mmw 1.3.0 installed — /mnt/oak/.mmw/site/bin/mmw (export PATH=/mnt/oak/.mmw/site/bin:$PATH)
```

- The bootstrap needs only `/bin/sh`, `tar`, `gzip` and `uname`. Everything
  goes under `--home`: the interpreter and a virtualenv in
  `runtime/VERSION-pyPYTHON-ARCH/`, and the launcher `bin/mmw`, which sets
  `MMW_SITE_HOME` to that home unless you set it yourself.
- A home on a share mounted by machines of different architectures works:
  run the bootstrap once on a machine of each, and `bin/mmw` runs that
  machine's install (or says how to install one).
- A machine of an architecture the bundle lacks is told which ones the
  bundle has (exit 2). On the connected side, `runtime_arches = ["x86_64",
  "aarch64"]` on the network ships both; `runtime_python` picks the Python
  (default `3.12`).
- A stage directory holds the same `.mmw/runtime/`: run its
  `bootstrap.sh` in place.

On the connected side, `mmw bundle --no-runtime` leaves the runtime out of
one bundle (it then rides the next), and `--runtime-wheel PATH` ships a
locally built mmw (`uv build`) when this version isn't on PyPI; `mmw stage`
takes both. The TUI bundles without the runtime, with a warning, when it
can't be built. Building a
runtime downloads from PyPI and python-build-standalone once; it is cached
under `{mirror_dir}/runtime/`. If a download fails, nothing is bundled.

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

### Updates to mmw itself

A bundle that brings a newer mmw — newer than both the one you are running
and the one `bin/mmw` runs — is applied by the mmw you are running; then
the new one is checked against its recorded sha256s, installed beside the
current one (with the bundle's own bootstrap) and `bin/mmw` switches to it:

```
! mmw updated to 1.4.0 — restart it (/mnt/oak/.mmw/site/bin/mmw)
```

The same mmw on a different `runtime_python` is installed too. Machines of
another architecture sharing the home are told the one command that
installs theirs. A damaged runtime is never installed. A failed install (a
full disk) leaves `bin/mmw` as it was and says why; the bundle's runtime stays in `{home}/runtime/incoming/` to
run its `bootstrap.sh` by hand. An older mmw is never installed unless you
ask: `mmw site apply … --allow-downgrade`. Older installs stay in
`{home}/runtime/` until you remove them.

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

## Placements: more than one root

Too big for one disk? Give the site more roots and put each repository in
one of them:

```bash
mmw site root add pypi /mnt/pypi --serve-path /export/pypi
mmw site apply bundle.tar --place PyPI=pypi       # a repository new to the site
mmw site move debian pypi                         # one already here, every version
mmw site root list
```

- `--serve-path` is where the **serving host** sees the root, when it
  mounts the share somewhere else: the generated Compose file mounts that.
- Each root gets its own `nginx/` (`mirror.conf` for just its hostnames,
  `compose.yaml`, the image), and its own store. Recreate nginx in each root
  the command names after an apply or a move.
- Two repositories that share a hostname must live in the same root —
  nginx routes a hostname to one place — so mmw refuses to split them.
- A move copies every kept version's files into the new root's store and
  places them before anything is recorded; an interrupted move just runs
  again. The old root keeps serving its copy — the nginx running there
  still routes the hostname to it — until you recreate nginx in both roots
  and run `mmw site tidy` (the next apply tidies too).
- A root that isn't mounted stops an apply or a move before anything changes.

## HTTPS

The far side makes its own keys: an mmw network's bundles never carry a
private key. Point the site at your CA (or make one), then sign:

```bash
mmw site ca load --cert /root/site-ca/ca.crt --key /root/site-ca/ca.key
mmw site ca make --out /root/site-ca --name "Lab CA" [--encrypt]   # no CA yet
mmw site certs                       # every root: key, request, certificate, HTTPS on
```

- mmw records **where** the CA is, nothing more: the key is never copied
  and its passphrase never kept — it is asked for each time (or read from
  `MMW_CA_PASSPHRASE`).
- Each root gets one certificate for exactly the hostnames its nginx serves,
  in `{root}/.mmw/tls/` (key 0600, directory 0700). A certificate that still
  fits, covers every hostname and isn't expiring within 30 days is kept;
  `--force` re-signs. The key is never regenerated.
- HTTPS turns on in a root's nginx only once its certificate fits its key;
  plain HTTP never stops. Recreate nginx in the roots the command names.
- An apply or a move says which root's certificate doesn't cover what it
  serves, or which root has none yet; `mmw site certs` puts it right. In
  the TUI, **c** signs.
- Changing CA re-signs every root on the next `mmw site certs`. A key an
  older bundle carried over (made on the connected side) is replaced once
  with one made here.

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

## The TUI

```bash
mmw site manage
```

Opens on every repository the site serves — what it serves, the newest
version here, held or current — with the selected repository's history
beside it, the served version marked ▶. Keys: **Enter** on a version serves
it (after a confirmation saying what changes), **f** forwards, **h** holds,
**d** deletes a version, **a** applies a bundle, **r** refreshes.

Applying from the TUI: pick the bundle (a `.tar` or a stage directory; the
last five are remembered), review it — every repository it changes, ticked
unless held; unticking one stores its new version and holds it — then
Apply. Progress shows the read rate and each repository switching; Stop
takes effect before any repository switches. The last screen says what was
done and what needs a person, such as recreating nginx.

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
