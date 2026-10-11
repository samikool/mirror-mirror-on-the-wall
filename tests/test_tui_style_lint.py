"""Rebuilt TUI code takes its look only from mmw.tcss and its colours only from
the theme — the two rules whose absence produced 36 private stylesheets and 91
hard-coded colours in the old TUI.

Scope is every module under src/mmw/tui/ except theme.py, which is where the
colours are defined.
"""
import re
from pathlib import Path

import pytest

TUI = Path(__file__).resolve().parent.parent / "src" / "mmw" / "tui"
REBUILT = sorted(p for p in TUI.rglob("*.py")
                 if "__pycache__" not in p.parts and p != TUI / "theme.py")
STYLESHEET = TUI / "mmw.tcss"

_NAMED = (r"(?:bright_|dark_)?(?:red|green|blue|yellow|cyan|magenta|white|black"
          r"|gr[ae]y\d*|orange\d*|purple)")
_HEX = r"#[0-9a-fA-F]{3,8}\b"
_FUNC = r"\b(?:rgba?|hsla?)\("


def css_violations(text: str) -> list[str]:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    out = []
    for value in re.findall(r":\s*([^;{}]*);", text):
        if re.search(_HEX, value) or re.search(_FUNC, value) or re.search(rf"\b{_NAMED}\b", value):
            out.append(value.strip())
    return out


def py_violations(text: str) -> list[str]:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if re.search(r"\bDEFAULT_CSS\s*=|\bCSS\s*=|\.styles\.\w+\s*=", line):
            out.append(f"{n}: inline style: {line.strip()}")
        for tag in re.findall(r"\[([^\[\]]*)\]", line):
            if re.search(rf"\b{_NAMED}\b", tag) or re.search(_HEX, tag) or re.search(_FUNC, tag):
                out.append(f"{n}: colour in markup [{tag}]")
            if "$text-muted" in tag:
                # "auto 60%": resolved in stylesheets, silently ignored in markup.
                out.append(f"{n}: [$text-muted] does nothing in markup — use [dim]")
        if re.search(r"""["']#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?["']""", line):
            out.append(f"{n}: colour literal: {line.strip()}")
    return out


# The checkers themselves, so a regex that silently matches nothing cannot pass.
@pytest.mark.parametrize("bad", [
    "X { color: red; }", "X { background: #112233; }", "X { color: rgb(1,2,3); }",
    "X { color: bright_cyan; }", "X { border: solid grey50; }",
])
def test_css_checker_catches(bad):
    assert css_violations(bad)


@pytest.mark.parametrize("ok", [
    "X:hover { background: $boost; }", "#add { color: $text-muted; }",
    "/* red */ X { color: $mmw-error; }", "X { background: $primary 25%; }",
])
def test_css_checker_allows(ok):
    assert css_violations(ok) == []


@pytest.mark.parametrize("bad", [
    'DEFAULT_CSS = """x"""', 'w.styles.color = "x"', 'Label("[green]ok[/]")',
    'Label("[bold #ff0000]x[/]")', 'COLOUR = "#336699"', '    CSS = "X {}"',
    'Label("[$text-muted]x[/]")',
])
def test_py_checker_catches(bad):
    assert py_violations(bad)


@pytest.mark.parametrize("ok", [
    'Label("[$mmw-ok]ok[/]")', 'query_one("#add")', "x: list[str] = []",
    'CSS_PATH = str(STYLESHEET)', 'Label("[bold]x[/]")',
])
def test_py_checker_allows(ok):
    assert py_violations(ok) == []


def test_stylesheet_uses_theme_colours_only():
    assert css_violations(STYLESHEET.read_text()) == []


@pytest.mark.parametrize("path", REBUILT, ids=lambda p: str(p.relative_to(TUI)))
def test_rebuilt_code_has_no_inline_styles_or_colours(path):
    assert py_violations(path.read_text()) == []
