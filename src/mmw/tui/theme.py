"""Colour themes for the TUI.

Every colour the TUI draws comes from the active theme: Textual's base roles
(`$primary`, `$surface`, `$text-muted`, …) plus mmw's own roles below. Nothing
else in `tui/` may name a colour — `tests/test_tui_style_lint.py` enforces it —
so any theme, built-in or a user's `themes/*.toml`, recolours everything.

mmw roles are *derived* from the theme's base colours rather than given fixed
defaults, because Textual theme-variable defaults must be concrete colours: a
default of `"$success"` is a CSS parse error. A theme may still override any
role through its `variables`.

In content markup use `[dim]` for muted text, not `[$text-muted]`: that
variable is "auto 60%", which stylesheets resolve and markup silently ignores.
"""
from __future__ import annotations

import tomllib
from collections.abc import Iterable
from pathlib import Path

from textual.color import Color
from textual.theme import Theme

from mmw.core.config import DEFAULT_THEME

THEMES_DIRNAME = "themes"

ROLE_NAMES = (
    "mmw-ok",        # finished cleanly
    "mmw-warn",      # finished with something to look at
    "mmw-error",     # failed
    "mmw-running",   # in progress
    "mmw-idle",      # not started / nothing to do
    "mmw-selected",  # ticked rows
    "mmw-path",      # paths and URIs
)

MMW_THEMES = (
    Theme(
        name=DEFAULT_THEME, dark=True,
        primary="#5FB3B3", secondary="#8A9BD8", accent="#E0A458",
        success="#7FBF7F", warning="#E5C07B", error="#E06C75",
        foreground="#D7DAE0", background="#14161B", surface="#1B1E25", panel="#232733",
    ),
    Theme(
        name="mmw-light", dark=False,
        primary="#2F7F86", secondary="#4A5EA8", accent="#B8732A",
        success="#3F8F4F", warning="#9A6F0E", error="#C2414B",
        foreground="#24262B", background="#F6F5F2", surface="#FFFFFF", panel="#E9E7E2",
    ),
)


def role_defaults(theme: Theme) -> dict[str, str]:
    """mmw's colour roles for `theme`, as concrete hex colours."""
    c = theme.to_color_system().generate()
    fg, bg = Color.parse(c["foreground"]), Color.parse(c["background"])
    return {
        "mmw-ok": Color.parse(c["success"]).hex,
        "mmw-warn": Color.parse(c["warning"]).hex,
        "mmw-error": Color.parse(c["error"]).hex,
        "mmw-running": Color.parse(c["primary"]).hex,
        "mmw-idle": bg.blend(fg, 0.55).hex,
        "mmw-selected": Color.parse(c["accent"]).hex,
        "mmw-path": Color.parse(c["secondary"]).hex,
    }


def ordered_theme_names(names: Iterable[str]) -> list[str]:
    """mmw's own themes first, then everything else alphabetically."""
    names = set(names)
    own = [t.name for t in MMW_THEMES if t.name in names]
    return own + sorted(names - set(own))


def load_custom_themes(directory: Path) -> tuple[list[Theme], list[str]]:
    """Read `directory/*.toml` into themes.

    Each file holds `Theme`'s fields (`primary` required; `name` defaults to the
    file's stem) and an optional `[variables]` table, typically `mmw-*` roles.
    A file that cannot become a usable theme is skipped and reported — one bad
    file must never stop the TUI opening. Colours are resolved here, not at
    first render, so a typo is reported against its file instead of surfacing
    as a stylesheet error.
    """
    themes: list[Theme] = []
    problems: list[str] = []
    if not directory.is_dir():
        return themes, problems
    for path in sorted(directory.glob("*.toml")):
        try:
            data = tomllib.loads(path.read_text())
            variables = {str(k): str(v) for k, v in data.pop("variables", {}).items()}
            data.setdefault("name", path.stem)
            theme = Theme(**data, variables=variables)
            theme.to_color_system().generate()
            for key, value in variables.items():
                try:
                    Color.parse(value)
                except Exception as e:
                    raise ValueError(f"variable {key}: {e}") from e
        except Exception as e:  # noqa: BLE001 — every failure is reported, none is fatal
            problems.append(f"{path.name}: {e}")
            continue
        themes.append(theme)
    return themes, problems
