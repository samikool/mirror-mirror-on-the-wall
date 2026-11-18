"""Directory aliases to the suite itself are not APT components."""
import gzip
import hashlib

import httpx
import pytest

from mmw.core import discover
from mmw.core.config import MirrorConfig
from mmw.providers.apt import AptProvider

BASE = 'https://security.example/debian-security'
SUITE = 'bookworm-security'
ROOT = f'/debian-security/dists/{SUITE}'
BODY = b'hello package'
INDEX = gzip.compress((f'Package: hello\nVersion: 1\nArchitecture: amd64\nFilename: pool/hello.deb\n'
                       f'Size: {len(BODY)}\nSHA256: {hashlib.sha256(BODY).hexdigest()}\n\n').encode())
SHA = hashlib.sha256(INDEX).hexdigest()
RELEASE = (f'Origin: Debian\nLabel: Debian-Security\nSuite: oldstable-security\nCodename: {SUITE}\n'
           f'Date: Thu, 15 Oct 2026 22:24:30 UTC\nArchitectures: amd64\nComponents: updates/main\n'
           f'SHA256:\n {SHA} {len(INDEX)} main/binary-amd64/Packages.gz\n').encode()
LISTING = b'<a href="main/">main/</a><a href="updates/">updates/</a>'
SNAPSHOT_LISTING = b'<a href="main/">main/</a><a href="updates">updates</a> -&gt; <a href=".">.</a>'


@pytest.fixture
def archive(monkeypatch):
    pages = {ROOT + '/': LISTING,
             ROOT + '/InRelease': RELEASE,
             ROOT + '/updates/InRelease': RELEASE,
             ROOT + '/main/': b'<a href="binary-amd64/">binary-amd64/</a>',
             ROOT + '/main/binary-amd64/Packages.gz': INDEX,
             ROOT + '/main/binary-amd64/by-hash/SHA256/' + SHA: INDEX,
             '/debian-security/pool/hello.deb': BODY}
    calls = []

    def serve(request):
        calls.append(request.url.path)
        body = pages.get(request.url.path)
        return httpx.Response(200 if body is not None else 404, content=body or b'')
    real = httpx.Client

    def client(*args, **kwargs):
        kwargs['transport'] = httpx.MockTransport(serve)
        return real(*args, **kwargs)
    monkeypatch.setattr(httpx, 'Client', client)
    return pages, calls, client


def test_discovery_excludes_suite_alias_but_keeps_real_empty_component(archive):
    pages, _, _ = archive
    pages[ROOT + '/'] += b'<a href="empty/">empty/</a>'
    assert discover.list_components(BASE, SUITE) == ['main', 'empty']


def test_discovery_does_not_filter_when_release_is_unavailable(archive):
    pages, _, _ = archive
    del pages[ROOT + '/InRelease']
    assert discover.list_components(BASE, SUITE) == ['main', 'updates']


def test_existing_alias_unit_accepts_snapshot_symlink_listing(archive):
    pages, _, client = archive
    pages[ROOT + '/'] = SNAPSHOT_LISTING
    unit = MirrorConfig(name='old-updates', provider='apt', source=BASE, source_url=BASE,
                        suite=SUITE, component='updates', arch='amd64')
    with client() as http:
        packages, metadata = AptProvider().fetch_all(unit, http)
    assert packages == [] and metadata and all(f.is_release for f in metadata)


def test_unlisted_typo_is_not_accepted_even_if_metadata_is_rewritten(archive):
    pages, _, client = archive
    pages[ROOT + '/typo/InRelease'] = RELEASE
    unit = MirrorConfig(name='bad', provider='apt', source=BASE, source_url=BASE,
                        suite=SUITE, component='typo', arch='amd64')
    with client() as http, pytest.raises(ValueError, match='lists no typo/binary-amd64/Packages'):
        AptProvider().fetch_all(unit, http)


def test_cli_all_components_uses_filtered_discovery(archive, tmp_path):
    from typer.testing import CliRunner
    from mmw.cli import app
    from mmw.core import config
    source = config.SourceConfig(url=BASE, suites=[SUITE], arches=['amd64'])
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / 'mirror'), sources=[source])
    config.save(cfg, tmp_path)
    result = CliRunner().invoke(app, ['--config', str(tmp_path), 'sync', '--quiet', '--dry-run'])
    assert result.exit_code == 0, result.output
    assert 'updates' not in result.output.split('all components →', 1)[-1].split('\n', 1)[0]
    assert 'main' in result.output


def test_alias_detection_is_not_specific_to_updates(archive):
    pages, _, _ = archive
    pages[ROOT + '/'] = b'<a href="main/">main/</a><a href="alias/">alias/</a>'
    pages[ROOT + '/alias/InRelease'] = RELEASE
    assert discover.list_components(BASE, SUITE) == ['main']


def test_non_alias_link_without_slash_is_not_an_empty_component(archive):
    pages, _, client = archive
    pages[ROOT + '/'] = SNAPSHOT_LISTING
    pages[ROOT + '/updates/InRelease'] = RELEASE + b'\nDifferent: yes\n'
    unit = MirrorConfig(name='bad-alias', provider='apt', source=BASE, source_url=BASE,
                        suite=SUITE, component='updates', arch='amd64')
    with client() as http, pytest.raises(ValueError, match='lists no updates/binary-amd64/Packages'):
        AptProvider().fetch_all(unit, http)
