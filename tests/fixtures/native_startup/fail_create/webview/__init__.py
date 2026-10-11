"""A pywebview stand-in that fails before the child can report shown."""

from __future__ import annotations

import os
from pathlib import Path

from .window import Window as Window

settings: dict[str, object] = {}


class FileDialog:
    OPEN = 10


def create_window(**_kwargs: object) -> None:
    print("NATIVE_STARTUP_FAILURE phase=create", flush=True)
    marker = os.environ.get("FERRY_NATIVE_FAILURE_MARKER")
    if marker is not None:
        Path(marker).write_text("create", encoding="utf-8")
    raise RuntimeError("native renderer unavailable")
