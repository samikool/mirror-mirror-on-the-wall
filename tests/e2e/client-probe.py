"""Run inside the isolated client, using its trusted test CA and real DNS."""
import hashlib
import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

expected = json.loads(Path('/checks/expected.json').read_text())


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = urllib.request.build_opener(NoRedirect)


def fetch(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as response:
        return response.code, dict(response.headers), response.read()


# A real fixture client cannot accidentally get a passing response upstream.
try:
    connection = socket.create_connection(('1.1.1.1', 443), timeout=1)
except OSError:
    pass
else:
    connection.close()
    raise AssertionError('client network unexpectedly has an external route')

body = {'filters': [{'criteria': [{'filterType': 7, 'value': ident}
                                for ident in expected['extensions']], 'pageSize': 10}], 'flags': 914}
status, _, response = fetch('https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery', body)
assert status == 200, (status, response)
found = json.loads(response)['results'][0]['extensions']
assert {f"{e['publisher']['publisherName']}.{e['extensionName']}".lower() for e in found} == set(expected['extensions'])
for extension in found:
    version = extension['versions'][0]
    ident = f"{extension['publisher']['publisherName']}.{extension['extensionName']}".lower()
    assert version['version'] == expected['extensions'][ident]
    for file in version['files']:
        url = file['source']
        status, _, data = fetch(url)
        assert status == 200, (url, status, data)
        parsed = urllib.parse.urlsplit(url)
        canonical = parsed._replace(netloc=parsed.netloc.lower()).geturl()
        assert hashlib.sha256(data).hexdigest() == expected['payloads'][canonical]
        fallback = version['fallbackAssetUri'] + '/' + file['assetType']
        status, headers, _ = fetch(fallback)
        assert status == 302 and headers['Location'] == url, (fallback, status, headers)
        assert fetch(headers['Location'])[2] == data

for ident, qualities in expected['updates'].items():
    for quality, entry in qualities.items():
        prefix = 'https://update.code.visualstudio.com'
        status, _, data = fetch(f'{prefix}/api/update/{ident}/{quality}/{entry["latest"]}')
        assert status == 204 and data == b''
        status, _, data = fetch(f'{prefix}/api/update/{ident}/{quality}/{"0"*40}')
        assert status == 200 and json.loads(data) == entry['builds'][entry['latest']]
        assert data == json.dumps(entry['builds'][entry['latest']], sort_keys=True).encode(), 'update JSON differs from pooled bytes'
        for commit, build in entry['builds'].items():
            status, headers, _ = fetch(f'{prefix}/commit:{commit}/{ident}/{quality}')
            assert status == 302 and headers['Location'] == build['url']
            status, _, archive = fetch(headers['Location'])
            assert status == 200 and hashlib.sha256(archive).hexdigest() == build['sha256hash']
        assert fetch(f'{prefix}/commit:{"f"*40}/{ident}/{quality}')[0] == 404

for url, digest in expected['static'].items():
    status, _, data = fetch(url)
    assert status == 200 and hashlib.sha256(data).hexdigest() == digest

assert fetch('https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery', {'filters': []})[0] == 400
assert fetch('https://marketplace.visualstudio.com/not-a-gallery-route')[0] == 404
try:
    fetch('https://uncovered.fixture.invalid/')
except urllib.error.URLError as error:
    assert isinstance(error.reason, ssl.SSLCertVerificationError), error
else:
    raise AssertionError('certificate verification unexpectedly accepted an uncovered host')

Path('/state/probe.json').write_text(json.dumps({'extensions': expected['extensions'],
                                              'updates': sorted(expected['updates']), 'tls': 'verified',
                                              'outside_route': False}, indent=2))
print('HTTPS query, payloads, signatures, fallback redirects, update bytes/204, commit redirects, APT and CA verified')
