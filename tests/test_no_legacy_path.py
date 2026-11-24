"""The network-walk history path is gone from `src/` (Task 23).

History belongs to repositories: a sync records repository points, a
network links them (a layer's view of them) and walks only its own track.
What is left of the older model lives in `legacy_points` — the one upgrade
chain that converts an older pool at startup — and nowhere else.

One exception stays, on purpose: a repository no sync ever recorded a
repository point of (only refs, from before repository points) is still
walked by `points.record_network_points`, and the points that walk records
are its network's own (scoped). `transfer_ranges.walked` marks a network
that holds such points. Its first sync ends the exception. See
design-notes § Batch 6.
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "mmw"
UPGRADE = "core/legacy_points.py"

# Names of the old path: none may appear in src/ (outside the upgrade chain,
# which reads and drops the old tables of an older pool).
GONE = [
    "layer_rows", "layer_digests", "layer_overlay", "network_repo_rows", r"\.(set_|drop_)?repo_rows\(",
    r"\.recorded_repos\(network", r"def ((set_|drop_)?repo_rows|recorded_repos)\(self", "_kept_by", "repository_owned", "build_layers", "published_legacy_holds",
    "current_points", "moment_rows", "_build_live",
]
CONVERSIONS = ["convert_snapshots", "seed_recordings", "migrate_sent"]


def _sources():
    for path in sorted(SRC.rglob("*.py")):
        yield path.relative_to(SRC).as_posix(), path.read_text()


def _hits(pattern: str, *, allow=()) -> list[str]:
    rx = re.compile(pattern)
    return [f"{rel}:{n}: {line.strip()}"
            for rel, text in _sources() if rel not in allow
            for n, line in enumerate(text.splitlines(), 1) if rx.search(line)]


def _attribute(name: str) -> list[str]:
    """Every `x.name` in code (not in strings: SQL keeps the column)."""
    import io
    import tokenize
    out = []
    for rel, text in _sources():
        toks = [t for t in tokenize.generate_tokens(io.StringIO(text).readline)
                if t.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT)]
        out += [f"{rel}:{b.start[0]}" for a, b in zip(toks, toks[1:])
                if a.type == tokenize.OP and a.string == "." and b.type == tokenize.NAME and b.string == name]
    return out


def test_no_legacy_attribute_on_points():
    # A point is a repository's, or one network's (`PoolPoint.scoped`).
    assert _attribute("legacy") == []
    assert _hits(r"^\s+legacy\s*:\s*bool\b") == []      # a dataclass field (PoolPoint, HistoryRow)
    assert _hits(r"record_point\([^)]*\blegacy\s*=", allow=(UPGRADE,)) == []


def test_no_old_path_names():
    for name in GONE:
        assert _hits(name if "\\" in name else rf"\b{name}\b", allow=(UPGRADE,)) == [], name


def test_conversion_runs_only_in_the_upgrade_chain():
    for name in CONVERSIONS:
        calls = _hits(rf"\b{name}\(", allow=(UPGRADE,))
        assert calls == [], f"{name} called outside {UPGRADE}: {calls}"


def test_the_walk_is_the_documented_exception_only():
    # The one place a repository's refs are walked into points, and the one
    # reader of what it walked: named here so a new caller is a decision.
    scoped = _hits(r"record_point\([^)]*\bscope\s*=", allow=(UPGRADE,))
    assert [h.split(":", 1)[0] for h in scoped] == ["core/points.py", "core/snapshots.py"], scoped
    walked = _hits(r"\bwalked\(pool")
    assert {h.split(":", 1)[0] for h in walked} <= {"core/transfer_ranges.py", "core/publisher/bundle.py"}, walked
