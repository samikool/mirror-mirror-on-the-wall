"""`LiveHitTest` — what is under the mouse, never a widget already removed.

Textual answers from the last drawn frame, so a widget removed since — a toast
whose timeout just ran out — can still be "under" a click. Its text selection
then reads the removed widget's parent, which is None, and the app crashes
(Textual 8.2.8). Every mmw screen mixes this in first.
"""


class LiveHitTest:
    def get_widget_and_offset_at(self, x: int, y: int):
        widget, offset = super().get_widget_and_offset_at(x, y)
        if widget is not None and not widget.is_attached:
            return None, None
        return widget, offset
