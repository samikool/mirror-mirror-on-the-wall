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


def test_non_standard_port_host_uses_real_hostname_for_server_name():
    """mirror_base encodes a port as host_port to keep two repos on the same
    host at different ports from colliding on disk. That mangled form is
    right for the on-disk directory (and thus the site filename and `root`
    path) but was also being used verbatim as `server_name` — which nginx
    would never match against any real request's Host header. Only
    `server_name` needed fixing; the directory/root naming is unchanged."""
    files = nginx_files([_m("http://internal.example.com:8080/repo/", "a")], root="/srv/lab")
    assert "nginx/sites/internal.example.com_8080.conf" in files
    conf = files["nginx/sites/internal.example.com_8080.conf"].decode()
    assert "server_name internal.example.com;" in conf
    assert "root /srv/lab/internal.example.com_8080;" in conf


def test_install_instructions_include_this_exact_file():
    """The header's install step must tell the admin to include mirror.conf
    itself by its real published path — not `cp -r` it somewhere else, which
    would leave the trailing `include .../sites/*.conf;` pointed at a root
    nothing ever copied there. Both includes below must resolve under the
    same `root`."""
    files = nginx_files([_m("https://ports.ubuntu.com/", "a")], root="/srv/lab")
    conf = files["nginx/mirror.conf"].decode()
    assert "cp -r" not in conf
    assert "include /srv/lab/nginx/mirror.conf;" in conf
    assert "include /srv/lab/nginx/sites/*.conf;" in conf
