"""An ssh root whose machine has no mmw, or an older one: offer to install
this mmw there, from the installer that travels next to a bundle, over the
same key-based ssh (what `mmw site open HOST:/PATH --install TAR` does)."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from textual.widgets import Static

from mmw.tui.site.run_page import RunPage, escape_markup


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
        say("", "info")
        page.app.push_screen(InstallRunPage(host, tar), lambda ok: ok and done())

    def done() -> None:
        say(f"mmw installed on {host}", "ok")
        then()

    page.run_worker(find, thread=True, group="site-install", exit_on_error=False)


class InstallRunPage(RunPage):
    """The installer going over ssh, then its install.sh running there."""

    TITLE = "Installing mmw"
    RATE = "sending"

    def __init__(self, host: str, tar: Path):
        from mmw.core import disk_io
        super().__init__(disk_io.ops.stat(tar).st_size, [(host, f"mmw → {host}")], {host: 0})
        self._host, self._tar = host, tar
        self._sizes[host] = self._total
        self.subtitle = f"{tar.name} → {host}"
        self.ok = False

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one("#btn-run-stop").disabled = True     # an install half-run helps nobody

    def updated_note(self) -> str:
        return "installed"

    def job(self, progress, stop):
        from mmw.site import ssh
        progress("repo", repo=self._host)
        ssh.install_remote(self._host, self._tar, progress=lambda n: progress("receive", bytes=n))
        progress("finished", repo=self._host)
        progress("done")

    def _done(self, _result) -> None:
        self.ok = True
        self.query_one("#run-done", Static).update(
            f"[b $success]✓ mmw installed on {escape_markup(self._host)}[/] — Close carries on")
        self.set_status("")
        self._finish()

    def close_value(self) -> bool:
        return self.ok                      # installed: the action that needed it carries on
