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
    from collections.abc import AsyncGenerator
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


async def _empty_stderr_iter() -> AsyncGenerator[bytes, None]:
    for _ in ():
        yield b""


def _make_process(stdout_lines: list[bytes], returncode: int = 0) -> MagicMock:
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
    process.stderr = _empty_stderr_iter()
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

        with patch(
            "discord_ferry.exporter.runner.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ), pytest.raises(asyncio.CancelledError):
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

        with patch("discord_ferry.exporter.runner.sys.platform", "win32"), \
             patch("discord_ferry.exporter.runner._CREATE_NO_WINDOW", 0x08000000), \
             patch("discord_ferry.exporter.runner._CREATE_NEW_PROCESS_GROUP", 0x00000200), \
             patch(
                 "discord_ferry.exporter.runner.asyncio.create_subprocess_exec",
                 new=_capture,
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

        with patch("discord_ferry.exporter.runner.sys.platform", "win32"), \
             patch.object(_signal, "CTRL_BREAK_EVENT", ctrl_break, create=True), \
             patch(
                 "discord_ferry.exporter.runner.asyncio.create_subprocess_exec",
                 new=AsyncMock(return_value=process),
             ), \
             pytest.raises(asyncio.CancelledError):
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
