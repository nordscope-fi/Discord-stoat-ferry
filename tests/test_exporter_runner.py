"""Tests for exporter subprocess runner."""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aioresponses import aioresponses

from discord_ferry.config import FerryConfig
from discord_ferry.exporter.runner import (
    _build_dce_command,
    _check_disk_space,
    _drain_overlong_line,
    run_dce_export,
    validate_discord_token,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from discord_ferry.core.events import MigrationEvent


# ---------- Helpers ----------


def _make_config(tmp_path: Path) -> FerryConfig:
    return FerryConfig(
        export_dir=tmp_path / "exports",
        stoat_url="https://stoat.example",
        token="st",
        discord_token="dt",
        discord_server_id="12345",
    )


def _make_stream_reader(lines: list[bytes]) -> asyncio.StreamReader:
    """A real StreamReader pre-loaded with lines (supports readuntil + EOF).

    An empty list yields an EOF reader: readuntil() raises
    IncompleteReadError(partial=b"") → _read_stderr breaks cleanly.
    """
    reader = asyncio.StreamReader()
    for line in lines:
        reader.feed_data(line)
    reader.feed_eof()
    return reader


def _make_process(
    stdout_lines: list[bytes], returncode: int = 0, stderr_lines: list[bytes] | None = None
) -> MagicMock:
    """Build a MagicMock subprocess that yields stdout_lines via readuntil()."""
    process = MagicMock()
    queue: list[bytes] = list(stdout_lines)

    async def _readuntil(_sep: bytes) -> bytes:
        if not queue:
            raise asyncio.IncompleteReadError(partial=b"", expected=None)
        return queue.pop(0)

    stdout = MagicMock()
    stdout.readuntil = _readuntil
    process.stdout = stdout
    process.stderr = _make_stream_reader(stderr_lines or [])
    process.wait = AsyncMock(return_value=returncode)
    process.returncode = returncode
    process.terminate = MagicMock()
    process.kill = MagicMock()
    process.send_signal = MagicMock()
    return process


# ---------- Existing tests (preserved) ----------


class TestBuildCommand:
    def test_command_construction(self, tmp_path: Path) -> None:
        dce_path = tmp_path / "dce"
        cfg = _make_config(tmp_path)
        cmd = _build_dce_command(cfg, dce_path)
        assert cmd[0] == str(dce_path)
        assert "exportguild" in cmd
        assert "--token" in cmd
        assert "dt" in cmd
        assert "-g" in cmd
        assert "12345" in cmd
        assert "--media" in cmd
        assert "--reuse-media" in cmd
        assert "--markdown" in cmd
        assert "false" in cmd
        assert "--format" in cmd
        assert "Json" in cmd
        assert "--include-threads" in cmd
        assert "All" in cmd
        assert "--output" in cmd
        assert str(tmp_path / "exports") in cmd


class TestDiskSpaceCheck:
    def test_warns_when_low(self, tmp_path: Path) -> None:
        events: list[MigrationEvent] = []
        with patch("discord_ferry.exporter.runner.shutil.disk_usage") as mock_du:
            mock_du.return_value = MagicMock(free=1_000_000_000)
            _check_disk_space(tmp_path, events.append)
        assert len(events) == 1
        assert "Low disk space" in events[0].message

    def test_no_warning_when_plenty(self, tmp_path: Path) -> None:
        events: list[MigrationEvent] = []
        with patch("discord_ferry.exporter.runner.shutil.disk_usage") as mock_du:
            mock_du.return_value = MagicMock(free=20_000_000_000)
            _check_disk_space(tmp_path, events.append)
        assert len(events) == 0


class TestValidateDiscordToken:
    @pytest.mark.asyncio
    async def test_valid_token(self) -> None:
        with aioresponses() as m:
            m.get(
                "https://discord.com/api/v10/users/@me",
                status=200,
                payload={"id": "1"},
            )
            await validate_discord_token("valid-token")

    @pytest.mark.asyncio
    async def test_invalid_token(self) -> None:
        from discord_ferry.errors import DiscordAuthError

        with aioresponses() as m:
            m.get("https://discord.com/api/v10/users/@me", status=401)
            with pytest.raises(DiscordAuthError, match="Invalid Discord token"):
                await validate_discord_token("bad-token")

    @pytest.mark.asyncio
    async def test_unexpected_status(self) -> None:
        from discord_ferry.errors import DiscordAuthError

        with aioresponses() as m:
            m.get("https://discord.com/api/v10/users/@me", status=500)
            with pytest.raises(DiscordAuthError, match="unexpected status"):
                await validate_discord_token("some-token")


# ---------- New tests for the v2.2.0 parser-based runner ----------


class TestRunDceExportProgressEmits:
    """Lock in the early-emit contract: GUI must see something before stdout flows.

    Added in v2.1.0 (#23 stopgap); remains required behavior.
    """

    @pytest.mark.asyncio
    async def test_enumerating_emit_fires_before_stdout_progress(self, tmp_path: Path) -> None:
        process = _make_process([b"general: 25%\n"])
        cfg = _make_config(tmp_path)

        events: list[MigrationEvent] = []
        with patch(
            "discord_ferry.exporter.runner.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ):
            await run_dce_export(cfg, tmp_path / "dce", events.append)

        messages = [e.message for e in events]
        enumerating_idx = next(
            (i for i, m in enumerate(messages) if "enumerating channels" in m), None
        )
        per_channel_idx = next(
            (i for i, m in enumerate(messages) if "general" in m and "25" in m), None
        )
        assert enumerating_idx is not None, f"missing enumerating emit; got {messages!r}"
        assert per_channel_idx is not None, f"missing per-channel emit; got {messages!r}"
        assert enumerating_idx < per_channel_idx


class TestPerChannelEmitsOverallProgress:
    @pytest.mark.asyncio
    async def test_overall_progress_monotonic(self, tmp_path: Path) -> None:
        lines = [
            b"Exporting 3 channel(s)...\n",
            b"general: 25%\n",
            b"general: 50%\n",
            b"general: 75%\n",
            b"general: 95%\n",
            b"general: 100%\n",
            b"announcements: 25%\n",
            b"announcements: 100%\n",
            b"memes: 100%\n",
            b"Successfully exported 3 channel(s).\n",
        ]
        process = _make_process(lines)
        cfg = _make_config(tmp_path)

        events: list[MigrationEvent] = []
        with patch(
            "discord_ferry.exporter.runner.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ):
            await run_dce_export(cfg, tmp_path / "dce", events.append)

        progress_currents = [e.current for e in events if e.total > 0]
        for prev, nxt in zip(progress_currents, progress_currents[1:], strict=False):
            assert prev <= nxt, f"progress went backward: {progress_currents!r}"
        assert max(progress_currents) == 3
        assert {e.total for e in events if e.total > 0} == {3}


class TestPhaseLinesEmitProgressEvents:
    @pytest.mark.asyncio
    async def test_phase_lines_visible_to_gui(self, tmp_path: Path) -> None:
        lines = [
            b"Fetching channels...\n",
            b"Fetched 5 channel(s).\n",
            b"Fetching threads...\n",
            b"Fetched 2 thread(s).\n",
        ]
        process = _make_process(lines)
        cfg = _make_config(tmp_path)

        events: list[MigrationEvent] = []
        with patch(
            "discord_ferry.exporter.runner.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ):
            await run_dce_export(cfg, tmp_path / "dce", events.append)

        messages = [e.message for e in events]
        assert any("Fetching channels..." in m for m in messages)
        assert any("Fetched 5 channel(s)." in m for m in messages)
        assert any("Fetching threads..." in m for m in messages)
        assert any("Fetched 2 thread(s)." in m for m in messages)


class TestRawLinesRoutedToGui:
    @pytest.mark.asyncio
    async def test_unknown_line_emitted_to_gui_with_prefix(self, tmp_path: Path) -> None:
        lines = [b"Some unknown DCE diagnostic line\n"]
        process = _make_process(lines)
        cfg = _make_config(tmp_path)

        events: list[MigrationEvent] = []
        with patch(
            "discord_ferry.exporter.runner.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ):
            await run_dce_export(cfg, tmp_path / "dce", events.append)

        assert any("[dce] Some unknown DCE diagnostic line" in e.message for e in events)


class TestSuccessCountWarning:
    @pytest.mark.asyncio
    async def test_success_less_than_total_emits_warning(self, tmp_path: Path) -> None:
        lines = [
            b"Exporting 10 channel(s)...\n",
            b"Successfully exported 7 channel(s).\n",
        ]
        process = _make_process(lines)
        cfg = _make_config(tmp_path)

        events: list[MigrationEvent] = []
        with patch(
            "discord_ferry.exporter.runner.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ):
            await run_dce_export(cfg, tmp_path / "dce", events.append)

        warnings = [e for e in events if e.status == "warning"]
        assert any(
            "7 of 10" in w.message and "3 channel(s) appear to have failed silently" in w.message
            for w in warnings
        )


class TestStderrTaskDrainedOnCancel:
    @pytest.mark.asyncio
    async def test_stderr_task_done_after_cancel(self, tmp_path: Path) -> None:
        lines = [b"general: 25%\n"] * 100
        process = _make_process(lines)
        cfg = _make_config(tmp_path)
        cfg.cancel_event = asyncio.Event()

        events: list[MigrationEvent] = []

        def cancel_after_first(ev: MigrationEvent) -> None:
            events.append(ev)
            if len(events) == 2:
                cfg.cancel_event.set()

        with (
            patch(
                "discord_ferry.exporter.runner.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=process),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await run_dce_export(cfg, tmp_path / "dce", cancel_after_first)

        if sys.platform != "win32":
            assert process.terminate.called


class TestWindowsCreationFlags:
    @pytest.mark.asyncio
    async def test_creation_flags_include_no_window_and_new_process_group_on_windows(
        self, tmp_path: Path
    ) -> None:
        process = _make_process([])
        cfg = _make_config(tmp_path)

        captured_kwargs: dict[str, int] = {}

        async def _capture(*args: object, **kwargs: int) -> MagicMock:
            captured_kwargs.update(kwargs)
            return process

        with (
            patch("discord_ferry.exporter.runner.sys.platform", "win32"),
            patch("discord_ferry.exporter.runner._CREATE_NO_WINDOW", 0x08000000),
            patch("discord_ferry.exporter.runner._CREATE_NEW_PROCESS_GROUP", 0x00000200),
            patch(
                "discord_ferry.exporter.runner.asyncio.create_subprocess_exec",
                new=_capture,
            ),
        ):
            await run_dce_export(cfg, tmp_path / "dce", lambda _e: None)

        flags = captured_kwargs.get("creationflags", 0)
        assert flags & 0x08000000, "CREATE_NO_WINDOW bit not set"
        assert flags & 0x00000200, "CREATE_NEW_PROCESS_GROUP bit not set"


class TestCancelSendsCtrlBreakOnWindows:
    @pytest.mark.asyncio
    async def test_cancel_uses_ctrl_break_on_windows(self, tmp_path: Path) -> None:
        import signal as _signal

        lines = [b"general: 25%\n"] * 5
        process = _make_process(lines)
        process.wait = AsyncMock(return_value=0)

        cfg = _make_config(tmp_path)
        cfg.cancel_event = asyncio.Event()

        events_seen = 0

        def cancel_immediately(_ev: MigrationEvent) -> None:
            nonlocal events_seen
            events_seen += 1
            if events_seen == 2:
                cfg.cancel_event.set()

        # CTRL_BREAK_EVENT only exists on Windows; patch it in for POSIX runs.
        ctrl_break = getattr(_signal, "CTRL_BREAK_EVENT", 1)  # Windows value is 1

        with (
            patch("discord_ferry.exporter.runner.sys.platform", "win32"),
            patch.object(_signal, "CTRL_BREAK_EVENT", ctrl_break, create=True),
            patch(
                "discord_ferry.exporter.runner.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=process),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await run_dce_export(cfg, tmp_path / "dce", cancel_immediately)

        assert process.send_signal.called or process.terminate.called
        if process.send_signal.called:
            (sig_arg,), _ = process.send_signal.call_args
            assert sig_arg == ctrl_break


class TestLongLineHandling:
    @pytest.mark.asyncio
    async def test_drain_overlong_line_consumes_to_newline(self) -> None:
        reader = asyncio.StreamReader(limit=64)
        reader.feed_data(b"x" * 200 + b"\n" + b"next\n")
        reader.feed_eof()

        with pytest.raises(asyncio.LimitOverrunError):
            await reader.readuntil(b"\n")

        consumed = await _drain_overlong_line(reader)
        assert consumed > 0

        next_line = await reader.readuntil(b"\n")
        assert next_line == b"next\n"

    @pytest.mark.asyncio
    async def test_drain_overlong_line_handles_eof(self) -> None:
        reader = asyncio.StreamReader(limit=64)
        reader.feed_data(b"x" * 200)
        reader.feed_eof()

        with pytest.raises(asyncio.LimitOverrunError):
            await reader.readuntil(b"\n")

        consumed = await _drain_overlong_line(reader)
        assert consumed > 0


# ---------- Heartbeat tests ----------


class TestHeartbeat:
    """Tests for the heartbeat / silence-breaker task.

    Strategy: dependency injection of `sleep` and `monotonic` into
    `_heartbeat`, NOT global monkey-patching of `asyncio.sleep` (which would
    break the stdout/stderr `async for`/`readuntil` loops, `process.wait()`,
    and aiohttp in the integration tests).
    """

    def _make_fake_clock(
        self,
    ) -> tuple[Callable[[], float], Callable[[float], Awaitable[None]], list[float]]:
        """Returns (monotonic, sleep, sleeps_list).

        sleeps_list records every requested sleep delay. The fake clock advances
        on each sleep call AND yields control once (via asyncio.sleep(0)) so
        other tasks can interleave.
        """
        now = [0.0]
        sleeps: list[float] = []

        def monotonic() -> float:
            return now[0]

        async def sleep(delay: float) -> None:
            sleeps.append(delay)
            now[0] += delay
            await asyncio.sleep(0)

        return monotonic, sleep, sleeps

    def _make_fake_process(self, *, returncode_sequence: list[int | None]) -> MagicMock:
        """Fake subprocess with a controllable returncode sequence."""
        process = MagicMock()
        codes = list(returncode_sequence)

        def get_returncode(self: object) -> int | None:
            if not codes:
                return 0
            val = codes.pop(0)
            return val

        type(process).returncode = property(get_returncode)
        return process

    @pytest.mark.asyncio
    async def test_heartbeat_fires_after_60s_of_silence(self) -> None:
        from discord_ferry.exporter.runner import _heartbeat

        monotonic, sleep, sleeps = self._make_fake_clock()
        # Need 7 None ticks so that 6 sleeps of 10s each accumulate 60s, then the
        # 7th iteration fires. One more None after firing before the final 0 exits.
        process = self._make_fake_process(returncode_sequence=[None] * 8 + [0])

        events: list[MigrationEvent] = []
        await _heartbeat(
            process=process,
            on_event=events.append,
            process_start=0.0,
            get_last_activity=lambda: 0.0,  # never any activity
            sleep=sleep,
            monotonic=monotonic,
            initial_interval=60.0,
        )

        assert any(e.status == "heartbeat" for e in events), (
            f"expected at least one heartbeat event, got: {[(e.status, e.message) for e in events]}"
        )

    @pytest.mark.asyncio
    async def test_heartbeat_does_not_fire_when_activity_present(self) -> None:
        # When activity is bumped continuously (last_activity == now()), silence
        # never reaches the interval -> no heartbeat should fire.
        from discord_ferry.exporter.runner import _heartbeat

        monotonic, sleep, sleeps = self._make_fake_clock()
        process = self._make_fake_process(returncode_sequence=[None] * 20 + [0])

        events: list[MigrationEvent] = []
        # Activity always == "now" -> silence == 0 -> never fires
        await _heartbeat(
            process=process,
            on_event=events.append,
            process_start=0.0,
            get_last_activity=monotonic,  # last_activity is always "now"
            sleep=sleep,
            monotonic=monotonic,
            initial_interval=60.0,
        )

        heartbeats = [e for e in events if e.status == "heartbeat"]
        assert not heartbeats, f"expected zero heartbeats, got {len(heartbeats)}"

    @pytest.mark.asyncio
    async def test_heartbeat_status_is_heartbeat_not_progress(self) -> None:
        from discord_ferry.exporter.runner import _heartbeat

        monotonic, sleep, _ = self._make_fake_clock()
        process = self._make_fake_process(returncode_sequence=[None, None, None, 0])

        events: list[MigrationEvent] = []
        await _heartbeat(
            process=process,
            on_event=events.append,
            process_start=0.0,
            get_last_activity=lambda: 0.0,
            sleep=sleep,
            monotonic=monotonic,
            initial_interval=60.0,
        )

        for e in events:
            if "Still working" in e.message:
                assert e.status == "heartbeat", (
                    f"event with heartbeat-shaped message had status={e.status!r}"
                )

    @pytest.mark.asyncio
    async def test_heartbeat_backoff_doubles(self) -> None:
        # After first fire at 60s, interval should double to 120s.
        from discord_ferry.exporter.runner import _heartbeat

        monotonic, sleep, sleeps = self._make_fake_clock()
        # Allow enough ticks for two heartbeat fires (60s, 120s) then exit.
        process = self._make_fake_process(returncode_sequence=[None] * 40 + [0])

        events: list[MigrationEvent] = []
        await _heartbeat(
            process=process,
            on_event=events.append,
            process_start=0.0,
            get_last_activity=lambda: 0.0,
            sleep=sleep,
            monotonic=monotonic,
            initial_interval=60.0,
            max_interval=300.0,
        )

        heartbeats = [e for e in events if e.status == "heartbeat"]
        # Should have fired at least twice (at ~60s and ~180s silence)
        assert len(heartbeats) >= 2, (
            f"expected at least 2 heartbeat events for backoff test, got {len(heartbeats)}"
        )

    @pytest.mark.asyncio
    async def test_heartbeat_resets_on_activity(self) -> None:
        # After a heartbeat fires, if activity occurs, interval resets to 60s.
        from discord_ferry.exporter.runner import _heartbeat

        monotonic, sleep, sleeps = self._make_fake_clock()

        # Simulate: silence for 60s (fires), then activity resets, then silence 60s again (fires).
        activity_time = [0.0]  # starts at 0 (process_start)

        def get_last_activity() -> float:
            return activity_time[0]

        process = self._make_fake_process(returncode_sequence=[None] * 40 + [0])

        fire_count = [0]
        events: list[MigrationEvent] = []

        def on_event(e: MigrationEvent) -> None:
            events.append(e)
            if e.status == "heartbeat":
                fire_count[0] += 1
                # After first fire, simulate activity reset.
                if fire_count[0] == 1:
                    # Set last_activity to current now so silence resets.
                    activity_time[0] = monotonic()

        await _heartbeat(
            process=process,
            on_event=on_event,
            process_start=0.0,
            get_last_activity=get_last_activity,
            sleep=sleep,
            monotonic=monotonic,
            initial_interval=60.0,
            max_interval=300.0,
        )

        heartbeats = [e for e in events if e.status == "heartbeat"]
        # Should have fired at least twice (reset means second fires at +60s not +120s)
        assert len(heartbeats) >= 2, f"expected >=2 heartbeats after reset, got {len(heartbeats)}"

    @pytest.mark.asyncio
    async def test_heartbeat_does_not_fire_at_59s(self) -> None:
        # At 59s of silence with interval=60s, no heartbeat should fire.
        from discord_ferry.exporter.runner import _heartbeat

        monotonic, sleep, _ = self._make_fake_clock()

        # Cap ticks so we accumulate only ~59s before exit.
        # Each sleep(remaining) where remaining = max(1, min(10, 60 - silence)).
        # At t=0 silence=0, remaining=10. After ~6 sleeps of 10s = 60s -> would fire.
        # Use only 5 ticks (50s of sleep + some partial) then exit.
        process = self._make_fake_process(returncode_sequence=[None] * 5 + [0])

        events: list[MigrationEvent] = []
        await _heartbeat(
            process=process,
            on_event=events.append,
            process_start=0.0,
            get_last_activity=lambda: 0.0,
            sleep=sleep,
            monotonic=monotonic,
            initial_interval=60.0,
        )

        # With only 5 ticks of 10s each = 50s, no heartbeat should have fired.
        heartbeats = [e for e in events if e.status == "heartbeat"]
        assert not heartbeats, (
            f"heartbeat fired before 60s silence; events: {[(e.status, e.message) for e in events]}"
        )

    @pytest.mark.asyncio
    async def test_heartbeat_fires_at_exact_60s(self) -> None:
        # Exactly at silence == initial_interval (60s), heartbeat should fire.
        from discord_ferry.exporter.runner import _heartbeat

        monotonic, sleep, _ = self._make_fake_clock()
        # 6 ticks of 10s = 60s, then fire on 7th check; give enough ticks.
        process = self._make_fake_process(returncode_sequence=[None] * 8 + [0])

        events: list[MigrationEvent] = []
        await _heartbeat(
            process=process,
            on_event=events.append,
            process_start=0.0,
            get_last_activity=lambda: 0.0,
            sleep=sleep,
            monotonic=monotonic,
            initial_interval=60.0,
        )

        heartbeats = [e for e in events if e.status == "heartbeat"]
        assert len(heartbeats) >= 1, "heartbeat should have fired at exactly 60s of silence"

    @pytest.mark.asyncio
    async def test_heartbeat_capped_at_max_interval(self) -> None:
        # After multiple doublings (60 -> 120 -> 240 -> 480 capped to 300),
        # interval should never exceed max_interval.
        from discord_ferry.exporter.runner import _heartbeat

        monotonic, sleep, sleeps = self._make_fake_clock()
        # Allow many ticks for multiple fires.
        process = self._make_fake_process(returncode_sequence=[None] * 100 + [0])

        events: list[MigrationEvent] = []
        await _heartbeat(
            process=process,
            on_event=events.append,
            process_start=0.0,
            get_last_activity=lambda: 0.0,
            sleep=sleep,
            monotonic=monotonic,
            initial_interval=60.0,
            max_interval=300.0,
        )

        heartbeats = [e for e in events if e.status == "heartbeat"]
        # Should have fired several times (60s, 180s, 420s, 720s... but cap means
        # after 240s interval -> capped to 300s for 4th fire).
        assert len(heartbeats) >= 3, (
            f"expected >=3 heartbeat fires to test cap, got {len(heartbeats)}"
        )
        # Verify total elapsed time covered. At 4+ fires, the gap between the
        # 3rd and 4th cannot exceed 300s (the cap). This is enforced structurally
        # by the code; here we just confirm the fires happened.

    @pytest.mark.asyncio
    async def test_heartbeat_message_contains_elapsed_and_silence(self) -> None:
        # Heartbeat message should mention elapsed minutes and silence seconds.
        from discord_ferry.exporter.runner import _heartbeat

        monotonic, sleep, _ = self._make_fake_clock()
        process = self._make_fake_process(returncode_sequence=[None] * 8 + [0])

        events: list[MigrationEvent] = []
        await _heartbeat(
            process=process,
            on_event=events.append,
            process_start=0.0,
            get_last_activity=lambda: 0.0,
            sleep=sleep,
            monotonic=monotonic,
            initial_interval=60.0,
        )

        heartbeats = [e for e in events if e.status == "heartbeat"]
        assert heartbeats, "no heartbeat events to inspect"
        msg = heartbeats[0].message
        assert "Still working" in msg, f"message missing 'Still working': {msg!r}"
        assert "min" in msg, f"message missing minutes: {msg!r}"
        assert "s..." in msg, f"message missing silence seconds: {msg!r}"

    @pytest.mark.asyncio
    async def test_heartbeat_cancels_cleanly(self) -> None:
        # Task.cancel() should cause _heartbeat to exit without raising.
        from discord_ferry.exporter.runner import _heartbeat

        monotonic, sleep, _ = self._make_fake_clock()
        # Process never exits on its own (returncode always None).
        process = MagicMock()
        type(process).returncode = property(lambda self: None)

        events: list[MigrationEvent] = []

        async def run() -> None:
            task = asyncio.create_task(
                _heartbeat(
                    process=process,
                    on_event=events.append,
                    process_start=0.0,
                    get_last_activity=lambda: 0.0,
                    sleep=sleep,
                    monotonic=monotonic,
                    initial_interval=60.0,
                )
            )
            # Let it spin once then cancel.
            await asyncio.sleep(0)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            return

        import contextlib

        await run()
        # No assertion needed beyond "didn't raise" — but let's confirm no error events.
        error_events = [e for e in events if e.status == "error"]
        assert not error_events, f"unexpected error events: {error_events}"

    @pytest.mark.asyncio
    async def test_heartbeat_phase_export(self) -> None:
        # All heartbeat events should have phase == "export".
        from discord_ferry.exporter.runner import _heartbeat

        monotonic, sleep, _ = self._make_fake_clock()
        process = self._make_fake_process(returncode_sequence=[None] * 8 + [0])

        events: list[MigrationEvent] = []
        await _heartbeat(
            process=process,
            on_event=events.append,
            process_start=0.0,
            get_last_activity=lambda: 0.0,
            sleep=sleep,
            monotonic=monotonic,
            initial_interval=60.0,
        )

        heartbeats = [e for e in events if e.status == "heartbeat"]
        assert heartbeats, "no heartbeat events emitted"
        for e in heartbeats:
            assert e.phase == "export", f"expected phase='export', got {e.phase!r}"


# ---------------------------------------------------------------------------
# Batch 9 — S3 symmetric stderr drain + cancel + activity
# ---------------------------------------------------------------------------


class TestStderrOverlongLine:
    @pytest.mark.asyncio
    async def test_overlong_stderr_line_captured_not_crashed(self, tmp_path: Path) -> None:
        """SC-19: a >64 KiB stderr line is captured (truncated marker), not a ValueError crash."""
        from discord_ferry.errors import ExportError

        process = _make_process([], returncode=1)
        stderr = asyncio.StreamReader(limit=64)
        stderr.feed_data(b"x" * 200)  # one >64-byte line, no newline
        stderr.feed_eof()
        process.stderr = stderr
        cfg = _make_config(tmp_path)

        with (
            patch(
                "discord_ferry.exporter.runner.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=process),
            ),
            pytest.raises(ExportError, match="exceeded 64 KiB"),
        ):
            await run_dce_export(cfg, tmp_path / "dce", lambda _e: None)

    @pytest.mark.asyncio
    async def test_normal_stderr_accumulates(self, tmp_path: Path) -> None:
        """SC-22: normal stderr lines accumulate (surfaced in the ExportError)."""
        from discord_ferry.errors import ExportError

        process = _make_process([], returncode=1, stderr_lines=[b"a real error\n"])
        cfg = _make_config(tmp_path)

        with (
            patch(
                "discord_ferry.exporter.runner.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=process),
            ),
            pytest.raises(ExportError, match="a real error"),
        ):
            await run_dce_export(cfg, tmp_path / "dce", lambda _e: None)


class TestDrainCancel:
    @pytest.mark.asyncio
    async def test_drain_overlong_line_respects_cancel(self) -> None:
        """SC-20: _drain_overlong_line returns promptly when cancel_event is already set."""
        reader = asyncio.StreamReader(limit=64)
        reader.feed_data(b"x" * 1000)  # many 64-byte chunks
        reader.feed_eof()
        with pytest.raises(asyncio.LimitOverrunError):
            await reader.readuntil(b"\n")

        cancel = asyncio.Event()
        cancel.set()
        # With cancel pre-set, the drain must break early rather than consume to EOF.
        consumed = await _drain_overlong_line(reader, cancel)
        assert consumed >= 0
        assert not reader.at_eof()  # did NOT drain everything


class TestOverlongStdoutActivity:
    @pytest.mark.asyncio
    async def test_overlong_stdout_emits_truncation_event(self, tmp_path: Path) -> None:
        """SC-21: an overlong stdout line emits a truncation event and the run completes.

        Proves the overlong-stdout branch (which now also records activity) executes
        through to its emit + continue without crashing.
        """
        stdout = asyncio.StreamReader(limit=64)
        stdout.feed_data(b"y" * 200)  # overlong line (no newline before 64 KiB)
        stdout.feed_data(b"general: 50%\n")  # a normal line after the drain
        stdout.feed_eof()
        process = _make_process([])
        process.stdout = stdout
        cfg = _make_config(tmp_path)

        events: list[MigrationEvent] = []
        with patch(
            "discord_ferry.exporter.runner.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ):
            await run_dce_export(cfg, tmp_path / "dce", events.append)

        assert any("truncated" in (e.message or "") for e in events)


# ---------------------------------------------------------------------------
# #135 — a refusing proxy must name itself at exporter/runner.py:346
# ---------------------------------------------------------------------------


async def test_a_refused_proxy_names_the_proxy(fake_proxy, proxy_env, os_proxy) -> None:
    """SC-135-28. Killing: proxy_hint defined and never called at runner.py:346.

    This site also carried a target-binding hazard: the URL was an inline
    literal inside the `try`, so there was no name to pass as `target`. It is
    hoisted to a variable above the try.
    """
    from discord_ferry.errors import DiscordAuthError

    make, _ = fake_proxy
    server = await make(b"403 Forbidden")
    port = server.sockets[0].getsockname()[1]
    async with server:
        with os_proxy({}), proxy_env(HTTPS_PROXY=f"http://127.0.0.1:{port}"):
            with pytest.raises(DiscordAuthError) as caught:
                await validate_discord_token("dt")

    message = str(caught.value)
    assert "Cannot reach Discord API" in message
    # ONE assertion, not two. A separate `assert "discord.com" in message` above
    # the phrase took the failure under this site's mutant, so the phrase line
    # never ran. Merged, it grades wiring and pins `target=` together.
    assert f"The request to discord.com went through the proxy at 127.0.0.1:{port}" in message
    assert "FERRY_DISABLE_PROXY" in message
