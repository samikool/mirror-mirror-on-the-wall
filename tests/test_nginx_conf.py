from mmw.core import config
from mmw.core.publisher.nginx import ROOT_PLACEHOLDER, nginx_files


def _m(url: str, name: str) -> config.MirrorConfig:
    return config.MirrorConfig(source_url=url, name=name, provider="apt", label="x")


def test_one_site_file_per_hostname():
    files = nginx_files(
        [_m("https://ports.ubuntu.com/", "a"),
         _m("https://repo.nvidia.com/cuda/", "b")],
        root="/srv/lab",
    )
    assert set(files) == {
        "nginx/mirror.conf",
        "nginx/sites/ports.ubuntu.com.conf",
        "nginx/sites/repo.nvidia.com.conf",
    }


def test_hostnames_derive_from_mirrors_never_hardcoded():
    """An arm64 network mirrors ports.ubuntu.com and neither archive nor security."""
    files = nginx_files([_m("https://ports.ubuntu.com/", "a")], root="/srv/lab")
    blob = b"".join(files.values()).decode()
    assert "ports.ubuntu.com" in blob
    assert "archive.ubuntu.com" not in blob
    assert "security.ubuntu.com" not in blob


def test_two_repos_on_one_host_share_one_site_file():
    files = nginx_files(
        [_m("https://deb.debian.org/debian/", "a"),
         _m("https://deb.debian.org/debian-security/", "b")],
        root="/srv/lab",
    )
    assert list(files) == ["nginx/mirror.conf", "nginx/sites/deb.debian.org.conf"]


def test_root_is_substituted():
    files = nginx_files([_m("https://ports.ubuntu.com/", "a")], root="/srv/lab")
    conf = files["nginx/sites/ports.ubuntu.com.conf"].decode()
    assert "root /srv/lab/ports.ubuntu.com;" in conf
    assert ROOT_PLACEHOLDER not in conf


def test_placeholder_root_is_left_for_apply_time():
    files = nginx_files([_m("https://ports.ubuntu.com/", "a")], root=ROOT_PLACEHOLDER)
    conf = files["nginx/sites/ports.ubuntu.com.conf"].decode()
    assert f"root {ROOT_PLACEHOLDER}/ports.ubuntu.com;" in conf


def test_non_standard_port_host_is_usable_as_a_server_name():
    """mirror_base encodes a port as host_port; a server_name may not contain '_'
    followed by digits as a port, so the underscore form is used verbatim and
    the listen port is left to the admin."""
    files = nginx_files([_m("http://internal.example.com:8080/repo/", "a")], root="/srv/lab")
    assert "nginx/sites/internal.example.com_8080.conf" in files
