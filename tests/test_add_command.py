"""`mmw add` — headless import of apt config files. No network."""
from pathlib import Path

import pytest

from mmw.commands import add
from mmw.core import config

DEB822 = """\
Types: deb
URIs: http://archive.ubuntu.com/ubuntu/
Suites: noble noble-updates
Components: main universe

Types: deb
URIs: http://security.ubuntu.com/ubuntu/
Suites: noble-security
Components: main
"""


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    (tmp_path / "mmw.toml").write_text(
        '[settings]\nmirror_dir = "/tmp/pool"\nmax_connections = 8\n'
    )
    return tmp_path


def _run(cfg_dir: Path, text: str, **kw) -> tuple[int, list[str]]:
    src = cfg_dir / "in.sources"
    src.write_text(text)
    out: list[str] = []
    code = add.run(file=src, cfg_dir=cfg_dir, echo=out.append, **kw)
    return code, out


def test_import_writes_sources(cfg_dir):
    code, out = _run(cfg_dir, DEB822)
    assert code == 0
    cfg = config.load(cfg_dir)
    assert [s.name for s in cfg.sources] == ["ubuntu-archive", "ubuntu-security"]
    assert "2 source(s) added" in out[-1]


def test_reimport_is_idempotent(cfg_dir):
    _run(cfg_dir, DEB822)
    code, out = _run(cfg_dir, DEB822)
    assert code == 0
    assert len(config.load(cfg_dir).sources) == 2
    assert "0 source(s) added, 0 updated" in out[-1]


def test_second_arch_merges_into_existing_source(cfg_dir):
    _run(cfg_dir, DEB822, arch="amd64")
    _run(cfg_dir, "deb [arch=arm64] http://archive.ubuntu.com/ubuntu noble main\n")
    cfg = config.load(cfg_dir)
    archive = next(s for s in cfg.sources if s.name == "ubuntu-archive")
    assert archive.arches == ["amd64", "arm64"]
    assert len(cfg.sources) == 2  # merged, not appended


def test_an_arch_less_file_stays_arch_less(cfg_dir):
    """No `[arch=]` in the file means every architecture upstream serves — the
    import must not invent one."""
    _run(cfg_dir, DEB822)
    assert all(s.arches == [] for s in config.load(cfg_dir).sources)


def test_merging_an_arch_into_all_arches_stays_all(cfg_dir):
    """"all" absorbs: importing one explicit arch must not narrow a repository
    that was mirroring every architecture."""
    _run(cfg_dir, DEB822)                       # arches = [] (all)
    _run(cfg_dir, "deb [arch=arm64] http://archive.ubuntu.com/ubuntu noble main\n")
    archive = next(s for s in config.load(cfg_dir).sources if s.name == "ubuntu-archive")
    assert archive.arches == []


def test_written_config_has_no_duplicate_mirror_names(cfg_dir):
    _run(cfg_dir, DEB822)
    _run(cfg_dir, "deb [arch=arm64] http://archive.ubuntu.com/ubuntu noble main\n")
    names = [m.name for m in config.load(cfg_dir).mirrors]
    assert len(set(names)) == len(names)


def test_dry_run_writes_nothing(cfg_dir):
    code, out = _run(cfg_dir, DEB822, dry_run=True)
    assert code == 0
    assert config.load(cfg_dir).sources == []
    assert "[[sources]]" in "\n".join(out)


def test_empty_input_is_an_error(cfg_dir):
    code, out = _run(cfg_dir, "# nothing here\n")
    assert code == 1
    assert "No mirrorable apt sources" in out[0]


def test_disabled_only_file_is_an_error(cfg_dir):
    code, _ = _run(cfg_dir, "Enabled: no\nTypes: deb\nURIs: http://x/deb\nSuites: s\n")
    assert code == 1


def test_missing_config_reports_cleanly(tmp_path):
    src = tmp_path / "in.sources"
    src.write_text("deb http://x/deb noble main\n")
    out: list[str] = []
    code = add.run(file=src, cfg_dir=tmp_path, echo=out.append)
    assert code == 1
    assert "mmw init" in out[0]


def test_label_and_arch_overrides(cfg_dir):
    _run(cfg_dir, "deb http://repo.internal/deb noble main\n",
         label="ubuntu", arch="ppc64el")
    s = config.load(cfg_dir).sources[0]
    assert s.label == "ubuntu"
    assert s.arches == ["ppc64el"]


# ── $(ARCH) placeholder resolution ───────────────────────────────────────────
# apt fills $(ARCH) in from the local machine's dpkg architecture; mmw has no
# such machine (see design-notes.md: "never invent an architecture" and
# core/apt_sources.py's arch_placeholder). --arch resolves it without network;
# omitting --arch probes each known architecture via discover, monkeypatched
# here since this module is deliberately network-free (see file docstring).

PLACEHOLDER = "deb https://nvidia.example/deb/$(ARCH) /\n"


def test_arch_override_resolves_placeholder_without_network(cfg_dir):
    code, out = _run(cfg_dir, PLACEHOLDER, arch="arm64")
    assert code == 0
    sources = config.load(cfg_dir).sources
    assert len(sources) == 1
    assert sources[0].url == "https://nvidia.example/deb/arm64"
    assert sources[0].arches == ["arm64"]


