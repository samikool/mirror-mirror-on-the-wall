import pytest
from pydantic import ValidationError

from mmw.core import config


def test_edit_replaces_policy_without_losing_untouched_fields():
    from mmw.tui.provider_form import ProviderFormDraft
    original = config.VscodeSource(
        provider="vscode", name="editor", label="Tools",
        kinds=["extensions", "server"], platforms=["linux-x64"], qualities=["insider"],
        extensions=config.ExtensionSelection(top_n=200, ids=["golang.go"], prerelease=True))
    draft = ProviderFormDraft(original)
    draft.set_value("kinds", ["server"])
    draft.set_value("extensions.top_n", None)
    edited = draft.to_source()
    assert edited.kinds == ["server"]
    assert edited.extensions.top_n is None
    assert edited.extensions.ids == ["golang.go"]
    assert edited.extensions.prerelease is True
    assert (edited.name, edited.label, edited.qualities) == ("editor", "Tools", ["insider"])
    assert original.kinds == ["extensions", "server"]


@pytest.mark.parametrize("text,want", [("", None), ("  ", None), ("1", 1), ("200", 200)])
def test_top_n_accepts_optional_positive_integer(text, want):
    from mmw.tui.provider_form import parse_top_n
    assert parse_top_n(text) == want


@pytest.mark.parametrize("text", ["0", "-1", "²", "１２", "garbage", "1.5"])
def test_top_n_refuses_invalid_input(text):
    from mmw.tui.provider_form import parse_top_n
    with pytest.raises(ValueError):
        parse_top_n(text)


def test_ids_normalize_comments_and_duplicates_without_materializing_dimensions():
    from mmw.tui.provider_form import ProviderFormDraft
    draft = ProviderFormDraft(config.VscodeSource(provider="vscode"))
    draft.set_value("extensions.ids", "MS-PYTHON.PYTHON # Python\n\ngolang.go\nms-python.python")
    source = draft.to_source()
    assert source.extensions.ids == ["ms-python.python", "golang.go"]
    assert source.kinds == source.platforms == source.qualities == []


@pytest.mark.parametrize("path,value", [("extensions.ids", "bad/id"), ("platforms", ["unknown"])])
def test_form_uses_config_validation(path, value):
    from mmw.tui.provider_form import ProviderFormDraft
    draft = ProviderFormDraft(config.VscodeSource(provider="vscode"))
    draft.set_value(path, value)
    with pytest.raises(ValidationError):
        draft.to_source()


def test_provider_exposes_marketplace_group_and_search_policy():
    from mmw.providers import get_provider
    provider = get_provider("vscode")
    src = config.VscodeSource(provider="vscode", kinds=["server"], extensions={"ids": ["golang.go"]})
    assert provider.group(src) == "VS Code · Marketplace"
    assert "golang.go" in provider.search_text(src)
    assert "server" in provider.source_summary(src)
    assert {f.path for f in provider.fields} >= {"kinds", "platforms", "qualities", "extensions.ids"}
