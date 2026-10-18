"""Run inside the isolated PyPI client: TLS, routing and PEP 691 negotiation
against the mirror at PyPI's own hostnames. Writes /state/probe.json."""
import json
import socket
import ssl
import urllib.error
import urllib.request

JSON = "application/vnd.pypi.simple.v1+json"
result = {}

try:
    socket.create_connection(("1.1.1.1", 443), timeout=1).close()
except OSError:
    result["external_route"] = False
else:
    raise AssertionError("client network unexpectedly has an external route")


def get(url, accept=None):
    request = urllib.request.Request(url, headers={"Accept": accept} if accept else {})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.headers.get("Content-Type", ""), response.read()


ctype, body = get("https://pypi.org/simple/", JSON)
assert ctype.startswith(JSON), ctype
assert {"name": "mmw-a"} in json.loads(body)["projects"]
ctype, body = get("https://pypi.org/simple/mmw-b/")
assert ctype.startswith("text/html") and b"mmw_b-1.0-py3-none-any.whl" in body, ctype
ctype, body = get("https://pypi.org/pypi/mmw-b/json")
assert ctype.startswith("application/json") and json.loads(body)["info"]["version"] == "2.0"
result["negotiation"] = "ok"
result["tls"] = "verified"

# Only a certificate-verification failure counts: an HTTPError (also a
# URLError) would mean the host was served, i.e. the check is not proven.
try:
    get("https://uncovered.fixture.invalid/")
except urllib.error.HTTPError as e:
    raise AssertionError("a hostname outside the certificate was served: HTTP %s" % e.code)
except urllib.error.URLError as e:
    assert isinstance(e.reason, ssl.SSLCertVerificationError), repr(e.reason)
    result["uncovered"] = "refused: " + type(e.reason).__name__
else:
    raise AssertionError("a hostname outside the certificate was accepted")

with open("/state/probe.json", "w") as fh:
    json.dump(result, fh)
