"""`mmw update`, and the offer at the end of a connected-side command."""
import sys
from pathlib import Path

from mmw.commands.common import eprint
from mmw.core import update as U

# Commands that never check or prompt: the far side never touches a network,
# `site _root` is run by another mmw over ssh, the TUI asks for itself.
NEVER = {"site", "update", "manage"}


def _interactive() -> bool:
    """A person at a terminal: stdin, stdout and stderr all ttys (a pipe on
    stdout is a script, and never gets a prompt)."""
    return sys.stdin.isatty() and sys.stdout.isatty() and sys.stderr.isatty()


def ask(offer: U.Offer) -> bool:
    """Update now? — on stderr, so stdout stays the command's own; Ctrl-C or
    end of input is No."""
    choice = "[Y/n]" if offer.default_yes() else "[y/N]"
    sys.stderr.write(f"{offer.prompt_text()} {choice} ")
    sys.stderr.flush()
    try:
        answer = input("").strip().lower()
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write("\n")
        return False
    return offer.default_yes() if not answer else answer in ("y", "yes")


_ask = ask


def _run(offer: U.Offer, mirror_dir: Path | None) -> bool:
    busy = U.busy_elsewhere(mirror_dir)
    if busy:
        eprint(busy)
        return False
    kind = U.detect_install()
    if kind.argv is None:
        eprint(kind.note)
        return False
    eprint(f"updating mmw to {offer.newest} ({kind.note})…")
    ok, last = U.run_update(kind, progress=lambda line: eprint(f"  {line}"))
    eprint(f"mmw updated to {offer.newest}" if ok else f"the update failed: {last} — still on {offer.current}")
    return ok


def after_command(checker: "U.Checker | None", mirror_dir: Path | None, exc=None) -> None:
    """At the end of a command: a newer mmw is offered (a terminal) or noted
    (anything else) — never after Ctrl-C, a usage error or a crash."""
    if checker is None:
        return
    import click
    if exc is not None and not isinstance(exc, click.exceptions.Exit):
        return
    offer = checker.result(wait=1.5)
    if offer is None:
        return
    if not _interactive():
        warn = " (a major update — see its release notes)" if offer.major else ""
        eprint(f"mmw {offer.newest} is available (you have {offer.current}){warn} — mmw update")
        return
    if offer.major:
        eprint(f"mmw {offer.newest} is a major update — it may change how mmw works")
    if _ask(offer):
        _run(offer, mirror_dir)


def run(cfg_dir: Path, yes: bool = False) -> int:
    from mmw.commands.common import load_config_or_none
    cfg = load_config_or_none(cfg_dir)
    mirror_dir = cfg.settings.mirror_dir if cfg is not None else None
    newest = U.newest_release()
    offer = U.offer(newest)
    if offer is None:
        if newest is None:
            eprint("couldn't reach PyPI to check for a newer mmw")
            return 1
        print(f"mmw {U.current_version()} is the newest")
        return 0
    if not yes:
        if not _interactive():
            eprint(f"mmw {offer.newest} is available — pass --yes to update (no terminal to ask on)")
            return 1
        if not _ask(offer):
            return 0
    if _run(offer, mirror_dir):
        print(f"mmw updated to {offer.newest}")
        return 0
    return 1
