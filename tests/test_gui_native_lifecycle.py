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

from typing import Any

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
