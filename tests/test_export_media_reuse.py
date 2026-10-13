"""Regression tests for safe DiscordChatExporter media reuse (#1223)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from discord_ferry.config import FerryConfig
from discord_ferry.core.engine import PhaseFunction, run_migration
from discord_ferry.core.events import MigrationEvent
from discord_ferry.discord.metadata import DiscordMetadata, save_discord_metadata
from discord_ferry.errors import ExportError
from discord_ferry.state import MigrationState, load_state, save_state
from tests.stoat_ids import stoat_id

if TYPE_CHECKING:
    from discord_ferry.core.events import EventCallback

_GUILD_ID = "111111111111111111"
_OTHER_GUILD_ID = "999999999999999999"
_FIXTURE = Path(__file__).parent / "fixtures" / "simple_channel.json"


async def _noop_phase(
    config: FerryConfig,
    state: MigrationState,
    exports: list,
    emit: EventCallback,
) -> None:
    """Keep these export-policy tests away from Stoat API calls."""


_NOOP_OVERRIDES: dict[str, PhaseFunction] = {
    phase: _noop_phase
    for phase in (
        "connect",
        "server",
        "roles",
        "categories",
        "channels",
        "emoji",
        "avatars",
        "messages",
        "reactions",
        "pins",
    )
}


def _orchestrated_config(
    output_dir: Path,
    *,
    resume: bool = False,
    incremental: bool = False,
) -> FerryConfig:
    export_dir = output_dir / "dce_cache" / _GUILD_ID
    export_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(_FIXTURE, export_dir / _FIXTURE.name)
    return FerryConfig(
        export_dir=export_dir,
        output_dir=output_dir,
        stoat_url="https://stoat.test",
        token="stoat-token",
        discord_token="discord-token",
        discord_server_id=_GUILD_ID,
        resume=resume,
        incremental=incremental,
    )


def _save_completed_export(output_dir: Path) -> None:
    save_state(
        MigrationState(
            export_completed=True,
            started_at="2026-10-13T00:00:00+00:00",
        ),
        output_dir,
    )
    save_discord_metadata(
        DiscordMetadata(
            guild_id=_GUILD_ID,
            fetched_at="2026-10-13T00:00:01+00:00",
            server_default_permissions=0,
            role_permissions={},
            channel_metadata={},
        ),
        output_dir,
    )


async def _run_with_mock_export(
    config: FerryConfig,
    run_dce: AsyncMock,
) -> MigrationState:
    with (
        patch("discord_ferry.core.engine.validate_discord_token", new=AsyncMock()),
        patch("discord_ferry.core.engine.get_dce_path", return_value=Path("/dce")),
        patch("discord_ferry.core.engine.run_dce_export", new=run_dce),
    ):
        return await run_migration(config, lambda _event: None, phase_overrides=_NOOP_OVERRIDES)


async def test_completed_resume_reuses_media_from_the_same_export_folder(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    config = _orchestrated_config(output_dir, resume=True)
    _save_completed_export(output_dir)
    run_dce = AsyncMock(return_value=config.export_dir)

    await _run_with_mock_export(config, run_dce)

    assert run_dce.await_args.kwargs["reuse_media"] is True


async def test_fresh_second_run_reuses_completed_export_without_carrying_migration_state(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "output"
    config = _orchestrated_config(output_dir)
    previous = MigrationState(
        export_completed=True,
        attachments_uploaded=42,
        started_at="2026-10-13T00:00:00+00:00",
    )
    save_state(previous, output_dir)
    save_discord_metadata(
        DiscordMetadata(
            guild_id=_GUILD_ID,
            fetched_at="2026-10-13T00:00:01+00:00",
            server_default_permissions=0,
            role_permissions={},
            channel_metadata={},
        ),
        output_dir,
    )
    run_dce = AsyncMock(return_value=config.export_dir)
    metadata = DiscordMetadata(
        guild_id=_GUILD_ID,
        fetched_at="2026-10-13T00:00:00+00:00",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
    )

    with patch(
        "discord_ferry.core.engine.fetch_and_translate_guild_metadata",
        new=AsyncMock(return_value=metadata),
    ):
        state = await _run_with_mock_export(config, run_dce)

    assert run_dce.await_args.kwargs["reuse_media"] is True
    assert state.attachments_uploaded == 0


async def test_incremental_run_carries_completed_export_media_authorization(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "output"
    config = _orchestrated_config(output_dir, incremental=True)
    _save_completed_export(output_dir)
    run_dce = AsyncMock(return_value=config.export_dir)
    metadata = DiscordMetadata(
        guild_id=_GUILD_ID,
        fetched_at="2026-10-13T00:00:00+00:00",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
    )

    with patch(
        "discord_ferry.core.engine.fetch_and_translate_guild_metadata",
        new=AsyncMock(return_value=metadata),
    ):
        await _run_with_mock_export(config, run_dce)

    assert run_dce.await_args.kwargs["reuse_media"] is True


async def test_completed_flag_does_not_authorize_a_different_export_folder(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "output"
    config = _orchestrated_config(output_dir, resume=True)
    _save_completed_export(output_dir)
    other_export_dir = output_dir / "another-cache"
    other_export_dir.mkdir()
    shutil.copy(_FIXTURE, other_export_dir / _FIXTURE.name)
    config.export_dir = other_export_dir
    run_dce = AsyncMock(return_value=config.export_dir)

    await _run_with_mock_export(config, run_dce)

    assert run_dce.await_args.kwargs["reuse_media_reason"] == (
        "the selected export folder is not Ferry's cache for this Discord server"
    )
    assert "reuse_media" not in run_dce.await_args.kwargs


@pytest.mark.parametrize("metadata_case", ["absent", "stale-for-current-server"])
async def test_completed_export_for_another_server_does_not_authorize_this_cache(
    tmp_path: Path,
    metadata_case: str,
) -> None:
    output_dir = tmp_path / "output"
    config = _orchestrated_config(output_dir)
    other_server_cache = output_dir / "dce_cache" / _OTHER_GUILD_ID
    other_server_cache.mkdir()
    shutil.copy(_FIXTURE, other_server_cache / _FIXTURE.name)
    save_state(
        MigrationState(
            export_completed=True,
            started_at="2026-10-13T00:00:00+00:00",
        ),
        output_dir,
    )
    if metadata_case == "stale-for-current-server":
        save_discord_metadata(
            DiscordMetadata(
                guild_id=_GUILD_ID,
                fetched_at="2026-10-12T00:00:00+00:00",
                server_default_permissions=0,
                role_permissions={},
                channel_metadata={},
            ),
            output_dir,
        )
    run_dce = AsyncMock(side_effect=ExportError("stop after policy check"))

    with pytest.raises(ExportError, match="stop after policy check"):
        await _run_with_mock_export(config, run_dce)

    assert "reuse_media" not in run_dce.await_args.kwargs
    assert run_dce.await_args.kwargs["reuse_media_reason"] == (
        "the completed export is not bound to this server cache"
    )


async def test_partial_channel_failure_does_not_mark_export_complete(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    config = _orchestrated_config(output_dir)
    metadata = DiscordMetadata(
        guild_id=_GUILD_ID,
        fetched_at="2026-10-13T00:00:01+00:00",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
    )

    async def emit_partial_failure(
        _config: FerryConfig,
        _dce_path: Path,
        emit: EventCallback,
    ) -> Path:
        emit(
            MigrationEvent(
                phase="export",
                status="error",
                message="DCE failed to export restricted: forbidden",
                channel_name="restricted",
                detail={"dce_channel_failure": True},
            )
        )
        return _config.export_dir

    run_dce = AsyncMock(side_effect=emit_partial_failure)
    with patch(
        "discord_ferry.core.engine.fetch_and_translate_guild_metadata",
        new=AsyncMock(return_value=metadata),
    ):
        state = await _run_with_mock_export(config, run_dce)

    assert state.export_completed is False
    partial_failures = [
        warning for warning in state.warnings if warning["type"] == "dce_channel_failed"
    ]
    assert partial_failures == [
        {
            "phase": "export",
            "type": "dce_channel_failed",
            "channel_name": "restricted",
            "message": "DCE failed to export restricted: forbidden",
        }
    ]


async def test_legacy_completed_state_with_partial_failure_does_not_reuse_media(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "output"
    config = _orchestrated_config(output_dir)
    save_state(
        MigrationState(
            export_completed=True,
            started_at="2026-10-13T00:00:00+00:00",
            warnings=[
                {
                    "phase": "export",
                    "type": "dce_channel_failed",
                    "channel_name": "restricted",
                    "message": "DCE failed to export restricted: forbidden",
                }
            ],
        ),
        output_dir,
    )
    save_discord_metadata(
        DiscordMetadata(
            guild_id=_GUILD_ID,
            fetched_at="2026-10-13T00:00:01+00:00",
            server_default_permissions=0,
            role_permissions={},
            channel_metadata={},
        ),
        output_dir,
    )
    run_dce = AsyncMock(side_effect=ExportError("stop after legacy policy check"))

    with pytest.raises(ExportError, match="stop after legacy policy check"):
        await _run_with_mock_export(config, run_dce)

    assert "reuse_media" not in run_dce.await_args.kwargs
    assert run_dce.await_args.kwargs["reuse_media_reason"] == (
        "the saved export includes failed Discord channels"
    )


@pytest.mark.parametrize("restart_mode", ["resume", "incremental"])
async def test_interrupted_reexport_clears_completed_flag_before_dce_writes(
    tmp_path: Path,
    restart_mode: str,
) -> None:
    output_dir = tmp_path / "output"
    config = _orchestrated_config(
        output_dir,
        resume=restart_mode == "resume",
        incremental=restart_mode == "incremental",
    )
    _save_completed_export(output_dir)
    run_dce = AsyncMock(side_effect=ExportError("interrupted"))

    with pytest.raises(ExportError, match="interrupted"):
        await _run_with_mock_export(config, run_dce)

    assert run_dce.await_args.kwargs["reuse_media"] is True
    assert load_state(output_dir).export_completed is False


async def test_failed_fresh_export_preserves_prior_checkpoint_maps(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    config = _orchestrated_config(output_dir)
    previous_message_map = {"1123456789012345678": stoat_id(1)}
    save_state(
        MigrationState(
            export_completed=True,
            message_map=previous_message_map,
        ),
        output_dir,
    )
    save_discord_metadata(
        DiscordMetadata(
            guild_id=_GUILD_ID,
            fetched_at="2026-10-13T00:00:00+00:00",
            server_default_permissions=0,
            role_permissions={},
            channel_metadata={},
        ),
        output_dir,
    )
    run_dce = AsyncMock(side_effect=ExportError("fresh export interrupted"))

    with pytest.raises(ExportError, match="fresh export interrupted"):
        await _run_with_mock_export(config, run_dce)

    saved = load_state(output_dir)
    assert saved.export_completed is False
    assert saved.message_map == previous_message_map


async def test_failed_export_state_disables_media_reuse_on_the_next_attempt(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "output"
    config = _orchestrated_config(output_dir, resume=True)
    save_state(MigrationState(export_completed=False), output_dir)
    run_dce = AsyncMock(side_effect=ExportError("interrupted again"))

    with pytest.raises(ExportError, match="interrupted again"):
        await _run_with_mock_export(config, run_dce)

    assert "reuse_media" not in run_dce.await_args.kwargs
    assert "reuse_media_reason" not in run_dce.await_args.kwargs