def test_placeholder_is_probed_and_expanded_into_one_source_per_arch(cfg_dir, monkeypatch):
    from mmw.core import discover
    monkeypatch.setattr(discover, "expand_arch_placeholder",
                         lambda url, token: ["amd64", "arm64"])

    code, out = _run(cfg_dir, PLACEHOLDER)
    assert code == 0
    sources = {s.url: s.arches for s in config.load(cfg_dir).sources}
    assert sources == {
        "https://nvidia.example/deb/amd64": ["amd64"],
        "https://nvidia.example/deb/arm64": ["arm64"],
    }
    assert any("amd64" in line and "arm64" in line for line in out)


def test_placeholder_resolving_to_nothing_is_a_hard_error(cfg_dir, monkeypatch):
    from mmw.core import discover
    monkeypatch.setattr(discover, "expand_arch_placeholder", lambda url, token: [])

    code, out = _run(cfg_dir, PLACEHOLDER)
    assert code == 1
    assert config.load(cfg_dir).sources == []
    assert any("$(ARCH)" in line for line in out)


def test_stdin_is_read_when_no_file(monkeypatch):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("deb http://x/deb noble main\n"))
    assert add.read_input(None, None).startswith("deb ")


# ── several files, and keys that live outside the config ─────────────────────

_STANZA = """\
Types: deb
URIs: http://{host}/ubuntu
Suites: noble
Components: main
Signed-By: /usr/share/keyrings/{key}
"""

ARMOUR = "-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nmQINBGXf\n-----END PGP PUBLIC KEY BLOCK-----\n"


def _bundle(tmp_path):
    d = tmp_path / "sources.list.d"
    d.mkdir()
    # No trailing blank line — exactly how these files ship, and what makes a
    # hand-rolled `cat` run the records together.
    (d / "a.sources").write_text(_STANZA.format(host="a.example.com", key="a.gpg"))
    (d / "b.sources").write_text(_STANZA.format(host="b.example.com", key="b.gpg"))
    (d / "notes.txt").write_text("not an apt config\n")
    return d


def test_a_directory_of_configs_is_joined_safely(tmp_path):
    from mmw.commands.add import read_input
    from mmw.core.apt_sources import parse_apt_sources

    entries = parse_apt_sources(read_input([_bundle(tmp_path)], None))

    assert sorted(e.url for e in entries) == [
        "http://a.example.com/ubuntu", "http://b.example.com/ubuntu"]


def test_several_files_are_joined_safely(tmp_path):
    from mmw.commands.add import read_input
    from mmw.core.apt_sources import parse_apt_sources

    d = _bundle(tmp_path)
    entries = parse_apt_sources(read_input([d / "a.sources", d / "b.sources"], None))

    assert len(entries) == 2


def test_a_directory_with_no_configs_is_an_error(tmp_path):
    from mmw.commands.add import read_input

    (tmp_path / "empty").mkdir()
    with pytest.raises(FileNotFoundError):
        read_input([tmp_path / "empty"], None)


def test_keys_dir_fills_in_a_signed_by_keyring(tmp_path):
    """`Signed-By: /usr/share/keyrings/a.gpg` names a file on the machine the
    config came from — the one thing a config file cannot carry."""
    from mmw.commands.add import attach_keys, read_input
    from mmw.core.apt_sources import entries_to_sources, parse_apt_sources

    keys_dir = tmp_path / "keys" / "nested"
    keys_dir.mkdir(parents=True)
    (keys_dir / "a.gpg").write_text(ARMOUR)

    sources = entries_to_sources(parse_apt_sources(read_input([_bundle(tmp_path)], None)))
    matched, missing = attach_keys(sources, tmp_path / "keys")

    assert (matched, missing) == (1, 1)
    by_name = {s.gpg_key_name: s for s in sources}
    assert by_name["a.gpg"].gpg_key == ARMOUR
    assert by_name["b.gpg"].gpg_key is None


def test_a_binary_keyring_is_armoured_on_the_way_in(tmp_path):
    """Keyrings copied off a real machine are binary. `read_key` armours them
    losslessly, and `write_key` emits both encodings again at sync time."""
    from mmw.commands.add import attach_keys
    from mmw.core.config import SourceConfig
    from mmw.core.keys import dearmor

    keys_dir = tmp_path / "keys"
    keys_dir.mkdir()
    (keys_dir / "a.gpg").write_bytes(dearmor(ARMOUR))

    s = SourceConfig(name="a", url="http://a.example.com/ubuntu", gpg_key_name="a.gpg")
    assert attach_keys([s], keys_dir) == (1, 0)
    assert dearmor(s.gpg_key) == dearmor(ARMOUR)


def test_an_inline_key_is_never_overwritten_by_a_same_named_file(tmp_path):
    from mmw.commands.add import attach_keys
    from mmw.core.config import SourceConfig

    keys_dir = tmp_path / "keys"
    keys_dir.mkdir()
    (keys_dir / "a.gpg").write_text("-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nT1RIRVI=\n-----END PGP PUBLIC KEY BLOCK-----\n")

    s = SourceConfig(name="a", url="http://a.example.com/ubuntu",
                     gpg_key=ARMOUR, gpg_key_name="a.gpg")
    assert attach_keys([s], keys_dir) == (0, 0)
    assert s.gpg_key == ARMOUR, "the config's own key is the authority"
