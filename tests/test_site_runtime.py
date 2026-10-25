"""A bundle's mmw runtime (v1 bundles carried one) is ignored: mmw comes
from its installer now, and self-update waits for its own design."""
import io
import json
import tarfile

from mmw.site.apply import apply_bundle

from test_dry_run_totals import two_mirrors  # noqa: F401 — the shared pool fixture
from test_site_apply import _bundle, env  # noqa: F401 — the site fixture

NOTE = "this bundle carries an mmw runtime — ignored; install mmw from its installer file"


def _with_runtime(bundle):
    """A v1-style bundle: the same tar with `.mmw/runtime/` members appended."""
    with tarfile.open(bundle, "a") as tar:
        for name, data in ((".mmw/runtime/RUNTIME.json",
                            json.dumps({"mmw": "9.0.0", "python": "3.12", "arches": ["x86_64"]}).encode()),
                           (".mmw/runtime/bootstrap.sh", b"#!/bin/sh\nexit 1\n"),
                           (".mmw/runtime/x86_64/wheels/mirror_mirror_on_the_wall-9.0.0-py3-none-any.whl",
                            b"wheel")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return bundle


def test_old_bundle_with_runtime_applies_and_ignores_it(env, tmp_path):  # noqa: F811
    cfg, net, md, home, root = env
    b = _with_runtime(_bundle(cfg, net, md, tmp_path / "1", from_ref=None))
    r = apply_bundle(home, str(b), say=lambda *a: None)
    assert r.updated                                               # the content applied
    assert r.actions.count(NOTE) == 1
    assert not (home / "runtime").exists() and not (home / "bin").exists()


def test_dry_run_mentions_ignored_runtime(env, tmp_path):  # noqa: F811
    cfg, net, md, home, root = env
    b = _with_runtime(_bundle(cfg, net, md, tmp_path / "1", from_ref=None))
    r = apply_bundle(home, str(b), dry_run=True, say=lambda *a: None)
    assert f"  {NOTE}" in r.lines
    assert not [line for line in r.lines if "would be installed" in line]


def test_a_bundle_without_runtime_says_nothing_of_it(env, tmp_path):  # noqa: F811
    cfg, net, md, home, root = env
    r = apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=lambda *a: None)
    assert NOTE not in r.actions
