"""A repository given as the URL of a sources file, NVIDIA-style:

    curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list

The file is read like a pasted config, and its URL is kept as an extra file of
every repository it names — so that same `curl` line works against the mirror.
"""
import asyncio

from mmw.commands import add as add_cmd
from mmw.core import discover
from mmw.core.apt_sources import is_sources_file_url, remember_sources_url
from mmw.core.draft import SourceDraft

LIST_URL = "https://nvidia.example/libnvidia-container/stable/deb/nvidia-container-toolkit.list"
LIST = ("deb https://nvidia.example/libnvidia-container/stable/deb/$(ARCH) /\n"
        "#deb https://nvidia.example/libnvidia-container/experimental/deb/$(ARCH) /\n")


def test_sources_file_urls_are_recognised():
    assert is_sources_file_url(LIST_URL)
    assert is_sources_file_url("https://x.example/a/b.sources")
    assert not is_sources_file_url("https://nvidia.example/libnvidia-container/stable/deb/amd64")
    assert not is_sources_file_url("https://x.example/a.list?token=1")     # no own path


def test_remembering_it_adds_it_once():
    d = SourceDraft(url="https://x.example/deb/amd64", provider="apt-flat", name="x")
    remember_sources_url([d], LIST_URL)
    remember_sources_url([d], LIST_URL)
    assert d.extra_files == [LIST_URL]
    src = d.to_source()
    assert src.extra_files == [LIST_URL]
    assert SourceDraft.from_source(src).extra_files == [LIST_URL]


def test_mmw_add_url_keeps_the_sources_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(add_cmd, "read_input", lambda files, url: LIST)
    monkeypatch.setattr(discover, "expand_arch_placeholder", lambda url, token: ["amd64", "arm64"])
    assert add_cmd.run(url=LIST_URL, dry_run=True, cfg_dir=tmp_path) == 0
    out = capsys.readouterr().out
    assert out.count("nvidia-container-toolkit.list") == 2      # both arch repositories


def test_probing_a_sources_file_url_reads_it_like_a_paste(monkeypatch):
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    import test_wizard_repository_flow as W
    from mmw.tui import custom_repo

    monkeypatch.setattr(custom_repo, "read_input", lambda files, url: LIST)
    monkeypatch.setattr(discover, "expand_arch_placeholder", lambda url, token: ["amd64", "arm64"])
    monkeypatch.setattr(discover, "detect_repository",
                        lambda url: (_ for _ in ()).throw(AssertionError("probed as a repo")))

    async def scenario():
        app = W._Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            step = await W._probe(pilot, url=LIST_URL)
            await W._settle(pilot)
            return step.get_sources()
    sources = asyncio.run(scenario())
    assert sorted(s.url for s in sources) == [
        "https://nvidia.example/libnvidia-container/stable/deb/amd64",
        "https://nvidia.example/libnvidia-container/stable/deb/arm64"]
    assert all(s.extra_files == [LIST_URL] for s in sources)
