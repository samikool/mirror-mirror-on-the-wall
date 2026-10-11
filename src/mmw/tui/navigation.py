"""The app's sections, in sidebar order. Each is a Textual mode of `MmwApp`."""
from typing import NamedTuple


class Section(NamedTuple):
    mode: str
    label: str
    key: str   # app-level shortcut; Inputs consume digits first, so typing is safe


SECTIONS = (
    Section("repositories", "Repositories", "1"),
    Section("networks", "Networks", "2"),
    Section("sync", "Sync", "3"),
    Section("publish", "Publish", "4"),
    Section("settings", "Settings", "5"),
)
SECTION_MODES = tuple(s.mode for s in SECTIONS)
