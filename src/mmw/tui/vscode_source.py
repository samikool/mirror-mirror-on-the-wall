"""VS Code authoring: shared editor, worker lookups and explicit confirmation."""
from dataclasses import dataclass

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Checkbox, Label

from mmw.core import config
from mmw.providers.vscode import marketplace as mp
from mmw.providers.vscode import provider as vs
from mmw.tui.provider_form import ProviderSourceForm
from mmw.tui.ui.form import Heading
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button
from mmw.tui.wizard_steps import WizardStep


@dataclass
class SourceCheck:
    mirrors: list
    notes: list[str]
    errors: list[str]
    extension_count: int


def lookup_ids(source, *, progress=None):
    with vs._client() as client:
        found = mp.by_names(client, source.extensions.ids, on_wait=progress)
    return [(ident, found[ident].get('displayName', ident) if ident in found else None)
            for ident in source.extensions.ids]


def check_source(source, *, progress=None):
    provider = vs.VscodeProvider()  # Preview must not mutate an active sync's catalog.
    result = provider.resolve(source, progress=progress)
    return SourceCheck(result.mirrors, result.notes, result.errors,
                       len(provider.catalog.get(source.identity, {})))


class VscodeEditor(Vertical):
    def __init__(self, source, *, existing_names=None):
        super().__init__()
        self.form = ProviderSourceForm(source, existing_names=existing_names)
        self._lookup_gen = 0

    def compose(self) -> ComposeResult:
        yield self.form
        yield Label('No extensions selected; choose Top N, IDs or All.', id='vs-policy-summary', markup=False)
        yield Label('All extensions can require substantial disk space and time.', id='vs-all-warning', markup=False)
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
        self.query_one('#vs-all-warning').display = source.extensions.all

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


class VscodeCheckView(Vertical):
    def __init__(self, source):
        super().__init__()
        self.source = source.model_copy(deep=True)
        self._check_gen = 0
        self.finished = False
        self.report = None

    def compose(self):
        yield Heading('Check selection')
        yield Label('All extensions can require substantial disk space and time.',
                    id='vs-check-warning', markup=False)
        yield Checkbox('I understand the size and time required', id='vs-all-ack', compact=True)
        yield Button('Check selection', id='btn-vs-check', compact=True)
        yield Label('', id='vs-check-status', markup=False)
        yield Checkbox('Save without checking (resolution has not succeeded)',
                       id='vs-save-unchecked', compact=True)

    def on_mount(self):
        all_ = self.source.extensions.all
        self.query_one('#vs-check-warning').display = all_
        self.query_one('#vs-all-ack').display = all_
        self.query_one('#vs-save-unchecked').display = False
        if not all_:
            self.call_after_refresh(self.start_check)

    def invalidate_checks(self):
        self._check_gen += 1
        self.finished = False
        self.report = None

    def on_unmount(self):
        self.invalidate_checks()

    def can_finish(self):
        acknowledged = not self.source.extensions.all or self.query_one('#vs-all-ack', Checkbox).value
        return acknowledged and self.finished and (not self.report.errors
            or self.query_one('#vs-save-unchecked', Checkbox).value)

    @on(Button.Pressed, '#btn-vs-check')
    def _check_pressed(self, event):
        event.stop()
        self.start_check()

    def start_check(self):
        if self.source.extensions.all and not self.query_one('#vs-all-ack', Checkbox).value:
            self.query_one('#vs-check-status', Label).update('Acknowledge the size warning before checking.')
            return
        self.invalidate_checks()
        self.query_one('#vs-save-unchecked', Checkbox).value = False
        self.query_one('#vs-save-unchecked').display = False
        self.query_one('#btn-vs-check', Button).disabled = True
        self.query_one('#vs-check-status', Label).update('Resolving selection…')
        self._check_worker(self._check_gen, self.source)

    def _progress(self, gen, text):
        if gen == self._check_gen and self.is_mounted:
            self.query_one('#vs-check-status', Label).update(text)

    def _done(self, gen, report):
        if gen != self._check_gen or not self.is_mounted:
            return
        self.report = report
        self.finished = True
        lines = [f'{report.extension_count} extensions · {len(report.mirrors)} sync units · sizes unknown until sync',
                 *report.notes, *report.errors]
        self.query_one('#vs-check-status', Label).update('\n'.join(lines))
        self.query_one('#vs-save-unchecked').display = bool(report.errors)
        self.query_one('#btn-vs-check', Button).disabled = False

    @work(thread=True, exit_on_error=False, group='vs-check')
    def _check_worker(self, gen, source):
        try:
            report = check_source(source, progress=lambda text: self.app.call_from_thread(self._progress, gen, text))
        except Exception as exc:
            report = SourceCheck([], [], [f'Check failed: {exc}'], 0)
        if gen == self._check_gen and self.is_mounted:
            self.app.call_from_thread(self._done, gen, report)


class StepVscode(WizardStep):
    def __init__(self, existing_urls):
        super().__init__()
        self.existing_urls = existing_urls
        self.editor = VscodeEditor(config.VscodeSource(provider='vscode'))

    def compose(self):
        yield Heading('VS Code · Marketplace')
        if config.MARKETPLACE_URL in self.existing_urls:
            yield Label('This replaces the existing VS Code selection.', markup=False)
        yield self.editor
        yield Label('', id='vs-form-error', markup=False)

    def focus_first(self):
        self.editor.form.focus_first()

    def get_sources(self):
        try:
            return [self.editor.form.get_source()]
        except ValueError as exc:
            self.query_one('#vs-form-error', Label).update(str(exc))
            return []


class VscodeConfirmStep(WizardStep):
    def __init__(self, sources):
        super().__init__()
        self.check = VscodeCheckView(sources[0])

    def compose(self):
        yield self.check

    def can_finish(self):
        return self.check.can_finish()

    def invalidate_checks(self):
        self.check.invalidate_checks()

    def get_include_sources(self):
        return False


class EditVscodePage(Page):
    TITLE = 'Edit VS Code source'

    def __init__(self, source, *, existing_names, existing_urls, save_source=None):
        super().__init__()
        self.save_source = save_source
        self.editor = VscodeEditor(source, existing_names=existing_names)
        self.check = None
        self._checked_source = None

    def compose_body(self):
        yield self.editor
        yield Vertical(id='vs-edit-check-slot')

    def compose_actions(self):
        yield Button('Cancel', id='btn-cancel', compact=True)
        yield Button('Save ✓', id='btn-save', variant='primary', compact=True)

    @on(Button.Pressed, '#btn-cancel')
    def _cancel(self):
        self.dismiss(None)

    @on(ProviderSourceForm.Changed)
    def _changed(self):
        if self.check is not None:
            self.check.invalidate_checks()
            self.check.display = False
            self._checked_source = None

    @on(Button.Pressed, '#btn-save')
    async def _save(self):
        try:
            source = self.editor.form.get_source()
        except ValueError as exc:
            self.set_status(str(exc), 'error')
            return
        if self._checked_source != source.model_dump():
            slot = self.query_one('#vs-edit-check-slot')
            await slot.remove_children()
            self.check = VscodeCheckView(source)
            self._checked_source = source.model_dump()
            await slot.mount(self.check)
            self.check.scroll_visible()
            self.set_status('Check the selection, then Save.')
            return
        if not self.check.can_finish():
            self.set_status('Wait for the check, or explicitly save without checking after a failure.', 'error')
            return
        if self.save_source is not None:
            try:
                await self.save_source(source)
            except Exception as exc:
                self.set_status(f'Not saved: {exc}', 'error')
                return
        self.dismiss(source)
