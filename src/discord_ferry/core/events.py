"""Event emitter for migration progress reporting."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class MigrationEvent:
    """Progress event emitted by the migration engine."""

    phase: str
    status: str  # "started", "progress", "completed", "error", "warning", "skipped", "confirm"
    message: str
    current: int = 0
    total: int = 0
    channel_name: str = ""
    detail: dict[str, object] | None = None


EventCallback = Callable[[MigrationEvent], Any]
