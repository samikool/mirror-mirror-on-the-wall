"""A network with PyPI units publishes a PyPI-shaped tree pip and uv read."""
import asyncio
import concurrent.futures
import json

from mmw.core import config
from mmw.core.pool import Pool
from mmw.core.publisher import publish_network, publish_snapshot
from mmw.core.publisher.nginx import nginx_files
from mmw.core.resolve import resolve_sources
from mmw.core.snapshots import take_snapshot
from mmw.core.sync_engine import sync_mirror
from mmw.providers import get_provider
from mmw.providers.base import ResolveContext
from pypi_fakes import file_entry, metadata_text


def _src(**kw):
    return config.PypiSource(provider="pypi", pythons=["3.12"], platforms=["linux-glibc"], arches=["amd64"],
                             **{"all": True, **kw})


def _cfg(tmp_path, src, retention="90d", sources=None):
    return config.Config(settings=config.Settings(mirror_dir=tmp_path / "m"),
                         sources=[src] if sources is None else sources,
                         networks=[config.NetworkConfig(name="lab", root=tmp_path / "pub", nginx=False,
                                                        retention=retention)])


def _sync(cfg):
    units, _, errors = resolve_sources(cfg.sources, context=ResolveContext.from_config(cfg))
    assert not errors, errors
    ex = concurrent.futures.ThreadPoolExecutor(4)
    mirror_dir = cfg.settings.mirror_dir

    async def go():
        with Pool(mirror_dir) as pool:
            return [await sync_mirror(u, pool=pool, mirror_dir=mirror_dir, max_connections=4, executor=ex)
                    for u in units]
    try:
        assert all(s.ok for s in asyncio.run(go()))
    finally:
        ex.shutdown()
    return units


def _publish(cfg, retention=None):
    net = cfg.networks[0] if retention is None else cfg.networks[0].model_copy(update={"retention": retention})
    result = publish_network(net, cfg, cfg.settings.mirror_dir)
    assert not [e for r in result.mirrors for e in r.errors]
    return cfg.networks[0].root


def _bump_pysocks(up):
    up.projects["pysocks"]["files"].append(
        file_entry("pysocks-9.0.0-py3-none-any.whl", metadata=metadata_text("pysocks", "9.0.0")))
    up.projects["pysocks"]["serial"] += 1


def _anchors(root, project):
    return (root / "pypi.org" / "simple" / project / "index.html").read_text().count("<a ")


def test_publish_lays_out_both_hosts(tmp_path, up):
    cfg = _cfg(tmp_path, _src())
    _sync(cfg)
    root = _publish(cfg)
    wheels = list((root / "files.pythonhosted.org" / "packages").rglob("requests-*.whl"))
    assert wheels and all(w.with_name(w.name + ".metadata").exists() for w in wheels)
    simple = root / "pypi.org" / "simple"
    assert (simple / "requests" / "index.html").exists() and (simple / "requests" / "index.json").exists()
    assert 'href="/simple/requests/"' in (simple / "index.html").read_text()
    api = json.loads((root / "pypi.org" / "pypi" / "requests" / "json").read_text())
    assert api["info"]["name"] == "requests"
    assert not list(root.rglob(".pypi-meta")) and (root / "PYPI.md").exists()


def test_shared_file_listed_once(tmp_path, up):
    cfg = _cfg(tmp_path, _src(requirements=["requests"]))
    units = _sync(cfg)
    assert {u.name for u in units} >= {"pypi-all-universal", "pypi-req-universal"}
    root = _publish(cfg)
    page = (root / "pypi.org" / "simple" / "requests" / "index.html").read_text()
    names = [a.split(">")[1].split("<")[0] for a in page.split("<a ")[1:]]
    assert len(names) == len(set(names))


def test_narrowed_retention_prunes_the_tree(tmp_path, up):
    cfg = _cfg(tmp_path, _src(), retention="forever")
    _sync(cfg)
    root = _publish(cfg)
    old = sorted(p.name for p in root.rglob("PySocks-*.whl"))
    _bump_pysocks(up)
    _sync(cfg)
    _publish(cfg)
    assert set(old) <= {p.name for p in root.rglob("PySocks-*.whl")}      # forever keeps them
    _publish(cfg, retention="current")
    now = {p.name for p in root.rglob("*.whl") if p.name.lower().startswith("pysocks-")}
    assert now == {"pysocks-9.0.0-py3-none-any.whl"}
    assert _anchors(root, "pysocks") == 1


def test_project_with_no_published_files_has_no_page(tmp_path, up):
    cfg = _cfg(tmp_path, _src(), retention="forever")
    _sync(cfg)
    root = _publish(cfg)
    assert (root / "pypi.org" / "simple" / "six").exists()
    up.projects["six"]["files"] = []
    up.projects["six"]["serial"] += 1
    _sync(cfg)
    _publish(cfg, retention="current")
    assert not (root / "pypi.org" / "simple" / "six").exists()
    assert 'href="/simple/six/"' not in (root / "pypi.org" / "simple" / "index.html").read_text()


def test_dropping_pypi_prunes_its_bases(tmp_path, up):
    cfg = _cfg(tmp_path, _src())
    _sync(cfg)
    root = _publish(cfg)
    assert (root / "pypi.org").exists()
    bare = _cfg(tmp_path, _src(), sources=[])
    publish_network(bare.networks[0], bare, bare.settings.mirror_dir)
    assert not (root / "pypi.org").exists() and not (root / "files.pythonhosted.org").exists()
    assert not (root / "PYPI.md").exists()


