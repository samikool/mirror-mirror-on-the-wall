"""latest + dependency closure: what the straight mirror keeps, per target."""
import json

from mmw.providers.pypi.closure import UNIVERSAL, keep_closure
from mmw.providers.pypi.targets import Floors, Target
from pypi_fakes import FakeUniverse, dist

T = Target("3.12", "linux-glibc", "amd64")
GLIBC = "cp312-cp312-manylinux_2_17_x86_64"


def run(*dists, targets=(T,), sdists=False, seeds=None):
    u = FakeUniverse(*dists)
    keep, report = keep_closure(u, list(targets), Floors(), sdists=sdists, seeds=seeds)
    return u, keep, report


def kept(keep, unit=UNIVERSAL):
    return sorted(f.filename for f in keep.files.get(unit, {}).values())


def releases(keep):
    return sorted({(f.project, f.version) for unit in keep.files.values() for f in unit.values()})


def test_latest_only_without_deps():
    _, keep, _ = run(dist("a", "1.0"), dist("a", "2.0"))
    assert kept(keep) == ["a-2.0-py3-none-any.whl"]


def test_old_pin_keeps_newest_satisfying():
    _, keep, _ = run(dist("app", "1.0", requires=["lib<2"]),
                     dist("lib", "1.8"), dist("lib", "1.9"), dist("lib", "2.0"))
    assert releases(keep) == [("app", "1.0"), ("lib", "1.9"), ("lib", "2.0")]


def test_requires_python_cutoff_per_target():
    t310 = Target("3.10", "linux-glibc", "amd64")
    _, keep, _ = run(dist("numpy", "2.0", requires_python=">=3.11"),
                     dist("numpy", "1.26", requires_python=">=3.9"), targets=(T, t310))
    assert releases(keep) == [("numpy", "1.26"), ("numpy", "2.0")]
    _, keep, _ = run(dist("numpy", "2.0", requires_python=">=3.11"),
                     dist("numpy", "1.26", requires_python=">=3.9"), targets=(t310,))
    assert releases(keep) == [("numpy", "1.26")]


def test_seed_rules_yanked_and_prerelease_only():
    _, keep, _ = run(dist("gone", "1.0", yanked=True),
                     dist("pre", "1.0a1"), dist("pre", "1.0b1"),
                     dist("mixed", "1.0"), dist("mixed", "2.0rc1"))
    assert releases(keep) == [("mixed", "1.0"), ("pre", "1.0b1")]


def test_yanked_exact_pin_is_kept():
    _, keep, _ = run(dist("app", "1.0", requires=["lib==1.5"]),
                     dist("lib", "1.5", yanked=True), dist("lib", "2.0"))
    assert ("lib", "1.5") in releases(keep)
    _, keep, report = run(dist("app", "1.0", requires=["lib<2"]),
                          dist("lib", "1.5", yanked=True), dist("lib", "2.0"))
    assert ("lib", "1.5") not in releases(keep) and report.unsatisfiable


def test_floor_unusable_latest_falls_back():
    _, keep, _ = run(dist("x", "2.0", "cp312-cp312-manylinux_2_34_x86_64"), dist("x", "1.0", GLIBC))
    assert kept(keep, T.key) == [f"x-1.0-{GLIBC}.whl"]


def test_all_compatible_variants_kept():
    _, keep, _ = run(dist("x", "1.0", GLIBC), dist("x", "1.0", "cp312-cp312-manylinux_2_34_x86_64"),
                     dist("x", "1.0", "cp312-cp312-win_amd64"))
    assert kept(keep, T.key) == [f"x-1.0-{GLIBC}.whl", "x-1.0-cp312-cp312-manylinux_2_34_x86_64.whl"]


def test_marker_differs_by_os():
    win = Target("3.12", "windows", "amd64")
    _, keep, _ = run(dist("app", "1.0", requires=['pywin32; sys_platform == "win32"']),
                     dist("pywin32", "306", "cp312-cp312-win_amd64"), targets=(T, win))
    assert kept(keep, win.key) == ["pywin32-306-cp312-cp312-win_amd64.whl"]
    assert T.key not in keep.files or not kept(keep, T.key)


def test_extras_followed_only_when_requested():
    _, keep, _ = run(dist("a", "1.0", requires=["b[fast]"]),
                     dist("b", "1.0", requires=['c; extra == "fast"'], extras=["fast"]),
                     dist("c", "1.0"), dist("d", "1.0"),
                     dist("e", "1.0", requires=['d<1; extra == "dev"'], extras=["dev"]),
                     dist("d", "0.5"), seeds=["a", "e"])
    assert releases(keep) == [("a", "1.0"), ("b", "1.0"), ("c", "1.0"), ("e", "1.0")]


def test_extras_gap_reported():
    _, _, report = run(dist("s", "1.0", requires=['nothere>=1; extra == "x"'], extras=["x"]))
    assert report.extras_gaps == [(T.key, "s==1.0", "x")]


def test_sdist_fallback_when_no_wheel():
    _, keep, report = run(dist("app", "1.0", requires=["psyco"]), dist("psyco", "2.9", kind="sdist"))
    assert kept(keep) == ["app-1.0-py3-none-any.whl", "psyco-2.9.tar.gz"]
    assert report.sdist_fallback == [(T.key, "psyco==2.9")]
    assert report.unknown_deps == [(T.key, "psyco==2.9")]


