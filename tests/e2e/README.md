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
docker pull nginx:1.27-alpine
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
