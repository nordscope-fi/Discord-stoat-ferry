"""Behaviour tests for Ferry's scoped NiceGUI native launcher."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import aiohttp
import pytest
from nicegui import core

from discord_ferry import gui, gui_native
from discord_ferry.core import http
from discord_ferry.gui_native import BrowserFallback, ScopedNativeLauncher
from tests.local_cdn import local_cdn

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

FIXTURES = Path(__file__).parent / "fixtures" / "native_startup"
FAIL_CREATE = FIXTURES / "fail_create"
FAIL_AFTER_HELPER = FIXTURES / "fail_after_helper"
SHOWN_THEN_CLOSED = FIXTURES / "shown_then_closed"


def _child_environment(renderer: Path) -> dict[str, str]:
    environment = {**os.environ, "PYTHONPATH": str(renderer)}
    environment.pop("PYTEST_CURRENT_TEST", None)
    return environment


def _run_harness(renderer: Path, tmp_path: Path, mode: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(FIXTURES / "harness.py"),
            "--storage-dir",
            str(tmp_path / mode),
            "--mode",
            mode,
        ],
        capture_output=True,
        check=False,
        env=_child_environment(renderer),
        text=True,
        timeout=15,
    )


class _Application:
    native = SimpleNamespace(main_window=None)
    is_stopping = False
    is_stopped = False

    def __init__(self) -> None:
        self.native = SimpleNamespace(
            main_window=None,
            window_args={},
            settings={},
            start_args={},
        )

    def shutdown(self) -> None:
        self.is_stopping = True


def _controlled_readiness(stop_at: str, stop: Callable[[], None]) -> Callable[[], Awaitable[int]]:
    attempts = 0

    async def get() -> int:
        nonlocal attempts
        attempts += 1
        if stop_at == "before_ready":
            stop()
        elif stop_at == "during_wait" and attempts == 1:
            asyncio.get_running_loop().call_soon(stop)
        if stop_at == "before_dispatch":
            return 200
        raise aiohttp.ClientConnectionError()

    return get


class _StoppingAtDispatch(_Application):
    def __init__(self, stop_at: str) -> None:
        self.stop_at = stop_at

    @property
    def is_stopping(self) -> bool:
        return self.stop_at == "before_dispatch"


class _StartFails:
    def start(self) -> None:
        raise OSError("controlled process creation failure")


class _LiveProcess:
    def __init__(self) -> None:
        self.checked = threading.Event()

    def start(self) -> None:
        return None

    def is_alive(self) -> bool:
        self.checked.set()
        return True


def _configure_launcher_protocol(
    monkeypatch: pytest.MonkeyPatch,
    process: object,
) -> None:
    monkeypatch.setattr(gui_native.native, "create_queues", lambda: None)
    monkeypatch.setattr(gui_native.native_mode, "_split_picklable", lambda value: (value, []))
    monkeypatch.setattr(
        gui_native.native,
        "SPAWN_CONTEXT",
        SimpleNamespace(Event=threading.Event, Process=lambda **_kwargs: process),
    )
    monkeypatch.setattr(gui_native, "native_event_manager", SimpleNamespace(start=lambda: None))


def _activate(launcher: ScopedNativeLauncher) -> None:
    launcher.activate(
        "http",
        "127.0.0.1",
        8765,
        "Discord Ferry",
        800,
        600,
        False,
        False,
    )


@pytest.mark.parametrize("renderer", [FAIL_CREATE, FAIL_AFTER_HELPER])
def test_before_shown_child_failure_keeps_http_available(renderer: Path, tmp_path: Path) -> None:
    """A real Ferry launch keeps serving after either pre-shown child failure."""
    result = _run_harness(renderer, tmp_path, "recover")

    assert result.returncode == 0, result.stderr
    assert "NATIVE_STARTUP_FAILURE" in result.stdout
    assert "NATIVE_FAILURE_OBSERVED" in result.stdout
    assert "HOST_GUARD_LOOPBACK_ONLY" in result.stdout
    assert "HTTP_READY status=200" in result.stdout
    assert result.stdout.count("BROWSER_DISPATCH") == 1
    assert "PROXY_CLEARED FOLDER_CONTROL_DISABLED" in result.stdout
    assert "Native startup failed before Ferry could show its window. " in result.stderr
    assert "Ferry remains available at http://127.0.0.1:" in result.stderr


def test_delayed_shown_event_still_uses_normal_shutdown(tmp_path: Path) -> None:
    result = _run_harness(SHOWN_THEN_CLOSED, tmp_path, "close")

    assert result.returncode == 0, result.stderr
    assert "NATIVE_SHOWN" in result.stdout
    assert "NORMAL_CLOSE_CLEANUP" in result.stdout
    assert "BROWSER_DISPATCH" not in result.stdout


def test_explicit_shutdown_cleans_up_without_browser_fallback(tmp_path: Path) -> None:
    result = _run_harness(SHOWN_THEN_CLOSED, tmp_path, "explicit-shutdown")

    assert result.returncode == 0, result.stderr
    assert "HEALTHY_CHILD_SHOWN" in result.stdout
    assert "HTTP_READY status=200" in result.stdout
    assert "EXPLICIT_SHUTDOWN_CLEANUP" in result.stdout
    assert "BROWSER_DISPATCH" not in result.stdout


async def test_readiness_dispatches_once_after_http_is_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _Application()
    coordinator = BrowserFallback("http://127.0.0.1:8765", application, lambda _url: True)
    responses: list[int | BaseException] = [
        aiohttp.ClientConnectionError(),
        TimeoutError(),
        200,
    ]
    opened: list[str] = []

    async def get() -> int:
        result = responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(coordinator, "_get", get)
    monkeypatch.setattr(coordinator, "open_browser", lambda url: opened.append(url) or True)
    coordinator.on_startup()
    coordinator.request("native startup failure")

    await coordinator.wait_for_idle()

    assert opened == ["http://127.0.0.1:8765"]
    assert coordinator.browser_dispatches == 1


async def test_readiness_uses_the_trusted_factory_and_connects_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readiness keeps loopback direct while using Ferry's session factory."""
    factory_calls: list[dict[str, object]] = []
    real_new_session = http.new_session

    def observing_factory(**kwargs: object) -> aiohttp.ClientSession:
        factory_calls.append(kwargs)
        return real_new_session(**kwargs)

    def proxy_resolution_must_not_run(_url: object) -> None:
        pytest.fail("the local readiness request must not resolve a proxy")

    monkeypatch.setattr(http, "new_session", observing_factory)
    monkeypatch.setattr(
        http._FerryRequest,
        "_resolve_or_direct",
        staticmethod(proxy_resolution_must_not_run),
    )
    async with local_cdn() as cdn:
        coordinator = BrowserFallback(cdn.url("/target"), _Application(), lambda _url: True)

        assert await coordinator._get() == 200

    assert factory_calls == [
        {
            "timeout": aiohttp.ClientTimeout(total=0.2),
            "request_class": aiohttp.ClientRequest,
        }
    ]


