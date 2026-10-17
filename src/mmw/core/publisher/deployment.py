"""Deployment assets shared by copied publish trees and applied bundles."""
import io
from pathlib import Path

from mmw.core.certs import bundle_tls_files
from mmw.resources import apply_script_bytes, ca_script_bytes, sign_script_bytes
from mmw.resources.apply import _install_tls, https_state


def scripts(certs: bool) -> dict[str, bytes]:
    files = {"apply.py": apply_script_bytes()}
    if certs:
        files["sign.py"] = sign_script_bytes()
        files["make-ca.py"] = ca_script_bytes()
    return files


def write_scripts(sink, certs: bool, *, existing=()) -> set[str]:
    files = scripts(certs)
    for rel, data in files.items():
        if rel not in existing:
            sink.write(rel, data)
    return set(files)


def install_tls(root: Path, files: dict[str, bytes]) -> None:
    """Use the far-side installer's atomic writes and private-key permissions."""
    for name, data in files.items():
        _install_tls(str(root), name, io.BytesIO(data))
    https_state(str(root))


def prepare_tree(root: Path, network, cfg) -> None:
    if network.certs:
        install_tls(root, bundle_tls_files(network, cfg))
