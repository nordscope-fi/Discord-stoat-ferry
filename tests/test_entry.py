"""Entry-point dispatch for the frozen binary (issue #123, symptom 3)."""

from __future__ import annotations

import pytest

from discord_ferry.core import entry


def test_no_args_never_runs_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Double-clicking the exe must still open the GUI."""
    monkeypatch.setattr("sys.frozen", True, raising=False)
    assert entry.should_run_cli(["Ferry.exe"]) is False


def test_frozen_with_args_runs_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr(entry, "acquire_console", lambda: True)
    assert entry.should_run_cli(["Ferry.exe", "--help"]) is True


def test_non_frozen_never_runs_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """`gui.main()` is also the `ferry-gui` console-script entry point.

    Under `uv run pytest --ignore=...` sys.argv[1:] is non-empty for the whole
    process. Without the frozen gate that would route two existing tests in
    test_gui_native_lifecycle.py into Click instead of _run_gui().
    """
    monkeypatch.delattr("sys.frozen", raising=False)
    monkeypatch.setattr(entry, "acquire_console", lambda: True)
    assert entry.should_run_cli(["ferry-gui", "--ignore=tests/x.py"]) is False


def test_multiprocessing_fork_never_runs_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defence in depth for the pywebview spawn child.

    freeze_support() exits before this is reached in production, so the guard is
    unreachable there. But the ordering has no unit-level backstop, and a future
    refactor that moves statements would route the window child into the CLI and
    break native mode.
    """
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr(entry, "acquire_console", lambda: True)
    argv = ["Ferry.exe", "--multiprocessing-fork", "parent_pid=123"]
    assert entry.should_run_cli(argv) is False


def test_no_console_falls_back_to_gui(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CLI whose output goes nowhere is the bug being fixed, not the fix."""
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr(entry, "acquire_console", lambda: False)
    assert entry.should_run_cli(["Ferry.exe", "--help"]) is False


def test_usable_stdout_needs_no_attach(monkeypatch: pytest.MonkeyPatch) -> None:
    """Step 1 of the ladder, and it must come FIRST.

    Redirection (`Ferry.exe --help > out.txt`), pipes, and CI harnesses all
    supply real handles. Reaching for AttachConsole before checking would be
    wrong in exactly the environment the CI verification depends on.
    """
    monkeypatch.setattr("sys.platform", "win32")
    attach_calls: list[int] = []
    monkeypatch.setattr(entry, "_attach_parent_console", lambda: attach_calls.append(1) or True)
    assert entry.acquire_console() is True
    assert attach_calls == [], "must not attach when stdout already works"


def test_non_windows_is_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    """A binary launched from a POSIX shell already has working streams."""
    monkeypatch.setattr("sys.platform", "darwin")
    assert entry.acquire_console() is True


def test_windows_without_stdout_attaches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr("sys.stdout", None)
    monkeypatch.setattr(entry, "_attach_parent_console", lambda: True)
    assert entry.acquire_console() is True


def test_windows_attach_failure_reports_no_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dragging a folder onto the exe: Explorer gives neither stdout nor a console."""
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr("sys.stdout", None)
    monkeypatch.setattr(entry, "_attach_parent_console", lambda: False)
    assert entry.acquire_console() is False