def test_network_without_pypi_is_untouched(tmp_path):
    prov = get_provider("pypi")
    net = config.NetworkConfig(name="lab")
    assert prov.cert_names(net, []) == []
    assert "pypi.org" not in nginx_files([], "/srv/lab")["nginx/mirror.conf"].decode()


def test_nginx_negotiates_simple_index():
    unit = get_provider("pypi").expand(_src())[1]
    conf = nginx_files([], "/srv/lab", sites=get_provider("pypi").nginx_sites([unit]))["nginx/mirror.conf"].decode()
    assert conf.count("map $http_accept $pypi_simple_index") == 1
    assert "application/vnd\\.pypi\\.simple\\.v1\\+json" in conf
    assert "location /simple/" in conf and "$pypi_simple_index" in conf
    assert "application/vnd.pypi.simple.v1+json json;" in conf
    assert "location /pypi/" in conf and "default_type application/json;" in conf
    assert "server_name files.pythonhosted.org;" in conf and "server_name pypi.org;" in conf


def test_certs_and_pypi_md(tmp_path, up):
    cfg = _cfg(tmp_path, _src(), retention="30d")
    units = _sync(cfg)
    assert get_provider("pypi").cert_names(cfg.networks[0], units) == ["pypi.org", "files.pythonhosted.org"]
    md = (_publish(cfg) / "PYPI.md").read_text()
    for needle in ("pypi.org", "files.pythonhosted.org", "UV_NATIVE_TLS", "PIP_CERT", "REQUESTS_CA_BUNDLE",
                   "30d"):
        assert needle in md


def test_republish_rewrites_nothing_unchanged(tmp_path, up):
    cfg = _cfg(tmp_path, _src())
    _sync(cfg)
    root = _publish(cfg)
    before = {p: p.stat().st_mtime_ns for p in (root / "pypi.org").rglob("*") if p.is_file()}
    _publish(cfg)
    after = {p: p.stat().st_mtime_ns for p in (root / "pypi.org").rglob("*") if p.is_file()}
    assert before == after


def test_bundle_carries_pages_and_snapshot_replays(tmp_path, up):
    cfg = _cfg(tmp_path, _src(), retention="current")
    _sync(cfg)
    mirror_dir = cfg.settings.mirror_dir
    a = take_snapshot(cfg.networks[0], cfg, mirror_dir)
    _bump_pysocks(up)
    _sync(cfg)
    b = take_snapshot(cfg.networks[0], cfg, mirror_dir)
    with Pool(mirror_dir, read_only=True) as pool:
        rows_a = dict(pool.snapshot_files(a.id))
        rows_b = dict(pool.snapshot_files(b.id))
    changed = {rel for rel in set(rows_a) | set(rows_b) if rows_a.get(rel) != rows_b.get(rel)}
    # Pages are derived (spec §5.0): the delta is pysocks's files plus the one
    # shard holding its records — never pages.
    assert changed and all("pysocks" in rel.lower() or rel.startswith("pypi-data/shards/") for rel in changed), changed
    assert sum(rel.startswith("pypi-data/shards/") for rel in changed) == 1
    replay = tmp_path / "replay"
    publish_snapshot(cfg.networks[0], cfg, mirror_dir, str(b.id), root=replay)
    live = _publish(cfg)

    def listing(r):
        return sorted(str(p.relative_to(r)) for p in r.rglob("*") if p.is_file()
                      and p.parts[len(r.parts)] in ("pypi.org", "files.pythonhosted.org"))
    assert listing(replay) == listing(live)


def test_files_host_is_pruned_when_nothing_is_published(tmp_path, up):
    cfg = _cfg(tmp_path, _src(), retention="current")
    _sync(cfg)
    root = _publish(cfg)
    assert list((root / "files.pythonhosted.org").rglob("*.whl"))
    for project in up.projects.values():
        project["files"] = []
        project["serial"] += 1
    _sync(cfg)
    _publish(cfg)
    assert not list((root / "files.pythonhosted.org").rglob("*.whl"))


def test_simple_pages_vary_on_accept():
    unit = get_provider("pypi").expand(_src())[1]
    conf = nginx_files([], "/srv/lab", sites=get_provider("pypi").nginx_sites([unit]))["nginx/mirror.conf"].decode()
    assert "add_header Vary Accept always;" in conf


def test_retention_window_is_counted_in_days_at_publish(tmp_path, up):
    from datetime import date, timedelta

    from mmw.providers.pypi.publish import published_rows
    cfg = _cfg(tmp_path, _src(), retention="forever")
    units = _sync(cfg)
    _bump_pysocks(up)
    units = _sync(cfg)
    thirty = cfg.networks[0].model_copy(update={"retention": "30d"})
    today = date.today()

    def has_old(day):
        with Pool(cfg.settings.mirror_dir, read_only=True) as pool:
            rows, _ = published_rows(pool, [u for u in units if u.provider == "pypi"], thirty, day)
        return any(r[0].endswith("PySocks-1.7.1-py3-none-any.whl") for r in rows)
    assert has_old(today + timedelta(days=29)) and not has_old(today + timedelta(days=30))
