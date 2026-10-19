"""What a network publishes for PyPI, and the far side's PYPI.md.

Rows come from the pool (each unit's refs), narrowed by the network's
retention against the unit's own history; pages are built from the units'
records. Nothing reads the live index cache, so a snapshot replays exactly.
"""
from datetime import date

from mmw.core.config import retention_days
from mmw.core.publisher.links import _dedup_rows
from mmw.providers.pypi import history as hist
from mmw.providers.pypi import records

HOSTS = ("pypi.org", "files.pythonhosted.org")
META_PREFIX = ".pypi-meta/"


def _read(pool, unit_name: str, rel: str) -> bytes | None:
    path = pool.resolve(unit_name, rel)
    try:
        return path.read_bytes() if path is not None else None
    except OSError:
        return None


def published_rows(pool, units, network, today: date):
    """(file rows to link, {repo_path: Record} for the files that survive)."""
    days = retention_days(network.retention)
    rows, recs = [], {}
    for unit in units:
        history = hist.load(_read(pool, unit.name, hist.HISTORY_PATH))
        gone = {path for path, e in history.items() if not hist.in_window(e.left_at, days, today)}
        unit_recs = records.load(_read(pool, unit.name, records.RECORDS_PATH))
        for row in pool.mirror_files([unit.name]):
            path = row[0]
            if path.startswith(META_PREFIX):
                continue
            if path in gone or path.removesuffix(".metadata") in gone:
                continue
            rows.append(row)
            if path in unit_recs:
                recs[path] = unit_recs[path]
    return _dedup_rows(rows), recs


def pypi_md(network, hosts=HOSTS) -> bytes:
    lines = [
        f"# PyPI on network `{network.name}`",
        "",
        "This tree serves PyPI at its real hostnames. Clients keep their stock",
        "settings: no `--index-url`, no `--find-links`, no client index flags needed.",
        "",
        "## DNS",
        "",
        "Point these names at this server (DNS, or a hosts file per client):",
        "",
        *(f"    <server-ip>  {h}" for h in hosts),
        "",
        "## Trusting the certificate",
        "",
        "Install the mmw CA in the system trust store, then:",
        "",
        "- **pip** ≥ 24.2 on Python ≥ 3.10 uses the system trust store. Older pip:",
        "  `PIP_CERT=/path/to/ca.pem`, or `cert = /path/to/ca.pem` in `pip.conf`.",
        "- **uv**: `UV_NATIVE_TLS=1` (system store) or `SSL_CERT_FILE=/path/to/ca.pem`.",
        "- **requests-based tools** (Poetry, twine, …): `REQUESTS_CA_BUNDLE=/path/to/ca.pem`.",
        "",
        "## What is here",
        "",
        "- `pip install <pkg>` installs every package the mirror lists, with its dependencies.",
        "- Packages from the requirements list install together, as `pip install -r` would.",
        f"- Retention: `{network.retention}` — releases that left the mirror stay this long,",
        "  so lock files and pinned requirements keep working.",
        "",
    ]
    return "\n".join(lines).encode()
