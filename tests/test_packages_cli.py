"""`mmw packages` and layered sources files."""
import shutil

import pytest
from typer.testing import CliRunner

from debs import make_deb
from mmw.cli import app
from mmw.core import config
from mmw.core.layers import layer_dir
from test_layers import lab  # noqa: F401 — the fixture

pytestmark = pytest.mark.skipif(shutil.which("gpg") is None or shutil.which("gpgv") is None,
                                reason="needs gpg and gpgv")


@pytest.fixture
def cli(lab):  # noqa: F811
    config.save(lab.cfg(), lab.tmp)

    def run(*args, input=None):
        return CliRunner().invoke(app, ["--config", str(lab.tmp), *args], input=input)
    run.lab = lab
    return run


def test_add_into_an_upstream_needs_a_signing_choice(cli):
    deb = make_deb(cli.lab.tmp / "new", "tools-ours", "1.0")
    result = cli("packages", "add", str(deb), "--into", cli.lab.src.name, "--network", "lab")
    assert result.exit_code == 2 and "--resign" in result.output and "--unsigned" in result.output
    assert not layer_dir(cli.lab.mirror_dir, "lab", cli.lab.src.name).exists()


def test_add_resign_copies_writes_the_layer_and_publishes(cli):
    deb = make_deb(cli.lab.tmp / "new", "tools-ours", "1.0")
    result = cli("packages", "add", str(deb), "--into", cli.lab.src.name, "--network", "lab",
                 "--resign", "tools")
    assert result.exit_code == 0, result.output
    assert deb.exists()                                                       # copied, not moved
    folder = layer_dir(cli.lab.mirror_dir, "lab", cli.lab.src.name)
    assert (folder / "jammy/main/tools-ours_1.0_amd64.deb").is_file()         # defaults: first suite, main
    [net] = config.load(cli.lab.tmp).networks
    assert net.layer_for(cli.lab.src.url).signing == "resign"
    listed = cli("packages", "list", "--network", "lab")
    assert "tools-ours_1.0_amd64.deb" in listed.output and "jammy/main" in listed.output
    from mmw.core.publisher import publish_network
    cfg = config.load(cli.lab.tmp)
    res = publish_network(cfg.networks[0], cfg, cli.lab.mirror_dir)
    assert not [r.errors for r in res.mirrors if r.errors]
    tree = cli.lab.tmp / "pub" / cli.lab.base
    assert (tree / "pool/mmw/main/tools-ours_1.0_amd64.deb").is_file()
    label = cli.lab.src.label
    assert (cli.lab.tmp / "pub" / "keys" / label / "tools.gpg").is_file()    # clients fetch the mmw key


def test_add_an_unpublished_suite_is_refused(cli):
    deb = make_deb(cli.lab.tmp / "new", "x", "1")
    result = cli("packages", "add", str(deb), "--into", cli.lab.src.name, "--network", "lab",
                 "--unsigned", "--suite", "noble")
    assert result.exit_code == 1 and "noble/main" in result.output


def test_clash_on_add_drop_and_keep(cli):
    clash = make_deb(cli.lab.tmp / "new", "hello", "2.9")
    result = cli("packages", "add", str(clash), "--into", cli.lab.src.name, "--network", "lab",
                 "--unsigned", "--on-clash", "drop")
    assert result.exit_code == 0 and "older than upstream" in result.output
    from mmw.core.layers import read_decisions
    folder = layer_dir(cli.lab.mirror_dir, "lab", cli.lab.src.name)
    assert read_decisions(folder) == {"hello_2.9_amd64.deb": "drop"}


