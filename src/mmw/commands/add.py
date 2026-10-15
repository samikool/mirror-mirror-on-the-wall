"""Headless source import — `mmw add` reading an apt config file.

A sources.list / .sources file already states URL, suite, components and arch,
so this path does no discovery at all: it never touches discover.py's scrape
caches, and it works offline apart from an optional --url fetch.
"""
import sys
from pathlib import Path

import tomli_w

from mmw.commands.common import ConfigError, eprint, load_config
from mmw.core import config, discover, keys
from mmw.providers import get_provider
from mmw.core.apt_sources import (
    AptEntry, entries_to_sources, expand_placeholder_entry, parse_apt_sources, remember_sources_url,
)


def read_input(files: "list[Path] | Path | None", url: str | None) -> str:
    """Config text from files, a directory, a URL, or stdin.

    Several files (or a directory of them) are joined with a **blank line**,
    because that is what separates deb822 records. Doing it here rather than
    leaving the user to `cat` matters: those files routinely end with a bare
    newline — one on a real DGX ends with a trailing space and no newline at
    all — so concatenating them by hand glues one record's last line onto the
    next record's first. mmw controls this join, so it cannot go wrong.
    """
    if url:
        from mmw.core.http import retrying_client
        with retrying_client(timeout=15, follow_redirects=True) as client:
            r = client.get(url)
            r.raise_for_status()
            return r.text

    paths = [files] if isinstance(files, Path) else list(files or [])
    if not paths or (len(paths) == 1 and str(paths[0]) == "-"):
        # An interactive terminal with no `-f` and no `--url` is not a request
        # to read a config off the keyboard: `sys.stdin.read()` simply blocked
        # forever with no prompt, which reads as a hang. An explicit `-f -`
        # still means stdin, TTY or not.
        if sys.stdin.isatty() and not (len(paths) == 1 and str(paths[0]) == "-"):
            raise ValueError(
                "nothing to read. Pass -f FILE (or a directory of them), "
                "--url URL, or pipe a config in:\n"
                "  mmw add -f /etc/apt/sources.list.d\n"
                "  cat ubuntu.sources | mmw add\n"
                "  mmw add -f -            # read stdin explicitly"
            )
        return sys.stdin.read()

    expanded: list[Path] = []
    for p in paths:
        if p.is_dir():
            expanded.extend(sorted(q for q in p.iterdir()
                                   if q.is_file() and q.suffix in (".sources", ".list")))
        else:
            expanded.append(p)
    if not expanded:
        raise FileNotFoundError(
            f"no .sources or .list files under {', '.join(str(p) for p in paths)}"
        )
    return "\n\n".join(p.read_text().strip() for p in expanded) + "\n"


def attach_keys(sources: list[config.SourceConfig], keys_dir: Path) -> tuple[int, int]:
    """Fill in key material from a directory of keyrings. Returns (matched, missing).

    A `Signed-By: /usr/share/keyrings/cuda_debian_prod.gpg` names a file on the
    machine the config came from — the one thing a config file cannot carry.
    Copy those keyrings off that machine, point `--keys-dir` at them, and they
    are matched by basename, exactly as the review pane does in the TUI. Binary
    keyrings are fine: `read_key` armours them losslessly.

    A key the config carried inline is the authority and is never overwritten.
    """
    found = keys.find_keys(keys_dir)
    matched = missing = 0
    for s in sources:
        if s.gpg_key or not s.gpg_key_name:
            continue
        path = found.get(s.gpg_key_name)
        if path is None:
            missing += 1
            continue
        try:
            s.gpg_key = keys.read_key(path)
            matched += 1
        except OSError:
            missing += 1
    return matched, missing


