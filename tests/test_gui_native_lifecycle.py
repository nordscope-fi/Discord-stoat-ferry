"""Tests for the native-window lifecycle in src/discord_ferry/gui.py.

Background: NiceGUI's native mode runs pywebview in a ``daemon=True`` multiprocessing
child. SIGTERM kills Python without running ``atexit``, so multiprocessing's
``_exit_function`` -- the only thing that terminates daemon children -- never runs and
the window is orphaned, showing "Connection lost. Trying to reconnect..." against a
server that no longer exists. Worse, if a child ever outlives the interpreter's exit,
``_exit_function`` joins it with NO timeout (multiprocessing/util.py:360), hanging the
process while it still holds port 8765.
"""

from __future__ import annotations

import ast
import inspect
from typing import Any
from unittest.mock import MagicMock

import pytest

from discord_ferry import gui
from discord_ferry.gui import _terminate_children


class FakeChild:
    """A ``multiprocessing.Process`` stand-in with a scripted liveness sequence.

    ``is_alive`` pops one entry per call until a single entry remains, which then
    sticks -- so ``[True]`` models an unkillable child and ``[True, False]`` a child
    that dies at the first rung.
    """

    def __init__(self, pid: int = 1234, alive_sequence: list[bool] | None = None) -> None:
        self.pid = pid
        self.daemon = True
        self._alive = list(alive_sequence or [True])
        self.terminate_calls = 0
        self.kill_calls = 0
        self.join_timeouts: list[float | None] = []

    def is_alive(self) -> bool:
        return self._alive[0] if len(self._alive) == 1 else self._alive.pop(0)

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1

    def join(self, timeout: float | None = None) -> None:
        self.join_timeouts.append(timeout)


class FakeWindow:
    """Records every attribute access, so "was it touched at all" is assertable."""

    def __init__(self, raises: bool = False) -> None:
        self.touched: list[str] = []
        self._raises = raises

    def __getattr__(self, name: str) -> Any:
        self.touched.append(name)

        def _call(*_args: Any, **_kwargs: Any) -> None:
            if self._raises:
                raise RuntimeError("method_queue is closed")

        return _call


class TestTeardownLadder:
    def test_destroy_alone_is_enough(self) -> None:
        child = FakeChild(alive_sequence=[True, False])
        window = FakeWindow()
        _terminate_children([child], window)
        assert window.touched == ["destroy"]
        assert child.terminate_calls == 0
        assert child.kill_calls == 0

    def test_escalates_to_terminate(self) -> None:
        child = FakeChild(alive_sequence=[True, True, False])
        _terminate_children([child], FakeWindow())
        assert child.terminate_calls == 1
        assert child.kill_calls == 0

    def test_escalates_to_kill(self) -> None:
        child = FakeChild(alive_sequence=[True, True, True, False])
        _terminate_children([child], FakeWindow())
        assert child.terminate_calls == 1
        assert child.kill_calls == 1

    def test_unkillable_child_does_not_hang(self) -> None:
        """A child that outlives the ladder must not turn into an unbounded wait."""
        child = FakeChild(alive_sequence=[True])
        _terminate_children([child], FakeWindow())
        assert child.kill_calls == 1
        assert child.join_timeouts
        assert all(t is not None for t in child.join_timeouts), "every join must be bounded"

    def test_no_children_leaves_the_window_untouched(self) -> None:
        """The precondition.

        NiceGUI's check_shutdown closes the proxy's method queues as soon as the server
        stops, so an unconditional destroy() would raise -- and log a traceback -- on
        every healthy shutdown.
        """
        window = FakeWindow()
        _terminate_children([FakeChild(alive_sequence=[False])], window)
        assert window.touched == []

    def test_second_invocation_is_a_no_op(self) -> None:
        child = FakeChild(alive_sequence=[True, False])
        window = FakeWindow()
        _terminate_children([child], window)
        window.touched.clear()
        _terminate_children([child], window)
        assert window.touched == []
        assert child.terminate_calls == 0

    def test_raising_window_does_not_abort_the_ladder(self) -> None:
        child = FakeChild(alive_sequence=[True, True, False])
        _terminate_children([child], FakeWindow(raises=True))
        assert child.terminate_calls == 1

    def test_joins_are_bounded(self) -> None:
        child = FakeChild(alive_sequence=[True, True, True, False])
        _terminate_children([child], FakeWindow(), join_timeout=0.25)
        assert child.join_timeouts
        assert all(t == 0.25 for t in child.join_timeouts)

    def test_window_is_optional(self) -> None:
        """Browser mode has no window proxy; the ladder still tears children down."""
        child = FakeChild(alive_sequence=[True, True, False])
        _terminate_children([child], None)
        assert child.terminate_calls == 1

    def test_teardown_never_raises_into_the_shutdown_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both call sites are shutdown paths, so nothing may escape.

        On ``app.on_shutdown`` an exception would propagate into uvicorn's lifespan
        shutdown and skip NiceGUI's remaining handlers; in ``_run_gui``'s ``finally:``
        it would mask whatever exception was already unwinding. The inner ladder only
        guards the calls it makes ON the child -- is_alive()/join()/active_children()
        are guarded here.
        """

        class ExplodingChild(FakeChild):
            def is_alive(self) -> bool:
                raise OSError("process table went away")

        monkeypatch.setattr(gui.multiprocessing, "active_children", lambda: [ExplodingChild()])
        gui._teardown_native_window()  # must not raise

    def test_production_wiring(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pins WHAT the production entry point feeds the ladder.

        The ladder takes its collaborators as arguments for testability, which means a
        mis-wired call site would leave every ladder test green while the real teardown
        operated on the wrong objects.
        """
        seen: dict[str, Any] = {}
        monkeypatch.setattr(gui.multiprocessing, "active_children", lambda: ["child"])
        monkeypatch.setattr(gui.app.native, "main_window", "window")
        monkeypatch.setattr(
            gui,
            "_terminate_children",
            lambda children, window: seen.update(children=children, window=window),
        )
        gui._teardown_native_window()
        assert seen == {"children": ["child"], "window": "window"}


