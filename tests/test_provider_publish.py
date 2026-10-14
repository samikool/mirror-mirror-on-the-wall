"""Each provider lays out its own units; snapshots remember the bases."""
import json

from mmw.core import config
from mmw.providers import get_provider
from test_seam_golden import _build


def test_walk_records_the_bases_it_published(tmp_path):
    from mmw.core.publisher import publish_network
    mirror_dir, cfg = _build(tmp_path)
    r = publish_network(config.NetworkConfig(name="lab", root=tmp_path / "pub"), cfg, mirror_dir)
    assert "archive.ubuntu.com/ubuntu" in r.bases
    assert any(b.startswith("developer.download.nvidia.com/") for b in r.bases)


def test_snapshots_record_bases_and_replay_uses_them(tmp_path):
    from mmw.core.snapshots import recorded_bases, take_snapshot
    mirror_dir, cfg = _build(tmp_path)
    snap = take_snapshot(config.NetworkConfig(name="lab"), cfg, mirror_dir)
    settings = json.loads(snap.settings)
    assert settings["bases"] and recorded_bases(snap.settings) == sorted(
        settings["bases"], key=len, reverse=True)


def test_old_snapshot_settings_still_derive_bases():
    from mmw.core.snapshots import recorded_bases
    unit = {"name": "u", "provider": "apt", "source_url": "https://h/x", "arch": "amd64"}
    assert recorded_bases(json.dumps({"mirrors": [unit]})) == ["h/x"]


def test_publishing_is_a_provider_member():
    import inspect
    from mmw.core import publisher
    assert "publish_group(" not in inspect.getsource(publisher.walk_network)
    assert callable(get_provider("apt").publish)


def test_an_unpublishable_provider_is_still_skipped_by_an_implicit_network(tmp_path, monkeypatch):
    """The generic rule stays for future providers: `publishes = False` units
    are left out of an implicit "everything" network."""
    from mmw.core.resolve import network_mirrors
    from mmw.providers.vscode.provider import VscodeProvider
    from test_vscode_review_fixes import _mixed
    cfg = _mixed(tmp_path)
    monkeypatch.setattr(VscodeProvider, "publishes", False)
    names = [m.name for m in network_mirrors(config.NetworkConfig(name="lab"), cfg,
                                             cfg.settings.mirror_dir)]
    assert names and not any(n.startswith("vscode-") for n in names)
