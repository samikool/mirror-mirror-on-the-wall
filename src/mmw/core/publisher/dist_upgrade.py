"""Ubuntu dist-upgrade support: meta-release publishing, upgrade-path detection,
and the generated UPGRADE.md."""
from mmw.core.config import Config, NetworkConfig
from mmw.core.pool import Pool
from mmw.core.resolve import network_mirrors

from mmw.core.publisher.links import hardlink_or_copy
from mmw.core.publisher.results import NetworkPublishResult


def parse_meta_release_lts(pool: Pool) -> list[tuple[str, str]]:
    """Parse the pooled meta-release-lts blob.

    Returns [(codename, version_str)] in file order, e.g. [("focal", "20.04"), ...].
    Returns [] if the file hasn't been synced yet.
    """
    path = pool.find_blob("changelogs.ubuntu.com/meta-release-lts")
    if path is None or not path.exists():
        return []
    releases: list[tuple[str, str]] = []
    dist = version = ""
    for line in path.read_text(errors="replace").splitlines():
        if line.startswith("Dist:"):
            dist = line.split(":", 1)[1].strip()
            version = ""
        elif line.startswith("Version:") and dist:
            version = line.split(":", 1)[1].strip().replace(" LTS", "").strip()
        elif line == "" and dist and version:
            releases.append((dist, version))
            dist = version = ""
    if dist and version:
        releases.append((dist, version))
    return releases


def dist_upgrade_info(network: NetworkConfig, cfg: Config, pool: Pool) -> tuple[list[tuple[str, str]], list[str]]:
    """Return (upgrade_paths, warnings) for dist-upgrade validation.

    Only considers suites actually selected in this network.
    upgrade_paths: consecutive LTS pairs both present as base suites.
    warnings: base suites missing their -updates counterpart.
    """
    mirror_map = {m.name: m for m in network_mirrors(network, cfg, cfg.settings.mirror_dir)}
    selected_names = list(mirror_map)

    upgrade_suites: set[str] = set()
    for name in selected_names:
        m = mirror_map.get(name)
        if m and m.provider == "apt" and getattr(m, "dist_upgrade", False) and m.suite:
            upgrade_suites.add(m.suite)

    if not upgrade_suites:
        return [], []

    _suffixes = ("-updates", "-security", "-backports", "-proposed")
    base_suites = {s for s in upgrade_suites if not any(s.endswith(sfx) for sfx in _suffixes)}

    warnings = []
    for suite in sorted(base_suites):
        if f"{suite}-updates" not in upgrade_suites:
            warnings.append(
                f"{suite}: {suite}-updates not in network — dist-upgrade tool tarball will be missing"
            )

    # Derive upgrade path ordering from the mirrored meta-release-lts file — no hardcoding.
    lts_order = [dist for dist, _ver in parse_meta_release_lts(pool)]
    lts_present = [s for s in lts_order if s in base_suites]
    upgrade_paths = [(lts_present[i], lts_present[i + 1]) for i in range(len(lts_present) - 1)]

    return upgrade_paths, warnings


def publish_changelogs(network: NetworkConfig, pool: Pool, result: NetworkPublishResult) -> None:
    """Hardlink pooled meta-release blobs into the network publish tree."""
    dst_dir = network.root / "changelogs.ubuntu.com"
    dst_dir.mkdir(parents=True, exist_ok=True)

    for name in ("meta-release", "meta-release-lts"):
        src = pool.find_blob(f"changelogs.ubuntu.com/{name}")
        if src is None or not src.exists():
            result.dist_upgrade_warnings.append(
                f"{name} not synced yet — run `mmw sync` for a Ubuntu main mirror first"
            )
            continue
        try:
            hardlink_or_copy(src, dst_dir / name)
        except OSError as e:
            result.dist_upgrade_warnings.append(f"could not publish {name}: {e}")


def upgrade_hostnames(mirrors: "list | None") -> list[tuple[str, str]]:
    """(hostname, purpose) rows for the DNS table — from what this network
    actually mirrors, never a hardcoded list.

    The table used to name `archive.ubuntu.com` and `security.ubuntu.com`
    unconditionally. An arm64 network mirrors **ports.ubuntu.com** and neither
    of those, so the guide told the user to point DNS at two hostnames they had
    no copy of — and omitted the one they did.
    """
    hosts: list[tuple[str, str]] = []
    for mirror in mirrors or []:
        host = mirror.mirror_base.split("/", 1)[0]
        if host and host not in [h for h, _ in hosts]:
            hosts.append((host, "Package downloads, upgrade tool tarballs"))
    hosts.sort()
    # Always required: `do-release-upgrade` discovers releases here, and it is
    # published to the network root rather than under a mirror base.
    hosts.append(("changelogs.ubuntu.com",
                  "Release upgrade discovery (`do-release-upgrade`)"))
    return hosts


def write_upgrade_md(network: NetworkConfig, pool: Pool, paths: list[tuple[str, str]],
                     warnings: list[str], mirrors: "list | None" = None) -> None:
    """Write UPGRADE.md at the network root."""
    versions = dict(parse_meta_release_lts(pool))

    lines = [
        "# Dist-Upgrade Guide",
        "",
        f"Network: **{network.name}**",
        f"Mirror root: `{network.root}`",
        "",
        "## DNS Configuration Required",
        "",
        "Point these hostnames at this mirror server:",
        "",
        "| Hostname | Purpose |",
        "|----------|---------|",
        *[f"| `{host}` | {purpose} |" for host, purpose in upgrade_hostnames(mirrors)],
        "",
        "## Available Upgrade Paths",
        "",
    ]

    if paths:
        for from_suite, to_suite in paths:
            from_ver = versions.get(from_suite, from_suite)
            to_ver = versions.get(to_suite, to_suite)
            lines.append(f"- **{from_suite}** ({from_ver} LTS) → **{to_suite}** ({to_ver} LTS)")
    else:
        lines.append("- None detected (need at least two consecutive LTS releases in this network)")

    lines += [
        "",
        "## Running a Dist-Upgrade",
        "",
        "On the managed machine:",
        "",
        "```bash",
        "sudo do-release-upgrade",
        "```",
        "",
        "## Notes",
        "",
        "**Valid-Until:** Ubuntu Release files include an expiry date. If `apt update`",
        "rejects the metadata, pass `-o Acquire::Check-Valid-Until=false`.",
        "",
    ]

    if warnings:
        lines += ["## Warnings", ""]
        for w in warnings:
            lines.append(f"- {w}")
        lines.append("")

    (network.root / "UPGRADE.md").write_text("\n".join(lines))
