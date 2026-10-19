"""Generated PyPI pages: PEP 503 HTML, PEP 691/700 JSON, a minimal JSON API."""
import json

from mmw.providers.pypi.records import Record
from mmw.providers.pypi.simple import project_pages, root_pages

URL = "https://files.pythonhosted.org/packages/ab/cd/ef/"


def rec(filename, version="1.0", *, project="demo", rp=">=3.9", yanked=False, meta="cd" * 32,
        upload="2026-01-01T00:00:00Z", size=10):
    return Record(project=project, version=version, filename=filename, url=URL + filename, sha256="ab" * 32,
                  size=size, requires_python=rp, yanked=yanked, metadata_sha256=meta, upload_time=upload)


W = "demo-1.0-py3-none-any.whl"


def test_html_anchor_carries_hash_requires_python_yanked_metadata():
    pages = project_pages("demo", [rec(W, yanked="broken")], metadata={W})
    html = pages["simple/demo/index.html"].decode()
    assert (f'<a href="{URL}{W}#sha256={"ab" * 32}" data-requires-python="&gt;=3.9" data-yanked="broken" '
            f'data-core-metadata="sha256={"cd" * 32}" data-dist-info-metadata="sha256={"cd" * 32}">{W}</a>') in html
    assert html.startswith("<!DOCTYPE html>") and 'name="pypi:repository-version" content="1.1"' in html


def test_json_page_is_pep691_with_pep700_fields():
    sd = "demo-0.9.tar.gz"
    page = json.loads(project_pages("demo", [rec(W), rec(sd, "0.9", meta=None)], metadata={W})
                      ["simple/demo/index.json"])
    assert page["meta"] == {"api-version": "1.1"} and page["name"] == "demo"
    assert page["versions"] == ["0.9", "1.0"]
    assert [f["filename"] for f in page["files"]] == [sd, W]
    whl = page["files"][1]
    assert whl == {"filename": W, "url": URL + W, "hashes": {"sha256": "ab" * 32}, "size": 10,
                   "upload-time": "2026-01-01T00:00:00Z", "requires-python": ">=3.9", "yanked": False,
                   "core-metadata": {"sha256": "cd" * 32}, "data-dist-info-metadata": {"sha256": "cd" * 32}}
    assert page["files"][0]["core-metadata"] is False


def test_metadata_advertised_only_when_published():
    html = project_pages("demo", [rec(W)], metadata=set())["simple/demo/index.html"].decode()
    assert "data-core-metadata" not in html


def test_html_escapes_attributes():
    html = project_pages("demo", [rec(W, rp='>=3.8,<4"', yanked="<script>")], metadata=set())[
        "simple/demo/index.html"].decode()
    assert "<script>" not in html and "&lt;script&gt;" in html and '<4"' not in html


def test_names_are_normalized_and_bytes_deterministic():
    recs = [rec("zope_interface-6.0-py3-none-any.whl", "6.0", project="zope-interface")]
    a = project_pages("Zope.Interface", recs, metadata=set())
    b = project_pages("Zope.Interface", list(reversed(recs)), metadata=set())
    assert set(a) == {"simple/zope-interface/index.html", "simple/zope-interface/index.json",
                      "pypi/zope-interface/json"} and a == b


def test_json_api_info_version_skips_yanked_and_prereleases():
    api = json.loads(project_pages("demo", [rec(W), rec("demo-2.0-py3-none-any.whl", "2.0", yanked=True),
                                            rec("demo-3.0a1-py3-none-any.whl", "3.0a1")],
                                   metadata=set())["pypi/demo/json"])
    assert api["info"]["name"] == "demo" and api["info"]["version"] == "1.0"
    assert sorted(api["releases"]) == ["1.0", "2.0", "3.0a1"]
    assert api["urls"][0]["filename"] == W and api["urls"][0]["digests"]["sha256"] == "ab" * 32
    assert api["releases"]["2.0"][0]["yanked"] is True


def test_root_pages_list_names_sorted():
    pages = root_pages(["b", "a"])
    assert pages["simple/index.html"].decode().index('href="/simple/a/"') < \
        pages["simple/index.html"].decode().index('href="/simple/b/"')
    assert json.loads(pages["simple/index.json"])["projects"] == [{"name": "a"}, {"name": "b"}]
