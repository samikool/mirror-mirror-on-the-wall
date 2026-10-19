"""A unit's release history: when each file was first kept and when it left."""
from mmw.providers.base import PackageInfo
from mmw.providers.pypi.history import advance, dump, load, retained


def pkg(name):
    return PackageInfo(name=f"{name}==1.0", url=f"https://files.pythonhosted.org/packages/{name}.whl",
                       checksum="ab" * 32, checksum_type="sha256", size=10,
                       repo_path=f"files.pythonhosted.org/packages/{name}.whl", filename=f"{name}.whl",
                       item_id=f"{name}==1.0")


def test_new_files_get_today():
    h = advance({}, [pkg("a")], "2026-10-18")
    e = h["files.pythonhosted.org/packages/a.whl"]
    assert e.first_kept == "2026-10-18" and e.left_at is None


def test_leaving_sets_left_at_once():
    h = advance({}, [pkg("a"), pkg("b")], "2026-10-01")
    h = advance(h, [pkg("b")], "2026-10-02")
    h = advance(h, [pkg("b")], "2026-10-09")
    a = h["files.pythonhosted.org/packages/a.whl"]
    assert a.first_kept == "2026-10-01" and a.left_at == "2026-10-02"


def test_returning_clears_left_at():
    h = advance(advance({}, [pkg("a")], "2026-10-01"), [], "2026-10-02")
    h = advance(h, [pkg("a")], "2026-10-03")
    e = h["files.pythonhosted.org/packages/a.whl"]
    assert e.left_at is None and e.first_kept == "2026-10-01"


def test_retained_are_exactly_left_entries():
    h = advance(advance({}, [pkg("a"), pkg("b")], "2026-10-01"), [pkg("b")], "2026-10-02")
    (p,) = retained(h)
    assert p == pkg("a")


def test_dump_is_byte_stable_and_load_tolerates_garbage():
    h = advance({}, [pkg("b"), pkg("a")], "2026-10-01")
    assert dump(h) == dump(dict(reversed(list(h.items()))))
    assert load(dump(h)) == h
    assert load(None) == {} and load(b"not gzip") == {}
