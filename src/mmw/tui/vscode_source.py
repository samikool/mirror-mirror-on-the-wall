"""VS Code authoring: shared editor and optional worker lookups."""

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Label

from mmw.core import config
from mmw.providers.vscode import marketplace as mp
from mmw.providers.vscode import provider as vs
from mmw.tui.provider_form import ProviderEditPage, ProviderSourceForm, ProviderStep
from mmw.tui.ui.widgets import Button


def lookup_ids(source, *, progress=None):
    with vs._client() as client:
        found = mp.by_names(client, source.extensions.ids, on_wait=progress)
    return [(ident, found[ident].get('displayName', ident) if ident in found else None)
            for ident in source.extensions.ids]


class VscodeEditor(Vertical):
    def __init__(self, source, *, existing_names=None):
        super().__init__()
        self.form = ProviderSourceForm(source, existing_names=existing_names)
        self._lookup_gen = 0

    def compose(self) -> ComposeResult:
        yield self.form
        yield Label('No extensions selected; choose Top N, IDs or All.', id='vs-policy-summary', markup=False)
        yield Button('Look up IDs', id='btn-vs-lookup', compact=True)
        yield Label('', id='vs-lookup-status', markup=False)

    def on_mount(self):
        self._policy_summary()

    def _policy_summary(self):
        if not self.is_mounted:
            return
        try:
            source = self.form.get_source(focus_errors=False)
        except ValueError:
            return
        selection = vs._selection_text(source.extensions)
        self.query_one('#vs-policy-summary', Label).update(
            'No extensions selected; choose Top N, IDs or All.' if selection == 'none'
            else selection + ' extensions')

    def invalidate_checks(self):
        self._lookup_gen += 1
        if self.is_mounted:
            self.query_one('#vs-lookup-status', Label).update('')

    def on_unmount(self):
        self._lookup_gen += 1

    @on(ProviderSourceForm.Changed)
    def _input_changed(self):
        self.invalidate_checks()
        self._policy_summary()

    @on(Button.Pressed, '#btn-vs-lookup')
    def _lookup(self, event):
        event.stop()
        try:
            source = self.form.get_source()
        except ValueError as exc:
            self.query_one('#vs-lookup-status', Label).update(str(exc))
            return
        self._lookup_gen += 1
        gen = self._lookup_gen
        if not source.extensions.ids:
            self.query_one('#vs-lookup-status', Label).update('Enter extension IDs to look up.')
            return
        self.query_one('#vs-lookup-status', Label).update('Looking up IDs…')
        self._lookup_worker(gen, source)

    def _lookup_text(self, gen, text):
        if gen == self._lookup_gen and self.is_mounted:
            self.query_one('#vs-lookup-status', Label).update(text)

    @work(thread=True, exit_on_error=False, group='vs-lookup')
    def _lookup_worker(self, gen, source):
        try:
            rows = lookup_ids(source, progress=lambda text: self.app.call_from_thread(self._lookup_text, gen, text))
            text = '\n'.join(f'{ident}: {name if name is not None else "not found"}' for ident, name in rows)
        except Exception as exc:
            text = f'Lookup failed: {exc}'
        if gen == self._lookup_gen and self.is_mounted:
            self.app.call_from_thread(self._lookup_text, gen, text)


class StepVscode(ProviderStep):
    def __init__(self, existing_urls):
        self.existing_urls = existing_urls
        super().__init__(VscodeEditor(config.VscodeSource(provider='vscode')), heading='VS Code · Marketplace',
                         replaces=('This replaces the existing VS Code selection.'
                                   if config.MARKETPLACE_URL in existing_urls else None),
                         error_id='vs-form-error')


class EditVscodePage(ProviderEditPage):
    TITLE = 'Edit VS Code source'

    def __init__(self, source, *, existing_names, existing_urls, save_source=None):
        super().__init__(VscodeEditor(source, existing_names=existing_names), title=self.TITLE,
                         save_source=save_source)
