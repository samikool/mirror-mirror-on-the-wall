"""Opt-in: real Microsoft CLI → HTTPS nginx → stock-image gallery, offline.

MMW_VSCODE_E2E=1 enables this test; ordinary pytest never starts Docker.
See tests/e2e/README.md for tooling preparation and isolated local execution.
"""
import hashlib
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from mmw.core import certs, config, downloader, container_image
from mmw.core.pool import Pool
from mmw.core.publisher import bundle_network, publish_network
from mmw.core.publisher import nginx_container
from mmw.core.verify import verify_mirror
from mmw.providers.vscode import image, provider
from test_seam_golden import _build
from test_vscode_client_fixtures import source
from test_vscode_sync_e2e import _sync
from vscode_client_fixtures import ClientUpstream, SIGNED_EXTENSION

pytestmark = pytest.mark.skipif(os.environ.get('MMW_VSCODE_E2E') != '1',
                                reason='opt-in real Docker/Microsoft client: MMW_VSCODE_E2E=1')
CHECKS = Path(__file__).parent / 'e2e'


def _run(args, log, *, timeout=120):
    result = subprocess.run([str(a) for a in args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=timeout)
    with log.open('a') as stream:
        stream.write(' '.join(str(a) for a in args) + '\n' + result.stdout + '\n')
    assert result.returncode == 0, result.stdout
    return result.stdout


def _ca(root):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'mmw disposable e2e CA')])
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=1))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                   .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False,
                                  content_commitment=False, data_encipherment=False, key_agreement=False,
                                  key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False),
                                  critical=True).sign(key, hashes.SHA256()))
    ca, private = root/'mmw-e2e.crt', root/'ca.key'
    ca.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    private.chmod(0o600)
    return ca, private


