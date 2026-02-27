"""Tests for migration phase behaviors (dry-run, limits, permission bootstrap)."""

import json
from pathlib import Path

import pytest

from discord_ferry.config import FerryConfig
from discord_ferry.core.engine import PhaseFunction, run_migration
from discord_ferry.core.events import EventCallback, MigrationEvent
from discord_ferry.errors import MigrationError
from discord_ferry.state import MigrationState, save_state

FIXTURES_DIR = Path(__file__).parent / "fixtures"


async def _noop_phase(
    config: FerryConfig,
    state: MigrationState,
    exports: list,
    emit: EventCallback,
) -> None:
    """No-op phase for tests that need to suppress specific phases."""


def _make_config(tmp_path: Path, **overrides: object) -> FerryConfig:
    defaults: dict[str, object] = {
        "export_dir": FIXTURES_DIR,
        "stoat_url": "https://api.test",
        "token": "test-token",
        "output_dir": tmp_path,
    }
    defaults.update(overrides)
    return FerryConfig(**defaults)  # type: ignore[arg-type]


# Noop overrides for phases that make real HTTP calls — used when dry_run=True
# handles the non-connect phases, but connect still needs to be suppressed.
_CONNECT_NOOP: dict[str, PhaseFunction] = {"connect": _noop_phase}


# ---------------------------------------------------------------------------
# Test 1 — Dry-run end-to-end
# ---------------------------------------------------------------------------


async def test_dry_run_state_flag_and_synthetic_ids(tmp_path: Path) -> None:
    """Dry-run sets is_dry_run=True and populates maps with dry- prefixed synthetic IDs."""
    config = _make_config(tmp_path, dry_run=True)
    state = await run_migration(config, lambda e: None, phase_overrides=_CONNECT_NOOP)

    assert state.is_dry_run is True
    assert all(v.startswith("dry-") for v in state.channel_map.values()), (
        "channel_map values should have 'dry-' prefix"
    )
    assert all(v.startswith("dry-") for v in state.message_map.values()), (
        "message_map values should have 'dry-' prefix"
    )


async def test_dry_run_no_http_calls(tmp_path: Path) -> None:
    """Dry-run completes without any aiohttp requests (no mock needed)."""
    # If any phase makes a real HTTP call this test will raise a ConnectionError.
    config = _make_config(tmp_path, dry_run=True)
    # Suppress connect (would hit /users/@me) — all other phases are dry-run aware.
    state = await run_migration(config, lambda e: None, phase_overrides=_CONNECT_NOOP)
    assert isinstance(state, MigrationState)


async def test_dry_run_state_json_persisted(tmp_path: Path) -> None:
    """Dry-run writes state.json with is_dry_run=true."""
    config = _make_config(tmp_path, dry_run=True)
    await run_migration(config, lambda e: None, phase_overrides=_CONNECT_NOOP)

    state_path = tmp_path / "state.json"
    assert state_path.exists()
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert data["is_dry_run"] is True


# ---------------------------------------------------------------------------
# Test 2 — Dry-run resume refusal
# ---------------------------------------------------------------------------


async def test_dry_run_resume_raises_migration_error(tmp_path: Path) -> None:
    """Resuming from a dry-run state raises MigrationError."""
    prior = MigrationState(
        is_dry_run=True,
        current_phase="channels",
        started_at="2024-01-01T00:00:00+00:00",
    )
    save_state(prior, tmp_path)

    config = _make_config(tmp_path, resume=True)
    with pytest.raises(MigrationError, match="dry-run"):
        await run_migration(config, lambda e: None, phase_overrides=_CONNECT_NOOP)


# ---------------------------------------------------------------------------
# Test 3 — Permission bootstrap warning (server phase)
# ---------------------------------------------------------------------------


