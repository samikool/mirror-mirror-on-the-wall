"""A newer mmw, offered in the TUI: a dialog on launch, the update with its
output shown, then mmw starts again where it was and says it worked."""
import os
import shutil
import sys

from textual import work
from textual.app import ComposeResult

from mmw.core import update as U
from mmw.tui.ui.dialog import ConfirmDialog
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button, ThemedLog

UPDATED_FROM = "MMW_UPDATED_FROM"


class UpdateDialog(ConfirmDialog):
    """Update now? — Yes is the default for a minor or patch update, "Not
    now" for a major one (it may change how mmw works)."""

    def __init__(self, offer: U.Offer):
        title = (f"mmw {offer.newest} is a major update" if offer.major
                 else f"mmw {offer.newest} is available")
        message = (f"You have {offer.current}. A major version may change commands, settings or a mirror's "
                   "layout — read its release notes first. Update now?" if offer.major
                   else f"You have {offer.current}. Update now? mmw restarts right where you are.")
        super().__init__(title, message, confirm_label="Update now", cancel_label="Not now",
                         confirm_variant="warning" if offer.major else "primary", cancel_variant="default")
        self._default_yes = offer.default_yes()
        # The screen's own auto-focus picks the default button: focusing it in
        # on_mount raced that auto-focus, which sometimes landed on Not now.
        self.AUTO_FOCUS = "#confirm" if self._default_yes else "#cancel"


RESUME_BUNDLE = "MMW_RESUME_BUNDLE"


def relaunch_if_updated(app) -> None:
    """After `app.run()` returns: an update finished, so start the new mmw —
    with the terminal already restored."""
    after = getattr(app, "relaunch_after", None)
    if after:
        relaunch(*after)


def relaunch(old_version: str, resume: str | None = None) -> None:
    """Start this mmw again, the same way, saying what it was updated from
    (and, on the far side, which bundle to carry on with)."""
    from mmw.core.update import installer_prefix
    prefix = installer_prefix()
    exe = str(prefix / "bin" / "mmw") if prefix else (shutil.which(sys.argv[0]) or sys.argv[0])
    env = {**os.environ, UPDATED_FROM: old_version, **({RESUME_BUNDLE: resume} if resume else {})}
    os.execve(exe, [exe, *sys.argv[1:]], env)


class UpdateRunPage(Page):
    """The update running, its output shown; then a restart (or why not)."""

    TITLE = "Updating mmw"

    def __init__(self, offer: U.Offer, run=None, resume: str | None = None, then=None):
        super().__init__()
        self._offer = offer
        self._run_it = run                      # the far side hands its own update in
        self._resume = resume
        self._then = then                       # carried on after a failed update (the far side's apply)
        self.subtitle = f"{offer.current} → {offer.newest}"

    def compose_body(self) -> ComposeResult:
        yield ThemedLog(id="update-log", markup=False)

    def compose_actions(self) -> ComposeResult:
        yield Button("Close", id="btn-update-close", compact=True, disabled=True)

    def on_mount(self) -> None:
        self.set_status("updating…")
        self._go()

    @work(thread=True, exclusive=True, group="mmw-update", exit_on_error=False)
    def _go(self) -> None:
        log = lambda line: self.app.call_from_thread(self.query_one("#update-log", ThemedLog).write, line)  # noqa: E731
        try:
            if self._run_it is not None:
                ok, last = self._run_it(log)
            else:
                kind = U.detect_install()
                if kind.argv is None:
                    ok, last = False, kind.note
                else:
                    log(f"$ {' '.join(kind.argv)}")
                    ok, last = U.run_update(kind, progress=log)
        except Exception as e:  # noqa: BLE001 — said on the page; the running mmw is untouched
            ok, last = False, str(e)
        self.app.call_from_thread(self._done, ok, last)

    def _done(self, ok: bool, last: str) -> None:
        if ok:
            # Restarted only once the app has exited and handed the terminal
            # back (`relaunch_if_updated`, after app.run() returns).
            self.set_status(f"updated to {self._offer.newest} — restarting…", "ok")
            self.app.relaunch_after = (self._offer.current, self._resume)
            self.app.exit()
            return
        self.set_status(f"the update failed: {last} — still on {self._offer.current}", "error")
        close = self.query_one("#btn-update-close", Button)
        close.disabled = False
        close.focus()

    def on_button_pressed(self, event) -> None:
        if event.button.id == "btn-update-close":
            self.dismiss(None)
            if self._then:
                self._then()                    # the update failed: carry on as with No


class UpdateOffers:
    """For a TUI app: offer a newer mmw on launch, keep a marker after a No,
    and toast after a relaunch. The app gives `_update_mirror_dir()` (for the
    busy check) and calls `_start_update_offers(checker)` from on_mount."""

    _update_offer: "U.Offer | None" = None
    _update_run = None                                  # a far-side update, when there is one

    def _update_toast(self) -> None:
        old = os.environ.pop(UPDATED_FROM, None)
        if old:
            self.notify(f"Updated to {U.current_version()} (from {old})", severity="information", timeout=8)

    def _start_update_offers(self, checker: "U.Checker | None") -> None:
        self._update_toast()
        if checker is None:
            return
        self._update_checker = checker
        self.set_timer(0.2, self._poll_update)

    def _poll_update(self, waited: float = 0.0) -> None:
        if not self._update_checker._done.is_set() and waited < 3.0:
            self.set_timer(0.2, lambda: self._poll_update(waited + 0.2))
            return
        offer = self._update_checker.result()
        if offer is not None:
            self.offer_update(offer)

    def offer_update(self, offer: "U.Offer", run=None, then=None, resume: str | None = None) -> None:
        """Update now? `then` carries on when the answer is No (or it can't
        run); `resume` is what the restarted mmw picks up again."""
        self._update_offer, self._update_run = offer, run
        self._update_then, self._update_resume = then, resume
        self.push_screen(UpdateDialog(offer), self._update_answered)

    def _update_answered(self, yes) -> None:
        offer, then = self._update_offer, getattr(self, "_update_then", None)
        if not yes:
            if "update available" not in self.sub_title:
                self.sub_title = f"{self.sub_title} · update available: {offer.newest} (u)".lstrip(" ·")
            if then:
                then()
            return
        busy = self._update_busy()
        if busy:
            self.notify(busy, severity="warning", timeout=10)
            if then:
                then()
            return
        self.push_screen(UpdateRunPage(offer, self._update_run, getattr(self, "_update_resume", None), then))

    def offer_far_update(self, src: str, then) -> None:
        """The far side: a newer mmw next to the bundle at `src` is asked
        about first; `then()` carries on (No, nothing newer, or checks off)."""
        from mmw.site.update import apply_far_update, far_offer
        if U.checks_disabled():
            then()
            return
        try:
            far = far_offer(src, getattr(self, "mset", None))
        except Exception:  # noqa: BLE001 — an odd folder never blocks an apply
            far = None
        if far is None or far.offer is None:
            then()
            return

        def run(log):
            try:
                apply_far_update(far, log)
                return True, ""
            except Exception as e:  # noqa: BLE001 — said on the page
                return False, str(e)
        self.offer_update(far.offer, run=run, then=then, resume=src)

    def _update_busy(self) -> str | None:
        for job in (getattr(self, "active_sync", None), getattr(self, "active_transfer", None)):
            if job is not None and getattr(job, "running", False):
                return "a sync or transfer is running — update once it's done"
        return U.busy_elsewhere(self._update_mirror_dir())

    def _update_mirror_dir(self):
        return None

    def action_update(self) -> None:
        if self._update_offer is not None:
            self.offer_update(self._update_offer, self._update_run)
