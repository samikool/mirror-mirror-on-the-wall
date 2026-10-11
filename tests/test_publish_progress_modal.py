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
        "  [$mmw-ok]→[/] host/repo/pool/a.deb",
        "  [$mmw-ok]→[/] host/repo/pool/b.deb",
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


def test_record_archive_phase_messages():
    """`BundleSink.close()` is where a bundle does its real I/O — reading pool
    blobs, streaming through zstd — invisible to the fast enumeration phase
    the tests above cover. These status events are its only signal.

    Raw (uncompressed) and compressed bytes are carried and tracked
    separately — conflating them was the bug: the raw figure is what the
    total (also uncompressed) can be meaningfully compared against, and the
    compressed figure is what's actually landing on disk."""
    modal = _modal()
    modal.record("", "archive-start:100:2048000")
    assert modal._archive_total_files == 100
    assert modal._archive_total_bytes == 2048000
    assert "100" in modal._log_pending[-1]

    # An empty `dst` (no file to name — used by the tests above that only
    # care about the counters) adds no per-file log line of its own; the
    # literal string "archive-progress" never appears either way, since the
    # log line is a rendered file path, not the wire status name.
    modal.record("", "archive-progress:40:900000:300000")
    assert modal._archive_files_done == 40
    assert modal._archive_raw_bytes_done == 900000
    assert modal._archive_compressed_bytes_done == 300000
    assert not any("archive-progress" in line for line in modal._log_pending)

    modal.record("", "archive-done:100:2048000:700000")
    assert modal._archive_files_done == 100
    assert modal._archive_raw_bytes_done == 2048000
    assert modal._archive_compressed_bytes_done == 700000
    last = modal._log_pending[-1]
    assert "archive written" in last
    # 2048000 / 700000 ≈ 2.9x — the ratio must actually appear, not just the
    # raw/compressed byte counts individually.
    assert "2.9x" in last


def test_record_large_file_gets_an_immediate_announcement_with_size():
    """A single multi-hundred-MB/GB file (driver/kernel packages this pool
    holds plenty of) can leave the throttled per-file sampling silent for as
    long as it takes to compress — this is the announcement that fires the
    instant such a file starts, so it reads as real progress, not a stall."""
    modal = _modal()
    modal.record("host/repo/pool/nvhpc_26.1_arm64.deb", f"archive-large-file:{5_200_000_000}")

    last = modal._log_pending[-1]
    assert "compressing" in last
    assert "nvhpc_26.1_arm64.deb" in last
    assert "5.2 GB" in last or "GB" in last  # exact _fmt_bytes rounding not asserted here


def test_record_large_file_progress_updates_state_without_logging():
    """Percentage-through progress on one big file can fire many times a
    second for as long as it takes to compress — it must update state for
    the status line to render, not add a line to the scrolling log every
    time (that would spam it for a multi-minute file)."""
    modal = _modal()
    rel = "host/repo/pool/nvhpc_26.1_arm64.deb"
    modal.record(rel, "archive-large-file:5200000000")
    before = len(modal._log_pending)

    modal.record(rel, "archive-large-file-progress:1000000:5200000000")

    assert modal._current_large_file == rel
    assert modal._current_large_file_done == 1000000
    assert modal._current_large_file_size == 5200000000
    assert len(modal._log_pending) == before, "no new log line for a progress update"


def test_record_large_file_progress_ignored_for_a_different_file():
    """A stray/late progress event for a file that is no longer the current
    large one (e.g. arriving just as the next large file starts) must not
    clobber the newer file's state."""
    modal = _modal()
    modal.record("a.deb", "archive-large-file:1000")
    modal.record("b.deb", "archive-large-file:2000")   # a new large file started

    modal.record("a.deb", "archive-large-file-progress:999:1000")  # stale

    assert modal._current_large_file == "b.deb"
    assert modal._current_large_file_done == 0
    assert modal._current_large_file_size == 2000


def test_large_file_state_clears_when_that_file_completes():
    modal = _modal()
    rel = "host/repo/pool/big.deb"
    modal.record(rel, "archive-large-file:1000")
    modal.record(rel, "archive-large-file-progress:500:1000")
    assert modal._current_large_file == rel

    modal.record(rel, "archive-progress:1:1000:400")   # the file finished

    assert modal._current_large_file == ""
    assert modal._current_large_file_done == 0
    assert modal._current_large_file_size == 0


