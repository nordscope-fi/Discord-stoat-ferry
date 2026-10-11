"""A renderer that fails after NiceGUI has started its window-method helper."""

from __future__ import annotations

import os
from pathlib import Path

from .window import Window as Window

settings: dict[str, object] = {}


class FileDialog:
    OPEN = 10


class _Event:
    def __iadd__(self, _handler: object) -> _Event:
        return self

    def is_set(self) -> bool:
        return False


class _Events:
    def __init__(self) -> None:
        self.closed = _Event()
        self.shown = _Event()
        self.loaded = _Event()
        self.minimized = _Event()
        self.maximized = _Event()
        self.restored = _Event()
        self.resized = _Event()
        self.moved = _Event()


class _Window(Window):
    def __init__(self) -> None:
        self.events = _Events()

    def evaluate_js(self, _script: str) -> bool:
        return True


def create_window(**_kwargs: object) -> _Window:
    return _Window()


def start(**_kwargs: object) -> None:
    print("NATIVE_STARTUP_FAILURE phase=start", flush=True)
    marker = os.environ.get("FERRY_NATIVE_FAILURE_MARKER")
    if marker is not None:
        Path(marker).write_text("start", encoding="utf-8")
    raise RuntimeError("native renderer unavailable after helper startup")
