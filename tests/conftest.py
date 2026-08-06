"""Shared test fixtures for Discord Ferry."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest
from aiohttp.client_reqrep import ClientResponse

if TYPE_CHECKING:
    from collections.abc import Iterator

from discord_ferry.core import logging_setup
from discord_ferry.core.security import reset_secret_registry

# --- aioresponses / aiohttp 3.14 compatibility shim ---------------------------
# aiohttp 3.14 made ``stream_writer`` a *required* keyword-only argument of
# ``ClientResponse.__init__``. aioresponses 0.7.8 builds its mock responses by
# calling that constructor directly and does not pass it, so every mocked HTTP
# call raises ``TypeError: ... missing 1 required keyword-only argument:
# 'stream_writer'``. This mirrors the upstream fix (aioresponses#288) by
# supplying a lightweight default; ``stream_writer`` is only consulted for its
# ``output_size`` attribute. The signature guard makes this a no-op on
# aiohttp < 3.14, and ``setdefault`` keeps it inert once a fixed aioresponses
# (or a caller that passes ``stream_writer``) is in play.
#
# REMOVE THIS once aioresponses ships a release containing
# https://github.com/pnuckowski/aioresponses/pull/288 and the dev dependency is
# bumped past it.
if "stream_writer" in inspect.signature(ClientResponse.__init__).parameters:
    _orig_client_response_init = ClientResponse.__init__

    def _patched_client_response_init(self: ClientResponse, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("stream_writer", Mock(output_size=0))
        _orig_client_response_init(self, *args, **kwargs)

    # Cannot statically reassign a method; this is a deliberate test-only patch.
    ClientResponse.__init__ = _patched_client_response_init  # type: ignore[method-assign]
# -----------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# NiceGUI's `user` fixture, for tests that must execute page code against a live
# client. Register the NARROW plugin, never `nicegui.testing.plugin`: the
# umbrella pulls in `screen_plugin`, which does `from selenium import webdriver`
# at module level. Selenium is not a dependency of this project, so the umbrella
# raises ModuleNotFoundError at collection time and aborts the ENTIRE suite.
pytest_plugins = ["nicegui.testing.user_plugin"]


@pytest.fixture
def fixtures_dir() -> Path:
    """Return the path to the test fixtures directory."""
    return FIXTURES_DIR


@pytest.fixture(autouse=True)
def _isolate_ferry_logging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep `configure_logging()` away from the developer's real home directory.

    `cli.main()` is a Click *group* callback, so Click runs it on EVERY
    `CliRunner().invoke(main, ...)` regardless of subcommand -- and this suite
    does that dozens of times. Without this fixture each of those invocations
    would attach a real RotatingFileHandler under ~/.discord-ferry/logs/, and
    they would stack for the whole session.

    Autouse and unconditional: a test that opts out by accident is a test that
    silently writes to the real filesystem.
    """
    monkeypatch.setattr(logging_setup, "_log_path", lambda: tmp_path / "ferry.log")
    logging_setup.reset_logging()
    reset_secret_registry()
    try:
        yield
    finally:
        logging_setup.reset_logging()
        reset_secret_registry()
