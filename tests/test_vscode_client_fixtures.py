"""Offline fixture contracts used by the opt-in real-client stack."""
import hashlib
import io
import json
import zipfile

from mmw.core.pool import Pool
from mmw.core.verify import verify_mirror
from mmw.providers.vscode import provider
from mmw.core import downloader, config
from test_vscode_sync_e2e import _sync
from vscode_client_fixtures import ClientUpstream, EXTENSION, SIGNED_EXTENSION, SIGNED_FIXTURES, manifest, vsix


def source():
    return config.VscodeSource(provider='vscode', kinds=['extensions', 'desktop', 'server'],
                               platforms=['linux-x64'], qualities=['stable'],
                               extensions={'ids': [EXTENSION, SIGNED_EXTENSION]})


def test_vsix_is_deterministic_and_contains_the_declared_extension():
    payload = vsix('1.0.0')
    assert payload == vsix('1.0.0')
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert json.loads(archive.read('extension/package.json')) == manifest('1.0.0')
        assert archive.read('extension/extension.js') == b'exports.activate = () => {};\n'
        assert 'extension.vsixmanifest' in archive.namelist()


def test_client_fixture_syncs_and_verifies_all_kinds(tmp_path, monkeypatch):
    fake = ClientUpstream()
    monkeypatch.setattr(provider, '_client', fake.client)
    monkeypatch.setattr(downloader, '_thread_client', fake.client)
    units, summaries = _sync(tmp_path, source())
    assert all(s.ok for s in summaries), [s.errors + s.metadata_errors for s in summaries]
    assert {u.dims['kind'] for u in units} == {'extensions', 'desktop', 'server'}
    with Pool(tmp_path, read_only=True) as pool:
        for unit in units:
            result = verify_mirror(unit, tmp_path, pool)
            assert result.complete, result.describe()
        ext = next(u for u in units if u.dims == {'kind': 'extensions'})
        rel = next(p for p in pool.ref_map(ext.name) if '/mmw-fixture/hello/' in p and p.endswith('VSIXPackage'))
        assert pool.resolve(ext.name, rel).read_bytes() == vsix('1.0.0')
    for build in fake.update_overrides.values():
        assert hashlib.sha256(fake.payloads[build['url']]).hexdigest() == build['sha256hash']


def test_recorded_signed_payloads_match_their_manifest_and_upstream_vsix_hash():
    records = json.loads((SIGNED_FIXTURES/'payloads.json').read_text())
    for record in records:
        data = (SIGNED_FIXTURES/record.get('file', record['assetType'])).read_bytes()
        assert len(data) == record['bytes']
        assert hashlib.sha256(data).hexdigest() == record['sha256']
    fake = ClientUpstream()
    for selected in ('6.3.4', '6.3.5'):
        fake.set_signed_version(selected)
        version = fake.catalog[SIGNED_EXTENSION]['versions'][0]
        upstream_hash = next(p['value'] for p in version['properties']
                             if p['key'] == 'Microsoft.VisualStudio.Services.VsixSha256')
        file = next(f for f in version['files'] if f['assetType'] == 'Microsoft.VisualStudio.Services.VSIXPackage')
        import httpx
        assert hashlib.sha256(fake.payloads[str(httpx.URL(file['source']))]).hexdigest() == upstream_hash
