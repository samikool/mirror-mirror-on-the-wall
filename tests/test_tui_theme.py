"""Theme definitions, mmw colour roles, and user theme files."""
import pytest
from textual.color import Color
from textual.theme import BUILTIN_THEMES

from mmw.tui import theme as T


@pytest.mark.parametrize("t", [*BUILTIN_THEMES.values(), *T.MMW_THEMES], ids=lambda t: t.name)
def test_every_theme_yields_every_role_as_a_colour(t):
    roles = T.role_defaults(t)
    assert set(roles) == set(T.ROLE_NAMES)
    for name, value in roles.items():
        Color.parse(value)  # raises on anything that is not a concrete colour


def test_mmw_themes_are_named_and_paired():
    assert [t.name for t in T.MMW_THEMES] == ["mmw", "mmw-light"]
    assert T.MMW_THEMES[0].dark and not T.MMW_THEMES[1].dark


def test_theme_order_puts_mmw_first():
    assert T.ordered_theme_names(["nord", "mmw-light", "dracula", "mmw"]) == \
        ["mmw", "mmw-light", "dracula", "nord"]


def test_missing_theme_dir_is_not_an_error(tmp_path):
    assert T.load_custom_themes(tmp_path / "themes") == ([], [])


def _write(d, name, text):
    d.mkdir(exist_ok=True)
    (d / name).write_text(text)


def test_valid_theme_file_loads_with_variables(tmp_path):
    d = tmp_path / "themes"
    _write(d, "night.toml", 'primary = "#336699"\ndark = true\n'
                            '[variables]\nmmw-ok = "#00ff00"\n')
    themes, problems = T.load_custom_themes(d)
    assert problems == []
    assert [t.name for t in themes] == ["night"]          # name defaults to file stem
    assert themes[0].variables == {"mmw-ok": "#00ff00"}


def test_explicit_name_wins_over_stem(tmp_path):
    d = tmp_path / "themes"
    _write(d, "a.toml", 'name = "ocean"\nprimary = "#336699"\n')
    assert [t.name for t in T.load_custom_themes(d)[0]] == ["ocean"]


@pytest.mark.parametrize("text,needle", [
    ('primary = "#336699', "a.toml"),                      # malformed TOML
    ('primary = "#336699"\nbogus = 1\n', "bogus"),          # unknown key
    ('primary = "notacolour"\n', "notacolour"),             # bad colour
    ('primary = "#336699"\n[variables]\nmmw-ok = "$success"\n', "mmw-ok"),  # vars must be concrete
    ('dark = true\n', "primary"),                           # primary is required
])
def test_bad_theme_files_are_skipped_with_a_reason(tmp_path, text, needle):
    d = tmp_path / "themes"
    _write(d, "a.toml", text)
    _write(d, "good.toml", 'primary = "#336699"\n')
    themes, problems = T.load_custom_themes(d)
    assert [t.name for t in themes] == ["good"]
    assert len(problems) == 1 and problems[0].startswith("a.toml: ")
    assert needle in problems[0]


def test_the_documented_example_theme_loads(tmp_path):
    import re
    from pathlib import Path
    doc = (Path(__file__).resolve().parent.parent / "docs" / "themes.md").read_text()
    example = re.search(r"```toml\n(.*?)```", doc, re.S).group(1)
    d = tmp_path / "themes"
    d.mkdir()
    (d / "harbour.toml").write_text(example)
    themes, problems = T.load_custom_themes(d)
    assert problems == []
    assert themes[0].name == "harbour"
    assert set(themes[0].variables) == set(T.ROLE_NAMES)
