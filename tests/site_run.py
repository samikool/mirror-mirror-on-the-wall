"""Reading a far-side run page from a TUI test: its result, once it has one."""


def run_done(app) -> str:
    """The run page's result text — "" until the page is composed and done."""
    nodes = app.screen.query("#run-done")
    return str(nodes.first().render()) if nodes else ""
