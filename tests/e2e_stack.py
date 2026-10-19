"""The Docker harness both real-client tests share: the network's own
nginx/compose.yaml with its host paths filled in, plus a test-only override —
an internal network, nginx answering for the provider's hostnames, no host
ports, and a client container that trusts the disposable CA."""
import json
import os
import subprocess
import uuid


def _run(args, log, *, timeout=120):
    result = subprocess.run([str(a) for a in args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=timeout)
    with log.open('a') as stream:
        stream.write(' '.join(str(a) for a in args) + '\n' + result.stdout + '\n')
    assert result.returncode == 0, result.stdout
    return result.stdout

HEALTH = {"interval": "1s", "timeout": "5s", "retries": 30, "start_period": "1s"}
NO_PROXY_ENV = {k: "" for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")}


class ComposeStack:
    PREFIX = "mmw-e2e-"
    ALIASES: tuple = ()
    CLIENT_IMAGE = ("MMW_E2E_CLIENT_IMAGE", "")       # (environment override, default)
    NGINX_IMAGE = ("MMW_E2E_NGINX_IMAGE", "nginx:latest")
    CLIENT_ENV: dict = {}

    def __init__(self, scratch, root, ca, network):
        self.scratch, self.root, self.ca = scratch, root, ca
        self.state = scratch / ("client-" + root.name)
        self.state.mkdir()
        self.project = self.PREFIX + uuid.uuid4().hex[:12]
        self.log = scratch / ("docker-" + root.name + ".log")
        self.deployed = scratch / ("compose-deploy-" + root.name + ".yaml")
        self.deployed.write_text((root / "nginx/compose.yaml").read_text().replace(f"/path/to/{network}", str(root)))
        self.override = scratch / ("compose-" + root.name + ".yaml")
        volumes = [{"type": "bind", "source": str(ca),
                    "target": "/usr/local/share/ca-certificates/mmw-e2e.crt", "read_only": True},
                   *self.client_volumes(),
                   {"type": "bind", "source": str(self.state), "target": "/state"}]
        self.override.write_text(f"""services:
{self.extra_services()}  nginx:
    image: {json.dumps(os.environ.get(*self.NGINX_IMAGE))}
    ports: !reset []
    restart: "no"
    networks:
      isolated:
        aliases: {json.dumps(list(self.ALIASES))}
    healthcheck: {json.dumps(dict(HEALTH, test=["CMD", "nginx", "-t"]))}
  client:
    init: true
    image: {json.dumps(os.environ.get(*self.CLIENT_IMAGE))}
    pull_policy: never
    command: ["bash", "-c", "update-ca-certificates > /state/ca-import.log 2>&1 && touch /tmp/ca-ready && exec sleep infinity"]
    environment: {json.dumps({**NO_PROXY_ENV, **self.CLIENT_ENV})}
    volumes: {json.dumps(volumes)}
    networks: [isolated]
    depends_on:
      nginx:
        condition: service_healthy
    healthcheck: {json.dumps(dict(HEALTH, test=["CMD", "test", "-f", "/tmp/ca-ready"]))}
networks:
  isolated:
    internal: true
""")
        self.command = ["docker", "compose", "-p", self.project, "-f", self.deployed, "-f", self.override]

    # ── hooks ────────────────────────────────────────────────────────────────
    def client_volumes(self) -> list:
        return []

    def extra_services(self) -> str:
        """YAML for services beside nginx and the client, indented two spaces."""
        return ""

    def started(self) -> None:
        """Runs once the stack is healthy."""

    # ── lifecycle ────────────────────────────────────────────────────────────
    def compose(self, *args, timeout=300):
        return _run([*self.command, *args], self.log, timeout=timeout)

    def __enter__(self):
        try:
            rendered = json.loads(self.compose("config", "--format", "json"))
            assert rendered["networks"]["isolated"]["internal"]
            assert all(not s.get("ports") for s in rendered["services"].values())
            self.compose("up", "-d", "--wait", "--wait-timeout", "60")
            self.started()
            return self
        except BaseException:
            try:
                self.close()
            except Exception:  # noqa: BLE001 — the start failure is the error to report
                pass
            raise

    def close(self):
        try:
            self.compose("logs", "--no-color")
            # The client writes as root; hand its files back so a re-run on the
            # same scratch path can clean them up.
            self.compose("exec", "-T", "client", "chown", "-R", f"{os.getuid()}:{os.getgid()}", "/state")
        except AssertionError:
            pass
        finally:
            self.compose("down", "--timeout", "2")

    def __exit__(self, *args):
        self.close()
