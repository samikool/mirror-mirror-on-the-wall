from mmw.core import discover
from mmw.core.discover import DetectResult


def test_listed_dirs_handles_single_quoted_hrefs():
    """developer.download.nvidia.com's autoindex emits single-quoted attributes
    (`<a href='debian12/'>debian12/</a>`), not the double-quoted form most
    autoindexes use. `_listed_dirs` must recognise both, or a flat repository
    nested a couple of directories deep (`.../repos/debian12/x86_64`) is
    unreachable: `detect_repository` finds neither InRelease nor dists/ at the
    intermediate levels and falls back to `candidates`, which silently comes
    back empty instead of offering debian12/ or x86_64/ to browse into.
    """
    html = """
    <ul class='directorycontents'>
      <li><span class='file'><a href='GPGKEY'>GPGKEY</a></span></li>
      <li><span class='dir'><a href='debian12/'>debian12/</a></span></li>
      <li><span class='dir'><a href='fedora40/'>fedora40/</a></span></li>
    </ul>
    """
    assert discover._listed_dirs(html) == ["debian12", "fedora40"]


def test_listed_dirs_still_handles_double_quoted_hrefs():
    html = '<a href="ubuntu/">ubuntu/</a><a href="debian/">debian/</a>'
    assert discover._listed_dirs(html) == ["ubuntu", "debian"]


def test_listed_dirs_drops_absolute_and_offsite_hrefs_both_quote_styles():
    html = """
    <a href='../'>..</a>
    <a href="http://elsewhere.example/x/">offsite</a>
    <a href='/abs/'>abs</a>
    <a href='real/'>real</a>
    """
    assert discover._listed_dirs(html) == ["real"]


# ── expand_arch_placeholder ──────────────────────────────────────────────────

def test_expand_arch_placeholder_keeps_only_archs_that_resolve(monkeypatch):
    """NVIDIA's libnvidia-container ships `deb .../deb/$(ARCH) /`: apt fills
    $(ARCH) in locally, mmw has to find out which archs actually exist by
    checking each one, since the host (GitHub Pages) serves no directory
    listing to crawl instead."""
    real = {
        "https://x.example/deb/amd64": DetectResult(provider="apt-flat"),
        "https://x.example/deb/arm64": DetectResult(provider="apt-flat"),
    }
    monkeypatch.setattr(
        discover, "detect_repository",
        lambda url: real.get(url, DetectResult(provider=None, error="404")),
    )
    found = discover.expand_arch_placeholder("https://x.example/deb/$(ARCH)", "$(ARCH)")
    assert found == ["amd64", "arm64"]


def test_expand_arch_placeholder_empty_when_nothing_resolves(monkeypatch):
    monkeypatch.setattr(
        discover, "detect_repository", lambda url: DetectResult(provider=None, error="404")
    )
    assert discover.expand_arch_placeholder("https://x.example/deb/$(ARCH)", "$(ARCH)") == []


def test_probe_arch_placeholder_carries_the_full_result_not_just_the_arch(monkeypatch):
    """The TUI's URL-probe path needs suites/provider per arch, not just the
    arch name — probing each candidate twice (once to check it exists, once
    to get its shape) would double the request count for no reason."""
    amd64_result = DetectResult(provider="apt", suites=["noble"])
    monkeypatch.setattr(
        discover, "detect_repository",
        lambda url: amd64_result if url.endswith("/amd64") else DetectResult(provider=None),
    )
    pairs = discover.probe_arch_placeholder("https://x.example/deb/$(ARCH)", "$(ARCH)")
    assert pairs == [("amd64", amd64_result)]


def test_expand_arch_placeholder_is_a_thin_wrapper_over_probe(monkeypatch):
    monkeypatch.setattr(
        discover, "detect_repository",
        lambda url: DetectResult(provider="apt-flat") if "arm64" in url else DetectResult(provider=None),
    )
    assert discover.expand_arch_placeholder("https://x.example/deb/$(ARCH)", "$(ARCH)") == ["arm64"]


def test_flat_detection_accepts_release_or_bare_index_but_not_html(monkeypatch):
    """InRelease alone used to be tried, so a flat repository with only a
    Release — or an unsigned one with only Packages — was "not a repository".
    A host answering every path with an HTML page must not match instead."""
    import gzip

    import httpx

    from mmw.core import discover

    real = httpx.Client

    def serve(files):
        def handler(request):
            body = files.get(request.url.path.rsplit("/", 1)[-1])
            if body is None:
                return httpx.Response(200, content=b"<html>landing page</html>")
            return httpx.Response(200, content=body)
        monkeypatch.setattr(discover.httpx, "Client",
                            lambda **kw: real(transport=httpx.MockTransport(handler), **kw))

    serve({"Release": b"Origin: vendor\nSHA256:\n"})
    assert discover.detect_repository("https://v.test/repo").provider == "apt-flat"
    serve({"Packages.gz": gzip.compress(b"Package: a\n")})
    assert discover.detect_repository("https://v.test/repo").provider == "apt-flat"
    serve({})
    assert discover.detect_repository("https://v.test/repo").provider is None
