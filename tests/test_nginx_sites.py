"""Provider-declared nginx server blocks and certificate names."""
from mmw.core import config
from mmw.core.publisher.nginx import nginx_files
from mmw.providers.base import Site


def _conf(**kw) -> str:
    return nginx_files([], "/srv/lab", **kw)["nginx/mirror.conf"].decode()


def test_a_static_regex_site():
    text = _conf(sites=[Site(r"~^[^.]+\.gallerycdn\.vsassets\.io$", root="$host")])
    assert r"server_name ~^[^.]+\.gallerycdn\.vsassets\.io$;" in text
    assert "root /srv/lab/$host;" in text and "try_files $uri $uri/ =404;" in text


def test_a_proxied_site():
    text = _conf(sites=[Site("update.code.visualstudio.com", proxy=("/",))], port=9000)
    block = text.split("server_name update.code.visualstudio.com;")[1].split("}\n}")[0]
    assert "location / {" in block and "proxy_pass http://127.0.0.1:9000;" in block
    assert "proxy_set_header Host $host;" in block


def test_an_exact_proxied_path_on_an_otherwise_empty_host():
    text = _conf(sites=[Site("marketplace.visualstudio.com",
                             proxy=("/_apis/public/gallery/extensionquery",), exact=True)])
    assert "location = /_apis/public/gallery/extensionquery {" in text


def test_proxy_prefixes_stay_prefixes_beside_exact_paths():
    text = _conf(sites=[Site("marketplace.visualstudio.com", proxy=("/",), exact=True,
                             proxy_prefixes=("/_apis/public/gallery/vscode/",))])
    assert "location = / {" in text
    assert "location /_apis/public/gallery/vscode/ {" in text


def test_sites_get_the_tls_block_too():
    text = _conf(sites=[Site("a.example", root="a.example")], tls=("/c.pem", "/k.pem"))
    assert text.count("listen 443 ssl;") == 1 and "ssl_certificate /c.pem;" in text


def test_wildcard_sans_in_the_csr(tmp_path):
    from cryptography import x509
    from mmw.core.certs import generate_key_and_csr
    _, _, csr_path = generate_key_and_csr(["*.gallerycdn.vsassets.io", "a.example"], tmp_path)
    csr = x509.load_pem_x509_csr(csr_path.read_bytes())
    names = csr.extensions.get_extension_for_class(
        x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    assert "*.gallerycdn.vsassets.io" in names


def test_cert_names_default_to_extra_hosts():
    from mmw.providers import get_provider
    net = config.NetworkConfig(name="lab", dist_upgrade=True)
    assert get_provider("apt").cert_names(net, []) == ["changelogs.ubuntu.com"]


def test_the_dns_list_in_the_header_names_wildcards_not_regexes():
    conf = nginx_files([], "/srv/www", sites=[
        Site(r"~^[^.]+\.gallerycdn\.vsassets\.io$", root="$host"),
        Site("update.code.visualstudio.com", proxy=("/",))])["nginx/mirror.conf"].decode()
    header = conf.split("server {")[0]
    assert "#   *.gallerycdn.vsassets.io\n" in header
    assert "#   update.code.visualstudio.com\n" in header
    assert "~^" not in header


def test_a_cors_site_answers_the_preflight_and_marks_every_answer():
    # VS Code's workbench fetches extension files cross-origin and preflights
    # them; a static block's 405 fails the install.
    text = _conf(sites=[Site(r"~^[^.]+\.gallerycdn\.vsassets\.io$", root="$host", cors=True),
                        Site("a.example", root="a.example")])
    cdn = text.split(r"server_name ~^[^.]+\.gallerycdn\.vsassets\.io$;")[1].split("\n}\n")[0]
    assert "if ($request_method = OPTIONS) {" in cdn and "return 204;" in cdn
    assert cdn.count('add_header Access-Control-Allow-Origin "*" always;') == 2
    plain = text.split("server_name a.example;")[1].split("\n}\n")[0]
    assert "Access-Control" not in plain
