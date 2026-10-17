"""Core reaches VS Code only through the provider contract.

Behaviour that differs by provider is a provider member (`retries_failures`,
`repair_packages`, …), never a name check or a provider's private path in
core. The VS Code config model and `mmw add vscode` are VS Code's by design.
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "mmw"
ALLOWED = {SRC / "core" / "config.py", SRC / "commands" / "add_vscode.py"}
LEAK = re.compile(r"""provider\s*[!=]=\s*["']vscode["']|\.vscode-meta""")


def test_no_vscode_special_cases_outside_its_provider():
    leaks = []
    for path in sorted(SRC.rglob("*.py")):
        if path.is_relative_to(SRC / "providers") or path in ALLOWED:
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if LEAK.search(line):
                leaks.append(f"{path.relative_to(SRC)}:{n}: {line.strip()}")
    assert not leaks, "\n".join(leaks)
