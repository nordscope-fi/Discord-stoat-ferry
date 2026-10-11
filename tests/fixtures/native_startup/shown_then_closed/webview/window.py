"""Supply the pywebview window imports needed by NiceGUI's parent proxy."""

from __future__ import annotations

from enum import IntFlag


class FixPoint(IntFlag):
    NORTH = 1
    WEST = 2


class Window:
    """Minimal parent class for NiceGUI's proxy subclass."""