@pytest.mark.parametrize("stop_at", ["before_ready", "during_wait", "before_dispatch"])
async def test_shutdown_suppresses_browser_dispatch(
    stop_at: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator = BrowserFallback(
        "http://127.0.0.1:8765", _StoppingAtDispatch(stop_at), lambda _url: True
    )
    monkeypatch.setattr(coordinator, "_get", _controlled_readiness(stop_at, coordinator.stop))
    coordinator.on_startup()
    coordinator.request("native startup failure")

    await coordinator.wait_for_idle()

    assert coordinator.browser_dispatches == 0


async def test_pending_readiness_cancellation_does_not_cancel_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = BrowserFallback("http://127.0.0.1:8765", _Application(), lambda _url: True)

    async def ready() -> int:
        return 200

    monkeypatch.setattr(coordinator, "_get", ready)
    coordinator.on_startup()
    coordinator.request("native startup failure")
    coordinator._create_readiness_task()
    task = coordinator._task
    assert task is not None

    coordinator.stop()
    await coordinator.wait_for_idle()

    assert task.cancelled()
    assert coordinator.browser_dispatches == 0
    assert asyncio.current_task() is not None
    assert asyncio.current_task().cancelling() == 0


async def test_wait_for_idle_preserves_explicit_caller_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only Ferry's stopping cancellation is absorbed, never the awaiting caller's."""
    coordinator = BrowserFallback("http://127.0.0.1:8765", _Application(), lambda _url: True)
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()

    async def waiting_get() -> int:
        probe_started.set()
        await release_probe.wait()
        return 200

    monkeypatch.setattr(coordinator, "_get", waiting_get)
    coordinator.on_startup()
    coordinator.request("native startup failure")
    await probe_started.wait()
    readiness = coordinator._task
    assert readiness is not None
    waiter = asyncio.create_task(coordinator.wait_for_idle())
    await asyncio.sleep(0)
    waiter.cancel()

    try:
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert readiness.cancelled() is False
    finally:
        coordinator.stop()
        release_probe.set()
        await coordinator.wait_for_idle()

    assert readiness.done()


@pytest.mark.parametrize("browser", [lambda _url: False, OSError("browser unavailable")])
async def test_browser_failure_keeps_the_once_only_claim(
    browser: Callable[[str], bool] | OSError,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    opened: list[str] = []

    def open_browser(url: str) -> bool:
        opened.append(url)
        if isinstance(browser, OSError):
            raise browser
        return browser(url)

    coordinator = BrowserFallback("http://127.0.0.1:8765", _Application(), open_browser)

    async def ready() -> int:
        return 200

    monkeypatch.setattr(coordinator, "_get", ready)
    coordinator.on_startup()
    coordinator.request("native startup failure")
    coordinator.request("renderer failure")
    await coordinator.wait_for_idle()

    assert opened == ["http://127.0.0.1:8765"]
    assert coordinator.browser_dispatches == 1
    assert "Ferry remains available at http://127.0.0.1:8765" in caplog.text


def test_concurrent_fallback_requests_claim_one_browser() -> None:
    application = _Application()
    opened: list[str] = []
    coordinator = BrowserFallback(
        "http://127.0.0.1:8765", application, lambda url: opened.append(url) or True
    )
    barrier = threading.Barrier(2)

    def dispatch() -> None:
        barrier.wait()
        coordinator._open_if_running()

    threads = [threading.Thread(target=dispatch), threading.Thread(target=dispatch)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert opened == ["http://127.0.0.1:8765"]
    assert coordinator.browser_dispatches == 1


def test_unknown_launcher_shape_uses_browser_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gui_native.native_package, "activate", lambda host: None)
    launcher = ScopedNativeLauncher(
        _Application(),
        "http://127.0.0.1:8765",
        BrowserFallback("http://127.0.0.1:8765", _Application(), lambda _url: True),
    )

    assert launcher.supported is False
    assert launcher.native_requested is False


def test_startup_clears_proxy_after_native_failure() -> None:
    application = _Application()
    application.native.main_window = object()
    launcher = ScopedNativeLauncher(
        application,
        "http://127.0.0.1:8765",
        BrowserFallback("http://127.0.0.1:8765", application, lambda _url: True),
    )
    launcher.native_controls_available = False

    launcher.on_startup()

    assert application.native.main_window is None


def test_process_start_error_clears_proxy_and_requests_recovery(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A parent-side process failure follows the same before-shown recovery path."""
    application = _Application()
    application.native.main_window = object()
    fallback = BrowserFallback("http://127.0.0.1:8765", application, lambda _url: True)
    launcher = ScopedNativeLauncher(application, "http://127.0.0.1:8765", fallback)
    _configure_launcher_protocol(monkeypatch, _StartFails())

    _activate(launcher)
    launcher.on_startup()

    assert launcher.native_controls_available is False
    assert application.native.main_window is None
    assert fallback.request("repeat native failure") is False
    assert "Native startup failed before Ferry could create its window" in caplog.text


def test_live_child_is_not_classified_as_a_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child that is still alive has no deadline that can trigger fallback."""
    application = _Application()
    fallback = BrowserFallback("http://127.0.0.1:8765", application, lambda _url: True)
    launcher = ScopedNativeLauncher(application, "http://127.0.0.1:8765", fallback)
    process = _LiveProcess()
    _configure_launcher_protocol(monkeypatch, process)

    _activate(launcher)

    assert process.checked.wait(timeout=1)
    assert launcher.fallback_requests == []
    launcher.on_shutdown()
    launcher.close()


@pytest.mark.parametrize("raises", [False, True])
def test_gui_restores_scoped_activation_and_preserves_core_exit_identity(
    raises: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real GUI wiring scopes only activation, including a ui.run error."""
    application = _Application()
    fallback = BrowserFallback("http://127.0.0.1:8765", application, lambda _url: True)
    launcher = ScopedNativeLauncher(application, "http://127.0.0.1:8765", fallback)
    monkeypatch.setattr(launcher, "supported", True)
    monkeypatch.setattr(launcher, "native_requested", True)
    original_activate = gui_native.native_package.activate
    original_stop_and_exit = core.stop_and_exit
    observed: list[object] = []

    monkeypatch.setattr(gui, "_native_enabled", lambda: True)
    monkeypatch.setattr(gui, "_install_host_guard", lambda: None)
    monkeypatch.setattr(gui, "_install_renderer_fallback", lambda _fallback: None)
    monkeypatch.setattr(gui, "_teardown_native_window", lambda: None)
    monkeypatch.setattr(gui, "_teardown_native_window_async", lambda: None)
    monkeypatch.setattr(gui.app, "on_startup", lambda _callback: None)
    monkeypatch.setattr(gui.app, "on_shutdown", lambda _callback: None)
    monkeypatch.setattr(gui, "BrowserFallback", lambda *_args: fallback)
    monkeypatch.setattr(gui, "ScopedNativeLauncher", lambda *_args: launcher)

    def run(**_kwargs: object) -> None:
        observed.append(gui_native.native_package.activate)
        if raises:
            raise RuntimeError("controlled ui.run startup error")

    monkeypatch.setattr(gui.ui, "run", run)
    if raises:
        with pytest.raises(RuntimeError, match="controlled ui.run startup error"):
            gui._run_gui()
    else:
        gui._run_gui()

    assert observed == [launcher.activate]
    assert gui_native.native_package.activate is original_activate
    assert core.stop_and_exit is original_stop_and_exit


def test_unsupported_protocol_runs_guarded_browser_mode_without_native_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrecognized private protocol preserves HTTP browser mode, not native calls."""
    application = _Application()
    requests: list[str] = []

    class _Fallback:
        def request(self, reason: str) -> bool:
            requests.append(reason)
            return len(requests) == 1

        def stop(self) -> None:
            return None

        def on_startup(self) -> None:
            return None

        def on_shutdown(self) -> None:
            return None

    fallback = _Fallback()
    launcher = ScopedNativeLauncher(application, "http://127.0.0.1:8765", fallback)  # type: ignore[arg-type]
    monkeypatch.setattr(launcher, "supported", False)
    monkeypatch.setattr(launcher, "native_requested", False)
    captured: dict[str, object] = {}

    monkeypatch.setattr(gui, "_native_enabled", lambda: True)
    monkeypatch.setattr(gui, "_install_host_guard", lambda: None)
    monkeypatch.setattr(gui, "_install_renderer_fallback", lambda _fallback: None)
    monkeypatch.setattr(gui, "_teardown_native_window", lambda: None)
    monkeypatch.setattr(gui, "_teardown_native_window_async", lambda: None)
    monkeypatch.setattr(gui.app, "on_startup", lambda _callback: None)
    monkeypatch.setattr(gui.app, "on_shutdown", lambda _callback: None)
    monkeypatch.setattr(gui, "BrowserFallback", lambda *_args: fallback)
    monkeypatch.setattr(gui, "ScopedNativeLauncher", lambda *_args: launcher)
    monkeypatch.setattr(
        gui_native.native_mode,
        "_open_window",
        lambda *_args: pytest.fail("child started"),
    )
    monkeypatch.setattr(gui.ui, "run", lambda **kwargs: captured.update(kwargs))

    gui._run_gui()

    assert captured["native"] is False
    assert captured["show"] is False
    assert requests == ["Native startup compatibility mode is unavailable"]
    assert gui._native_controls_available() is True


def test_renderer_and_native_monitor_share_one_request_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real renderer endpoint and monitor-side request share the same gate."""
    application = _Application()
    opened: list[str] = []
    fallback = BrowserFallback(
        "http://127.0.0.1:8765", application, lambda url: opened.append(url) or True
    )
    handlers: list[Callable[[], dict[str, str]]] = []

    def get(_path: str) -> Callable[[Callable[[], dict[str, str]]], Callable[[], dict[str, str]]]:
        return lambda handler: handlers.append(handler) or handler

    monkeypatch.setattr(gui.app, "get", get)
    monkeypatch.setattr(gui.ui, "add_body_html", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gui.ui, "add_head_html", lambda *_args, **_kwargs: None)
    gui._install_renderer_fallback(fallback)
    assert len(handlers) == 1
    barrier = threading.Barrier(2)

    responses: list[dict[str, str] | None] = []

    def native_request() -> None:
        barrier.wait()
        responses.append(
            {"status": "ok"}
            if fallback.request("native startup failure")
            else {"status": "already-opened"}
        )

    def renderer_request() -> None:
        barrier.wait()
        responses.append(handlers[0]())

    threads = [threading.Thread(target=native_request), threading.Thread(target=renderer_request)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    fallback._open_if_running()

    assert sorted(response["status"] for response in responses if response is not None) == [
        "already-opened",
        "ok",
    ]
    assert opened == ["http://127.0.0.1:8765"]
    assert fallback.browser_dispatches == 1
