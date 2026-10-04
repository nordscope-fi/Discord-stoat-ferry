"""Containment policy for export-supplied local media paths."""

from __future__ import annotations

import re
from pathlib import Path

_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9_-]+$")


def contained_media_path(root: Path, relative: str) -> Path | None:
    """Return the resolved candidate only when it stays inside the resolved root."""
    if not relative or Path(relative).is_absolute():
        return None
    try:
        resolved_root = root.resolve()
        candidate = (resolved_root / relative).resolve()
    except (OSError, RuntimeError):
        # pathlib raises RuntimeError, not OSError, on symlink loops.
        return None
    if not candidate.is_relative_to(resolved_root):
        return None
    return candidate


def is_media_escape(root: Path, relative: str) -> bool:
    """True when a local media string fails containment; empty and remote are not escapes."""
    if not relative or relative.startswith(("http://", "https://")):
        return False
    return contained_media_path(root, relative) is None


def is_safe_filename_component(value: str) -> bool:
    """True when value is safe to compose into a download destination filename."""
    return _SAFE_FILENAME.fullmatch(value) is not None
