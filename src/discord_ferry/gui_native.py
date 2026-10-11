"""Scoped NiceGUI native-window launch and local browser recovery."""

from __future__ import annotations

import asyncio
import inspect
import logging
import multiprocessing
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import aiohttp
from nicegui import native as native_package
from nicegui.native import native, native_mode
from nicegui.server import Server

from discord_ferry.core import http

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

native_event_manager: Any | None = None
try:
    from nicegui.native.event_manager import event_manager as _native_event_manager
except ModuleNotFoundError:
    pass
else:
    native_event_manager = _native_event_manager

logger = logging.getLogger(__name__)

_ACTIVATE_PARAMETERS = (
    "protocol",
    "host",
    "port",
    "title",
    "width",
    "height",
    "fullscreen",
    "frameless",
    "shutdown_event",
    "favicon",
)
_OPEN_WINDOW_PARAMETERS = (
    "protocol",
    "host",
    "port",
    "title",
    "width",
    "height",
    "fullscreen",
    "frameless",
    "method_queue",
    "response_queue",
    "event_sender",
    "favicon",
    "window_args",
    "settings",
    "start_args",
    "dropped_keys",
)


def _parameter_names(function: Callable[..., Any]) -> tuple[str, ...]:
    """Return the positional and keyword parameter names of a callable."""
    return tuple(inspect.signature(function).parameters)


def _has_parameters(object_: Any, name: str, expected: tuple[str, ...]) -> bool:
    """Whether a private NiceGUI callable has the tested protocol shape."""
    try:
        candidate = getattr(object_, name)
        return callable(candidate) and _parameter_names(candidate) == expected
    except (AttributeError, TypeError, ValueError):
        return False


def _protocol_supported() -> bool:
    """Return whether the installed NiceGUI native internals match the adapter."""
    try:
        return (
            _has_parameters(native_package, "activate", _ACTIVATE_PARAMETERS)
            and _has_parameters(native_mode, "_open_window", _OPEN_WINDOW_PARAMETERS)
            and callable(native_mode._split_picklable)
            and callable(native.create_queues)
            and native.SPAWN_CONTEXT is not None
            and hasattr(native, "event_sender")
            and native_event_manager is not None
            and callable(native_event_manager.start)
        )
    except AttributeError:
        return False


@dataclass
class BrowserFallback:
    """Open the local browser once after HTTP has become reachable."""

    url: str
    application: Any
    open_browser: Callable[[str], bool]
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _loop: asyncio.AbstractEventLoop | None = None
    _task: asyncio.Task[None] | None = None
    _requested: bool = False
    _stopping: bool = False
    _claimed: bool = False
    browser_dispatches: int = 0

    def on_startup(self) -> None:
        """Capture the server event loop and schedule a prior request."""
        self._loop = asyncio.get_running_loop()
        self._schedule_if_requested()

    def on_shutdown(self) -> None:
        """Cancel recovery before NiceGUI tears down its local server."""
        self.stop()

    def stop(self) -> None:
        """Prevent a pending recovery from opening a browser during shutdown."""
        with self._lock:
            self._stopping = True
            task = self._task
        if task is not None:
            task.cancel()

    def request(self, reason: str) -> bool:
        """Ask once for browser recovery, from any native or renderer thread."""
        with self._lock:
            if self._stopping or self._claimed:
                return False
            first_request = not self._requested
            self._requested = True
        if first_request:
            logger.warning("%s. Ferry remains available at %s.", reason, self.url)
        self._schedule_if_requested()
        return first_request

    def _schedule_if_requested(self) -> None:
        with self._lock:
            loop = self._loop
            should_schedule = self._requested and not self._stopping and self._task is None
        if loop is not None and loop.is_running() and should_schedule:
            loop.call_soon_threadsafe(self._create_readiness_task)

    def _create_readiness_task(self) -> None:
        with self._lock:
            if self._stopping or self._task is not None:
                return
            self._task = asyncio.create_task(
                self._wait_for_ready(), name="browser fallback readiness"
            )

    async def _get(self) -> int:
        timeout = aiohttp.ClientTimeout(total=0.2)
        async with (
            http.new_session(timeout=timeout, request_class=aiohttp.ClientRequest) as session,
            session.get(self.url, headers={"Host": self.url.removeprefix("http://")}) as response,
        ):
            return response.status

    async def _wait_for_ready(self) -> None:
        try:
            while not self._is_stopping():
                try:
                    if await self._get() == 200:
                        await asyncio.to_thread(self._open_if_running)
                        return
                except (TimeoutError, aiohttp.ClientError):
                    pass
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            return

    def _is_stopping(self) -> bool:
        with self._lock:
            return self._stopping

    def _server_requested_exit(self) -> bool:
        instance = getattr(Server, "instance", None)
        return bool(
            getattr(instance, "should_exit", False) or getattr(instance, "force_exit", False)
        )

    def _open_if_running(self) -> None:
        with self._lock:
            if (
                self._stopping
                or self._claimed
                or self.application.is_stopping
                or self.application.is_stopped
                or self._server_requested_exit()
            ):
                return
            self._claimed = True
            self.browser_dispatches += 1
            try:
                if not self.open_browser(self.url):
                    logger.warning(
                        "No browser could be opened. Ferry remains available at %s.", self.url
                    )
            except OSError:
                logger.warning(
                    "Opening a browser failed. Ferry remains available at %s.",
                    self.url,
                    exc_info=True,
                )

    async def wait_for_idle(self) -> None:
        """Wait for the requested readiness task without hiding caller cancellation."""
        while True:
            with self._lock:
                task = self._task
                wait_for_scheduled_task = (
                    self._requested
                    and not self._stopping
                    and task is None
                    and self._loop is not None
                    and self._loop.is_running()
                )
            if task is not None:
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    caller = asyncio.current_task()
                    if (
                        self._is_stopping()
                        and task.cancelled()
                        and task is not caller
                        and caller is not None
                        and caller.cancelling() == 0
                    ):
                        return
                    raise
                return
            if not wait_for_scheduled_task:
                return
            await asyncio.sleep(0)