def test_large_file_state_clears_when_archive_finishes():
    """Belt-and-suspenders: even if the last large file's own completion
    sample got throttled away, the archive finishing at all must not leave a
    stale "still compressing" claim on screen."""
    modal = _modal()
    modal.record("host/repo/pool/big.deb", "archive-large-file:1000")
    modal.record("", "archive-done:1:1000:400")

    assert modal._current_large_file == ""


def test_large_file_progress_renders_on_its_own_line(tmp_path):
    """On its own widget/line, not appended to the main status line — that
    line was long enough to run off-screen once a driver/kernel package's
    full filename joined everything else already on it."""
    from textual.app import App, ComposeResult

    modal_holder = {}

    class _Host(App):
        def compose(self) -> ComposeResult:
            m = _modal()
            modal_holder["modal"] = m
            yield m

    async def run():
        app = _Host()
        async with app.run_test():
            modal = modal_holder["modal"]
            modal.record("", "archive-start:10:1000")

            from textual.widgets import Label
            file_label = modal.query_one(f"#{modal.ID_PREFIX}-archive-file-label", Label)
            assert file_label.display is False, "hidden until a large file is in flight"

            modal.record("host/repo/pool/big.deb", "archive-large-file:800")
            modal.record("host/repo/pool/big.deb", "archive-large-file-progress:400:800")
            modal._tick()

            assert file_label.display is True
            file_label_text = str(file_label.render())
            assert "compressing big.deb: 50%" in file_label_text
            mirror_label_text = str(
                modal.query_one(f"#{modal.ID_PREFIX}-mirror-label", Label).render())
            assert "compressing" not in mirror_label_text

            # And it hides again once the file completes.
            modal.record("host/repo/pool/big.deb", "archive-progress:1:800:400")
            modal._tick()
            assert file_label.display is False

    import asyncio
    asyncio.run(run())


def test_record_archive_progress_logs_the_file_that_just_completed():
    """Once enumeration's list of "planned" files finishes, the log should
    keep moving with what's actually been tarred and compressed — sampled at
    the same throttled cadence as the numeric counters (see BundleSink.
    _maybe_report_archive_progress), not a second unthrottled per-file path."""
    modal = _modal()
    modal.record("", "archive-start:10:1000")
    modal.record("host/repo/pool/nvhpc_26.1_arm64.deb", "archive-progress:1:500:200")

    assert modal._log_pending[-1] == "  [$accent]⇢[/] host/repo/pool/nvhpc_26.1_arm64.deb"


def test_archive_progress_repurposes_overall_bar_and_mirror_label(tmp_path):
    """Once the archive phase starts, the overall bar and the mirror-label
    line switch to reporting it — the enumeration phase's own progress is
    already over by the time BundleSink.close() runs.

    Both the bar and the percentage are bytes-based (raw/uncompressed), not
    file-count-based: a pool holding files from a few KB to several GB would
    make a file-count fraction read as "almost done" the instant the many
    small files clear, while most of the actual data volume remained.

    Rate/output info (files/s, speed, written+ratio) lives on its own line —
    see test_large_file_progress_renders_on_its_own_line for why the combined
    line had to be split up in the first place."""
    from textual.app import App, ComposeResult

    modal_holder = {}

    class _Host(App):
        def compose(self) -> ComposeResult:
            m = _modal()
            modal_holder["modal"] = m
            yield m

    async def run():
        app = _Host()
        async with app.run_test():
            modal = modal_holder["modal"]
            modal.record("", "archive-start:10:1000")
            modal.record("", "archive-progress:4:400:100")
            modal._tick()

            from textual.widgets import Label, ProgressBar
            bar = modal.query_one(f"#{modal.ID_PREFIX}-pbar", ProgressBar)
            assert bar.total == 1000
            assert bar.progress == 400

            label_text = str(modal.query_one(f"#{modal.ID_PREFIX}-mirror-label", Label).render())
            assert "Writing archive" in label_text
            assert "4 / 10 files" in label_text
            assert "(40%)" in label_text          # 400 / 1000 raw bytes

            rate_label = modal.query_one(f"#{modal.ID_PREFIX}-archive-rate-label", Label)
            assert rate_label.display is True
            rate_text = str(rate_label.render())
            assert "written" in rate_text
            assert "4.0x" in rate_text            # 400 raw / 100 compressed

    import asyncio
    asyncio.run(run())
