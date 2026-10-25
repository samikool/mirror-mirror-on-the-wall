"""`p` and `b` on Networks open a picker page before anything starts; these
take the defaults (live into the root / from the last bundle to now)."""
from waiting import until


async def confirm_picker(app, pilot, page_type: str) -> None:
    """Wait for the picker, then press its go button."""
    await until(pilot, lambda: type(app.screen).__name__ == page_type)
    await until(pilot, lambda: app.screen.query("#btn-pick-go"))
    app.screen.query_one("#btn-pick-go").press()


async def press_publish(app, pilot) -> None:
    await pilot.press("p")
    await confirm_picker(app, pilot, "PublishPickPage")


async def press_bundle(app, pilot) -> None:
    await pilot.press("t")
    await confirm_picker(app, pilot, "BundlePickPage")


async def choose_add_entry(app, pilot, key: str = "apt") -> None:
    """Answer "What are you adding?" with `key` and move to its step."""
    await until(pilot, lambda: type(app.screen).__name__ == "AddSourcePage"
                and app.screen.query("#add-entry-list"))
    page = app.screen
    page.query_one("#add-entry-list").highlighted = [e.key for e in page.entries].index(key)
    page.query_one("#btn-wizard-next").press()
    await until(pilot, lambda: page.step == 1)
