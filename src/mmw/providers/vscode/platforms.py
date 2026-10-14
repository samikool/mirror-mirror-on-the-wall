"""VS Code's platform vocabulary.

Target platforms are what extensions are built for (`targetPlatform` on a
gallery version) and what a source narrows by. Desktop and server builds are
named differently by the update API; `builds()` maps one to the other. The
identities follow vscodeoffline's platform × architecture × buildtype grid
(vsc.py, 9da2010); one the API does not serve answers 204/404 and is
reported, not fatal.
"""
KINDS = ("extensions", "desktop", "server")
QUALITIES = ("stable", "insider")
UNIVERSAL = "universal"

TARGET_PLATFORMS = (
    "linux-x64", "linux-arm64", "linux-armhf", "alpine-x64", "alpine-arm64",
    "win32-x64", "win32-arm64", "darwin-x64", "darwin-arm64", "web",
)

_BUILDS: dict[str, dict[str, tuple[str, ...]]] = {
    "linux-x64": {"desktop": ("linux-x64", "linux-deb-x64", "linux-rpm-x64"),
                  "server": ("server-linux-x64", "cli-alpine-x64")},
    "linux-arm64": {"desktop": ("linux-arm64", "linux-deb-arm64", "linux-rpm-arm64"),
                    "server": ("server-linux-arm64", "cli-alpine-arm64")},
    "linux-armhf": {"desktop": ("linux-armhf", "linux-deb-armhf", "linux-rpm-armhf"),
                    "server": ("server-linux-armhf",)},
    "alpine-x64": {"server": ("server-linux-alpine", "cli-alpine-x64")},
    "alpine-arm64": {"server": ("cli-alpine-arm64",)},
    "win32-x64": {"desktop": ("win32-x64", "win32-x64-user", "win32-x64-archive")},
    "win32-arm64": {"desktop": ("win32-arm64", "win32-arm64-user", "win32-arm64-archive")},
    "darwin-x64": {"desktop": ("darwin",), "server": ("server-darwin",)},
    "darwin-arm64": {"desktop": ("darwin-arm64",), "server": ("server-darwin-arm64",)},
    "web": {},
}


def builds(kind: str, platform: str) -> tuple[str, ...]:
    """Update-API identities a (kind, platform) unit downloads."""
    return _BUILDS.get(platform, {}).get(kind, ())
