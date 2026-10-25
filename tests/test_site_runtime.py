"""A site updates its own mmw when a bundle brings a newer one."""
import pytest

from mmw.core import runtime as R
from mmw.site.apply import apply_bundle

from test_dry_run_totals import two_mirrors  # noqa: F401 — the shared pool fixture
from test_site_apply import _bundle, env  # noqa: F401 — the site fixture
from test_snapshot_repo_versions import _change_a

# Stands in for bootstrap.sh: logs how it was run, writes a launcher.
STUB = """#!/bin/sh
echo "$*" >> "$2/bootstrap.log"
[ -f "$4/FAIL" ] && exit 1
mkdir -p "$2/bin" && printf '#!/bin/sh\\necho new\\n' > "$2/bin/mmw"
"""


@pytest.fixture
def stub_bootstrap(tmp_path, monkeypatch):
    import mmw.resources
    p = tmp_path / "stub-bootstrap.sh"
    p.write_text(STUB)
    monkeypatch.setattr(mmw.resources, "BOOTSTRAP", p)
    return p


def _wheel(tmp_path, version):
    w = tmp_path / f"mirror_mirror_on_the_wall-{version}-py3-none-any.whl"
    w.write_bytes(b"wheel")
    return w


def test_newer_runtime_installs_and_switches(env, tmp_path, stub_bootstrap):  # noqa: F811
    cfg, net, md, home, root = env
    b = _bundle(cfg, net, md, tmp_path / "1", from_ref=None, runtime_wheel=_wheel(tmp_path, "9.0.0"))
    r = apply_bundle(home, str(b), say=lambda *a: None)
    assert r.updated                                               # the apply itself went through
    assert f"mmw updated to 9.0.0 — restart it ({home}/bin/mmw)" in r.actions
    assert (home / "bootstrap.log").read_text().split() == [
        "--home", str(home), "--from", str(home / "runtime/incoming")]
    assert (home / "bin/mmw").read_text() == "#!/bin/sh\necho new\n"
    assert not (home / "runtime/incoming.tmp").exists()


def test_same_version_does_nothing(env, tmp_path, stub_bootstrap):  # noqa: F811
    cfg, net, md, home, root = env
    r = apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=lambda *a: None)
    assert not [a for a in r.actions if "mmw" in a and "updated" in a]
    assert not (home / "bootstrap.log").exists() and not (home / "bin").exists()


def test_downgrade_refused(env, tmp_path, stub_bootstrap):  # noqa: F811
    from mmw.commands import site as cmd
    cfg, net, md, home, root = env
    b = _bundle(cfg, net, md, tmp_path / "1", from_ref=None, runtime_wheel=_wheel(tmp_path, "0.1.0"))
    r = apply_bundle(home, str(b), say=lambda *a: None)
    assert r.updated
    assert any(f"mmw 0.1.0, older than this mmw {R.mmw_version()}" in a and "--allow-downgrade" in a
               for a in r.actions)
    assert not (home / "bootstrap.log").exists()
    _change_a(md)
    b = _bundle(cfg, net, md, tmp_path / "2", runtime_wheel=_wheel(tmp_path, "0.1.0"), from_ref=None)
    assert cmd.apply(str(b), home, allow_downgrade=True) == 0
    assert (home / "bin/mmw").read_text() == "#!/bin/sh\necho new\n"


def test_failed_update_keeps_launcher(env, tmp_path):  # noqa: F811
    """The real bootstrap, given the fake fetcher's runtime (its interpreter
    archive isn't one): the install fails, the old launcher stays."""
    cfg, net, md, home, root = env
    (home / "bin").mkdir(parents=True)
    (home / "bin/mmw").write_text("#!/bin/sh\necho old\n")
    b = _bundle(cfg, net, md, tmp_path / "1", from_ref=None, runtime_wheel=_wheel(tmp_path, "9.0.0"))
    r = apply_bundle(home, str(b), say=lambda *a: None)
    assert r.updated
    assert any("mmw 9.0.0 didn't install" in a for a in r.actions), r.actions
    assert (home / "bin/mmw").read_text() == "#!/bin/sh\necho old\n"
    assert not (home / "runtime/9.0.0").exists()


def test_dry_run_takes_no_runtime(env, tmp_path, stub_bootstrap):  # noqa: F811
    cfg, net, md, home, root = env
    b = _bundle(cfg, net, md, tmp_path / "1", from_ref=None, runtime_wheel=_wheel(tmp_path, "9.0.0"))
    apply_bundle(home, str(b), say=lambda *a: None, dry_run=True)
    assert not (home / "runtime").exists() and not (home / "bootstrap.log").exists()


# ── review findings ──────────────────────────────────────────────────────────

def _incoming(tmp_path, net, version="9.0.0"):
    """A bundle's runtime as it lands on the far side."""
    from test_runtime import FakeFetcher
    import shutil
    members = R.runtime_files(net, None, tmp_path / "pool", fetcher=FakeFetcher(), wheel=_wheel(tmp_path, version))
    inc = tmp_path / "site-home" / "runtime" / "incoming.tmp"
    for name, data in members.items():
        dest = inc / name[len(".mmw/runtime/"):]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data) if isinstance(data, bytes) else shutil.copy(data, dest)
    return inc.parent.parent, inc


def test_a_damaged_runtime_is_not_installed(env, tmp_path, stub_bootstrap):  # noqa: F811
    from mmw.site.runtime import update_runtime
    cfg, net, md, home, root = env
    home, inc = _incoming(tmp_path, net)
    uv = inc / "x86_64" / "uv"
    uv.write_bytes(uv.read_bytes() + b"flipped")
    notes = update_runtime(home, inc)
    assert any("damaged" in n and "9.0.0" in n for n in notes), notes
    assert not (home / "bootstrap.log").exists()


def test_an_installed_newer_mmw_is_not_switched_back(env, tmp_path, stub_bootstrap):  # noqa: F811
    from mmw.site.runtime import update_runtime
    cfg, net, md, home, root = env
    home, inc = _incoming(tmp_path, net, "9.0.0")
    (home / "runtime" / "CURRENT").write_text("9.5.0 3.12\n")
    notes = update_runtime(home, inc)
    assert any("older than this mmw 9.5.0" in n for n in notes), notes
    assert not (home / "bootstrap.log").exists()


def test_a_new_python_reinstalls(env, tmp_path, stub_bootstrap):  # noqa: F811
    from mmw.site.runtime import update_runtime
    cfg, net, md, home, root = env
    home, inc = _incoming(tmp_path, net, R.mmw_version())
    (home / "runtime" / "CURRENT").write_text(f"{R.mmw_version()} 3.11\n")
    notes = update_runtime(home, inc)
    assert any("updated" in n for n in notes) and (home / "bootstrap.log").exists()


def test_a_rebuilt_wheel_reinstalls(env, tmp_path, stub_bootstrap):  # noqa: F811
    from mmw.site.runtime import update_runtime
    cfg, net, md, home, root = env
    home, inc = _incoming(tmp_path, net, R.mmw_version())
    name = f"{R.mmw_version()}-py3.12-123"
    (home / "runtime" / "CURRENT").write_text(f"{R.mmw_version()} 3.12 {name}\n")
    (home / "runtime" / f"{name}-x86_64").mkdir()
    (home / "runtime" / f"{name}-x86_64" / "mmw.whl").write_bytes(b"an older build")
    notes = update_runtime(home, inc)
    assert any("updated" in n for n in notes) and (home / "bootstrap.log").exists()
