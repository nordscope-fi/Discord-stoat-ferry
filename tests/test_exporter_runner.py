"""Tests for exporter subprocess runner."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aioresponses import aioresponses

from discord_ferry.config import FerryConfig
from discord_ferry.exporter.runner import (
    _DCE_PROGRESS_RE,
    _build_dce_command,
    _check_disk_space,
    run_dce_export,
    validate_discord_token,
)

if TYPE_CHECKING:
    from pathlib import Path

    from discord_ferry.core.events import MigrationEvent


class TestProgressRegex:
    def test_channel_with_percentage(self):
        line = "[1/15] Exporting #general... 50.0%"
        match = _DCE_PROGRESS_RE.search(line)
        assert match is not None
        assert match.group("channel") == "general"
        assert match.group("pct") == "50.0"

    def test_channel_start_no_percentage(self):
        line = "[2/15] Exporting #announcements..."
        match = _DCE_PROGRESS_RE.search(line)
        assert match is not None
        assert match.group("channel") == "announcements"
        assert match.group("pct") is None

    def test_no_match_for_other_lines(self):
        line = "Starting export of guild 123..."
        assert _DCE_PROGRESS_RE.search(line) is None

    def test_channel_100_percent(self):
        line = "[3/15] Exporting #memes... 100.0%"
        match = _DCE_PROGRESS_RE.search(line)
        assert match is not None
        assert match.group("channel") == "memes"
        assert match.group("pct") == "100.0"


class TestBuildCommand:
    def test_command_construction(self, tmp_path: Path):
        dce_path = tmp_path / "dce"
        cfg = FerryConfig(
            export_dir=tmp_path / "exports",
            stoat_url="https://stoat.example",
            token="st",
            discord_token="dt",
            discord_server_id="12345",
        )
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
    def test_warns_when_low(self, tmp_path: Path):
        events: list[MigrationEvent] = []
        with patch("discord_ferry.exporter.runner.shutil.disk_usage") as mock_du:
            mock_du.return_value = MagicMock(free=1_000_000_000)  # 1 GB
            _check_disk_space(tmp_path, events.append)
        assert len(events) == 1
        assert "Low disk space" in events[0].message

    def test_no_warning_when_plenty(self, tmp_path: Path):
        events: list[MigrationEvent] = []
        with patch("discord_ferry.exporter.runner.shutil.disk_usage") as mock_du:
            mock_du.return_value = MagicMock(free=20_000_000_000)  # 20 GB
            _check_disk_space(tmp_path, events.append)
        assert len(events) == 0


class TestValidateDiscordToken:
    @pytest.mark.asyncio
    async def test_valid_token(self):
        with aioresponses() as m:
            m.get(
                "https://discord.com/api/v10/users/@me",
                status=200,
                payload={"id": "1"},
            )
            await validate_discord_token("valid-token")  # should not raise

    @pytest.mark.asyncio
    async def test_invalid_token(self):
        from discord_ferry.errors import DiscordAuthError

        with aioresponses() as m:
            m.get("https://discord.com/api/v10/users/@me", status=401)
            with pytest.raises(DiscordAuthError, match="Invalid Discord token"):
                await validate_discord_token("bad-token")

    @pytest.mark.asyncio
    async def test_unexpected_status(self):
        from discord_ferry.errors import DiscordAuthError

        with aioresponses() as m:
            m.get("https://discord.com/api/v10/users/@me", status=500)
            with pytest.raises(DiscordAuthError, match="unexpected status"):
                await validate_discord_token("some-token")


class TestRunDceExportProgressEmits:
    """Lock in the progress-emit contract that closes the silent-window UX gap.

    Without an emit between subprocess spawn and the first stdout regex match,
    the GUI used to freeze on the last status for the entire DCE enumeration
    phase (minutes on large servers — see issue #23).
    """

    @pytest.mark.asyncio
    async def test_enumerating_emit_fires_before_stdout_progress(self, tmp_path: Path) -> None:
        async def _empty_iter():
            for _ in ():
                yield b""

        async def _stdout_iter():
            yield b"[1/3] Exporting #general... 50.0%\n"

        process = MagicMock()
        process.stdout = _stdout_iter()
        process.stderr = _empty_iter()
        process.wait = AsyncMock(return_value=0)
        process.returncode = 0

        cfg = FerryConfig(
            export_dir=tmp_path / "exports",
            stoat_url="https://stoat.example",
            token="st",
            discord_token="dt",
            discord_server_id="12345",
        )

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
        exporting_idx = next(
            (i for i, m in enumerate(messages) if m.startswith("Exporting #")), None
        )
        assert enumerating_idx is not None, f"missing 'enumerating channels' emit; got {messages!r}"
        assert exporting_idx is not None, f"missing per-channel emit; got {messages!r}"
        assert enumerating_idx < exporting_idx, (
            "enumeration emit must precede the first per-channel progress emit"
        )
