"""nginx for one root of a site: the network's own generator, run over just
the repositories placed in that root, with Compose mounting the path the
serving host sees."""
import json

from mmw.core.config import MirrorConfig
from mmw.core.publisher.nginx import deployment_nginx_files, split_for_nginx
from mmw.core.repos import repository_key
from mmw.site.home import RootConfig


def site_nginx_files(settings_json: str, placed: set[str], root: RootConfig, *,
                     primary: bool) -> dict[str, bytes]:
    """`nginx/…` path → bytes for `root`. `placed`: the repository keys it
    serves. Hosts that belong to no repository (upstream files, extra hosts)
    are the primary root's."""
    recorded = json.loads(settings_json or "{}")
    net = recorded.get("network", {})
    if not net.get("nginx", True):
        return {}
    mirrors = [m for m in (MirrorConfig.model_validate(d) for d in recorded.get("mirrors", []))
               if repository_key(m) in placed]
    extra = []
    if primary:
        extra = list(recorded.get("upstream_hosts", []))
        extra += [h for h in recorded.get("extra_hosts", []) if h not in extra]
    if not mirrors and not extra:
        return {}
    static, sites = split_for_nginx(mirrors)
    name = f"{net.get('name', 'network')}-{root.name}"
    files = deployment_nginx_files(static, network_name=name, tls_include=net.get("certs", True) or None,
                                   extra_hosts=extra, sites=sites)
    placeholder = f"/path/to/{name}".encode()
    return {f"nginx/{rel.split('/', 1)[1]}" if rel.startswith("nginx/") else rel:
            data.replace(placeholder, str(root.serve_path).encode()) for rel, data in files.items()}
