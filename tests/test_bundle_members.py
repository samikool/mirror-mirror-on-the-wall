"""The bundle's writing half: tar or directory, same members either way —
and the apply-network bundle unchanged by the split."""
import json
import os
from pathlib import Path

from mmw.core import config
from mmw.core.publisher.bundle import bundle_network

from bundle_golden import describe
from test_bundle import seeded  # noqa: F401 — the shared pool fixture

GOLDEN = Path(__file__).parent / "data" / "bundle_members_golden.json"


def test_bundle_bytes_unchanged_by_refactor(seeded, tmp_path):  # noqa: F811
    mirror_dir, cfg = seeded
    net = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out", certs=False)
    result = bundle_network(net, cfg, mirror_dir, from_ref=None)
    got = describe(result.out_path)
    if os.environ.get("MMW_WRITE_GOLDEN"):
        GOLDEN.write_text(json.dumps(got, indent=1) + "\n")
    assert got == json.loads(GOLDEN.read_text())
