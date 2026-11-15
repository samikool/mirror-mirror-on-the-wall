"""mmw is the one far-side tool: a config that still says far_side = "apply"
warns once and is read as any other network; every bundle carries points;
published trees carry no scripts."""
import json
import tarfile
import warnings

from mmw.core import config
from mmw.core.publisher import publish_network
from mmw.core.publisher.bundle import bundle_network

from test_dry_run_totals import two_mirrors  # noqa: F401


def test_far_side_apply_is_ignored_with_a_warning(tmp_path):
    (tmp_path / "mmw.toml").write_text(
        f'[settings]\nmirror_dir = "{tmp_path / "pool"}"\n\n[[networks]]\nname = "oak"\nfar_side = "apply"\n'
        '\n[[networks]]\nname = "elm"\nfar_side = "mmw"\n')
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        cfg = config.load(tmp_path)
    assert [n.name for n in cfg.networks] == ["oak", "elm"]
    said = [str(w.message) for w in seen if "far_side" in str(w.message)]
    assert len(said) == 1 and "oak" in said[0]
    config.save(cfg, tmp_path)
    assert "far_side =" not in (tmp_path / "mmw.toml").read_text()


def test_every_network_bundles_points(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = two_mirrors
    r = bundle_network(net, cfg, md, out_path=tmp_path / "b.tar")
    with tarfile.open(r.out_path) as tar:
        names = tar.getnames()
        head = json.loads(tar.extractfile(".mmw/manifest.jsonl").readline())
    assert head["format"] == 3 and ".mmw/apply.py" not in names
    assert not (tmp_path / "apply.py").exists()


def test_publish_carries_no_scripts(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = two_mirrors
    (net.root).mkdir(parents=True)
    for old in ("apply.py", "sign.py", "make-ca.py"):
        (net.root / old).write_text("old")                     # left by a publish before mmw ran both sides
    publish_network(net, cfg, md)
    assert not any((net.root / n).exists() for n in ("apply.py", "sign.py", "make-ca.py"))
