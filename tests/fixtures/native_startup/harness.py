"""Run Ferry's real GUI entry point against a controlled native renderer."""

from __future__ import annotations

import argparse
import multiprocessing
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


def _unused_loopback_port() -> int:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    return port


def _wait_for_http(url: str) -> int:
    for _ in range(100):
        try:
            with urllib.request.urlopen(url, timeout=0.2) as response:
                return response.status
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"loopback server did not answer at {url}")


def _assert_host_guard(url: str) -> None:
    assert _wait_for_http(url) == 200
    request = urllib.request.Request(url, headers={"Host": "example.invalid"})
    try:
        urllib.request.urlopen(request, timeout=0.2)
    except urllib.error.HTTPError as error:
        assert error.code == 400
        print("HOST_GUARD_LOOPBACK_ONLY", flush=True)
        return
    raise AssertionError("host guard accepted a non-loopback host")


def _wait_for_healthy_native_child(shown: threading.Event) -> None:
    for _ in range(100):
        if shown.is_set() and any(child.is_alive() for child in multiprocessing.active_children()):
            print("HEALTHY_CHILD_SHOWN", flush=True)
            return
        time.sleep(0.05)
    raise RuntimeError("native child was not alive and shown before explicit shutdown")


def _run_ferry(storage_dir: Path, mode: str, port: int) -> None:
    from nicegui import app
    from nicegui.native.event_manager import event_manager

    from discord_ferry import gui

    os.environ.pop("FERRY_NO_NATIVE", None)
    os.environ["FERRY_NATIVE_FIXTURE_MODE"] = mode
    gui._PORT = port
    gui._URL = f"http://127.0.0.1:{port}"
    gui._gui_data_dir = lambda: storage_dir
    gui._configure_gui_storage()

    def browser(url: str) -> bool:
        _assert_host_guard(url)
        launcher = gui._active_native_launcher
        assert launcher is not None
        assert app.native.main_window is None
        assert gui._native_controls_available() is False
        print("HTTP_READY status=200", flush=True)
        print("PROXY_CLEARED FOLDER_CONTROL_DISABLED", flush=True)
        print("BROWSER_DISPATCH", flush=True)
        app.shutdown()
        return True

    gui.webbrowser.open = browser
    print("RECOVERY_OBSERVER_ARMED", flush=True)

    if mode == "explicit-shutdown":
        shown = threading.Event()
        event_manager.on("shown", lambda _event: shown.set())

        def shutdown_after_ready() -> None:
            _assert_host_guard(gui._URL)
            _wait_for_healthy_native_child(shown)
            print("HTTP_READY status=200", flush=True)
            print("EXPLICIT_SHUTDOWN", flush=True)
            app.shutdown()

        threading.Thread(target=shutdown_after_ready, daemon=True).start()

    gui._run_gui()
    if mode == "close":
        print("NORMAL_CLOSE_CLEANUP", flush=True)
    elif mode == "explicit-shutdown":
        print("EXPLICIT_SHUTDOWN_CLEANUP", flush=True)
    print(f"FINAL_CLEANUP active_launcher={gui._active_native_launcher!r}", flush=True)


def _wait_for_marker(process: subprocess.Popen[object], marker: Path) -> None:
    for _ in range(100):
        if marker.exists():
            print("NATIVE_FAILURE_OBSERVED", flush=True)
            return
        if process.poll() is not None:
            raise RuntimeError("Ferry exited before its controlled renderer reported failure")
        time.sleep(0.05)
    raise RuntimeError("controlled renderer did not report startup failure")


def _supervise(storage_dir: Path, mode: str) -> None:
    port = _unused_loopback_port()
    marker = storage_dir / "native-failure"
    storage_dir.mkdir(parents=True, exist_ok=True)
    environment = {
        **os.environ,
        "FERRY_NATIVE_FAILURE_MARKER": str(marker),
    }
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--storage-dir",
        str(storage_dir),
        "--mode",
        mode,
        "--port",
        str(port),
        "--ferry-child",
    ]
    process = subprocess.Popen(command, env=environment)
    try:
        if mode == "recover":
            _wait_for_marker(process, marker)
        result = process.wait(timeout=10)
    except BaseException:
        process.kill()
        process.wait()
        raise
    if result != 0:
        raise SystemExit(result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--storage-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=("recover", "close", "explicit-shutdown"), required=True)
    parser.add_argument("--port", type=int)
    parser.add_argument("--ferry-child", action="store_true")
    arguments = parser.parse_args()
    if arguments.ferry_child:
        if arguments.port is None:
            parser.error("--ferry-child requires --port")
        _run_ferry(arguments.storage_dir, arguments.mode, arguments.port)
    else:
        _supervise(arguments.storage_dir, arguments.mode)