def test_remove_and_signing(cli):
    deb = make_deb(cli.lab.tmp / "new", "tools-ours", "1.0")
    cli("packages", "add", str(deb), "--into", cli.lab.src.name, "--network", "lab", "--unsigned")
    signing = cli("packages", "signing", cli.lab.src.name, "--network", "lab", "--resign", "tools",
                  "--contents", "--drop-valid-until")
    assert signing.exit_code == 0, signing.output
    layer = config.load(cli.lab.tmp).networks[0].layer_for(cli.lab.src.url)
    assert (layer.signing, layer.key, layer.contents, layer.drop_valid_until) == ("resign", "tools", True, True)
    removed = cli("packages", "remove", "tools-ours_1.0_amd64.deb", "--into", cli.lab.src.name,
                  "--network", "lab")
    assert removed.exit_code == 0
    assert not list(layer_dir(cli.lab.mirror_dir, "lab", cli.lab.src.name).rglob("*.deb"))


def test_add_into_a_folder_repository(cli, tmp_path):
    src = config.LocalAptSource(provider="apt-local", url="https://tools.lab/apt",
                                folder=str(tmp_path / "debs"), key="tools")
    cfg = config.load(cli.lab.tmp)
    config.save(cfg.model_copy(update={"sources": [*cfg.sources, src]}), cli.lab.tmp)
    folder_debs = make_deb(tmp_path / "new", "a", "1"), make_deb(tmp_path / "new", "b", "1")
    result = cli("packages", "add", str(tmp_path / "new"), "--into", src.name)
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in (tmp_path / "debs").iterdir()) == sorted(d.name for d in folder_debs)


def test_layered_sources_file(cli):
    deb = make_deb(cli.lab.tmp / "new", "tools-ours", "1.0")
    cli("packages", "add", str(deb), "--into", cli.lab.src.name, "--network", "lab", "--resign", "tools")
    text = cli("sources-file", cli.lab.src.name, "--network", "lab", "--format", "list").output
    assert f"{cli.lab.src.url} jammy main universe" in text and "signed-by=/etc/apt/keyrings/tools.gpg" in text
    assert f"/mmw-keys/{cli.lab.src.label}/tools.gpg" in text
    cli("packages", "signing", cli.lab.src.name, "--network", "lab", "--unsigned")
    text = cli("sources-file", cli.lab.src.name, "--network", "lab").output
    assert "Trusted: yes" in text and "unsafe" in text


def test_remove_takes_a_name_not_a_pattern(cli):
    deb = make_deb(cli.lab.tmp / "new", "tools-ours", "1.0")
    cli("packages", "add", str(deb), "--into", cli.lab.src.name, "--network", "lab", "--unsigned")
    result = cli("packages", "remove", "*", "--into", cli.lab.src.name, "--network", "lab")
    assert result.exit_code == 1
    assert list(layer_dir(cli.lab.mirror_dir, "lab", cli.lab.src.name).rglob("*.deb"))


def test_signing_with_an_unknown_key_is_refused(cli):
    result = cli("packages", "signing", cli.lab.src.name, "--network", "lab", "--resign", "nope")
    assert result.exit_code == 1 and "no signing key named 'nope'" in result.output


def test_a_failed_add_leaves_nothing_copied(cli):
    folder = layer_dir(cli.lab.mirror_dir, "lab", cli.lab.src.name) / "jammy/main"
    a = make_deb(cli.lab.tmp / "new", "a-ours", "1.0")
    cli("packages", "add", str(a), "--into", cli.lab.src.name, "--network", "lab", "--unsigned")
    other = make_deb(cli.lab.tmp / "other", "a-ours", "1.0", extra={"Depends": "x"})   # same name, other bytes
    b = make_deb(cli.lab.tmp / "other", "b-ours", "1.0")
    result = cli("packages", "add", str(b), str(other), "--into", cli.lab.src.name, "--network", "lab")
    assert result.exit_code == 1
    assert not (folder / "b-ours_1.0_amd64.deb").exists()


def test_removing_a_network_removes_its_layers(cli):
    deb = make_deb(cli.lab.tmp / "new", "tools-ours", "1.0")
    cli("packages", "add", str(deb), "--into", cli.lab.src.name, "--network", "lab", "--unsigned")
    from mmw.tui.networks import clear_ledger
    assert clear_ledger(cli.lab.mirror_dir, "lab") is None
    assert not (cli.lab.mirror_dir / "layers" / "lab").exists()
