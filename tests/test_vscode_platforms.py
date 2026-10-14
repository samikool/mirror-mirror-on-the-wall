"""VS Code target platforms → update-API build identities."""
from mmw.providers.vscode.platforms import KINDS, QUALITIES, TARGET_PLATFORMS, builds


def test_vocabulary():
    assert KINDS == ("extensions", "desktop", "server")
    assert QUALITIES == ("stable", "insider")
    assert "linux-x64" in TARGET_PLATFORMS and "darwin-arm64" in TARGET_PLATFORMS
    assert "universal" not in TARGET_PLATFORMS


def test_linux_x64_builds():
    assert builds("desktop", "linux-x64") == ("linux-x64", "linux-deb-x64", "linux-rpm-x64")
    assert builds("server", "linux-x64") == ("server-linux-x64", "cli-alpine-x64")
    assert builds("extensions", "linux-x64") == ()


def test_platforms_without_builds():
    assert builds("desktop", "alpine-x64") == ()
    assert builds("desktop", "web") == () and builds("server", "web") == ()
