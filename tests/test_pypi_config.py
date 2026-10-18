"""PypiSource in mmw.toml: load, validate, select by python/platform/arch."""
import pytest
from pydantic import ValidationError

from mmw.core import config
from mmw.providers.pypi.targets import Floors

PY = """[[sources]]
provider = "pypi"
pythons = ["3.12"]
platforms = ["linux-glibc"]
arches = ["amd64"]
all = true
"""


def _cfg(tmp_path, body: str) -> config.Config:
    (tmp_path / "mmw.toml").write_text(f'[settings]\nmirror_dir = "{tmp_path}/m"\n\n' + body)
    return config.load(tmp_path)


def _src(**kw):
    return config.PypiSource(provider="pypi", **kw)


def test_pypi_source_loads_with_defaults():
    # Loading mmw.toml needs the registered provider; Task 7's round-trip covers that.
    from pydantic import TypeAdapter
    src = TypeAdapter(config.AnySource).validate_python(
        {"provider": "pypi", "pythons": ["3.12"], "platforms": ["linux-glibc"],
         "arches": ["amd64"], "all": True})
    assert isinstance(src, config.PypiSource)
    assert src.identity == "https://pypi.org/simple"
    assert src.floors == Floors(glibc="2.31", musl="1.2", macos="11.0")
    assert src.sdists is False and src.all is True and src.name == "pypi"


def test_unknown_values_are_rejected():
    with pytest.raises(ValidationError, match="3.9"):
        _src(pythons=["2.7"])
    with pytest.raises(ValidationError, match="linux-glibc"):
        _src(platforms=["linux"])
    with pytest.raises(ValidationError, match="amd64"):
        _src(arches=["x86_64"])


def test_bad_floor_rejected():
    with pytest.raises(ValidationError, match="glibc"):
        _src(glibc="2")
    with pytest.raises(ValidationError, match="macos"):
        _src(macos="eleven")


def test_other_index_url_rejected():
    with pytest.raises(ValidationError, match="pypi.org"):
        _src(url="https://download.pytorch.org/whl/")


def test_only_one_pypi_source(tmp_path):
    with pytest.raises(Exception, match="only one pypi source"):
        _cfg(tmp_path, PY + "\n" + PY)


def test_covers_filters_by_dims():
    src = _src(pythons=["3.12"], all=True)

    def unit(**dims):
        return config.MirrorConfig(name="u", provider="pypi", source_url=src.url,
                                   source=src.identity, dims=dims)
    assert src.covers(unit(python="3.12", platform="windows", arch="amd64"))
    assert not src.covers(unit(python="3.13", platform="windows", arch="amd64"))
    assert src.covers(unit())
    other = config.MirrorConfig(name="u", provider="pypi", source_url="x", source="https://elsewhere",
                                dims={"python": "3.12"})
    assert not src.covers(other)


def test_requirements_lines_and_file_union(tmp_path):
    (tmp_path / "reqs.txt").write_text("numpy>=2\n# comment\n\nrequests\n")
    cfg = _cfg(tmp_path, PY + 'requirements = ["requests", "# c", ""]\nrequirements_file = "reqs.txt"\n')
    (src,) = cfg.sources
    assert src.requirements == ["requests"]
    assert src.requirement_lines() == ["requests", "numpy>=2"]


@pytest.mark.parametrize("line", ["-r base.txt", "-e .", "--index-url x", "pkg @ https://x/p.whl",
                                  "git+https://x/y.git", "./x.whl", "/abs/x.tar.gz",
                                  "pkg==1 --hash=sha256:ab", "https://x/p.whl"])
def test_unsupported_lines_are_rejected(line):
    with pytest.raises(ValidationError, match="unsupported requirements line") as e:
        _src(requirements=[line])
    assert line in str(e.value)


def test_markers_and_extras_are_fine():
    src = _src(requirements=['requests[socks]>=2; python_version >= "3.10"', "Django"])
    assert src.requirements == ['requests[socks]>=2; python_version >= "3.10"', "Django"]


def test_missing_requirements_file_raises_at_read(tmp_path):
    (src,) = _cfg(tmp_path, PY + 'requirements_file = "nope.txt"\n').sources
    with pytest.raises(FileNotFoundError, match="nope.txt"):
        src.requirement_lines()


def test_bad_line_in_file_raises_at_read(tmp_path):
    (tmp_path / "r.txt").write_text("requests\n-r other.txt\n")
    (src,) = _cfg(tmp_path, PY + 'requirements_file = "r.txt"\n').sources
    with pytest.raises(ValueError, match="-r other.txt"):
        src.requirement_lines()


def test_to_toml_keeps_requirements(tmp_path):
    (src,) = _cfg(tmp_path, PY + 'requirements = ["requests"]\nrequirements_file = "r.txt"\n').sources
    config.save(config.load(tmp_path), tmp_path)
    text = (tmp_path / "mmw.toml").read_text()
    assert 'requirements_file = "r.txt"' in text
    (again,) = config.load(tmp_path).sources
    assert again == src
