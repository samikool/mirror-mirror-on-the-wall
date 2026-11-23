"""Name a point (`mmw history label`, `mmw site label`): one page for both sides."""
from textual import on
from textual.app import ComposeResult
from textual.widgets import Static

from mmw.tui.ui.form import Form, TextField
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button


class LabelPage(Page):
    """Dismisses with the name to give, '' to clear it, or None (cancelled)."""
    TITLE = 'Label'
    HELP = ('A name for this point, e.g. golden-q4: letters, digits and . _ + -, never a date. '
            'A named point is kept whatever the retention, and serving or comparing can name it. '
            'A suite\'s name is on one point: giving it here takes it from the point that had it. '
            'Names given on the connected side travel to the far side with the next bundle.')

    def __init__(self, what: str, current: str | None = None):
        super().__init__()
        self._what = what
        self._current = current

    def compose_body(self) -> ComposeResult:
        yield Static(self._what, id='label-what', markup=False)
        yield Form(TextField('Name', self._current or '', id='label-name',
                             hint='e.g. golden-q4 — never a date, a version or a digest.'),
                   id='label-form')

    def compose_actions(self) -> ComposeResult:
        yield Button('Cancel', id='btn-label-cancel', compact=True)
        yield Button('Clear', id='btn-label-clear', compact=True, disabled=not self._current)
        yield Button('Label', id='btn-label-save', variant='primary', compact=True)

    @on(Button.Pressed, '#btn-label-cancel')
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, '#btn-label-clear')
    def _clear(self) -> None:
        self.dismiss('')

    @on(Button.Pressed, '#btn-label-save')
    def _save(self) -> None:
        from mmw.site.versionref import check_label
        try:
            name = check_label(str(self.query_one(Form).values()['label-name'] or ''))
        except ValueError as exc:
            self.set_status(str(exc), 'error')
            return
        self.dismiss(name)
