"""PyPI authoring: the shared provider form, a live summary, and Check —
resolve the requirements list per target without saving anything."""
import tempfile
import threading
from pathlib import Path

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Label, Static

from mmw.core import config, disk_io
from mmw.providers import get_provider
from mmw.providers.pypi import uv as uvmod
from mmw.providers.pypi.targets import targets
from mmw.tui.provider_form import ProviderEditPage, ProviderSourceForm, ProviderStep
from mmw.tui.ui.widgets import Button

IDENTITY = config.normalize_url(config.PYPI_URL)


def uv_reason(error: str) -> str:
    """The sentence worth showing from a uv failure: its "╰─▶ Because …"
    explanation joined onto one line, else its first line."""
    lines = error.splitlines()
    start = next((i for i, line in enumerate(lines) if "╰─▶" in line), None)
    if start is None:
        return next((line.strip(" ×") for line in lines if line.strip()), "uv failed")
    reason = [lines[start].split("╰─▶", 1)[1]]
    for line in lines[start + 1:]:
        if not line.strip() or line.strip().startswith(("hint:", "help:", "×")):
            break
        reason.append(line)
    return " ".join(" ".join(reason).split())


def check_requirements(source, *, config_dir, cache_dir, progress=None, cancelled=None) -> list[str]:
    """Resolve `source`'s requirements list for each of its targets with uv —
    nothing saved, nothing synced. One line per target, or one message."""
    try:
        lines = source.requirement_lines(config_dir)
    except (OSError, ValueError) as e:
        return [f"Requirements not read — {e}"]
    if not lines:
        return ["No requirements to check."]
    ts = targets(source.pythons, source.platforms, source.arches)
    out = []
    for i, target in enumerate(ts, start=1):
        if cancelled is not None and cancelled():
            return out                     # superseded: stop spending uv runs on it
        if progress:
            progress(f"Checking {target.key} ({i}/{len(ts)})…")
        got = uvmod.compile_for(lines, target, source.floors, cache_dir=cache_dir, cancelled=cancelled)
        if got.error == "cancelled":
            return out
        if got.error is None:
            out.append(f"{target.key}: ✓ {len(got.pins)} packages"
                       + (" — needs source builds" if got.built else ""))
        else:
            out.append(f"{target.key}: ✗ {uv_reason(got.error)}")
    return out


class PypiEditor(Vertical):
    def __init__(self, source, *, existing_names=None):
        super().__init__()
        self.form = ProviderSourceForm(source, existing_names=existing_names)
        self._check_gen = 0
        self._scratch: str | None = None      # a uv cache when there is no mirror dir yet

    def compose(self) -> ComposeResult:
        yield self.form
        yield Label("", id="pypi-summary", markup=False)
        yield Button("Check requirements", id="btn-pypi-check", compact=True)
        # Static, not Label: a conflict explanation is long and must wrap.
        yield Static("", id="pypi-check-status", markup=False)

    def on_mount(self):
        self._summary()

    def _summary(self):
        if not self.is_mounted:
            return
        try:
            source = self.form.get_source(focus_errors=False)
        except ValueError:
            return
        self.query_one("#pypi-summary", Label).update(get_provider("pypi").source_summary(source))

    @on(ProviderSourceForm.Changed)
    def _form_changed(self):
        self.invalidate_checks()
        self._summary()

    def invalidate_checks(self):
        """A result belongs to the list it checked: any edit, a new Check or
        leaving the page drops a result still on its way."""
        self._check_gen += 1
        if self.is_mounted:
            self.query_one("#pypi-check-status", Static).update("")

    def on_unmount(self):
        self._check_gen += 1
        if self._scratch is not None:
            # Off the event loop: removing a uv cache is disk work.
            threading.Thread(target=disk_io.ops.rmtree, args=(self._scratch,), kwargs={"ignore_errors": True},
                             daemon=True).start()

    @on(Button.Pressed, "#btn-pypi-check")
    def _check(self, event):
        event.stop()
        try:
            source = self.form.get_source()
        except ValueError as exc:
            self.query_one("#pypi-check-status", Static).update(str(exc))
            return
        self._check_gen += 1
        self._status(self._check_gen, "Checking…")
        self._check_worker(self._check_gen, source)

    def _status(self, gen, text):
        if gen == self._check_gen and self.is_mounted:
            self.query_one("#pypi-check-status", Static).update(text)

    def _dirs(self):
        cfg = getattr(self.app, "cfg", None)
        config_dir = getattr(self.app, "cfg_dir", None)
        if cfg is not None:
            return config_dir, uvmod.cache_dir(cfg.settings.mirror_dir)
        if self._scratch is None:
            self._scratch = tempfile.mkdtemp(prefix="mmw-uv-check-")
        return config_dir, Path(self._scratch)

    @work(thread=True, exit_on_error=False, group="pypi-check", exclusive=True)
    def _check_worker(self, gen, source):
        config_dir, cache_dir = self._dirs()
        say = lambda text: self.app.call_from_thread(self._status, gen, text)  # noqa: E731
        try:
            text = "\n".join(check_requirements(source, config_dir=config_dir, cache_dir=cache_dir,
                                                 progress=say, cancelled=lambda: gen != self._check_gen))
        except Exception as exc:
            text = f"Check failed: {exc}"
        if gen == self._check_gen and self.is_mounted:
            self.app.call_from_thread(self._status, gen, text)


class StepPypi(ProviderStep):
    def __init__(self, existing_urls):
        self.existing_urls = existing_urls
        super().__init__(PypiEditor(config.PypiSource(provider="pypi")), heading="PyPI · pypi.org",
                         replaces=("This replaces the existing PyPI source."
                                   if IDENTITY in existing_urls else None),
                         error_id="pypi-form-error")


class EditPypiPage(ProviderEditPage):
    TITLE = "Edit PyPI source"

    def __init__(self, source, *, existing_names, existing_urls, save_source=None):
        super().__init__(PypiEditor(source, existing_names=existing_names), title=self.TITLE,
                         save_source=save_source)
