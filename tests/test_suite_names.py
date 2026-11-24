"""Suites are named by their dists/ directory, with the alias their Release
gives them now: `oldstable` is bookworm only until the next Debian release."""
import pytest

from mmw.site.tracks import suite_heading, suite_names


@pytest.mark.parametrize('name, suite, codename, want', [
    ('deb.debian.org/debian/dists/bookworm', 'oldstable', 'bookworm', ('bookworm', 'oldstable')),
    ('deb.debian.org/debian/dists/bookworm-updates', 'oldstable-updates', 'bookworm-updates',
     ('bookworm-updates', 'oldstable-updates')),
    ('deb.debian.org/debian/dists/stable', 'stable', 'trixie', ('stable', 'trixie')),
    ('archive.ubuntu.com/ubuntu/dists/jammy-updates', 'jammy-updates', 'jammy', ('jammy-updates', None)),
    ('archive.ubuntu.com/ubuntu/dists/jammy', 'jammy', 'jammy', ('jammy', None)),
    ('security.debian.org/dists/bullseye/updates', 'oldoldstable', 'bullseye-security',
     ('bullseye/updates', 'oldoldstable')),
    ('dists/sid', 'unstable', 'sid', ('sid', 'unstable')),
    ('example.org/flat', 'flat', None, ('flat', None)),     # a flat repository: no dists/
    ('', None, None, ('files', None)),
])
def test_suite_names(name, suite, codename, want):
    assert suite_names(name, suite, codename) == want


def test_suite_heading():
    assert suite_heading('bookworm', 'oldstable') == 'bookworm (oldstable)'
    assert suite_heading('jammy', None) == 'jammy'


def test_far_side_names_the_same():
    """The far side's history heading and its pin line follow the directory too."""
    from mmw.site.db import TrackInfo
    from mmw.site.ops import dist_of, heading_of
    t = TrackInfo(1, 'r', 'deb.debian.org/debian/dists/bookworm', 'oldstable', 'bookworm', None, None)
    assert heading_of(t) == 'bookworm (oldstable)' and dist_of(t) == 'bookworm'
    flat = TrackInfo(2, 'r', '', None, None, None, None)
    assert heading_of(flat) == dist_of(flat) == 'files'