class TestMainLifecycle:
    def test_freeze_support_is_the_first_statement_of_main(self) -> None:
        """PyInstaller's rthook only REBINDS multiprocessing.freeze_support; the
        diversion to spawn_main happens when it is CALLED, and NiceGUI calls it deep
        inside native_mode.activate(). So in the frozen app the spawned window child
        runs every statement of main() above ui.run(). Calling freeze_support first
        makes the child divert immediately.

        No source test can prove the child actually diverts -- that rides on the
        built-bundle check. This pins placement only.
        """
        tree = ast.parse(inspect.getsource(gui.main))
        func = tree.body[0]
        assert isinstance(func, ast.FunctionDef)
        body = [
            node
            for node in func.body
            if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
        ]
        first = body[0]
        assert isinstance(first, ast.Expr), "first statement of main() must be a call"
        call = first.value
        assert isinstance(call, ast.Call)
        assert ast.unparse(call.func) == "multiprocessing.freeze_support"

    def test_finally_runs_the_sync_teardown_on_normal_return(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The anti-inertness test.

        An un-awaited coroutine in that finally: would be a silent no-op on the one
        path where nothing else cleans up (uvicorn's force_exit, or a boot failure).
        """
        calls: list[str] = []
        monkeypatch.setattr(gui, "_HAS_WEBVIEW", True)
        monkeypatch.setattr(gui.ui, "run", lambda **_kwargs: calls.append("ran"))
        monkeypatch.setattr(gui, "_teardown_native_window", lambda: calls.append("torn down"))
        monkeypatch.setattr(gui.app, "on_shutdown", MagicMock())
        gui._run_gui()
        assert calls == ["ran", "torn down"]

    def test_finally_runs_the_sync_teardown_when_ui_run_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        def _boom(**_kwargs: object) -> None:
            raise RuntimeError("server exploded during startup")

        monkeypatch.setattr(gui, "_HAS_WEBVIEW", True)
        monkeypatch.setattr(gui.ui, "run", _boom)
        monkeypatch.setattr(gui, "_teardown_native_window", lambda: calls.append("torn down"))
        monkeypatch.setattr(gui.app, "on_shutdown", MagicMock())
        with pytest.raises(RuntimeError):
            gui._run_gui()
        assert calls == ["torn down"]

    def test_shutdown_handler_registered_in_native_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        on_shutdown = MagicMock()
        monkeypatch.setattr(gui, "_HAS_WEBVIEW", True)
        monkeypatch.setattr(gui.ui, "run", lambda **_kwargs: None)
        monkeypatch.setattr(gui, "_teardown_native_window", lambda: None)
        monkeypatch.setattr(gui.app, "on_shutdown", on_shutdown)
        gui._run_gui()
        on_shutdown.assert_called_once_with(gui._teardown_native_window_async)

    def test_no_handler_and_no_teardown_in_browser_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        on_shutdown = MagicMock()
        calls: list[str] = []
        monkeypatch.setattr(gui, "_HAS_WEBVIEW", False)
        monkeypatch.setattr(gui.ui, "run", lambda **_kwargs: None)
        monkeypatch.setattr(gui, "_teardown_native_window", lambda: calls.append("torn down"))
        monkeypatch.setattr(gui.app, "on_shutdown", on_shutdown)
        gui._run_gui()
        on_shutdown.assert_not_called()
        assert calls == []

    def test_keyboard_interrupt_exits_130(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """main()'s contract: an escaping KeyboardInterrupt becomes exit 130.

        This is a defensive path, NOT the observed end-to-end behaviour. Measured on
        both a source run and the built bundle: a real SIGINT is consumed by uvicorn's
        handle_exit, the graceful shutdown runs, and the re-raised signal never surfaces
        as a KeyboardInterrupt here -- the process exits 0, cleanly, with no orphans and
        no traceback. The handler still matters for an interrupt during the boot window,
        before uvicorn installs its handlers.
        """

        def _interrupt() -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(gui, "_run_gui", _interrupt)
        with pytest.raises(SystemExit) as excinfo:
            gui.main()
        assert excinfo.value.code == 130

    def test_normal_return_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gui, "_run_gui", lambda: None)
        gui.main()

    def test_non_frozen_main_never_dispatches_to_cli(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The sys.frozen gate, pinned so it cannot be removed by accident.

        Without it, `uv run pytest --ignore=...` leaves sys.argv[1:] non-empty
        for the whole test process, and test_keyboard_interrupt_exits_130 and
        test_normal_return_does_not_raise would route into Click instead of
        _run_gui(). Both are rescued by this gate, not by luck.
        """
        monkeypatch.delattr("sys.frozen", raising=False)
        monkeypatch.setattr("sys.argv", ["ferry-gui", "--ignore=tests/x.py"])
        called: list[str] = []
        monkeypatch.setattr(gui, "_run_gui", lambda: called.append("gui"))
        monkeypatch.setattr(gui, "configure_logging", lambda: None)

        gui.main()

        assert called == ["gui"], "non-frozen main() must go to the GUI"

    def test_ui_run_is_called_with_tuned_timings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """NiceGUI derives the socket.io heartbeat from reconnect_timeout; its 3.0
        default gives the browser only ~6s before the "Connection lost" banner."""
        captured: dict[str, object] = {}
        monkeypatch.setattr(gui, "_HAS_WEBVIEW", False)
        monkeypatch.setattr(gui.ui, "run", lambda **kwargs: captured.update(kwargs))
        monkeypatch.setattr(gui, "_teardown_native_window", lambda: None)
        gui._run_gui()
        assert captured["reconnect_timeout"] == 10.0
        assert captured["timeout_graceful_shutdown"] == 1


class TestFolderPicker:
    """The picker has never worked in the packaged app.

    gui.py called ``webview.windows[0]`` in the SERVER process, but
    ``webview.create_window()`` only ever runs in the child (native_mode._open_window),
    so the list is permanently empty and every click raised IndexError straight into
    the "requires native mode" toast.
    """

    async def test_returns_the_chosen_path(self) -> None:
        class Window:
            async def create_file_dialog(self, _dialog_type: int) -> tuple[str, ...]:
                return ("/Users/pete/export",)

        assert await gui._pick_folder(Window()) == "/Users/pete/export"

    async def test_cancelled_dialog_returns_none(self) -> None:
        class Window:
            async def create_file_dialog(self, _dialog_type: int) -> tuple[str, ...] | None:
                return None

        assert await gui._pick_folder(Window()) is None

    async def test_empty_tuple_returns_none(self) -> None:
        """Guards an IndexError regression of exactly the kind being fixed."""

        class Window:
            async def create_file_dialog(self, _dialog_type: int) -> tuple[str, ...]:
                return ()

        assert await gui._pick_folder(Window()) is None

    async def test_raising_window_returns_none(self) -> None:
        class Window:
            async def create_file_dialog(self, _dialog_type: int) -> tuple[str, ...]:
                raise RuntimeError("window is gone")

        assert await gui._pick_folder(Window()) is None

    async def test_browser_mode_returns_none(self) -> None:
        assert await gui._pick_folder(None) is None

    def test_dialog_type_prefers_the_modern_enum(self) -> None:
        """FOLDER_DIALOG still resolves to 20 on pywebview 6.2.1 but emits a
        DeprecationWarning; FileDialog.FOLDER is the supported spelling."""
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            assert gui._folder_dialog_type() == 20
