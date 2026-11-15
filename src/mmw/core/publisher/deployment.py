"""Deployment assets shared by published trees and far sides: the TLS
material under `{root}/.mmw/tls/`. Nothing else is shipped beside a tree —
the far side runs mmw (`mmw site`)."""
import io
from pathlib import Path

from mmw.core import disk_io
from mmw.core.certs import bundle_tls_files
from mmw.core.served_tree import _install_tls, https_state

# Shipped beside trees until mmw ran on both sides: removed where a prune
# finds them (top level, and the signing tools once kept in .mmw/tls/).
RETIRED = ("apply.py", "sign.py", "make-ca.py")


def drop_retired(root: Path) -> None:
    for name in RETIRED:
        disk_io.ops.unlink(Path(root) / name, missing_ok=True)
        disk_io.ops.unlink(Path(root) / ".mmw" / "tls" / name, missing_ok=True)


def install_tls(root: Path, files: dict[str, bytes]) -> None:
    """Atomic writes, and private-key permissions."""
    for name, data in files.items():
        _install_tls(str(root), name, io.BytesIO(data))
    https_state(str(root))


def prepare_tree(root: Path, network, cfg) -> None:
    if network.certs:
        install_tls(root, bundle_tls_files(network, cfg))