class Stack:
    def __init__(self, scratch, root, ca, tls=None):
        self.scratch, self.root = scratch, root
        self.state = scratch/('client-'+root.name)
        self.state.mkdir()
        self.project = 'mmw-e2e-'+uuid.uuid4().hex[:12]
        self.log = scratch/('docker-'+root.name+'.log')
        self.expected = scratch/'checks'
        self.expected.mkdir(exist_ok=True)
        self.nginx_conf = scratch/('nginx-'+root.name+'.conf')
        self.nginx_conf.write_text('events {}\nhttp {\n access_log /dev/stdout;\n error_log /dev/stderr info;\n'
                                  ' include /srv/mmw/nginx/mirror.conf;\n}\n')
        self.override = scratch/('compose-'+root.name+'.yaml')
        client_image = os.environ.get('MMW_VSCODE_CLIENT_IMAGE', 'mmw-vscode-client:local')
        nginx_image = os.environ.get('MMW_VSCODE_NGINX_IMAGE', 'nginx:latest')
        client_volumes = [{'type': 'bind', 'source': str(ca),
                           'target': '/usr/local/share/ca-certificates/mmw-e2e.crt', 'read_only': True},
                          {'type': 'bind', 'source': str(CHECKS.resolve()), 'target': '/scripts', 'read_only': True},
                          {'type': 'bind', 'source': str(self.expected), 'target': '/checks', 'read_only': True},
                          {'type': 'bind', 'source': str(self.state), 'target': '/state'}]
        if code_root := os.environ.get('MMW_VSCODE_ROOT'):
            code_root = Path(code_root).resolve()
            assert (code_root/'resources/app/product.json').exists(), 'MMW_VSCODE_ROOT must be a Microsoft desktop installation'
            client_volumes.append({'type': 'bind', 'source': str(code_root), 'target': '/opt/code', 'read_only': True})
        aliases = ['marketplace.visualstudio.com', 'update.code.visualstudio.com',
                   'vscode.download.prss.microsoft.com', 'main.vscode-cdn.net',
                   'mmw-fixture.gallerycdn.vsassets.io', 'mmw-fixture.gallery.vsassets.io',
                   'github.gallerycdn.vsassets.io', 'github.gallery.vsassets.io',
                   'archive.ubuntu.com', 'uncovered.fixture.invalid']
        nginx_volumes = [{'type': 'bind', 'source': str(root), 'target': '/srv/mmw', 'read_only': True},
                         {'type': 'bind', 'source': str(self.nginx_conf), 'target': '/etc/nginx/nginx.conf',
                          'read_only': True}]
        if tls:
            nginx_volumes.extend({'type': 'bind', 'source': str(path),
                                  'target': '/etc/mmw/tls/' + name, 'read_only': True}
                                 for path, name in zip(tls, ('mirror.crt', 'mirror.key')))
        health = {'interval': '1s', 'timeout': '5s', 'retries': 30, 'start_period': '1s'}
        self.override.write_text(f'''services:
  gallery:
    init: true
    ports: !reset []
    pull_policy: never
    restart: "no"
    networks:
      isolated:
        aliases: {json.dumps(aliases)}
    healthcheck:
      interval: 1s
      timeout: 5s
      retries: 30
  nginx:
    image: {json.dumps(nginx_image)}
    pull_policy: never
    network_mode: service:gallery
    depends_on:
      gallery:
        condition: service_healthy
    volumes: {json.dumps(nginx_volumes)}
    healthcheck: {json.dumps(dict(health, test=['CMD', 'nginx', '-t']))}
  client:
    init: true
    image: {json.dumps(client_image)}
    pull_policy: never
    command: ["bash", "-c", "update-ca-certificates > /state/ca-import.log 2>&1 && exec sleep infinity"]
    environment:
      NODE_EXTRA_CA_CERTS: /usr/local/share/ca-certificates/mmw-e2e.crt
      HTTP_PROXY: ""
      HTTPS_PROXY: ""
      ALL_PROXY: ""
      http_proxy: ""
      https_proxy: ""
      all_proxy: ""
    volumes: {json.dumps(client_volumes)}
    networks: [isolated]
    depends_on:
      nginx:
        condition: service_healthy
    healthcheck: {json.dumps(dict(health, test=['CMD', 'test', '-f', '/etc/ssl/certs/mmw-e2e.pem']))}
networks:
  isolated:
    internal: true
''')
        self.command = ['docker', 'compose', '-p', self.project, '-f', root/'vscode-gallery/compose.yaml',
                        '-f', self.override]

    def compose(self, *args, timeout=120):
        return _run([*self.command, *args], self.log, timeout=timeout)

    def __enter__(self):
        try:
            rendered = json.loads(self.compose('config', '--format', 'json'))
            assert rendered['networks']['isolated']['internal']
            assert all(not s.get('ports') for s in rendered['services'].values())
            self.compose('up', '-d', '--wait', '--wait-timeout', '60')
            self.code('--version')
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        try:
            self.compose('logs', '--no-color')
        finally:
            self.compose('down', '--timeout', '2')

    def __exit__(self, *args):
        self.close()

    def code(self, *args):
        return self.compose('exec', '-T', 'client', '/opt/code/bin/code',
                            '--user-data-dir=/state/user', '--extensions-dir=/state/extensions', *args)

    def probe(self):
        self.compose('exec', '-T', 'client', 'python3', '/scripts/client-probe.py')
        assert json.loads((self.state/'probe.json').read_text())['tls'] == 'verified'

    def install(self, extension, version):
        output = self.code('--install-extension', extension, '--force', '--log', 'trace', '--verbose')
        assert f'Extension signature verification result for {extension}: Success.' in output, output
        listed = self.code('--list-extensions', '--show-versions')
        assert f'{extension}@{version}' in listed.lower(), listed
        installed = list((self.state/'extensions').glob(f'{extension}-{version}*/package.json'))
        assert len(installed) == 1, installed
        assert json.loads(installed[0].read_text())['version'] == version


def _expected(fake, root, scratch):
    catalog = json.loads((root/'vscode-gallery/catalog.json').read_text())
    payloads = {str(httpx.URL(f['source'])): hashlib.sha256(fake.payloads[str(httpx.URL(f['source']))]).hexdigest()
                for e in catalog for v in e['versions'] for f in v['files']}
    static = {}
    for rel in ['archive.ubuntu.com/ubuntu/pool/main/h/hello/hello_1.0_amd64.deb',
                'main.vscode-cdn.net/extensions/marketplace.json']:
        static['https://'+rel] = hashlib.sha256((root/rel).read_bytes()).hexdigest()
    result = {'extensions': {f"{e['publisher']['publisherName']}.{e['extensionName']}".lower(): e['versions'][0]['version']
                             for e in catalog}, 'payloads': payloads, 'static': static,
              'updates': json.loads((root/'vscode-gallery/updates.json').read_text())}
    (scratch/'checks/expected.json').write_text(json.dumps(result))
    return result