def _open_window_with_shown_state(child_shown: Any, *arguments: Any) -> None:
    """Run NiceGUI's child entry point while publishing an actual shown state."""
    # NiceGUI imports this module attribute conditionally, so its typed package surface
    # does not expose it even though the matched native protocol requires it.
    webview: Any = native_mode.webview  # type: ignore[attr-defined]
    original_create_window = webview.create_window
    window: Any | None = None

    def capture_window(**kwargs: Any) -> Any:
        nonlocal window
        window = original_create_window(**kwargs)
        return window

    webview.create_window = capture_window
    try:
        native_mode._open_window(*arguments)
    except BaseException:
        if window is not None and window.events.shown.is_set():
            child_shown.set()
        logger.exception("native window child failed before returning")
        os._exit(1)
    finally:
        webview.create_window = original_create_window
    if window is not None and window.events.shown.is_set():
        child_shown.set()


class ScopedNativeLauncher:
    """Preserve NiceGUI's native transport while monitoring only Ferry's child."""

    def __init__(self, application: Any, url: str, fallback: BrowserFallback) -> None:
        self.application = application
        self.url = url
        self.fallback = fallback
        self.supported = _protocol_supported()
        self.native_requested = self.supported
        self.native_controls_available = self.supported
        self.fallback_requests: list[str] = []
        self._stopped = threading.Event()
        self._startup = threading.Event()
        self._monitor: threading.Thread | None = None
        self._process: Any | None = None

    def on_startup(self) -> None:
        """Record server readiness and clear a proxy after native unavailability."""
        self._startup.set()
        if not self.native_controls_available:
            self.application.native.main_window = None

    def on_shutdown(self) -> None:
        """Tell the monitor that a normal server shutdown owns termination."""
        self._stopped.set()

    def _recover_before_shown(self, reason: str) -> None:
        self.native_controls_available = False
        self.application.native.main_window = None
        self.fallback_requests.append("native startup failure")
        self.fallback.request(reason)

    def activate(
        self,
        protocol: str,
        host: str,
        port: int,
        title: str,
        width: int,
        height: int,
        fullscreen: bool,
        frameless: bool,
        shutdown_event: Any = None,
        favicon: str | Path | None = None,
    ) -> None:
        """Implement the installed NiceGUI activation shape for Ferry's child."""
        if shutdown_event is not None:
            raise RuntimeError("Ferry native fallback requires reload=False")
        manager = native_event_manager
        if manager is None:
            self._recover_before_shown("Native startup compatibility mode is unavailable")
            return
        multiprocessing.freeze_support()
        native.create_queues()
        manager.start()
        window_args, dropped_window_args = native_mode._split_picklable(
            self.application.native.window_args
        )
        settings, dropped_settings = native_mode._split_picklable(self.application.native.settings)
        start_args, dropped_start_args = native_mode._split_picklable(
            self.application.native.start_args
        )
        arguments = (
            protocol,
            host,
            port,
            title,
            width,
            height,
            fullscreen,
            frameless,
            native.method_queue,
            native.response_queue,
            native.event_sender,
            favicon,
            window_args,
            settings,
            start_args,
            {
                "window_args": dropped_window_args,
                "settings": dropped_settings,
                "start_args": dropped_start_args,
            },
        )
        child_shown = native.SPAWN_CONTEXT.Event()
        process = native.SPAWN_CONTEXT.Process(
            target=_open_window_with_shown_state,
            args=(child_shown, *arguments),
            daemon=True,
        )
        self._process = process
        try:
            process.start()
        except OSError:
            self._recover_before_shown("Native startup failed before Ferry could create its window")
            return

        def monitor() -> None:
            while process.is_alive() and not self._stopped.wait(0.1):
                pass
            if self._stopped.is_set():
                return
            process.join()
            if not child_shown.is_set():
                self._recover_before_shown(
                    "Native startup failed before Ferry could show its window"
                )
                return
            self._startup.wait()
            if not self._stopped.is_set():
                self.application.shutdown()

        self._monitor = threading.Thread(target=monitor, name="native window monitor", daemon=True)
        self._monitor.start()

    @contextmanager
    def activation(self) -> Iterator[None]:
        """Replace only the exported native activation during this GUI run."""
        if not self.supported:
            logger.warning(
                "Installed NiceGUI native launcher is unsupported; using guarded browser mode."
            )
            self.fallback.request("Native startup compatibility mode is unavailable")
            yield
            return
        original_activate = native_package.activate
        native_package.activate = self.activate
        try:
            yield
        finally:
            native_package.activate = original_activate

    def close(self) -> None:
        """Release only adapter-owned transport resources without blocking shutdown."""
        self._stopped.set()
        monitor = self._monitor
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join(timeout=0.5)
            if monitor.is_alive():
                logger.warning("native window monitor did not stop during bounded cleanup")
        receiver = getattr(native, "event_receiver", None)
        if receiver is not None:
            try:
                receiver.close()
            except OSError:
                logger.debug("native event receiver was already closed", exc_info=True)
        queues = (getattr(native, "method_queue", None), getattr(native, "response_queue", None))
        for queue in queues:
            if queue is not None:
                try:
                    queue.close()
                    queue.cancel_join_thread()
                except (OSError, ValueError):
                    logger.debug("native queue was already closed", exc_info=True)
