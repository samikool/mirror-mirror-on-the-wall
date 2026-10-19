"""Per-unit index records, and the widest retention window over networks."""
from mmw.core import config
from mmw.providers.pypi.index import DistFile
from mmw.providers.pypi.records import Record, dump, load, record_of, widest_window

SRC = "https://pypi.org/simple"


def _file(**kw):
    base = dict(project="a", filename="a-1.0-py3-none-any.whl",
                url="https://files.pythonhosted.org/packages/ab/cd/ef/a-1.0-py3-none-any.whl",
                sha256="ab" * 32, size=10, version="1.0", kind="wheel", requires_python=">=3.9",
                yanked=False, metadata_sha256="cd" * 32, upload_time="2026-01-01T00:00:00Z")
    return DistFile(**{**base, **kw})


def test_records_round_trip_and_are_byte_stable():
    r1, r2 = record_of(_file()), record_of(_file(filename="a-1.1-py3-none-any.whl", version="1.1",
                                                     yanked="broken"))
    recs = {"files.pythonhosted.org/x": r1, "files.pythonhosted.org/a": r2}
    assert load(dump(recs)) == recs
    assert dump(recs) == dump(dict(reversed(list(recs.items()))))
    assert r2.yanked == "broken" and r1.upload_time == "2026-01-01T00:00:00Z"
    assert load(None) == {} and load(b"garbage") == {}
    assert isinstance(r1, Record)


def _net(name, retention, select=()):
    return config.NetworkConfig(name=name, retention=retention,
                                select=[config.NetworkSelect(source=s) for s in select])


def test_widest_window():
    def cfg(*nets):
        return config.Config(settings=config.Settings(mirror_dir="/tmp/x"),
                             sources=[config.PypiSource(provider="pypi", all=True)], networks=list(nets))
    assert widest_window(cfg(_net("a", "30d", [SRC]), _net("b", "forever")), SRC) is None
    assert widest_window(cfg(_net("a", "current", [SRC]), _net("b", "7d", [SRC])), SRC) == 7
    assert widest_window(cfg(_net("a", "30d", ["https://elsewhere.example"])), SRC) == 0
    assert widest_window(cfg(), SRC) == 0
