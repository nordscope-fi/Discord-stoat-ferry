"""A renderer that reports shown before an ordinary close."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from .window import Window

settings: dict[str, object] = {}


class FileDialog:
    OPEN = 10


class _Event:
    def __init__(self) -> None:
        self._handlers: list[Any] = []
        self._set = threading.Event()

    def __iadd__(self, handler: Any) -> _Event:
        self._handlers.append(handler)
        return self

    def is_set(self) -> bool:
        return self._set.is_set()

    def fire(self) -> None:
        self._set.set()

        def deliver() -> None:
            time.sleep(0.25)
            for handler in self._handlers:
                handler()

        threading.Thread(target=deliver, daemon=True).start()


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

    def destroy(self) -> None:
        self.events.closed.fire()

    def evaluate_js(self, _script: str) -> bool:
        return True


_window: _Window | None = None


def create_window(**_kwargs: object) -> _Window:
    global _window
    _window = _Window()
    return _window


def start(**_kwargs: object) -> None:
    assert _window is not None
    _window.events.shown.fire()
    print("NATIVE_SHOWN", flush=True)
    if os.environ["FERRY_NATIVE_FIXTURE_MODE"] == "explicit-shutdown":
        while not _window.events.closed.is_set():
            time.sleep(0.01)
        return
    _window.events.closed.fire()
