"""Entry-point dispatch for the frozen binary (issue #123, symptom 3)."""

from __future__ import annotations

import ctypes
import sys

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


def test_usable_stdout_but_unusable_stdin_attaches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Step 1 requires stdin too, not stdout alone.

    A caller could redirect stdout without redirecting stdin. click.confirm()
    reads stdin, so a usable stdout with no usable stdin still needs the
    CONIN$ reopen from step 2, exactly like a fully unusable console.
    """
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr("sys.stdin", None)
    attach_calls: list[int] = []
    monkeypatch.setattr(entry, "_attach_parent_console", lambda: attach_calls.append(1) or True)
    assert entry.acquire_console() is True
    assert attach_calls == [1], "must attach when stdin is unusable even if stdout works"


def test_non_windows_is_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even without a usable stdout, a non-Windows platform returns True.

    sys.stdout is forced to None so the first branch of acquire_console()
    cannot short-circuit the result. That isolates the platform check this
    test is meant to cover: there is nothing left to try on POSIX, so the
    function returns True.
    """
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("sys.stdout", None)
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


def test_attach_partial_open_failure_restores_original_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure partway through the three CONOUT$/CONIN$ opens must not leave
    sys.stdout, sys.stderr, or sys.stdin reassigned.

    Regression test: the three opens used to share one try/except with no
    rollback. If the first open (stdout) succeeded and the second (stderr)
    raised OSError, the function returned False but sys.stdout was left
    pointing at the console handle from the first open. A caller that falls
    back to gui.main() on a False return would then run with sys.stdout
    mutated. This forces AttachConsole to report success so the open() calls
    are reached, then fails the second open to reproduce that sequence.
    """

    class _FakeKernel32:
        @staticmethod
        def AttachConsole(_pid: int) -> bool:  # noqa: N802 -- mirrors the real Win32 API name
            return True

    class _FakeWinDLL:
        kernel32 = _FakeKernel32()

    monkeypatch.setattr(ctypes, "windll", _FakeWinDLL(), raising=False)

    original_stdout = object()
    original_stderr = object()
    original_stdin = object()
    monkeypatch.setattr("sys.stdout", original_stdout)
    monkeypatch.setattr("sys.stderr", original_stderr)
    monkeypatch.setattr("sys.stdin", original_stdin)

    open_calls = 0

    def fake_open(*args: object, **kwargs: object) -> object:
        nonlocal open_calls
        open_calls += 1
        if open_calls == 1:
            return object()
        raise OSError("simulated failure opening CONOUT$/CONIN$")

    monkeypatch.setattr("builtins.open", fake_open)

    assert entry._attach_parent_console() is False
    assert open_calls == 2, "must fail on the second open to match the scenario under test"
    assert sys.stdout is original_stdout
    assert sys.stderr is original_stderr
    assert sys.stdin is original_stdin


def test_run_cli_never_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_cli() exits the process. It never returns, and it never raises
    anything other than SystemExit.

    Command.main() is typed -> NoReturn in standalone mode and every path ends
    in sys.exit(). Asserting `== 0` here would assert on a value that is never
    produced.

    This does not prove run_cli() calls the bound cli_main.main(). Click's
    BaseCommand.__call__ just forwards to self.main(), so both raise
    SystemExit(0) identically at runtime. No test built on
    pytest.raises(SystemExit) can tell a bound call apart from a bare
    cli_main() call. That distinction is enforced by mypy's --warn-no-return,
    not by this test.

    Uses --help, not --version: Task 3 adds --version and has not landed on
    this branch yet, so that option does not exist here and would raise
    SystemExit(2) with "No such option". --help is a Click built-in present
    on every Command and gives the same exit-0 path.
    """
    monkeypatch.setattr("sys.argv", ["Ferry.exe", "--help"])
    with pytest.raises(SystemExit) as excinfo:
        entry.run_cli()
    assert excinfo.value.code == 0
