"""Shared test fixtures for Discord Ferry."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import inspect
import os
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock, patch

import pytest
from aiohttp.client_reqrep import ClientResponse

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
    from contextlib import AbstractContextManager

from discord_ferry.core import logging_setup
from discord_ferry.core.http import reset_http_state
from discord_ferry.core.security import reset_secret_registry
from discord_ferry.migrator import api as migrator_api
from discord_ferry.migrator.api import (
    _reset_circuit_state,
    _reset_pacer_state,
    _reset_rate_state,
)

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


@pytest.fixture
def user_store(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """A writable stand-in for `app.storage.user`.

    The real property is request-scoped (it reads a contextvar and a session
    cookie), so a test body cannot seed it from outside a request. The page code
    under test only ever treats it as a mapping.
    """
    store: dict[str, object] = {}
    monkeypatch.setattr("nicegui.storage.Storage.user", property(lambda self: store), raising=False)
    return store


@pytest.fixture
def tab_store(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """A writable stand-in for `app.storage.tab`.

    The real property raises unless a connected client is resolvable from the
    current task's slot stack -- which a test body does not have. That is the
    very constraint this fix exists to respect, so the page code must still see
    a plain mapping.
    """
    store: dict[str, object] = {}
    monkeypatch.setattr("nicegui.storage.Storage.tab", property(lambda self: store), raising=False)
    return store


@pytest.fixture
def proxy_env() -> Callable[..., AbstractContextManager[None]]:
    """Clear every *_proxy variable, then apply the given ones.

    The autouse fixture does not neutralise these, so without this a developer
    behind a proxy gets different results from CI.
    """

    @contextmanager
    def _apply(**pairs: str) -> Iterator[None]:
        saved = dict(os.environ)
        for k in list(os.environ):
            if k.lower().endswith("_proxy"):
                os.environ.pop(k)
        # Also neutralise the kill switch and the CGI marker. An ambient
        # FERRY_DISABLE_PROXY=1 turns every `is not None` assertion red and
        # every `is None` assertion vacuous, and Task 11 builds its kill-switch
        # tests on this fixture.
        os.environ.pop("FERRY_DISABLE_PROXY", None)
        os.environ.pop("REQUEST_METHOD", None)
        os.environ.update(pairs)
        try:
            yield
        finally:
            os.environ.clear()
            os.environ.update(saved)

    return _apply


@pytest.fixture
def os_proxy() -> Callable[..., AbstractContextManager[None]]:
    """Patch BOTH seams. Never patches a stdlib name: getproxies_registry does
    not exist on ubuntu-latest, where CI runs."""

    @contextmanager
    def _apply(proxies: dict[str, str], bypass: set[str] | None = None) -> Iterator[None]:
        hosts = bypass or set()
        with (
            patch("discord_ferry.core.http._os_proxies", return_value=dict(proxies)),
            patch("discord_ferry.core.http._os_proxy_bypass", side_effect=lambda h: h in hosts),
        ):
            yield

    return _apply


@pytest.fixture
async def fake_proxy() -> AsyncIterator[
    tuple[Callable[[bytes], Awaitable[asyncio.Server]], list[str]]
]:
    """A loopback server that records the first request and answers a status.

    Needed because aioresponses patches ClientSession._request, so the request
    object is never constructed and proxy behaviour is invisible to all 21
    aioresponses modules. Real-socket precedent: tests/test_gui_native_lifecycle.py.

    The fixture owns teardown. Every server it hands out is closed here, so a
    test cannot leak a listening socket by forgetting, and `wait_closed` is
    awaited so the loop does not shut down mid-close.
    """
    captured: list[str] = []
    servers: list[asyncio.Server] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        captured.append((await reader.read(4096)).decode("latin1"))
        writer.write(_status[0])
        await writer.drain()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    _status: list[bytes] = [b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n"]

    async def _make(status: bytes = b"403 Forbidden") -> asyncio.Server:
        _status[0] = b"HTTP/1.1 " + status + b"\r\nContent-Length: 0\r\n\r\n"
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        servers.append(server)
        return server

    try:
        yield _make, captured
    finally:
        for server in servers:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()


def _reset_api_runtime_state() -> None:
    _reset_circuit_state()
    _reset_rate_state()
    _reset_pacer_state()
    migrator_api._request_semaphore = None


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
    _reset_api_runtime_state()
    try:
        monkeypatch.setattr(logging_setup, "_log_path", lambda: tmp_path / "ferry.log")
        logging_setup.reset_logging()
        reset_secret_registry()
        reset_http_state()
        yield
    finally:
        _reset_api_runtime_state()
        logging_setup.reset_logging()
        reset_secret_registry()
        reset_http_state()


# --- Windows filesystem semantics ---------------------------------------------
# POSIX rename(2) is an atomic replace, so on Linux and macOS a swap onto an
# existing file just works and no test can tell Path.rename from Path.replace.
# Win32 MoveFile refuses, which is issue #172. save_state runs at every phase
# boundary and every checkpoint_interval messages, so the SECOND save of any run
# hit an existing destination and died, not only a second run into an existing
# directory. test_repeated_checkpoints_overwrite_on_windows is the one that shows
# this: it starts from an empty tmp_path.
#
# Every other job in ci.yml is ubuntu-only, so this fixture is the only way a pull
# request can see that difference. The windows-atomic-write job runs the same two
# files on a real Windows runner, where no simulation is needed.
_REAL_RENAME = Path.rename


def _win32_rename(self: Path, target: str | Path) -> Path:
    """Path.rename with Win32 MoveFile semantics: refuse an existing destination."""
    if Path(target).exists():
        raise FileExistsError(
            errno.EEXIST,
            "Cannot create a file when that file already exists",
            str(self),
        )
    return _REAL_RENAME(self, target)


@pytest.fixture
def windows_filesystem(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make Path.rename behave as it does on Windows, for one test.

    monkeypatch rather than a bare ``Path.rename = ...`` so the attribute is
    restored when the test ends. A leaked patch would break every test that ran
    after it in the same process.
    """
    monkeypatch.setattr(Path, "rename", _win32_rename)