def _resolve_arch_placeholders(entries: list[AptEntry], arch: str, echo,
                               echo_err=None) -> list[AptEntry] | None:
    """Fan out a `$(ARCH)`-templated entry into one concrete entry per
    architecture it actually serves.

    `--arch` resolves it without touching the network — an explicit user
    choice is not a guess. Otherwise this is the one case where headless
    `mmw add` must reach into `discover` (see the module docstring): apt's own
    substitution has no local machine to run on here, and the repository root
    usually has nothing else at it to fall back to (see
    `apt_sources._parse_one_line_body`). Never writes an unresolved
    placeholder into the config — resolving to nothing is a hard error.
    """
    resolved: list[AptEntry] = []
    for e in entries:
        if not e.arch_placeholder:
            resolved.append(e)
            continue
        if arch:
            resolved.extend(expand_placeholder_entry(e, [arch]))
            continue
        found = discover.expand_arch_placeholder(e.url, e.arch_placeholder)
        if not found:
            echo(f"{e.url}: {e.arch_placeholder} resolved to no architecture — "
                 "pass --arch to force one, or check the repository is reachable.")
            return None
        echo(f"# {e.url}: {e.arch_placeholder} -> {', '.join(found)}")
        resolved.extend(expand_placeholder_entry(e, found))
    return resolved


def _as_toml(sources: list[config.SourceConfig]) -> str:
    """Render sources exactly as config.save() would write them."""
    return tomli_w.dumps({"sources": [config.source_to_dict(s) for s in sources]})


def run(
    *,
    file: "list[Path] | Path | None" = None,
    url: str | None = None,
    label: str | None = None,
    arch: str = "",
    keys_dir: Path | None = None,
    dry_run: bool = False,
    cfg_dir: Path = Path("."),
    echo=print,
    echo_err=None,
) -> int:
    """Import sources into mmw.toml. Returns a process exit code.

    `echo` carries the result (the TOML preview, the per-source lines);
    `echo_err` carries everything that means the command failed. They were one
    parameter, so every failure here went to **stdout** — a cron job mailing
    only stderr got silence from the one run that did nothing.
    """
    if echo_err is None:
        echo_err = eprint
    try:
        text = read_input(file, url)
    except Exception as e:
        echo_err(f"Could not read config: {e}")
        return 1

    entries = parse_apt_sources(text)
    if not entries:
        echo_err(
            "No mirrorable apt sources found.\n"
            "Expected a sources.list or deb822 .sources file. Note that "
            "`Enabled: no` stanzas, deb-src-only entries, and non-http "
            "sources (cdrom:/file:) are skipped."
        )
        return 1

    entries = _resolve_arch_placeholders(entries, arch, echo, echo_err)
    if entries is None:
        return 1

    new_sources = entries_to_sources(entries, label=label, default_arch=arch)
    if url:
        remember_sources_url(new_sources, url)

    if keys_dir is not None:
        matched, missing = attach_keys(new_sources, keys_dir)
        echo(f"# {matched} key(s) matched from {keys_dir}"
             + (f", {missing} still missing" if missing else ""))

    if dry_run:
        echo(_as_toml(new_sources))
        total = sum(len(get_provider(s.provider).expand(s)) for s in new_sources)
        echo(f"# {len(new_sources)} source(s), {total} mirror(s) — nothing written")
        return 0

    try:
        cfg = load_config(cfg_dir)
    except ConfigError as e:
        echo_err(f"{e}\n(--dry-run previews without a config.)")
        return 1

    # Each of these refuses rather than writes: a URI configured as the other
    # provider, a duplicate mirror name, or a file `load()` would reject.
    # They used to escape as tracebacks — one line on stderr says it.
    try:
        merged, added, updated = config.merge_sources(cfg.sources, new_sources)
        # Re-validate before writing: duplicate mirror names must never reach disk.
        cfg = config.Config(settings=cfg.settings, sources=merged, networks=cfg.networks)
        config.save(cfg, cfg_dir)
    except (ValueError, OSError) as e:
        echo_err(f"Nothing written: {e}")
        return 1

    for s in new_sources:
        echo(f"  {s.name}  [{s.provider}] {', '.join(s.arches)}  {s.url}")
    echo(f"{added} source(s) added, {updated} updated → {config.config_path(cfg_dir)}")
    return 0