def test_stock_client_live_publish_and_applied_bundle(tmp_path, monkeypatch):
    scratch = tmp_path.resolve()
    scratch.chmod(0o755)  # nginx workers must traverse the disposable published tree.
    fake = ClientUpstream()
    fake.set_signed_version('6.3.4')
    monkeypatch.setattr(provider, '_client', fake.client)
    monkeypatch.setattr(downloader, '_thread_client', fake.client)
    monkeypatch.setattr(image, 'DOCKER', 'docker')  # Explicit opt-in; never enable Docker for ordinary tests.
    monkeypatch.setattr(nginx_container, 'DOCKER', 'docker')
    original_run = container_image._run
    def cached_images_only(docker, *args, **kwargs):
        if args[0] == 'pull':
            return ''
        return original_run(docker, *args, **kwargs)
    monkeypatch.setattr(container_image, '_run', cached_images_only)
    mirror_dir, apt_cfg = _build(scratch/'apt')
    src = source()
    cfg = apt_cfg.model_copy(update={'sources': [*apt_cfg.sources, src]})
    for number, commit in enumerate(('a'*40, 'b'*40, 'c'*40, 'd'*40), 1):
        fake.set_commit(commit, number)
        units, summaries = _sync(mirror_dir, src)
        assert all(s.ok for s in summaries)
    with Pool(mirror_dir, read_only=True) as pool:
        assert all(verify_mirror(u, mirror_dir, pool).complete for u in units)
    net = config.NetworkConfig(name='e2e-'+uuid.uuid4().hex[:12], root=scratch/'published',
                               certs=True, gallery_port=8080, bundle_dir=scratch/'bundles')
    ca, key = _ca(scratch)
    _, _, csr = certs.generate_key_and_csr(certs.collect_hostnames(net, cfg), certs.cert_dir(net, cfg))
    certs.sign_csr(csr, ca, key)
    published = publish_network(net, cfg, mirror_dir)
    assert not published.errors and not published.warnings, (published.errors, published.warnings)
    assert (net.root/'vscode-gallery'/image.TAR_NAME).is_file()
    with Stack(scratch, net.root, ca, tls=certs.servable_paths(net, cfg)) as stack:
        expected = _expected(fake, net.root, scratch)
        assert len(expected['updates']['server-linux-x64']['stable']['builds']) == 3
        assert len(expected['updates']['linux-x64']['stable']['builds']) == 1
        stack.probe()
        stack.install(SIGNED_EXTENSION, '6.3.4')
        identity = stack.compose('ps', '-q', 'gallery').strip()
        fake.set_extension('1.1.0')
        fake.set_signed_version('6.3.5')
        _, summaries = _sync(mirror_dir, src)
        assert all(s.ok for s in summaries)
        assert not publish_network(net, cfg, mirror_dir).errors
        _expected(fake, net.root, scratch)
        stack.probe()
        stack.install(SIGNED_EXTENSION, '6.3.5')
        assert stack.compose('ps', '-q', 'gallery').strip() == identity
        logs = stack.compose('logs', '--no-color', 'nginx')
        assert 'POST /_apis/public/gallery/extensionquery' in logs
        assert 'GET /extensions/github/github-vscode-theme/6.3.5/' in logs
        assert 'Microsoft.VisualStudio.Services.VsixSignature' in logs
    bundled = bundle_network(net, cfg, mirror_dir)
    assert not bundled.errors and not bundled.warnings
    far = scratch/'applied'
    far.mkdir()
    _run([sys.executable, bundled.out_path.parent/'apply.py', bundled.out_path, '--root', far],
         scratch/'apply.log')
    for file in (net.root/'vscode-gallery').iterdir():
        assert (far/'vscode-gallery'/file.name).read_bytes() == file.read_bytes(), file.name
    _run(['docker', 'load', '-i', far/'vscode-gallery'/image.TAR_NAME], scratch/'image-load.log')
    with Stack(scratch, far, ca) as stack:
        _expected(fake, far, scratch)
        stack.probe()
        stack.install(SIGNED_EXTENSION, '6.3.5')
    unchanged = bundle_network(net, cfg, mirror_dir)
    assert not unchanged.errors
    import tarfile
    with tarfile.open(unchanged.out_path) as archive:
        assert not any(m.name == 'vscode-gallery/'+image.TAR_NAME for m in archive.getmembers())