def test_sdists_true_keeps_every_kept_sdist():
    _, keep, report = run(dist("a", "1.0"), dist("a", "1.0", kind="sdist"), sdists=True)
    assert kept(keep) == ["a-1.0-py3-none-any.whl", "a-1.0.tar.gz"] and not report.sdist_fallback


def test_unsatisfiable_reported():
    _, _, report = run(dist("app", "1.0", requires=["ghost>=1"]))
    assert report.unsatisfiable == [(T.key, "app==1.0", "ghost>=1")]


def test_cycles_and_self_extras_terminate():
    _, keep, _ = run(dist("a", "1.0", requires=["b"]), dist("b", "1.0", requires=["a"]),
                     dist("pkg", "1.0", requires=['pkg[x]; extra == "all"', 'y; extra == "x"'],
                          extras=["all", "x"]),
                     dist("y", "1.0"), dist("user", "1.0", requires=["pkg[all]"]), seeds=["a", "user"])
    assert releases(keep) == [("a", "1.0"), ("b", "1.0"), ("pkg", "1.0"), ("user", "1.0"), ("y", "1.0")]


def test_requirement_names_are_normalized():
    _, keep, _ = run(dist("app", "1.0", requires=["Zope.Interface>=5"]), dist("zope-interface", "6.0"),
                     seeds=["app"])
    assert ("zope-interface", "6.0") in releases(keep)


def test_invalid_requirement_is_reported_not_fatal():
    _, keep, report = run(dist("app", "1.0", requires=["not a req ===", "lib"]), dist("lib", "1.0"),
                          seeds=["app"])
    assert ("lib", "1.0") in releases(keep)
    assert report.invalid == [("app==1.0", "not a req ===")]


def test_prefetch_called_per_wave():
    u, _, _ = run(dist("a", "1.0", requires=["b"]), dist("b", "1.0", requires=["c"]), dist("c", "1.0"),
                  seeds=["a"])
    assert u.prefetched == [["a-1.0-py3-none-any.whl"], ["b-1.0-py3-none-any.whl"], ["c-1.0-py3-none-any.whl"]]


def test_metadata_read_from_the_targets_chosen_wheel():
    _, keep, _ = run(dist("a", "1.0", GLIBC, requires=["onlylinux"]),
                     dist("a", "1.0", "cp312-cp312-win_amd64", requires=["onlywin"]),
                     dist("onlylinux", "1.0"), dist("onlywin", "1.0"), seeds=["a"])
    assert ("onlylinux", "1.0") in releases(keep) and ("onlywin", "1.0") not in releases(keep)


def test_report_for_units_and_json_deterministic():
    win = Target("3.12", "windows", "amd64")
    _, _, report = run(dist("app", "1.0", requires=["ghost"]), targets=(T, win))
    assert len(report.unsatisfiable) == 2
    assert report.for_units({win.key}).unsatisfiable == [(win.key, "app==1.0", "ghost")]
    assert report.to_json() == report.to_json()
    assert json.loads(report.to_json())["unsatisfiable"][0] == [T.key, "app==1.0", "ghost"]
    assert "2 unsatisfiable" in report.summary()


def test_self_extra_binds_to_the_release_being_expanded():
    # pkg 1.0's "all" extra means pkg 1.0's "x" extra, not the newest pkg's.
    _, keep, report = run(dist("user", "1.0", requires=["pkg[all]<2"]),
                          dist("pkg", "1.0", requires=['pkg[x]; extra == "all"', 'old-y; extra == "x"'],
                               extras=["all", "x"]),
                          dist("pkg", "2.0", requires=['pkg[x]; extra == "all"', 'new-y; extra == "x"'],
                               extras=["all", "x"]),
                          dist("old-y", "1.0"), dist("new-y", "1.0"), seeds=["user"])
    assert ("old-y", "1.0") in releases(keep)
    assert ("new-y", "1.0") not in releases(keep)


def test_stop_ends_the_closure_between_waves():
    import threading
    stop = threading.Event()
    u = FakeUniverse(dist("a", "1.0", requires=["b"]), dist("b", "1.0", requires=["c"]), dist("c", "1.0"))
    original = u.prefetch
    u.prefetch = lambda files: (original(files), stop.set())
    keep, _ = keep_closure(u, [T, Target("3.12", "windows", "amd64")], Floors(), sdists=False,
                           seeds=["a"], stop_event=stop)
    assert len(u.prefetched) == 1     # stopped after the first wave, before the second target


def test_progress_names_each_target():
    msgs = []
    run_u = FakeUniverse(dist("a", "1.0"))
    keep_closure(run_u, [T, Target("3.12", "windows", "amd64")], Floors(), sdists=False, progress=msgs.append)
    assert any("3.12-linux-glibc-amd64" in m for m in msgs) and any("3.12-windows-amd64" in m for m in msgs)


def test_seeding_reads_the_universe_by_one_scan():
    u = FakeUniverse(dist("a", "1.0", requires=["lib"]), dist("lib", "1.0"), dist("lone", "2.0"))
    keep, _ = keep_closure(u, [T], Floors(), sdists=False)
    assert releases(keep) == [("a", "1.0"), ("lib", "1.0"), ("lone", "2.0")]
    assert set(u.files_calls) <= {"lib"}    # seeds come from the scan; only a dependency pick may look up
