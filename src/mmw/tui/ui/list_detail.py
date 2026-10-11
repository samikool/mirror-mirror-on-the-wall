"""Two panes: a list, and the detail of what is highlighted in it."""
from textual.containers import Horizontal, Vertical
from textual.widget import Widget


class ListDetail(Horizontal):
    def __init__(self, list_widget: Widget, detail_widget: Widget, *, id: str | None = None):
        super().__init__(Vertical(list_widget, classes="list-pane"),
                         Vertical(detail_widget, classes="detail-pane"), id=id)
