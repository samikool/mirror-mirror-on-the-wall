"""An ssh root whose machine has no mmw, or an older one: offer to install
this mmw there, from the installer that travels next to a bundle, over the
same key-based ssh (what `mmw site open HOST:/PATH --install TAR` does)."""
from __future__ import annotations

from pathlib import Path
from typing import Callable


def offer_install(page, location: str, problem, then: Callable[[], None],
                  say: Callable[[str, str], None]) -> None:
    """Find the installer for the remote's architecture, ask, push it, then
    `then()` — the action that found mmw missing, again. `problem` is the
    RemoteMmw; `say(text, kind)` reports on the page."""
    from mmw.core.runtime import mmw_version
    from mmw.site import ssh
    host = ssh.parse_location(location)[1]
    here = mmw_version()

    def find() -> None:
        from mmw.site.update import installer_for
        from mmw.tui.site.apply_flow import recent_paths
        try:
            arch = ssh.remote_arch(host)
        except Exception as e:  # noqa: BLE001 — ssh refused, host gone: said on the page
            page.app.call_from_thread(say, f"{problem.reason} — and asking its architecture failed: {e}", "error")
            return
        page.app.call_from_thread(ask, arch, installer_for(arch, recent_paths(), here))

    def ask(arch: str, tar: Path | None) -> None:
        if tar is None:
            from mmw.tui.path_pick import PathPickPage
            say(f"{problem.reason} — pick mmw-{here}-{arch}.tar (or newer) to install there", "info")
            page.app.push_screen(
                PathPickPage(f"The mmw installer for {host}",
                             f"mmw-{here}-{arch}.tar or newer — it travels next to a bundle",
                             dirs_only=False, suffixes=(".tar",)),
                lambda path: path and picked(arch, Path(path)))
            return
        from mmw.tui.ui.dialog import ConfirmDialog
        page.app.push_screen(
            ConfirmDialog(f"Install mmw on {host}?", f"{problem.reason}.",
                          confirm_label="Install", confirm_variant="primary", cancel_variant="default",
                          details=(f"pushes {tar.name} (from {tar.parent}) over this same ssh and runs its "
                                   f"install.sh there, then carries on").replace("[", "\\[")),
            lambda ok: ok and install(tar))

    def picked(arch: str, tar: Path) -> None:
        from packaging.version import InvalidVersion, Version
        from mmw.site.update import _NAME
        m = _NAME.match(tar.name)
        try:
            ok = m is not None and m["arch"] == arch and Version(m["version"]) >= Version(here)
        except InvalidVersion:
            ok = False
        if not ok:
            say(f"{tar.name} isn't an installer for {host}: it needs mmw-{here}-{arch}.tar or newer", "error")
            return
        install(tar)

    def install(tar: Path) -> None:
        say(f"installing mmw on {host}…", "info")
        page.run_worker(lambda: push(tar), thread=True, group="site-install", exit_on_error=False)

    def push(tar: Path) -> None:
        try:
            ssh.install_remote(host, tar)
        except Exception as e:  # noqa: BLE001
            page.app.call_from_thread(say, str(e), "error")
            return
        page.app.call_from_thread(done)

    def done() -> None:
        say(f"mmw installed on {host}", "ok")
        then()

    page.run_worker(find, thread=True, group="site-install", exit_on_error=False)
