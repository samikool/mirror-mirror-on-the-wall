"""Deterministic installable VSIX and build payloads for the isolated client test.

This publisher is synthetic and unsigned. No Marketplace payload is downloaded
or patched; the fixture's own upstream URLs and bytes flow through real sync.
"""
import gzip
import hashlib
import io
import json
import tarfile
import uuid
import zipfile
from pathlib import Path

import httpx

from mmw.providers.vscode.platforms import builds
from vscode_fakes import FakeUpstream

PUBLISHER = 'mmw-fixture'
EXTENSION = PUBLISHER + '.hello'
SIGNATURE_TYPE = 'Microsoft.VisualStudio.Services.VsixSignature'
SIGNED_EXTENSION = 'github.github-vscode-theme'
SIGNED_FIXTURES = Path(__file__).parent / 'fixtures' / 'vscode-client'


def manifest(version):
    return {'name': 'hello', 'publisher': PUBLISHER, 'version': version,
            'displayName': 'mmw offline fixture', 'description': 'Offline protocol fixture',
            'engines': {'vscode': '^1.60.0'}, 'main': './extension.js',
            'activationEvents': [], 'contributes': {}}


def vsix(version):
    data = io.BytesIO()
    xml = f'''<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
<Metadata><Identity Language="en-US" Id="hello" Version="{version}" Publisher="{PUBLISHER}"/>
<DisplayName>mmw offline fixture</DisplayName><Description xml:space="preserve">Offline fixture</Description></Metadata>
<Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation>
<Dependencies/><Assets><Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/></Assets>
</PackageManifest>'''
    files = {'extension.vsixmanifest': xml,
             '[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="json" ContentType="application/json"/><Default Extension="js" ContentType="application/javascript"/><Default Extension="md" ContentType="text/plain"/><Default Extension="vsixmanifest" ContentType="text/xml"/></Types>',
             'extension/package.json': json.dumps(manifest(version), sort_keys=True),
             'extension/extension.js': 'exports.activate = () => {};\n',
             'extension/README.md': '# mmw offline fixture\n'}
    with zipfile.ZipFile(data, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            info = zipfile.ZipInfo(name, (2024, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content)
    return data.getvalue()


def build_archive(identifier, commit):
    data = io.BytesIO()
    body = f'fixture {identifier} {commit}\n'.encode()
    with tarfile.open(fileobj=data, mode='w') as archive:
        info = tarfile.TarInfo('fixture.txt')
        info.size = len(body)
        archive.addfile(info, io.BytesIO(body))
    return gzip.compress(data.getvalue(), mtime=0)


class ClientUpstream(FakeUpstream):
    def __init__(self):
        super().__init__()
        self.payloads = {}
        self.version = '1.0.0'
        self.signed_version = '6.3.5'
        self.set_extension(self.version)
        self.set_commit('a' * 40, 1)

    def set_extension(self, version):
        self.version = version
        asset = f'https://{PUBLISHER}.gallerycdn.vsassets.io/extensions/{PUBLISHER}/hello/{version}/1700000000000'
        fallback = f'https://{PUBLISHER}.gallery.vsassets.io/_apis/public/gallery/publisher/{PUBLISHER}/extension/hello/{version}/assetbyname'
        files = {'Microsoft.VisualStudio.Services.VSIXPackage': vsix(version),
                 'Microsoft.VisualStudio.Code.Manifest': json.dumps(manifest(version)).encode(),
                 'Microsoft.VisualStudio.Services.Content.Details': b'# mmw offline fixture\n'}
        for kind, content in files.items():
            self.payloads[asset + '/' + kind] = content
        self.catalog = {EXTENSION: {
            'extensionId': str(uuid.uuid5(uuid.NAMESPACE_URL, EXTENSION)),
            'extensionName': 'hello', 'displayName': 'mmw offline fixture',
            'shortDescription': 'Offline protocol fixture', 'flags': 'validated, public',
            'publisher': {'publisherId': str(uuid.uuid5(uuid.NAMESPACE_URL, PUBLISHER)),
                          'publisherName': PUBLISHER, 'displayName': PUBLISHER},
            'publishedDate': '2024-01-01T00:00:00Z', 'lastUpdated': '2024-01-01T00:00:00Z',
            'statistics': [{'statisticName': 'install', 'value': 1}],
            'versions': [{'version': version, 'lastUpdated': '2024-01-01T00:00:00Z',
                          'targetPlatform': 'universal', 'assetUri': asset, 'fallbackAssetUri': fallback,
                          'properties': [{'key': 'Microsoft.VisualStudio.Code.Engine', 'value': '^1.60.0'}],
                          'files': [{'assetType': kind, 'source': asset + '/' + kind} for kind in files]}]}}
        response = 'signed-gallery.json' if self.signed_version == '6.3.5' else 'signed-history.json'
        signed = json.loads((SIGNED_FIXTURES / response).read_text())['results'][0]['extensions'][0]
        signed['versions'] = [v for v in signed['versions'] if v['version'] == self.signed_version]
        self.catalog[SIGNED_EXTENSION] = signed
        for file in signed['versions'][0]['files']:
            folder = SIGNED_FIXTURES if self.signed_version == '6.3.5' else SIGNED_FIXTURES/self.signed_version
            self.payloads[str(httpx.URL(file['source']))] = (folder / file['assetType']).read_bytes()
        self.all_versions = self.catalog

    def set_signed_version(self, version):
        self.signed_version = version
        self.set_extension(self.version)

    def set_commit(self, commit, timestamp):
        self.commit = commit
        for kind in ('desktop', 'server'):
            for identifier in builds(kind, 'linux-x64'):
                url = f'https://vscode.download.prss.microsoft.com/dbazure/download/stable/{commit}/{identifier}.tar.gz'
                payload = build_archive(identifier, commit)
                self.payloads[url] = payload
                self.update_overrides[identifier] = {
                    'url': url, 'name': '1.95.2', 'productVersion': '1.95.2', 'version': commit,
                    'timestamp': timestamp, 'sha256hash': hashlib.sha256(payload).hexdigest()}

    def __call__(self, request):
        key = str(request.url).split('?', 1)[0]
        if key in self.payloads:
            self.requests.append(request)
            return httpx.Response(200, content=self.payloads[key])
        return super().__call__(request)
