"""`mmw site …` — mmw on the far side of the airgap."""
from pathlib import Path

from mmw.commands.common import eprint
from mmw.site import home as site_home


def init(root: Path, home: Path | None = None) -> int:
    home = Path(home) if home is not None else site_home.default_home(root)
    try:
        settings = site_home.init_site(home, root)
    except FileExistsError as e:
        eprint(str(e))
        return 1
    print(f"Site ready: serving {settings.root}, state in {home}.")
    print(f"export MMW_SITE_HOME={home}")
    return 0