async def test_permission_bootstrap_failure_is_warned_not_fatal(tmp_path: Path) -> None:
    """When default_permissions PATCH fails, the migration continues with a warning."""
    from aioresponses import aioresponses

    config = _make_config(tmp_path)

    # Only override connect with noop — let run_server use its real implementation.
    structure_noops: dict[str, PhaseFunction] = {
        "connect": _noop_phase,
        "roles": _noop_phase,
        "categories": _noop_phase,
        "channels": _noop_phase,
        "emoji": _noop_phase,
        "messages": _noop_phase,
        "reactions": _noop_phase,
        "pins": _noop_phase,
    }

    events: list[MigrationEvent] = []

    with aioresponses() as m:
        # Server creation succeeds.
        m.post(
            f"{config.stoat_url}/servers/create",
            payload={"_id": "stoat-server-001"},
        )
        # Icon upload and PATCH icon — skip (no icon in fixture guild without a real file).
        # First PATCH (icon) is never reached since guild_icon.png doesn't exist locally.
        # Second PATCH (default_permissions bootstrap) returns 403 — simulates missing perms.
        m.patch(
            f"{config.stoat_url}/servers/stoat-server-001",
            status=403,
            payload={"type": "Forbidden"},
        )

        state = await run_migration(config, events.append, phase_overrides=structure_noops)

    warning_messages = [w["message"] for w in state.warnings if w.get("phase") == "server"]
    assert any("permission" in msg.lower() or "Could not set" in msg for msg in warning_messages), (
        f"Expected a permissions warning in state.warnings, got: {warning_messages}"
    )
    # Migration must NOT have raised — it completed successfully.
    assert state.completed_at != ""


# ---------------------------------------------------------------------------
# Test 4 — Configurable limits (max_channels, max_emoji)
# ---------------------------------------------------------------------------


async def test_max_channels_limit_emits_warning(tmp_path: Path) -> None:
    """When exports exceed max_channels, a warning is emitted and state records the drop."""
    events: list[MigrationEvent] = []

    # Set max_channels=1 — fixtures have multiple channels, so truncation must fire.
    config = _make_config(tmp_path, dry_run=True, max_channels=1)
    state = await run_migration(config, events.append, phase_overrides=_CONNECT_NOOP)

    channel_warnings = [e for e in events if e.phase == "channels" and e.status == "warning"]
    assert len(channel_warnings) > 0, "Expected a channels warning event for overflow"
    assert any(
        "limit" in e.message.lower() or "exceed" in e.message.lower() or "Dropped" in e.message
        for e in channel_warnings
    )

    state_channel_warnings = [w for w in state.warnings if w.get("phase") == "channels"]
    assert len(state_channel_warnings) > 0


async def test_max_channels_respects_limit(tmp_path: Path) -> None:
    """channel_map never exceeds max_channels entries after truncation."""
    config = _make_config(tmp_path, dry_run=True, max_channels=1)
    state = await run_migration(config, lambda e: None, phase_overrides=_CONNECT_NOOP)

    assert len(state.channel_map) <= 1


async def test_max_emoji_limit_emits_warning(tmp_path: Path) -> None:
    """When discovered emoji exceed max_emoji, a warning is recorded in state."""
    # Build a phase override that plants emoji in the state and then triggers truncation.
    # We can test this directly via the emoji phase's dry_run path.

    async def fake_emoji_phase(
        config: FerryConfig,
        state: MigrationState,
        exports: list,
        emit: EventCallback,
    ) -> None:
        """Simulate having more emoji than max_emoji allows."""
        from discord_ferry.migrator.emoji import run_emoji
        from discord_ferry.parser.models import (
            DCEEmoji,
            DCEReaction,
        )

        # Inject synthetic emoji into the first export's messages so run_emoji discovers them.
        if exports:
            fake_reactions = [
                DCEReaction(
                    emoji=DCEEmoji(
                        id=str(900_000 + i),
                        name=f"fake_emoji_{i}",
                        is_animated=False,
                        image_url="",
                    ),
                    count=1,
                )
                for i in range(config.max_emoji + 3)
            ]
            # Patch the first message to carry these reactions temporarily.
            original_msgs = exports[0].messages
            if original_msgs:
                original_reactions = original_msgs[0].reactions
                original_msgs[0].reactions = fake_reactions
                await run_emoji(config, state, exports, emit)
                original_msgs[0].reactions = original_reactions
                return
        await run_emoji(config, state, exports, emit)

    overrides: dict[str, PhaseFunction] = {
        **{
            "connect": _noop_phase,
            "server": _noop_phase,
            "roles": _noop_phase,
            "categories": _noop_phase,
            "channels": _noop_phase,
            "messages": _noop_phase,
            "reactions": _noop_phase,
            "pins": _noop_phase,
        },
        "emoji": fake_emoji_phase,
    }

    config = _make_config(tmp_path, dry_run=True, max_emoji=2)
    events: list[MigrationEvent] = []
    state = await run_migration(config, events.append, phase_overrides=overrides)

    emoji_warnings = [w for w in state.warnings if w.get("phase") == "emoji"]
    assert any("truncat" in w["message"].lower() for w in emoji_warnings), (
        f"Expected truncation warning in state.warnings, got: {emoji_warnings}"
    )
    assert len(state.emoji_map) <= 2
