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

- A **root** is a folder nginx serves, laid out as usual:
  `{root}/{hostname}/{upstream path}`. It can be a local disk or a share
  (an NFS export is fine). Keep each root on one filesystem.
- Each root **describes itself**: its `.mmw/` holds its store (one file
  per distinct content; every served file is a hardlink into it), a small
  database of every version it was sent, what it serves, its nginx setup
  and a lock. Point any mmw on any machine at it and it knows what it is.
- Every root of one connected-side network's mirror forms a **set**. Each
  hostname lives in exactly one root; the files that belong to no
  hostname (keys, the nginx image) go into every root. Roots know each
  other by name, never by path — the same share can be mounted anywhere.
- **This machine** only remembers which roots it has opened
  (`~/.local/state/mmw/roots.json`) and where the CA is. Deleting that
  loses nothing but convenience: `mmw site open PATH` finds a root again.

## Installing mmw

The far side needs no Python of its own and no network. mmw comes as its
own small file, apart from any bundle: `mmw-<version>-<arch>.tar`, holding
the `uv` binary, a standalone Python (the glibc 2.17 build, so RHEL 8 and
newer), mmw with every dependency as wheels, and `install.sh`. On the
connected side, tick **Include mmw installer** on the Transfer page (or
`mmw bundle --installer`) to write it beside a bundle, or write it on its
own with `mmw installer --out DIR`. Carry it across, then:

```bash
tar -xf mmw-1.3.0-x86_64.tar
sh mmw-1.3.0/install.sh                 # or --prefix DIR for a system-wide install
```

```
mmw 1.3.0 installed — /home/sam/.local/bin/mmw
```

