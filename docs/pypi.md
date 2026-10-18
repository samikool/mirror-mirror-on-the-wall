# PyPI

mmw mirrors pypi.org and serves it offline at PyPI's own hostnames. On the far
side, `pip install` and `uv add` work with stock settings: no `--index-url`,
no `--find-links`. Every package the mirror lists installs, dependencies
included.

---

## What it mirrors

One `pypi` source, two ways to choose what goes in. They can be used
together, and they publish into one index.

| | What | Guarantee |
|---|---|---|
| `all = true` | Every project's latest release a client can install, plus the older releases its dependencies require ("latest + closure") | `pip install <pkg>` works for every package listed |
| `requirements` | Your list, resolved **together** by uv, like `pip download -r`, but for every target at once, plus the build tools for anything built from source | `pip install -r <the list>` works |

Everything is per **target**: a Python version × platform × arch.

- **Platforms:** `linux-glibc` (most distros), `linux-musl` (Alpine), `windows`, `macos`.
- **Arches:** `amd64`, `arm64`.
- **OS floors** (`glibc`, `musl`, `macos`; defaults `2.31` = Ubuntu 20.04,
  `1.2`, `11.0`) name the oldest far-side systems. A release only counts as
  installable if a machine that old can install it.

Anything that can't be guaranteed is listed in each unit's report, never
dropped silently. That includes a dependency with no compatible file, or an
extra that pulls in something unmirrored.

## Add it

```bash
mmw add pypi --python 3.12 --platform linux-glibc --arch amd64 --all
mmw add pypi --python 3.12 --req "django>=5" --req numpy --requirements-file reqs.txt
```

or in `mmw.toml`:

```toml
[[sources]]
provider     = "pypi"
pythons      = ["3.12"]
platforms    = ["linux-glibc", "windows"]
arches       = ["amd64"]
all          = true
requirements = ["django>=5"]
requirements_file = "reqs.txt"     # next to mmw.toml; re-read every sync
```

`mmw dry-run` shows the exact size first. The straight mirror of all of PyPI
for one target is roughly 0.7–1.3 TB.

Requirements lines take names, versions, extras and markers. Options (`-r`,
`-e`), URLs, local paths and `--hash` are refused.

> Like `pip download -r`, resolving the list may build an sdist to read its
> metadata, which runs that package's build backend on the mirror host. Build
> requirements are static only: what a backend asks for at build time is not
> mirrored.

## Retention

When a newer release replaces one, the old one leaves the keep set. Each
network decides how long it stays published, so lock files and pinned
requirements keep working:

```toml
[[networks]]
name = "lab"
retention = "90d"   # current | forever | <N>d   (default 90d)
```

The pool keeps a file while any network that selects the source still wants
it, so the widest window wins. Widening is not retroactive: what was already
pruned does not come back.

## The far side

Publishing writes `pypi.org/…` (generated PEP 503/691 pages and
`/pypi/<name>/json`) and `files.pythonhosted.org/…` (the files at their real
paths, with PEP 658 metadata), plus the nginx sites and certificate names for
both hosts. Each network gets a `PYPI.md` with:

- DNS or hosts entries for `pypi.org` and `files.pythonhosted.org`;
- trusting the mmw CA:
  - pip ≥ 24.2 on Python ≥ 3.10 uses the system store; older pip uses `PIP_CERT`;
  - uv uses `UV_NATIVE_TLS=1` or `SSL_CERT_FILE`;
  - requests-based tools use `REQUESTS_CA_BUNDLE`.
