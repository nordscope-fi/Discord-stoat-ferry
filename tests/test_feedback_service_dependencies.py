"""The feedback service must declare every third-party package it imports."""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICE_DIR = ROOT / "src" / "discord_ferry" / "feedback_service"

# Import name to distribution name, where the two differ.
_DISTRIBUTION = {"jwt": "pyjwt"}


def _declared() -> set[str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    requirements = [
        *project["dependencies"],
        *project["optional-dependencies"]["feedback-service"],
    ]
    return {re.split(r"[\[<>=!~;\s]", item, maxsplit=1)[0].lower() for item in requirements}


def _third_party_imports() -> set[str]:
    found: set[str] = set()
    for path in SERVICE_DIR.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".", 1)[0]
                if top not in sys.stdlib_module_names and top != "discord_ferry":
                    found.add(top)
    return found


def test_feedback_service_declares_every_third_party_import() -> None:
    imports = _third_party_imports()
    assert {"cryptography", "jwt"} <= imports  # the scan saw the imports it guards
    missing = {name for name in imports if _DISTRIBUTION.get(name, name).lower() not in _declared()}
    assert not missing, f"imported but not declared directly: {sorted(missing)}"
