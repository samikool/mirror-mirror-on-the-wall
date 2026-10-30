"""Folder repositories: the shared provider form, with a live summary."""
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Label

from mmw.core import config
from mmw.providers import get_provider
from mmw.tui.provider_form import NEW_KEY, ProviderEditPage, ProviderSourceForm, ProviderStep


def blank_source() -> config.LocalAptSource:
    """The Add step's starting point: nothing typed yet, so unvalidated."""
    return config.LocalAptSource.model_construct(
        provider="apt-local", url="", folder="", key=NEW_KEY, name="", label="", structured=False,
        suite="stable", component="main", contents=False)


class LocalEditor(Vertical):
    def __init__(self, source, *, existing_names=None):
        super().__init__()
        self.form = ProviderSourceForm(source, existing_names=existing_names)

    def compose(self) -> ComposeResult:
        yield self.form
        yield Label("", id="local-summary", markup=False)

    def on_mount(self):
        self._summary()

    @on(ProviderSourceForm.Changed)
    def _summary(self):
        if not self.is_mounted:
            return
        try:
            source = self.form.get_source(focus_errors=False)
        except ValueError:
            text = ""
        else:
            key = "a new key" if source.key == NEW_KEY else f"key {source.key}"
            layout = (f"dists/{source.suite}/{source.component}" if source.structured
                      else "flat (deb URL ./)")
            text = f"Serves {source.folder} at {source.url} — {layout}, signed with {key}."
        self.query_one("#local-summary", Label).update(text)


class StepLocal(ProviderStep):
    def __init__(self, existing_urls):
        super().__init__(LocalEditor(blank_source()), heading="Local folder · your own .deb files",
                         replaces=None, error_id="local-form-error")


class EditLocalPage(ProviderEditPage):
    TITLE = "Edit folder repository"

    def __init__(self, source, *, existing_names, existing_urls, save_source=None):
        super().__init__(LocalEditor(source, existing_names=existing_names), title=self.TITLE,
                         save_source=save_source)


def describe(source) -> str:
    return get_provider("apt-local").source_summary(source)
