"""Adopted trees stay until someone removes them: listed with what removing
each frees (only what nothing else holds), removed only when asked."""
import pytest

from mmw.site.adopt import originals, remove_original

from apt_tree import make_tree
from test_site_adopt import BASE, _adopt, site  # noqa: F401
from test_site_adopt_cli import run, site as cli_site  # noqa: F401


def test_originals_are_listed_with_what_they_free(site, tmp_path):  # noqa: F811
    home, _root = site
    src = make_tree(tmp_path / "t", extra={"project/trace/x": b"stray" * 10})
    _adopt(home, src)
    (o,) = originals(home)
    assert o.path == src.resolve() and o.present
    assert o.frees == 50                       # only the stray: everything adopted is linked into the store


def test_removing_one_keeps_serving(site, tmp_path):  # noqa: F811
    home, root = site
    src = make_tree(tmp_path / "t")
    _adopt(home, src)
    remove_original(home, src)
    assert not src.exists() and originals(home) == []
    assert (root / BASE / "pool/main/h/hello/hello_1.0_amd64.deb").read_bytes() == b"hello" * 50


def test_a_root_or_inside_one_is_refused(site, tmp_path):  # noqa: F811
    home, root = site
    with pytest.raises(ValueError, match="not an adopted tree"):
        remove_original(home, tmp_path / "nowhere")
    from mmw.site.adopt import record_original
    record_original(home, root / BASE, "20261115T000000Z")
    with pytest.raises(ValueError, match="mirror"):
        remove_original(home, root / BASE)
    record_original(home, root.parent, "20261115T000000Z")
    with pytest.raises(ValueError, match="mirror"):
        remove_original(home, root.parent)


def test_removal_needs_yes(cli_site, tmp_path):  # noqa: F811
    src = make_tree(tmp_path / "t")
    run("adopt", str(src), "--root", str(cli_site), "--as", BASE, "--unverified", "--yes")
    r = run("adopted", "--root", str(cli_site))
    assert r.exit_code == 0 and str(src.resolve()) in r.output and "frees" in r.output
    r = run("adopted", "--root", str(cli_site), "--remove", str(src))
    assert r.exit_code == 1 and "--yes" in r.output and src.exists()
    r = run("adopted", "--root", str(cli_site), "--remove", str(src), "--yes")
    assert r.exit_code == 0 and not src.exists()


def test_removing_an_original_takes_the_lock_and_reads_only_its_own_record(tmp_path, monkeypatch):
    from pathlib import Path

    from mmw.site import adopt as A
    from mmw.site import home as H
    from mmw.site.lock import LockHeld, SiteLock
    home = H.init_root(tmp_path / "srv", name="main", set_id="s1", network="oak")
    for name in ("one", "two"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "f").write_bytes(b"x")
        A.record_original(home, tmp_path / name, "20261101T000000Z")
    seen = []
    real = A._files
    monkeypatch.setattr(A, "_files", lambda p: seen.append(p) or real(p))
    with SiteLock(home), pytest.raises(LockHeld):
        A.remove_original(home, tmp_path / "one")
    assert (tmp_path / "one").exists()
    A.remove_original(home, tmp_path / "one")
    assert not (tmp_path / "one").exists() and (tmp_path / "two").exists()
    assert all(Path(p).name != "two" for p in seen)               # the other tree is never walked