- The installer needs only `/bin/sh`, `tar`, `gzip` and `uname`. The
  interpreter and a virtualenv go in
  `~/.local/share/mmw/VERSION-pyPYTHON-BUILD-ARCH/`, the launcher in
  `~/.local/bin/mmw` (it says so when that isn't on your `PATH`).
- Installing a newer one installs beside the old and switches the
  launcher; the old stays until you remove it.
- One file per architecture: `runtime_arches = ["x86_64", "aarch64"]` on
  the network writes both; `runtime_python` picks the Python (default
  `3.12`).
- From a source checkout, the installer ships that checkout's own build
  (`uv build`), never PyPI's release of the same version;
  `--runtime-wheel PATH` ships a wheel of your choosing.

## Apply a bundle

The first apply makes the roots it names — there is no setup step:

```bash
mmw site apply /media/drive/oak-20261024T101500Z.tar --root '*'=/mnt/oak       # every hostname here
mmw site apply oak.tar --root deb.debian.org=/mnt/big --root '*'=/mnt/oak       # or split them
```

After that, each hostname goes to the root that holds it; only a hostname
new to the mirror needs `--root`:

```bash
mmw site apply /media/drive/oak-20261031T101500Z.tar
mmw site apply /media/drive/oak-20261031T101500Z/         # a stage directory
cat /media/drive/oak.tar | ssh server mmw site apply -    # a tar on stdin
```

```
applied 20261031T101500Z
  [big]
  deb.debian.org/debian  v14 → v15
  [oak]
  VS Code  v11 → v12
! nginx config changed — recreate nginx on the serving host (docker compose … up -d --force-recreate)
```

- The bundle is read once. Before anything is written, each root's free
  space is checked against what it will receive (files it already has
  don't count); a root without room stops the apply, and says so.
- Everything arrives in the store and is checked against the bundle's
  manifest **before anything served changes**: a damaged, truncated or
  incomplete bundle leaves every root as it was — copy it again.
- A hostname switches whole: all its repositories together, then the
  next. Stop takes effect before the next hostname.
- A root that isn't mounted holds back only its own hostnames, by name;
  the rest apply.
- A repository whose new version needs files a root never received (it
  missed an earlier bundle) is refused on its own, with the
  connected-side command that fixes it; the rest still apply.
- A bundle from another network is refused.
- An older bundle, or one already applied, never moves what a repository
  serves: its versions are stored, and `mmw site serve` can use them.

### Updates to mmw itself

The far side never checks a network: its newer mmw is the installer file
that travels next to a bundle. A transfer includes it whenever the
connected side's mmw is newer than the last one sent to that network.

When you apply a bundle with a newer `mmw-<version>-<arch>.tar` next to
it, mmw asks first ("Update now?" in the TUI, `[Y/n]` in a terminal). Yes
updates **every machine of the mirror** — each ssh root, then this one —
restarts, and carries on applying the same bundle with the new mmw. If any
root can't take it (no installer for its architecture next to the bundle,
or it can't be reached), nothing is updated and the message says which.

- A new major version (1.x → 2.x) warns that it may change how mmw works
  and defaults to No.
- No is asked again next time.
- `MMW_NO_UPDATE_CHECK=1` turns the offer off (cron, scripts).
- Older installs stay in place next to the new one.

Bundles made before the installer existed carried mmw's runtime inside
them: it is ignored, with the note "this bundle carries an mmw runtime —
ignored; install mmw from its installer file", and the content applies as
usual.

## Serve another version

```bash
mmw site history debian         # every version kept here
mmw site serve debian v6        # relinks; asks first (--yes to skip)
mmw site serve debian 28oct     # the same, by the bundle that brought it
mmw site status                 # what each repository serves, held or current
```

A version is `v6` (or `6`), or the bundle that brought it: its label (any
case), its date (`28oct`, `oct 28`, `28 October`, `28/10` — day first —
or `2026-10-28`), or the start of its stamp (`20261028T09`). A day and
month alone means the most recent one, up to today; an older year has to
be said (`2024-10-28`), and the error says when one exists. Dates are the
bundle's stamp, in UTC. Two bundles on one day are not guessed between —
the error names both, and the stamp picks one.

To put the whole mirror back to how it was after one bundle:

```bash
mmw site serve-bundle 28oct     # every repository at its newest version from then
```

- A repository that bundle didn't change serves its version from before it.
- One that first arrived later, or whose version from then has since been
  deleted, is left as it is, and the list says which.
- A root that isn't here is named, and its repositories are left as they are.
- It lists each change and asks first (`--yes` to skip). The TUI's **As of…**
  (**b**) does the same.

Every repository it moves off its newest version is held there; those
already at their newest are not, so the next bundle moves them on.
`mmw site forward --all` moves the held ones on too. If one switch fails
part-way (a lock, an ssh root dropping), the ones before it stay switched,
and the error says which were served and which were not.

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

## More than one root

```bash
mmw site roots                      # the mirror's roots, their hostnames, or MISSING
mmw site open /mnt/nas              # a root this machine hasn't opened yet
mmw site move debian big            # a repository, every kept version, to another root
mmw site tidy                       # once nginx is recreated in both: clear the old copy
```

- Each root gets its own `nginx/` (`mirror.conf` for just its hostnames,
  `compose.yaml`, the image). Recreate nginx in each root the command names.
- A hostname is served from one root, so a repository that shares one —
  `deb.debian.org` carries both `debian` and `debian-security` — can't move
  on its own: the whole host moves, every repository on it, and Move says
  so before it asks.
- A move copies every kept version's files into the new root first, then
  switches them together; Stop during the copy switches nothing, and
  moving again picks up from what was copied. An interrupted switch just
  runs again. In the TUI (**m**) it runs on the same progress page as an
  apply, and Move lists each root with where it is — *on HOST · /path* for
  one over ssh.
  The old root keeps serving its copy until you recreate nginx in both
  roots and run `mmw site tidy`.
- Every command takes `--root PATH` (any root of the mirror) when this
  machine has opened more than one mirror.

## Roots over ssh

A root can live on another machine: give its location as
`[user@]host:/path` anywhere a root goes (`--root`, `mmw site open`,
**Add a root…**). The mmw on that host does the root's disk work; this one
streams it only that root's files and shows its progress.

```bash
mmw site open lnxp005:/data/mirror --install mmw-1.3.0-x86_64.tar   # first time: put mmw there
mmw site apply oak.tar --root pypi.org=lnxp005:/data/mirror
```

- **Key-based ssh only, never a prompt.** mmw runs the system `ssh` with
  `BatchMode=yes`. If plain `ssh lnxp005` doesn't get in without a
  password, mmw stops: *lnxp005 needs key-based ssh access for mmw (a
  security requirement) — configure it and try again*.
- **Host keys are yours to accept.** An unknown or changed host key stops
  mmw too; connect once with plain `ssh` to check it.
- **Nothing secret, ever.** mmw never stores, copies, reads, logs or shows a
  private key, a passphrase or a password. Keys stay in your agent and
  `~/.ssh/config` (aliases work).
- **Certificates:** the remote root makes its own key and request; only the
  request comes back to be signed, and only the certificate goes out. The
  CA key never moves.
- The remote account's login shell must be a POSIX shell (sh, bash,
  dash, zsh): mmw quotes every remote command for one. fish and csh are
  untested.
- The remote's mmw must be the same version or newer; `--install` pushes
  the installer over the same ssh. In the TUI (Add root, Open a mirror,
  placing a bundle's hostnames) a machine without mmw, or with an older
  one, gets the offer instead: it finds `mmw-<version>-<arch>.tar` for
  that machine beside a recently applied bundle — or asks for one — and
  installs it before carrying on. A host that can't be reached shows as
  MISSING (*can't reach lnxp005*) and its hostnames are held back.

## HTTPS

The far side makes its own keys: an mmw network's bundles never carry a
private key. Point the site at your CA (or make one), then sign:

```bash
mmw site ca load --cert /root/site-ca/ca.crt --key /root/site-ca/ca.key   # recorded on this machine
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

Opens on a card for every repository the site serves — what it serves
against the newest version here, held or current, its hostnames, root, size
and versions kept. Beside it, the selected repository in full (its root,
the bundle it serves from, what old versions would free, https, the last
apply) and its versions, the served one marked ▶, with Serve, Forward, Hold
and Delete under them. Keys: **Enter** on a version serves it (after a
confirmation saying what changes), **f** forwards, **h** holds, **d** deletes
a version, **a** applies a bundle, **r** adds a root (an empty folder or
`host:/path` becomes a new root of this mirror), **m** moves a repository to
another root, **b** serves the whole mirror as of a bundle, **c** manages
certificates, **t** tidies, **ctrl+r** refreshes.
The buttons along the bottom do the same.

Applying from the TUI: pick the bundle (a `.tar` or a stage directory; the
last five are remembered), review it — every repository it changes, ticked
unless held; unticking one stores its new version and holds it — then
Apply. Progress shows the read rate and each repository switching; Stop
takes effect before any repository switches. The last screen says what was
done and what needs a person, such as recreating nginx.

## One operator at a time

Commands that change a root take the lock in its `.mmw/`. A second one says
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
