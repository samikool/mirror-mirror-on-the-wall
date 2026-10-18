# VS Code client end-to-end test

This opt-in test runs the Microsoft desktop CLI against the real generated
nginx configuration, test TLS certificates and `python:3.13-slim` gallery.
It verifies a mixed APT/VS Code network, signed GitHub theme installation,
signed extension updates and synthetic catalog refresh, static assets and
signatures, fallback redirects, update JSON/204, retained server/CLI redirects and hashes, live
catalog reload, a real gallery image tar, bundle/apply replay and an unchanged
delta. Ordinary `pytest` skips it and never runs real Docker.

Prepare tooling once, with internet access (Docker Engine/Compose, Linux x64):

```bash
docker pull python:3.13-slim
docker pull nginx:latest
docker build -t mmw-vscode-client:local -f tests/e2e/client.Dockerfile tests/e2e
```

The client image contains the official Microsoft desktop distribution, fetched
from `update.code.visualstudio.com`. The default is current stable; to repeat
a particular client version use `--build-arg VSCODE_VERSION=VERSION`.
The test prints the actual client's version into its log.

From the project root:

```bash
MMW_VSCODE_E2E=1 uv run pytest -q tests/test_vscode_client_e2e.py \
  --basetemp=.superpowers/work/vscode-client-e2e
```

Use a fresh scratch path for each run. Failure logs, nginx/gallery logs,
CLI traces and the downloaded/installed payloads remain there for inspection.
Projects get unique names; `finally` tears down only that project's containers
and network. All images must already exist (`pull_policy: never`). The gallery
image helper skips its pull step only for this explicit offline test and uses
real Docker inspect/save/load on the cached image.

To reuse a desktop installation and a cached image with its shared-library
dependencies, instead of building the default client image:

```bash
MMW_VSCODE_E2E=1 \
MMW_VSCODE_CLIENT_IMAGE=mcr.microsoft.com/playwright:v1.63.0-noble \
MMW_VSCODE_ROOT=/usr/share/code \
uv run pytest -q tests/test_vscode_client_e2e.py \
  --basetemp=.superpowers/work/vscode-client-local
```

The desktop installation is mounted read-only. Client settings/extensions
live solely in scratch storage. Host DNS, CA stores, extensions and existing
services are unchanged. The test Compose override adds nginx and a client
solely for testing; production compose still runs one gallery service. Nginx
shares the gallery network namespace so the generated localhost proxy works
without rewriting configuration. The scratch tree is mounted read-only at
its original absolute path, and no service exposes a host port.

The Docker network is internal; known fixture Microsoft hostnames are DNS
aliases for nginx. The probe checks that the client has no external route and
that an uncovered hostname fails TLS validation. The disposable CA is trusted
in the OS store and via `NODE_EXTRA_CA_CERTS` for the CLI's bundled Node
runtime. This is [additional CA trust](https://nodejs.org/api/cli.html#node_extra_ca_certsfile),
not a certificate-verification bypass. Product endpoints and signature
verification settings remain at their defaults. Installation uses the
[standard Code CLI](https://code.visualstudio.com/docs/configure/command-line).

The signed theme is a recorded, unmodified Marketplace extension with its MIT
license retained. Synthetic server/editor archives prove protocol and exact
payload delivery; this test does not launch a real editor update or an SSH
server. Real upstream editor/server payload processing is covered by the
provider's recorded metadata fixtures and checksum/retention tests.

# PyPI client end-to-end test

This opt-in test proves the PyPI far side with stock clients: **pip** from
`python:3.13-slim` and mmw's own bundled **uv**, in an isolated container,
against the generated nginx with TLS at `pypi.org` and
`files.pythonhosted.org`. There are no index flags: the clients find the mirror
the way `PYPI.md` tells users to, through DNS (Compose aliases here) and a
trusted CA (`UV_NATIVE_TLS=1` for uv). Ordinary `pytest` skips it.

The fixture graph is real: wheels with correct RECORD hashes, a flit-built
sdist, and the committed `tests/fixtures/pypi/real/flit_core-3.9.0-py3-none-any.whl`
(BSD-3, sha256 `7aada352fb0c7f5538c4fafeddf314d3a6a92ee8e2b1de70482329e42de70301`).
It checks:

- `pip install mmw-a` gets the pinned `mmw-b 1.0`, not the newer 2.0;
- `mmw-c`, an sdist only, builds offline with pip and with uv, using flit_core
  fetched from the mirror;
- `pip install --require-hashes -r` with upstream hashes works;
- a `uv.lock` written against the mirror names `files.pythonhosted.org` URLs
  and `uv sync --frozen` works from a fresh copy;
- the straight mirror carries these anyway, so the test also asserts that the
  requirements unit itself selected the sdist and its build tool;
- after bundle → `apply.py` on an empty directory, the far side renders its
  own `pypi.org/` pages, the installs work again, and the hashed requirements
  file and `uv.lock` made on the connected side work there unchanged (synced
  with an empty uv cache);
- TLS really verifies: an uncovered hostname fails certificate verification
  (an HTTP answer would fail the test), `/simple/` serves
  PEP 691 JSON only on request, and the client has no external route.

Prepare once, with internet access:

```bash
docker pull python:3.13-slim
docker pull nginx:latest
```

Both client tests share one Compose harness (`tests/e2e_stack.py`).

From the project root:

```bash
mkdir -p .superpowers/work
MMW_PYPI_E2E=1 uv run pytest -q tests/test_pypi_client_e2e.py \
  --basetemp=.superpowers/work/pypi-client-e2e
```

`MMW_PYPI_CLIENT_IMAGE` and `MMW_PYPI_NGINX_IMAGE` override the images.
