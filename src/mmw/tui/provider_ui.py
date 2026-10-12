"""What the TUI needs per provider that core must not know: which wizard step
adds one, and which page edits one. Core providers stay Textual-free; this is
the one place a provider name meets a widget."""
from collections.abc import Callable
from dataclasses import dataclass

from mmw.providers import all_providers


@dataclass(frozen=True)
class AddEntry:
    """One row of "What are you adding?"."""
    key: str
    label: str
    description: str
    step_title: str                                  # its wizard step's name
    make_step: Callable[[set[str]], object]          # existing identities → WizardStep
    offer_source_packages: bool                      # Confirm shows the deb-src box


def _apt_step(existing_urls: set[str]):
    from mmw.tui.custom_repo import StepCustom
    return StepCustom(existing_urls)


def _apt_edit(source, existing_names, existing_urls):
    from mmw.tui.source_screens import EditSourcePage
    return EditSourcePage(source, existing_names=existing_names, existing_urls=existing_urls)


# provider name → (Add entry or None, Edit page factory). apt's step adds
# flat repositories too (probing tells them apart), so apt-flat has no entry.
_ADD: dict[str, AddEntry] = {
    "apt": AddEntry("apt", "APT repository",
                    "Debian, Ubuntu or vendor apt repository — dists/ tree or flat",
                    "Repository", _apt_step, offer_source_packages=True),
}
_EDIT: dict[str, Callable] = {"apt": _apt_edit, "apt-flat": _apt_edit}

_DETECT = AddEntry("detect", "Detect from URL or paste…",
                   "A repository URL, a .list / .sources file, or pasted config",
                   "Repository", _apt_step, offer_source_packages=True)


def add_entries() -> list[AddEntry]:
    providers = all_providers()
    entries = [_ADD[p.name] for p in providers if p.name in _ADD]
    if any(callable(getattr(p, "probe", None)) for p in providers):
        entries.append(_DETECT)
    return entries


def edit_page(source, existing_names: set[str], existing_urls: set[str]):
    factory = _EDIT.get(source.provider)
    if factory is None:
        raise ValueError(f"no edit page for provider {source.provider!r}")
    return factory(source, existing_names, existing_urls)
