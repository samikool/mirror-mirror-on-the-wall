from mmw.core import config
from mmw.core.publisher.nginx import ROOT_PLACEHOLDER, nginx_files


def _m(url: str, name: str) -> config.MirrorConfig:
    return config.MirrorConfig(source_url=url, name=name, provider="apt", label="x")


def _conf(*args, **kwargs) -> str:
    return nginx_files(*args, **kwargs)["nginx/mirror.conf"].decode()


def test_one_file_with_one_server_block_per_hostname():
    """A single file, never a `sites/` directory beside it: Debian's stock
    nginx.conf loads `sites-enabled/*`, which matches a subdirectory too, and
    nginx refuses to start on "is a directory". One file works wherever it is
    dropped or symlinked — conf.d/ or sites-enabled/."""
    files = nginx_files(
        [_m("https://ports.ubuntu.com/", "a"),
         _m("https://repo.nvidia.com/cuda/", "b")],
        root="/srv/lab",
    )
    assert set(files) == {"nginx/mirror.conf"}
    conf = files["nginx/mirror.conf"].decode()
    assert conf.count("server {") == 2
    assert "server_name ports.ubuntu.com;" in conf
    assert "server_name repo.nvidia.com;" in conf
    assert "sites/" not in conf
    # No live `include` directive: the only `include` is the commented
    # install instruction, so nothing depends on a second file existing.
    assert not [ln for ln in conf.splitlines()
                if ln.strip().startswith("include")]


def test_hostnames_derive_from_mirrors_never_hardcoded():
    """An arm64 network mirrors ports.ubuntu.com and neither archive nor security."""
    conf = _conf([_m("https://ports.ubuntu.com/", "a")], root="/srv/lab")
    assert "ports.ubuntu.com" in conf
    assert "archive.ubuntu.com" not in conf
    assert "security.ubuntu.com" not in conf


def test_two_repos_on_one_host_share_one_server_block():
    conf = _conf(
        [_m("https://deb.debian.org/debian/", "a"),
         _m("https://deb.debian.org/debian-security/", "b")],
        root="/srv/lab",
    )
    assert conf.count("server {") == 1


def test_root_is_substituted():
    conf = _conf([_m("https://ports.ubuntu.com/", "a")], root="/srv/lab")
    assert "root /srv/lab/ports.ubuntu.com;" in conf
    assert ROOT_PLACEHOLDER not in conf


def test_placeholder_root_is_left_for_apply_time():
    conf = _conf([_m("https://ports.ubuntu.com/", "a")], root=ROOT_PLACEHOLDER)
    assert f"root {ROOT_PLACEHOLDER}/ports.ubuntu.com;" in conf


def test_non_standard_port_host_uses_real_hostname_for_server_name():
    """mirror_base encodes a port as host_port to keep two repos on the same
    host at different ports from colliding on disk. That mangled form is
    right for the on-disk directory (and thus the `root` path) but must never
    be the `server_name` — nginx would never match it against any real
    request's Host header."""
    conf = _conf([_m("http://internal.example.com:8080/repo/", "a")], root="/srv/lab")
    assert "server_name internal.example.com;" in conf
    assert "root /srv/lab/internal.example.com_8080;" in conf


def test_install_instructions_name_this_exact_file():
    """The generated header points to the container setup and its mounts."""
    conf = _conf([_m("https://ports.ubuntu.com/", "a")], root="/srv/lab")
    assert "cp -r" not in conf
    assert "Used by nginx/compose.yaml" in conf
    assert "nginx/ directory at /etc/nginx/conf.d" in conf


def test_no_tls_means_plain_http_only():
    conf = _conf([_m("https://ports.ubuntu.com/", "a")], root="/srv/lab")
    assert "listen 80;" in conf
    assert "443" not in conf
    assert "ssl_certificate" not in conf


def test_tls_adds_https_to_every_server_block():
    conf = _conf(
        [_m("https://ports.ubuntu.com/", "a"),
         _m("https://repo.nvidia.com/cuda/", "b")],
        root="/srv/lab",
        tls=("/etc/mmw/mirror.crt", "/etc/mmw/mirror.key"),
    )
    assert conf.count("listen 443 ssl;") == 2
    assert conf.count("ssl_certificate /etc/mmw/mirror.crt;") == 2
    assert conf.count("ssl_certificate_key /etc/mmw/mirror.key;") == 2
    # Plain HTTP stays: a client whose sources say http:// must keep working.
    assert conf.count("listen 80;") == 2


def test_tls_paths_can_carry_the_root_placeholder():
    conf = _conf(
        [_m("https://ports.ubuntu.com/", "a")],
        root=ROOT_PLACEHOLDER,
        tls=(f"{ROOT_PLACEHOLDER}/.mmw/tls/mirror.crt",
             f"{ROOT_PLACEHOLDER}/.mmw/tls/mirror.key"),
    )
    assert f"ssl_certificate {ROOT_PLACEHOLDER}/.mmw/tls/mirror.crt;" in conf


def test_extra_hosts_get_a_server_block():
    """dist_upgrade publishes `changelogs.ubuntu.com/` beside the mirrors; it
    is not any mirror's host, so it has to be named explicitly — before this
    it was published and never served."""
    conf = _conf(
        [_m("https://ports.ubuntu.com/", "a")],
        root="/srv/lab",
        extra_hosts=["changelogs.ubuntu.com"],
    )
    assert "server_name changelogs.ubuntu.com;" in conf
    assert "root /srv/lab/changelogs.ubuntu.com;" in conf
    assert "#   changelogs.ubuntu.com" in conf
