"""Guard: the unsafe_media_path warning wording carries no em dash (#1081).

The property is the literal text of the operator-facing strings, so this scans the
four migrator modules that emit them. It collects every ``unsafe_media_path`` warning
message (including a message held in a local variable) and every companion progress
event that uses the same "unsafe path" or "containment check" wording.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_MIGRATOR = Path(__file__).resolve().parent.parent / "src" / "discord_ferry" / "migrator"
_FILES = ("avatars.py", "emoji.py", "structure.py", "messages.py")
_EM_DASH = "—"
_MARKERS = ("containment check", "unsafe path")


def _is_string_expr(node: ast.AST) -> bool:
    return isinstance(node, ast.JoinedStr) or (
        isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def _render(node: ast.AST) -> str:
    """Flatten a string expression, with ``{}`` standing in for f-string fields."""
    if isinstance(node, ast.Constant):
        return str(node.value)
    assert isinstance(node, ast.JoinedStr)
    return "".join(
        _render(part) if isinstance(part, ast.Constant) else "{}" for part in node.values
    )


def _collect(path: Path) -> tuple[list[str], list[str]]:
    """Return (unsafe_media_path warning messages, marker-bearing strings)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assigned: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_string_expr(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned.setdefault(target.id, []).append(node.value)

    warnings: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        entries = {
            key.value: value
            for key, value in zip(node.keys, node.values, strict=True)
            if isinstance(key, ast.Constant)
        }
        kind = entries.get("type")
        if not (isinstance(kind, ast.Constant) and kind.value == "unsafe_media_path"):
            continue
        message = entries.get("message")
        if message is not None and _is_string_expr(message):
            warnings.append(_render(message))
        elif isinstance(message, ast.Name):
            warnings.extend(_render(value) for value in assigned.get(message.id, []))

    # Outermost string expressions only: a JoinedStr's own parts are rendered with it.
    nested: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            nested.update(id(part) for part in ast.walk(node) if part is not node)
    marked = [
        text
        for node in ast.walk(tree)
        if _is_string_expr(node) and id(node) not in nested
        for text in [_render(node)]
        if any(marker in text for marker in _MARKERS)
    ]
    return warnings, marked


@pytest.mark.parametrize("name", _FILES)
def test_unsafe_media_path_wording_has_no_em_dash(name: str) -> None:
    warnings, marked = _collect(_MIGRATOR / name)
    assert warnings, f"{name}: found no unsafe_media_path warning, so the scan checks nothing"
    offenders = [text for text in (*warnings, *marked) if _EM_DASH in text]
    assert offenders == [], f"em dash in operator-facing wording in {name}: {offenders}"


def test_scan_finds_every_known_warning_site() -> None:
    """Eight messages, nine dict sites (the attachment one is built twice)."""
    total = sum(len(_collect(_MIGRATOR / name)[0]) for name in _FILES)
    assert total >= 9, f"expected at least 9 unsafe_media_path warning sites, found {total}"
