"""The requirements list per target: uv's pins → files, plus build tools."""
import threading

from mmw.providers.pypi.build import BuildRequires
from mmw.providers.pypi.closure import UNIVERSAL, KeepSet, collect_release
from mmw.providers.pypi.requirements import resolve_requirements
from mmw.providers.pypi.targets import Floors, TagMatcher, Target
from mmw.providers.pypi.uv import Compiled
from pypi_fakes import FakeUniverse, dist

T = Target("3.12", "linux-glibc", "amd64")
W = Target("3.12", "windows", "amd64")
GLIBC = "cp312-cp312-manylinux_2_17_x86_64"


def compiler(answers, default=None):
    """compile_fn from {(tuple(lines), target key) or tuple(lines): Compiled}."""
    calls = []

    def compile_fn(lines, target):
        calls.append((tuple(lines), target.key))
        return answers.get((tuple(lines), target.key)) or answers.get(tuple(lines)) or default
    compile_fn.calls = calls
    return compile_fn


def ok(*pins):
    return Compiled(list(pins), None, False)


def run(universe, compile_fn, build_fn=lambda f: BuildRequires([]), targets=(T,), sdists=False, **kw):
    return resolve_requirements(["app"], list(targets), Floors(), sdists=sdists, universe=universe,
                                compile_fn=compile_fn, build_fn=build_fn, **kw)


def names(keep, unit):
    return sorted(keep.files.get(unit, {}))


def test_pins_become_target_files():
    u = FakeUniverse(dist("app", "1.0", GLIBC), dist("app", "1.0", "cp312-cp312-win_amd64"), dist("dep", "2.0"))
    r = run(u, compiler({("app",): ok(("app", "1.0"), ("dep", "2.0"))}))
    assert names(r.keep, T.key) == [f"app-1.0-{GLIBC}.whl"]
    assert names(r.keep, UNIVERSAL) == ["dep-2.0-py3-none-any.whl"]
    assert not r.failed and not r.built


def test_source_only_pin_brings_its_build_tools():
    u = FakeUniverse(dist("app", "1.0", requires=["c"]), dist("c", "2.0", kind="sdist"), dist("hatchling", "1.0"))
    builds = {"c-2.0.tar.gz": BuildRequires(["hatchling"])}
    r = run(u, compiler({("app",): ok(("app", "1.0"), ("c", "2.0")), ("hatchling",): ok(("hatchling", "1.0"))}),
            build_fn=lambda f: builds[f.filename])
    assert names(r.keep, UNIVERSAL) == ["app-1.0-py3-none-any.whl", "c-2.0.tar.gz", "hatchling-1.0-py3-none-any.whl"]
    assert r.built == [(T.key, "c==2.0")] and r.report.built_from_source == [(T.key, "c==2.0")]


def test_build_tool_recursion_terminates():
    u = FakeUniverse(dist("app", "1.0"), dist("flit-core", "3.9", kind="sdist"), dist("setuptools", "70", kind="sdist"))
    builds = {"flit_core-3.9.tar.gz": BuildRequires(["flit-core"]),
              "setuptools-70.tar.gz": BuildRequires(["setuptools", "flit-core"])}
    c = compiler({("app",): ok(("app", "1.0"), ("flit-core", "3.9"), ("setuptools", "70")),
                  ("flit-core",): ok(("flit-core", "3.9")),
                  ("setuptools", "flit-core"): ok(("setuptools", "70"), ("flit-core", "3.9"))})
    r = run(u, c, build_fn=lambda f: builds[f.filename])
    assert names(r.keep, UNIVERSAL) == ["app-1.0-py3-none-any.whl", "flit_core-3.9.tar.gz", "setuptools-70.tar.gz"]
    assert len(c.calls) <= 3


def test_conflict_fails_only_that_target():
    u = FakeUniverse(dist("app", "1.0", GLIBC), dist("app", "1.0", "cp312-cp312-win_amd64"))
    bad = Compiled([], "No solution found: c<2 and c>=3", False)
    r = run(u, compiler({(("app",), W.key): bad}, default=ok(("app", "1.0"))), targets=(T, W))
    assert r.failed == {W.key}
    assert names(r.keep, T.key) == [f"app-1.0-{GLIBC}.whl"] and W.key not in r.keep.files
    assert any(row[0] == W.key and "c<2" in row[2] for row in r.report.unsatisfiable)


def test_build_requirements_failure_fails_the_target():
    u = FakeUniverse(dist("app", "1.0"), dist("c", "2.0", kind="sdist"))
    r = run(u, compiler({("app",): ok(("app", "1.0"), ("c", "2.0")),
                         ("weird-backend",): Compiled([], "no such project weird-backend", False)}),
            build_fn=lambda f: BuildRequires(["weird-backend"]))
    assert r.failed == {T.key} and not r.keep.files


def test_legacy_note_is_reported():
    u = FakeUniverse(dist("app", "1.0"), dist("old", "1.0", kind="sdist"), dist("setuptools", "70"))
    r = run(u, compiler({("app",): ok(("app", "1.0"), ("old", "1.0")),
                         ("setuptools>=40.8.0",): ok(("setuptools", "70"))}),
            build_fn=lambda f: BuildRequires(["setuptools>=40.8.0"], "no pyproject.toml"))
    assert any("no pyproject.toml" in row[1] for row in r.report.unknown_deps)


def test_stop_between_targets():
    stop = threading.Event()
    c = compiler({}, default=ok(("app", "1.0")))
    original = c

    def stopping(lines, target):
        stop.set()
        return original(lines, target)
    run(FakeUniverse(dist("app", "1.0")), stopping, targets=(T, W), stop_event=stop)
    assert len(c.calls) == 1


def test_collect_release_matches_closure_rules():
    keep = KeepSet()
    files = [dist("x", "1.0", GLIBC)[0], dist("x", "1.0", kind="sdist")[0]]
    collect_release(keep, T, TagMatcher(T, Floors()), files, "wheel", sdists=True)
    assert names(keep, T.key) == [f"x-1.0-{GLIBC}.whl"] and names(keep, UNIVERSAL) == ["x-1.0.tar.gz"]
    keep = KeepSet()
    collect_release(keep, T, TagMatcher(T, Floors()), files, "wheel", sdists=False)
    assert UNIVERSAL not in keep.files


def test_pin_without_files_fails_the_target():
    # A page that could not be fetched (or is stale) must not publish a partial solution.
    u = FakeUniverse(dist("app", "1.0"))
    r = run(u, compiler({("app",): ok(("app", "1.0"), ("ghost", "1.0"))}))
    assert r.failed == {T.key} and not r.keep.files
    assert any("ghost==1.0" in row[1] for row in r.report.unsatisfiable)


def test_summary_says_build_requirements_are_static_only():
    u = FakeUniverse(dist("app", "1.0"), dist("c", "2.0", kind="sdist"))
    r = run(u, compiler({("app",): ok(("app", "1.0"), ("c", "2.0"))}))
    assert "build requirements: static only" in r.report.summary()
