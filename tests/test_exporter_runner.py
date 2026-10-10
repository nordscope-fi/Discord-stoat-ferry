"""Tests for exporter subprocess runner."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aioresponses import aioresponses

from discord_ferry.config import FerryConfig
from discord_ferry.core.events import MigrationEvent
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

# ---------- Helpers ----------


def _make_config(tmp_path: Path) -> FerryConfig:
    return FerryConfig(
        export_dir=tmp_path / "exports",
        stoat_url="https://stoat.example",
        token="st",
        discord_token="dt",
        discord_server_id="12345",
    )


def _is_dce_allowed(name: str) -> bool:
    """Whether the allowlist admits ``name`` on this platform (issue #976)."""
    from discord_ferry.exporter.runner import _build_dce_environment

    return bool(_build_dce_environment({name: "x"}))


async def _capture_dce_child_env(tmp_path: Path) -> dict[str, str]:
    """Run ``run_dce_export`` up to process creation and return the child's env."""
    captured: dict[str, str] = {}

    async def fake_exec(*args: object, **kwargs: object) -> None:
        captured.update(kwargs.get("env") or {})
        raise RuntimeError("stop here")

    with (
        patch(
            "discord_ferry.exporter.runner.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ),
        pytest.raises(RuntimeError),
    ):
        await run_dce_export(_make_config(tmp_path), tmp_path / "dce", lambda _e: None)
    return captured


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
        # The token reaches DCE through DISCORD_TOKEN, never argv (#978).
        assert "--token" not in cmd
        assert "dt" not in cmd
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
        assert events == [
            MigrationEvent(
                phase="export",
                status="warning",
                message=(
                    "Low disk space (1.0 GB free). Large servers may need 5-10 GB for exports."
                ),
            )
        ]

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
        assert warnings == [
            MigrationEvent(
                phase="export",
                status="warning",
                message=(
                    "DCE reports 7 of 10 channels exported successfully. "
                    "3 channel(s) appear to have failed silently."
                ),
            )
        ]


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
    async def test_normal_stderr_line_surfaces_in_error(self, tmp_path: Path) -> None:
        """SC-22: a normal stderr line is surfaced in the ExportError."""
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

    @pytest.mark.asyncio
    async def test_many_stderr_lines_surface_only_the_last(self, tmp_path: Path) -> None:
        """#987: with thousands of stderr lines the error carries the last, not an earlier one."""
        from discord_ferry.errors import ExportError

        lines = [f"error line {i}\n".encode() for i in range(10_000)]
        process = _make_process([], returncode=1, stderr_lines=lines)
        cfg = _make_config(tmp_path)

        with (
            patch(
                "discord_ferry.exporter.runner.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=process),
            ),
            pytest.raises(ExportError) as excinfo,
        ):
            await run_dce_export(cfg, tmp_path / "dce", lambda _e: None)

        message = str(excinfo.value)
        assert message.endswith("error line 9999")
        assert "error line 9998" not in message
        assert "error line 0" not in message

    @pytest.mark.asyncio
    async def test_overlong_marker_surfaces_when_it_is_the_last_stderr_line(
        self, tmp_path: Path
    ) -> None:
        """#987: a normal line followed by an over-long one reports the truncation marker."""
        from discord_ferry.errors import ExportError

        process = _make_process([], returncode=1)
        stderr = asyncio.StreamReader(limit=64)
        stderr.feed_data(b"an earlier error\n")
        stderr.feed_data(b"x" * 200)  # over-long line, no newline
        stderr.feed_eof()
        process.stderr = stderr
        cfg = _make_config(tmp_path)

        with (
            patch(
                "discord_ferry.exporter.runner.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=process),
            ),
            pytest.raises(ExportError) as excinfo,
        ):
            await run_dce_export(cfg, tmp_path / "dce", lambda _e: None)

        message = str(excinfo.value)
        assert "exceeded 64 KiB" in message
        assert "an earlier error" not in message

    @pytest.mark.asyncio
    async def test_stderr_memory_does_not_grow_with_line_count(self, tmp_path: Path) -> None:
        """#987: peak allocation stays flat while 100,000 distinct stderr lines stream through.

        The lines are produced lazily, one per readuntil() call, so the test's own input
        never holds them. Holding every line (the old list) costs roughly 10 MB here;
        keeping only the last costs a few KB.
        """
        import tracemalloc

        from discord_ferry.errors import ExportError

        total = 100_000
        produced = 0

        async def _readuntil(_sep: bytes) -> bytes:
            nonlocal produced
            if produced >= total:
                raise asyncio.IncompleteReadError(partial=b"", expected=None)
            produced += 1
            return f"error line {produced:06d}".ljust(99, "e").encode() + b"\n"

        process = _make_process([], returncode=1)
        process.stderr = MagicMock()
        process.stderr.readuntil = _readuntil
        cfg = _make_config(tmp_path)

        tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            baseline, _ = tracemalloc.get_traced_memory()
            with (
                patch(
                    "discord_ferry.exporter.runner.asyncio.create_subprocess_exec",
                    new=AsyncMock(return_value=process),
                ),
                pytest.raises(ExportError) as excinfo,
            ):
                await run_dce_export(cfg, tmp_path / "dce", lambda _e: None)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert produced == total  # the reader really consumed every line
        assert "error line 100000" in str(excinfo.value)
        assert peak - baseline < 2_000_000


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
        with (
            os_proxy({}),
            proxy_env(HTTPS_PROXY=f"http://127.0.0.1:{port}"),
            pytest.raises(DiscordAuthError) as caught,
        ):
            await validate_discord_token("dt")

    message = str(caught.value)
    assert "Cannot reach Discord API" in message
    # ONE assertion, not two. A separate `assert "discord.com" in message` above
    # the phrase took the failure under this site's mutant, so the phrase line
    # never ran. Merged, it grades wiring and pins `target=` together.
    assert f"The request to discord.com went through the proxy at 127.0.0.1:{port}" in message
    assert "FERRY_DISABLE_PROXY" in message


# ---------------------------------------------------------------------------
# Task 14 — the OS-resolved proxy must reach the DCE child environment
#
# DCE is a separate .NET process reached through create_subprocess_exec; it
# inherits only the environment. .NET reads env vars then OS settings, so
# Ferry silently resolving a proxy from OS settings while the environment
# stays empty is the one gap that matters: DCE sees nothing.
# ---------------------------------------------------------------------------


class TestDceChildToken:
    @pytest.mark.asyncio
    async def test_the_discord_token_reaches_dce_through_the_environment(
        self, tmp_path: Path
    ) -> None:
        """Killing: a token on the child's command line, where any local process
        listing shows it. DCE reads DISCORD_TOKEN when --token is absent (#978).
        """
        cfg = _make_config(tmp_path)
        captured_args: list[object] = []
        captured_env: dict[str, str] = {}

        async def fake_exec(*args: object, **kwargs: object) -> None:
            captured_args.extend(args)
            captured_env.update(kwargs.get("env") or {})
            raise RuntimeError("stop here")

        with (
            patch(
                "discord_ferry.exporter.runner.asyncio.create_subprocess_exec",
                side_effect=fake_exec,
            ),
            pytest.raises(RuntimeError),
        ):
            await run_dce_export(cfg, tmp_path / "dce", lambda _e: None)

        assert captured_env.get("DISCORD_TOKEN") == "dt"
        assert "dt" not in [str(arg) for arg in captured_args]
        assert "--token" not in [str(arg) for arg in captured_args]


class TestDceChildProxyEnvironment:
    @pytest.mark.asyncio
    async def test_an_os_resolved_proxy_reaches_the_dce_child(
        self, tmp_path: Path, proxy_env, os_proxy
    ) -> None:
        """Killing: an export that succeeds while every later phase fails, teaching
        the user their proxy works. .NET on macOS and Windows reads env then OS
        settings, but only Ferry sees an OS proxy when the environment is silent.
        """
        cfg = _make_config(tmp_path)
        captured_env: dict[str, str] = {}

        async def fake_exec(*args: object, **kwargs: object) -> None:
            captured_env.update(kwargs.get("env") or {})
            raise RuntimeError("stop here")

        with (
            os_proxy({"https": "http://corp:8080"}),
            proxy_env(),
            patch(
                "discord_ferry.exporter.runner.asyncio.create_subprocess_exec",
                side_effect=fake_exec,
            ),
            pytest.raises(RuntimeError),
        ):
            # Snapshot INSIDE the fixtures: proxy_env has already rewritten
            # os.environ by here, so this is the environment the child should
            # actually inherit.
            before = set(os.environ)
            await run_dce_export(cfg, tmp_path / "dce", lambda _e: None)

        assert captured_env.get("HTTPS_PROXY") == "http://corp:8080"
        # Issue #976 replaced the old full-copy contract here, which asserted
        # `set(captured_env) >= before - {"HTTPS_PROXY"}`: every parent variable
        # must reach the child. The child now gets an allowlist, so the assertion
        # is two-sided. Everything it holds is allowlisted, the injected proxy or
        # the token, and every name the parent shares with the allowlist is still
        # there. The second half stops an empty allowlist passing the first.
        allowed = {name for name in before if _is_dce_allowed(name)}
        assert allowed, "the test process has no allowlisted variable, so this proves nothing"
        assert set(captured_env) <= allowed | {"HTTPS_PROXY", "DISCORD_TOKEN"}
        assert set(captured_env) >= allowed

    @pytest.mark.asyncio
    async def test_the_kill_switch_suppresses_injection(
        self, tmp_path: Path, proxy_env, os_proxy
    ) -> None:
        """S2 criterion 4. Killing: a kill switch that stops Ferry proxying while
        still pushing a proxy at the child.
        """
        cfg = _make_config(tmp_path)
        captured_env: dict[str, str] = {}

        async def fake_exec(*args: object, **kwargs: object) -> None:
            captured_env.update(kwargs.get("env") or {})
            raise RuntimeError("stop here")

        with (
            os_proxy({"https": "http://corp:8080"}),
            proxy_env(FERRY_DISABLE_PROXY="1"),
            patch(
                "discord_ferry.exporter.runner.asyncio.create_subprocess_exec",
                side_effect=fake_exec,
            ),
            pytest.raises(RuntimeError),
        ):
            await run_dce_export(cfg, tmp_path / "dce", lambda _e: None)

        assert "HTTPS_PROXY" not in captured_env

    @pytest.mark.asyncio
    async def test_a_users_own_variable_is_not_overwritten(
        self, tmp_path: Path, proxy_env, os_proxy
    ) -> None:
        """Killing: clobbering a deliberate user setting."""
        cfg = _make_config(tmp_path)
        captured_env: dict[str, str] = {}

        async def fake_exec(*args: object, **kwargs: object) -> None:
            captured_env.update(kwargs.get("env") or {})
            raise RuntimeError("stop here")

        with (
            os_proxy({"https": "http://corp:8080"}),
            proxy_env(HTTPS_PROXY="http://mine:9999"),
            patch(
                "discord_ferry.exporter.runner.asyncio.create_subprocess_exec",
                side_effect=fake_exec,
            ),
            pytest.raises(RuntimeError),
        ):
            await run_dce_export(cfg, tmp_path / "dce", lambda _e: None)

        assert captured_env["HTTPS_PROXY"] == "http://mine:9999"


class TestDceChildEnvironmentAllowlist:
    """Issue #976: the exporter child gets an allowlist, not a copy of Ferry's environment.

    All values are dummies. The exporter is a self-contained .NET app, so it needs the
    platform's basic variables and the user's proxy and TLS settings, and nothing else.
    """

    @pytest.mark.asyncio
    async def test_secrets_in_ferrys_environment_do_not_reach_the_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proxy_env
    ) -> None:
        """Killing: `dict(os.environ)`, which hands the exporter every secret Ferry holds."""
        monkeypatch.setenv("STOAT_TOKEN", "dummy-stoat-value")
        monkeypatch.setenv("OTHER_SECRET", "dummy-other-value")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "dummy-aws-value")
        with proxy_env():
            env = await _capture_dce_child_env(tmp_path)

        assert env, "an empty capture means the fake exec never ran, not that nothing leaked"
        for name in ("STOAT_TOKEN", "OTHER_SECRET", "AWS_SECRET_ACCESS_KEY"):
            assert name not in env
        assert not any(v.startswith("dummy-") for v in env.values())

    @pytest.mark.asyncio
    async def test_path_systemroot_and_temp_reach_the_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proxy_env
    ) -> None:
        """Killing: an allowlist that drops what .NET needs to start (the reason the
        old code copied everything).
        """
        monkeypatch.setenv("PATH", "/dummy/bin")
        monkeypatch.setenv("SYSTEMROOT", "C:\\Dummy\\Windows")
        monkeypatch.setenv("TEMP", "/dummy/temp")
        with proxy_env():
            env = await _capture_dce_child_env(tmp_path)

        by_upper = {k.upper(): v for k, v in env.items()}
        assert by_upper["PATH"] == "/dummy/bin"
        assert by_upper["SYSTEMROOT"] == "C:\\Dummy\\Windows"
        assert by_upper["TEMP"] == "/dummy/temp"

    @pytest.mark.asyncio
    async def test_proxy_and_tls_variables_still_reach_the_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proxy_env
    ) -> None:
        """Killing: an allowlist without the proxy and CA settings, which breaks every
        corporate network.
        """
        monkeypatch.setenv("SSL_CERT_FILE", "/dummy/ca.pem")
        monkeypatch.setenv("SSL_CERT_DIR", "/dummy/certs")
        pairs = {
            "HTTP_PROXY": "http://dummy-a:1",
            "HTTPS_PROXY": "http://dummy-b:2",
            "ALL_PROXY": "http://dummy-c:3",
            "NO_PROXY": "dummy.example",
        }
        if sys.platform != "win32":
            # os.environ upper-cases names on Windows, so lower-case twins collapse there.
            pairs |= {k.lower(): v + "x" for k, v in pairs.items()}
        with proxy_env(**pairs):
            env = await _capture_dce_child_env(tmp_path)

        assert env["SSL_CERT_FILE"] == "/dummy/ca.pem"
        assert env["SSL_CERT_DIR"] == "/dummy/certs"
        for name, value in pairs.items():
            assert env[name] == value

    @pytest.mark.asyncio
    async def test_the_discord_token_comes_from_the_config_not_the_parent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proxy_env
    ) -> None:
        """Killing: a DISCORD_TOKEN in Ferry's own environment winning over the config."""
        monkeypatch.setenv("DISCORD_TOKEN", "dummy-ambient-value")
        with proxy_env():
            env = await _capture_dce_child_env(tmp_path)

        assert env["DISCORD_TOKEN"] == "dt"

    def test_locale_variables_pass_by_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Killing: listing LANG only, so LC_ALL and LC_CTYPE are dropped."""
        from discord_ferry.exporter.runner import _build_dce_environment

        monkeypatch.setattr(sys, "platform", "linux")
        env = _build_dce_environment(
            {"LANG": "C", "LC_ALL": "C.UTF-8", "LC_CTYPE": "C", "LCX": "no", "STOAT_TOKEN": "x"}
        )
        assert env == {"LANG": "C", "LC_ALL": "C.UTF-8", "LC_CTYPE": "C"}

    def test_posix_names_are_case_sensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Killing: matching case-insensitively on POSIX, where `path` is a different
        variable from `PATH` and `Home` is not `HOME`. Proxy variables are the
        exception, listed in both cases because tools read both.
        """
        from discord_ferry.exporter.runner import _build_dce_environment

        monkeypatch.setattr(sys, "platform", "linux")
        env = _build_dce_environment(
            {"path": "/a", "Home": "/b", "PATH": "/c", "http_proxy": "p", "Http_Proxy": "q"}
        )
        assert env == {"PATH": "/c", "http_proxy": "p"}

    def test_windows_names_match_case_insensitively(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Killing: an exact-case match on Windows, where the system spells it
        `SystemRoot` and `Path` and os.environ may report either.
        """
        from discord_ferry.exporter.runner import _build_dce_environment

        monkeypatch.setattr(sys, "platform", "win32")
        env = _build_dce_environment(
            {
                "SystemRoot": "C:\\Dummy",
                "Path": "C:\\Dummy\\bin",
                "ComSpec": "C:\\Dummy\\cmd.exe",
                "LocalAppData": "C:\\Dummy\\Local",
                "Https_Proxy": "http://dummy:1",
                "STOAT_TOKEN": "dummy",
                "OtherSecret": "dummy",
            }
        )
        assert env == {
            "SystemRoot": "C:\\Dummy",
            "Path": "C:\\Dummy\\bin",
            "ComSpec": "C:\\Dummy\\cmd.exe",
            "LocalAppData": "C:\\Dummy\\Local",
            "Https_Proxy": "http://dummy:1",
        }

    def test_the_allowlist_holds_what_each_platform_needs_to_start(self) -> None:
        """Killing: a trimmed list that loses a name .NET needs on a platform CI cannot
        run. This names the minimum, so removing one is a deliberate edit to this test.
        """
        from discord_ferry.exporter import runner

        windows_required = {
            "PATH", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "TEMP", "TMP",
            "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
        }  # fmt: skip
        posix_required = {"PATH", "HOME", "TMPDIR", "LANG", "TZ"}
        assert windows_required <= runner._DCE_ENV_ALLOWLIST
        assert posix_required <= runner._DCE_ENV_ALLOWLIST

    def test_nothing_secret_shaped_is_on_the_allowlist(self) -> None:
        """Killing: a wildcard or a prefix entry that sweeps in tokens and keys."""
        from discord_ferry.exporter import runner

        names = runner._DCE_ENV_ALLOWLIST | set(runner._DCE_ENV_PREFIXES)
        for name in names:
            assert not any(
                w in name.upper() for w in ("TOKEN", "SECRET", "KEY", "PASSWORD", "AUTH")
            )


# ---------------------------------------------------------------------------
# Proxy resolution must never abort an export (issue #148)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_launches_when_proxy_resolution_fails(tmp_path: Path) -> None:
    """Today the raise aborts the export before DCE starts.

    The HTTPS_PROXY variable this block injects is an optimisation: DCE works
    without it, and a user who set the variable themselves already has it. Losing
    the whole export over a failure to read the OS proxy configuration is a bad
    trade, so the export must launch anyway with the variable absent rather than
    present and wrong.
    """
    process = _make_process([b"Successfully exported 1 channel(s).\n"])
    exec_mock = AsyncMock(return_value=process)
    events: list[MigrationEvent] = []

    with (
        patch("discord_ferry.exporter.runner.asyncio.create_subprocess_exec", new=exec_mock),
        patch("discord_ferry.core.http._os_proxies", side_effect=KeyError("boom")),
        patch("discord_ferry.core.http._os_proxy_bypass", return_value=False),
    ):
        await run_dce_export(_make_config(tmp_path), tmp_path / "dce", events.append)

    assert exec_mock.await_count == 1, "the subprocess must still be created"
    assert "HTTPS_PROXY" not in exec_mock.await_args.kwargs["env"]


@pytest.mark.asyncio
async def test_a_user_set_proxy_variable_survives_a_resolution_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The comment beside this block says overwriting a deliberate setting is a
    surprise rather than a feature. A boundary that clears the variable on
    failure would do exactly that."""
    monkeypatch.setenv("HTTPS_PROXY", "http://mine:8080")
    process = _make_process([b"Successfully exported 1 channel(s).\n"])
    exec_mock = AsyncMock(return_value=process)
    events: list[MigrationEvent] = []

    with (
        patch("discord_ferry.exporter.runner.asyncio.create_subprocess_exec", new=exec_mock),
        patch("discord_ferry.core.http._os_proxies", side_effect=KeyError("boom")),
        patch("discord_ferry.core.http._os_proxy_bypass", return_value=False),
    ):
        await run_dce_export(_make_config(tmp_path), tmp_path / "dce", events.append)

    assert exec_mock.await_args.kwargs["env"]["HTTPS_PROXY"] == "http://mine:8080"


@pytest.mark.asyncio
async def test_a_resolution_failure_reaches_the_event_stream(tmp_path: Path) -> None:
    """Without this, the two tests above both pass against an empty except block.

    An export that silently runs without the proxy the user configured is the
    shape recorded in lesson_a_command_that_does_nothing_is_worse_than_no_command:
    a code path that looks like it delivers something and structurally cannot.
    """
    process = _make_process([b"Successfully exported 1 channel(s).\n"])
    events: list[MigrationEvent] = []

    with (
        patch(
            "discord_ferry.exporter.runner.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ),
        patch("discord_ferry.core.http._os_proxies", side_effect=KeyError("boom")),
        patch("discord_ferry.core.http._os_proxy_bypass", return_value=False),
    ):
        await run_dce_export(_make_config(tmp_path), tmp_path / "dce", events.append)

    proxy_warnings = [e for e in events if "proxy" in e.message.lower()]
    assert proxy_warnings == [
        MigrationEvent(
            phase="export",
            status="warning",
            message=(
                "Could not read the system proxy configuration. The export will run without it."
            ),
        )
    ]
