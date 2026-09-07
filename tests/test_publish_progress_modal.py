"""Regression coverage for `PublishProgressModal.record` — the Sink progress
callback contract (`core/publisher/sinks.py`) is sink-relative POSIX strings,
never `Path`. `record` used to take `dst: Path` and call `dst.parts` before
even looking at `status`, so the very first progress event of every TUI
publish raised `AttributeError: 'str' object has no attribute 'parts'` once
`publish_group`/`TreeSink` started passing strings — nothing in the suite
called `record` at all, which is why it went unnoticed.

`record` is documented as "pure counter + buffer update, no DOM work", so it
is exercised directly without mounting a Textual app.
"""
from mmw.tui.publish_modals import PublishProgressModal


def _modal() -> PublishProgressModal:
    return PublishProgressModal("net", total=10, per_mirror_counts={"m": 10})


def test_record_accepts_sink_relative_strings_not_paths():
    """The historical bug, pinned: this exact call must not raise."""
    modal = _modal()
    modal.record("archive.ubuntu.com/ubuntu/dists/noble/Release", "linked")
    assert modal._linked == 1


def test_record_counts_linked_and_skipped():
    modal = _modal()
    modal.record("host/repo/pool/a.deb", "linked")
    modal.record("host/repo/pool/b.deb", "linked")
    modal.record("host/repo/pool/c.deb", "skipped")
    assert modal._linked == 2
    assert modal._skipped == 1
    assert modal._mirror_linked == 2
    assert modal._mirror_skipped == 1
    assert modal._log_pending == [
        "  [green]→[/] host/repo/pool/a.deb",
        "  [green]→[/] host/repo/pool/b.deb",
    ]


def test_record_linked_meta_and_skipped_meta_do_not_count_toward_packages():
    modal = _modal()
    modal.record("host/repo/dists/noble/Release", "linked-meta")
    modal.record("host/repo/dists/noble/InRelease", "skipped-meta")
    assert modal._linked == 0
    assert modal._skipped == 0
    assert modal._mirror_linked == 0
    assert modal._mirror_skipped == 0
    assert modal._log_pending == []


def test_record_pruned_increments_and_logs():
    modal = _modal()
    modal.record("host/repo/dists/noble/old.deb", "pruned")
    assert modal._pruned == 1
    assert "pruned" in modal._log_pending[0]
    assert "old.deb" in modal._log_pending[0]


def test_record_by_hash_logs_count():
    modal = _modal()
    modal.record("host/repo", "by-hash:3")
    assert modal._log_pending == ["  [dim]# by-hash links: 3[/]"]


def test_record_enumerate_phase_messages():
    modal = _modal()
    modal.record("host/repo", "enumerate-start")
    assert modal._phase == "reading pool index…"
    modal.record("host/repo", "enumerate-done:42")
    assert modal._phase == ""
    assert any("42" in line for line in modal._log_pending)


def test_record_prune_scan_phase_messages():
    modal = _modal()
    modal.record("host/repo", "prune-scan-start")
    assert modal._phase == "scanning destination…"
    modal.record("host/repo", "prune-scan-progress:100")
    assert modal._phase == "scanning destination · 100 checked"
    modal.record("host/repo", "prune-scan-done:100:5")
    assert modal._phase == ""
    last = modal._log_pending[-1]
    assert "100" in last and "5" in last


def test_record_short_path_truncates_to_last_four_segments():
    modal = _modal()
    modal.record("a/b/c/d/e/f.deb", "linked")
    logged = modal._log_pending[-1]
    assert "c/d/e/f.deb" in logged
    assert "a/b/" not in logged


def test_record_short_path_under_four_segments_kept_whole():
    modal = _modal()
    modal.record("a/b.deb", "linked")
    assert "a/b.deb" in modal._log_pending[-1]
