"""`mmw add local` and `mmw signing-key`."""
import shutil

import pytest
from typer.testing import CliRunner

from mmw.cli import app
from mmw.core import config
from mmw.core.signing import Keyring

pytestmark = pytest.mark.skipif(shutil.which("gpg") is None, reason="needs gpg")


@pytest.fixture
def cfg_dir(tmp_path):
    config.save(config.Config(settings=config.Settings(mirror_dir=tmp_path / "m")), tmp_path)
    return tmp_path


def mmw(cfg_dir, *args, input=None):
    return CliRunner().invoke(app, ["--config", str(cfg_dir), *args], input=input)


def test_add_local_generates_a_key_and_writes_the_source(cfg_dir):
    result = mmw(cfg_dir, "add", "local", "--folder", "debs", "--url", "https://tools.lab/apt")
    assert result.exit_code == 0, result.output
    [src] = config.load(cfg_dir).sources
    assert (src.provider, src.folder, src.key, src.structured) == ("apt-local", "debs", src.name, False)
    assert f"generated signing key '{src.name}'" in result.output
    assert [k.name for k in Keyring(cfg_dir / "m").list()] == [src.name]
    listed = mmw(cfg_dir, "signing-key", "list")
    assert src.name in listed.output


def test_add_local_with_an_existing_key_and_replacing(cfg_dir):
    assert mmw(cfg_dir, "signing-key", "generate", "ours").exit_code == 0
    first = mmw(cfg_dir, "add", "local", "--folder", "a", "--url", "https://tools.lab/apt", "--key", "ours")
    assert first.exit_code == 0, first.output
    again = mmw(cfg_dir, "add", "local", "--folder", "b", "--url", "https://tools.lab/apt", "--key", "ours",
                "--structured", "--suite", "jammy")
    assert again.exit_code == 0 and "replaced" in again.output
    [src] = config.load(cfg_dir).sources
    assert (src.folder, src.structured, src.suite) == ("b", True, "jammy")
    unknown = mmw(cfg_dir, "add", "local", "--folder", "c", "--url", "https://x.lab/apt", "--key", "nope")
    assert unknown.exit_code == 1 and "no signing key named 'nope'" in unknown.output


def test_add_local_refuses_structured_flags_without_structured(cfg_dir):
    result = mmw(cfg_dir, "add", "local", "--folder", "d", "--url", "https://t.lab/apt", "--suite", "x")
    assert result.exit_code == 2 and "--structured" in result.output
    missing = mmw(cfg_dir, "add", "local", "--url", "https://t.lab/apt")
    assert missing.exit_code == 2 and "--folder" in missing.output


def test_dry_run_writes_nothing(cfg_dir):
    result = mmw(cfg_dir, "add", "local", "--folder", "d", "--url", "https://t.lab/apt", "--key", "k",
                 "--dry-run")
    assert result.exit_code == 0 and 'provider = "apt-local"' in result.output
    assert config.load(cfg_dir).sources == []


def test_signing_key_lifecycle_and_remove_refusal(cfg_dir):
    assert mmw(cfg_dir, "signing-key", "generate", "x").exit_code == 0
    public = mmw(cfg_dir, "signing-key", "export", "x")
    assert "BEGIN PGP PUBLIC KEY" in public.output
    private = mmw(cfg_dir, "signing-key", "export", "x", "--private")
    assert "BEGIN PGP PRIVATE KEY" in private.stdout and "can sign as you" in private.stderr
    assert mmw(cfg_dir, "add", "local", "--folder", "d", "--url", "https://t.lab/apt", "--key", "x").exit_code == 0
    refused = mmw(cfg_dir, "signing-key", "remove", "x")
    assert refused.exit_code == 1 and "'x' signs" in refused.output
    assert mmw(cfg_dir, "signing-key", "generate", "spare").exit_code == 0
    assert mmw(cfg_dir, "signing-key", "remove", "spare").exit_code == 0
    assert "spare" not in mmw(cfg_dir, "signing-key", "list").output


def test_signing_key_import_from_stdin(cfg_dir, tmp_path):
    other = Keyring(tmp_path / "other")
    other.generate("o")
    from test_signing import export_secret
    result = mmw(cfg_dir, "signing-key", "import", "mine", "--file", "-", input=export_secret(tmp_path / "other", "o"))
    assert result.exit_code == 0, result.output
    assert "mine" in mmw(cfg_dir, "signing-key", "list").output
