"""Tests for the migration engine orchestrator."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from aioresponses import aioresponses

from discord_ferry.config import FerryConfig
from discord_ferry.core.engine import PHASE_ORDER, PhaseFunction, run_migration, run_retry_failed
from discord_ferry.core.events import EventCallback, MigrationEvent
from discord_ferry.parser.models import DCEExport
from discord_ferry.state import FailedMessage, MigrationState

FIXTURES_DIR = Path(__file__).parent / "fixtures"


async def _noop_phase(
    config: FerryConfig,
    state: MigrationState,
    exports: list,
    emit: EventCallback,
) -> None:
    """No-op phase for tests that don't need real HTTP."""


# Use this for tests that don't care about phases making real API calls
_NOOP_OVERRIDES: dict[str, PhaseFunction] = {
    "connect": _noop_phase,
    "server": _noop_phase,
    "roles": _noop_phase,
    "categories": _noop_phase,
    "channels": _noop_phase,
    "emoji": _noop_phase,
    "avatars": _noop_phase,
    "messages": _noop_phase,
    "reactions": _noop_phase,
    "pins": _noop_phase,
}


def _make_config(tmp_path: Path, **overrides: object) -> FerryConfig:
    defaults: dict[str, object] = {
        "export_dir": FIXTURES_DIR,
        "stoat_url": "https://api.test",
        "token": "test-token",
        "output_dir": tmp_path,
        "skip_export": True,  # existing tests use offline mode
    }
    defaults.update(overrides)
    return FerryConfig(**defaults)  # type: ignore[arg-type]


async def test_run_migration_validates_exports(tmp_path: Path) -> None:
    """Engine parses exports and emits validate events."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)
    validate_events = [e for e in events if e.phase == "validate"]
    assert any(e.status == "started" for e in validate_events)
    assert any(e.status == "completed" for e in validate_events)
    # Validation warnings should be stored in state for the report
    warning_events = [e for e in validate_events if e.status == "warning"]
    assert len(state.warnings) == len(warning_events)
    # Author names should be populated from the fixture exports
    assert len(state.author_names) > 0


async def test_run_migration_emits_phase_events(tmp_path: Path) -> None:
    """Engine emits started/completed for each injected phase."""
    events: list[MigrationEvent] = []
    called: list[str] = []

    async def mock_phase(
        config: FerryConfig,
        state: MigrationState,
        exports: list,
        emit: EventCallback,
    ) -> None:
        called.append("connect")

    config = _make_config(tmp_path)
    overrides = {**_NOOP_OVERRIDES, "connect": mock_phase}
    await run_migration(config, events.append, phase_overrides=overrides)
    assert "connect" in called
    connect_events = [e for e in events if e.phase == "connect"]
    assert any(e.status == "started" for e in connect_events)
    assert any(e.status == "completed" for e in connect_events)


async def test_run_migration_phases_called_in_order(tmp_path: Path) -> None:
    """Mock phases are called in the correct order."""
    call_order: list[str] = []

    def make_phase(name: str):
        async def fn(
            config: FerryConfig,
            state: MigrationState,
            exports: list,
            emit: EventCallback,
        ) -> None:
            call_order.append(name)

        return fn

    phase_names = ["connect", "server", "roles", "categories", "channels"]
    overrides = {name: make_phase(name) for name in phase_names}

    config = _make_config(tmp_path)
    await run_migration(config, lambda e: None, phase_overrides=overrides)
    assert call_order == phase_names


async def test_run_migration_skip_messages(tmp_path: Path) -> None:
    """skip_messages config flag skips the messages phase."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path, skip_messages=True)
    await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)
    msg_events = [e for e in events if e.phase == "messages"]
    assert any(e.status == "skipped" for e in msg_events)


async def test_run_migration_skip_emoji(tmp_path: Path) -> None:
    """skip_emoji config flag skips the emoji phase."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path, skip_emoji=True)
    await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)
    emoji_events = [e for e in events if e.phase == "emoji"]
    assert any(e.status == "skipped" for e in emoji_events)


async def test_run_migration_skip_reactions(tmp_path: Path) -> None:
    """skip_reactions config flag skips the reactions phase."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path, skip_reactions=True)
    await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)
    reaction_events = [e for e in events if e.phase == "reactions"]
    assert any(e.status == "skipped" for e in reaction_events)


async def test_run_migration_saves_state(tmp_path: Path) -> None:
    """State file exists after migration completes."""
    config = _make_config(tmp_path)
    await run_migration(config, lambda e: None, phase_overrides=_NOOP_OVERRIDES)
    assert (tmp_path / "state.json").exists()


async def test_run_migration_resume_skips_completed(tmp_path: Path) -> None:
    """On resume, phases before current_phase are skipped."""
    from discord_ferry.state import save_state

    # Save state with current_phase = "channels"
    prior_state = MigrationState(current_phase="channels", started_at="2024-01-01T00:00:00+00:00")
    save_state(prior_state, tmp_path)

    events: list[MigrationEvent] = []
    config = _make_config(tmp_path, resume=True)
    await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)

    # connect, server, roles, categories should all be skipped
    for phase in ["connect", "server", "roles", "categories"]:
        phase_events = [e for e in events if e.phase == phase]
        assert any(e.status == "skipped" for e in phase_events), f"{phase} should be skipped"


async def test_run_migration_phase_error(tmp_path: Path) -> None:
    """Engine catches phase exceptions and raises MigrationError."""
    from discord_ferry.errors import MigrationError

    async def failing_phase(
        config: FerryConfig,
        state: MigrationState,
        exports: list,
        emit: EventCallback,
    ) -> None:
        raise RuntimeError("Something broke")

    config = _make_config(tmp_path)
    overrides = {**_NOOP_OVERRIDES, "connect": failing_phase}
    with pytest.raises(MigrationError, match="connect"):
        await run_migration(config, lambda e: None, phase_overrides=overrides)


async def test_run_migration_phase_error_recorded_in_state(tmp_path: Path) -> None:
    """Phase errors are recorded in state.errors before raising."""
    from discord_ferry.errors import MigrationError

    captured_state: list[MigrationState] = []

    async def failing_phase(
        config: FerryConfig,
        state: MigrationState,
        exports: list,
        emit: EventCallback,
    ) -> None:
        captured_state.append(state)
        raise RuntimeError("boom")

    config = _make_config(tmp_path)
    overrides = {**_NOOP_OVERRIDES, "connect": failing_phase}
    with pytest.raises(MigrationError):
        await run_migration(config, lambda e: None, phase_overrides=overrides)

    assert len(captured_state) == 1
    state = captured_state[0]
    assert any(e["phase"] == "connect" for e in state.errors)


async def test_run_migration_builds_author_names(tmp_path: Path) -> None:
    """Author names are populated from export data, preferring nickname."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)
    # simple_channel.json has alice (id 400000000000000001) with nickname "Alice"
    assert "400000000000000001" in state.author_names
    assert state.author_names["400000000000000001"] == "Alice"


async def test_run_migration_report_generated(tmp_path: Path) -> None:
    """Report file exists after migration."""
    config = _make_config(tmp_path)
    await run_migration(config, lambda e: None, phase_overrides=_NOOP_OVERRIDES)
    assert (tmp_path / "migration_report.json").exists()


async def test_run_migration_report_events_emitted(tmp_path: Path) -> None:
    """Engine emits started and completed events for the report phase."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)
    report_events = [e for e in events if e.phase == "report"]
    assert any(e.status == "started" for e in report_events)
    assert any(e.status == "completed" for e in report_events)


async def test_run_migration_returns_migration_state(tmp_path: Path) -> None:
    """run_migration returns a MigrationState instance."""
    config = _make_config(tmp_path)
    result = await run_migration(config, lambda e: None, phase_overrides=_NOOP_OVERRIDES)
    assert isinstance(result, MigrationState)


async def test_run_migration_creates_output_dir(tmp_path: Path) -> None:
    """Engine creates the output directory if it doesn't exist."""
    nested_output = tmp_path / "deep" / "nested" / "output"
    config = _make_config(tmp_path, output_dir=nested_output)
    await run_migration(config, lambda e: None, phase_overrides=_NOOP_OVERRIDES)
    assert nested_output.exists()


async def test_run_migration_state_has_timestamps(tmp_path: Path) -> None:
    """Completed state has non-empty started_at and completed_at."""
    config = _make_config(tmp_path)
    state = await run_migration(config, lambda e: None, phase_overrides=_NOOP_OVERRIDES)
    assert state.started_at != ""
    assert state.completed_at != ""


async def test_run_migration_unimplemented_phases_skipped(tmp_path: Path) -> None:
    """Phases without implementations emit a 'Not yet implemented' skipped event."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)
    skipped_events = [
        e for e in events if e.status == "skipped" and "Not yet implemented" in e.message
    ]
    skipped_phases = {e.phase for e in skipped_events}
    # Phases with overrides or defaults run normally; only truly unimplemented ones are skipped
    implemented_phases = set(_NOOP_OVERRIDES.keys())
    runnable = [p for p in PHASE_ORDER if p not in ("export", "validate", "report")]
    for phase in runnable:
        if phase in implemented_phases:
            continue
        assert phase in skipped_phases, f"{phase} should be skipped when unimplemented"


async def test_run_migration_validate_total_in_event(tmp_path: Path) -> None:
    """The validate completed event carries the total message count."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)
    completed = next((e for e in events if e.phase == "validate" and e.status == "completed"), None)
    assert completed is not None
    assert completed.total > 0


async def test_run_migration_default_connect_phase(tmp_path: Path) -> None:
    """Connect phase runs by default when no override is provided (uses _DEFAULT_PHASES)."""
    from aioresponses import aioresponses

    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)

    # Override structure phases with noops so only connect uses _DEFAULT_PHASES
    structure_noops: dict[str, PhaseFunction] = {
        "server": _noop_phase,
        "roles": _noop_phase,
        "categories": _noop_phase,
        "channels": _noop_phase,
    }

    with aioresponses() as m:
        m.get(
            f"{config.stoat_url}/",
            payload={
                "stoat": "0.8.5",
                "features": {"autumn": {"enabled": True, "url": "https://autumn.test"}},
            },
        )
        m.get(
            f"{config.stoat_url}/users/@me",
            payload={"_id": "user123", "username": "ferry"},
        )
        state = await run_migration(config, events.append, phase_overrides=structure_noops)

    assert state.autumn_url == "https://autumn.test"
    connect_events = [e for e in events if e.phase == "connect"]
    assert any(e.status == "started" for e in connect_events)
    assert any(e.status == "completed" for e in connect_events)


async def test_export_phase_in_phase_order() -> None:
    """PHASE_ORDER starts with 'export'."""
    assert PHASE_ORDER[0] == "export"


async def test_export_skipped_in_offline_mode(tmp_path: Path) -> None:
    """When skip_export is True, the export phase is skipped."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path, skip_export=True)
    await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)
    export_events = [e for e in events if e.phase == "export"]
    assert any(e.status == "skipped" for e in export_events)


_DISCORD_API = "https://discord.com/api/v10"

_MOCK_ROLES = [
    {
        "id": "111111111111111111",  # @everyone role (id == guild_id)
        "name": "@everyone",
        "permissions": "1024",
        "position": 0,
        "color": 0,
        "hoist": False,
        "managed": False,
    },
    {
        "id": "222222222222222222",
        "name": "Moderator",
        "permissions": "2048",
        "position": 1,
        "color": 0xFF0000,
        "hoist": True,
        "managed": False,
    },
]

_MOCK_CHANNELS = [
    {
        "id": "333333333333333333",
        "name": "general",
        "type": 0,
        "nsfw": False,
        "permission_overwrites": [],
    },
    {
        "id": "444444444444444444",
        "name": "nsfw-channel",
        "type": 0,
        "nsfw": True,
        "permission_overwrites": [
            {"id": "222222222222222222", "type": 0, "allow": "0", "deny": "1024"},
        ],
    },
]

_GUILD_ID = "111111111111111111"


async def test_discord_metadata_fetched_when_token_provided(tmp_path: Path) -> None:
    """When discord_token and discord_server_id are set, metadata is fetched and saved."""
    from aioresponses import aioresponses

    events: list[MigrationEvent] = []
    config = _make_config(
        tmp_path,
        discord_token="test-discord-token",
        discord_server_id=_GUILD_ID,
    )

    with aioresponses() as m:
        m.get(
            f"{_DISCORD_API}/guilds/{_GUILD_ID}",
            payload={"id": _GUILD_ID, "name": "Test", "banner": None},
        )
        m.get(
            f"{_DISCORD_API}/guilds/{_GUILD_ID}/roles",
            payload=_MOCK_ROLES,
        )
        m.get(
            f"{_DISCORD_API}/guilds/{_GUILD_ID}/channels",
            payload=_MOCK_CHANNELS,
        )
        await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)

    assert (tmp_path / "discord_metadata.json").exists()
    export_events = [e for e in events if e.phase == "export"]
    assert any("metadata" in e.message.lower() for e in export_events if e.status == "progress")


async def test_discord_metadata_skipped_when_no_token(tmp_path: Path) -> None:
    """When discord_token is not set, no Discord API calls are made."""
    from aioresponses import aioresponses

    events: list[MigrationEvent] = []
    # _make_config defaults have no discord_token
    config = _make_config(tmp_path)

    with aioresponses() as m:
        await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)
        # Verify no Discord API requests were made
        assert len(m.requests) == 0

    assert not (tmp_path / "discord_metadata.json").exists()
    export_events = [e for e in events if e.phase == "export"]
    assert any("No Discord token" in e.message for e in export_events if e.status == "warning")


async def test_discord_metadata_cached_on_resume(tmp_path: Path) -> None:
    """On resume with existing metadata, no Discord API calls are made."""
    from aioresponses import aioresponses

    from discord_ferry.discord.metadata import (
        ChannelMeta,
        DiscordMetadata,
        PermissionPair,
        save_discord_metadata,
    )
    from discord_ferry.state import save_state

    # Pre-create state.json (required for resume=True) and metadata file
    prior_state = MigrationState(started_at="2024-01-01T00:00:00+00:00")
    save_state(prior_state, tmp_path)

    existing_meta = DiscordMetadata(
        guild_id=_GUILD_ID,
        fetched_at="2024-01-01T00:00:00+00:00",
        server_default_permissions=1024,
        role_permissions={"222222222222222222": PermissionPair(allow=2048, deny=0)},
        channel_metadata={"333333333333333333": ChannelMeta(nsfw=False)},
    )
    save_discord_metadata(existing_meta, tmp_path)

    events: list[MigrationEvent] = []
    config = _make_config(
        tmp_path,
        discord_token="test-discord-token",
        discord_server_id=_GUILD_ID,
        resume=True,
    )

    with aioresponses() as m:
        await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)
        # No Discord API calls should be made
        assert len(m.requests) == 0

    export_events = [e for e in events if e.phase == "export"]
    assert any("cached" in e.message.lower() for e in export_events if e.status == "progress")


async def test_no_discord_token_emits_warning(tmp_path: Path) -> None:
    """When discord_token is absent, engine emits status='warning' about permissions."""
    config = _make_config(tmp_path, discord_token=None, discord_server_id=None)
    events: list[MigrationEvent] = []
    await run_migration(config, events.append, _NOOP_OVERRIDES)

    warning_events = [
        e
        for e in events
        if e.status == "warning"
        and "permission" in e.message.lower()
        and "private" in e.message.lower()
    ]
    assert len(warning_events) >= 1, "Expected warning about permissions and private channels"


async def test_discord_token_present_no_permission_warning(tmp_path: Path) -> None:
    """When discord_token IS set, no permission warning emitted."""
    from aioresponses import aioresponses

    config = _make_config(
        tmp_path,
        discord_token="fake-token",
        discord_server_id="fake-server",
    )
    events: list[MigrationEvent] = []

    with aioresponses() as m:
        m.get(
            f"{_DISCORD_API}/guilds/fake-server/roles",
            payload=_MOCK_ROLES,
        )
        m.get(
            f"{_DISCORD_API}/guilds/fake-server/channels",
            payload=_MOCK_CHANNELS,
        )
        await run_migration(config, events.append, _NOOP_OVERRIDES)

    warning_events = [
        e
        for e in events
        if e.status == "warning"
        and "permission" in e.message.lower()
        and "private" in e.message.lower()
    ]
    assert len(warning_events) == 0, "Should not warn about permissions when token is present"


def test_emoji_phase_before_messages() -> None:
    """Emoji phase must run before messages for content transforms."""
    assert PHASE_ORDER.index("emoji") < PHASE_ORDER.index("messages")


def test_avatars_phase_in_phase_order() -> None:
    """Avatars phase positioned between emoji and messages."""
    assert "avatars" in PHASE_ORDER
    assert PHASE_ORDER.index("emoji") < PHASE_ORDER.index("avatars")
    assert PHASE_ORDER.index("avatars") < PHASE_ORDER.index("messages")


async def test_skip_avatars(tmp_path: Path) -> None:
    """skip_avatars config flag skips the avatars phase."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path, skip_avatars=True)
    await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)
    avatar_events = [e for e in events if e.phase == "avatars"]
    assert any(e.status == "skipped" for e in avatar_events)


async def test_avatars_phase_runs_when_not_skipped(tmp_path: Path) -> None:
    """Avatars phase runs and emits started/completed events."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)
    avatar_events = [e for e in events if e.phase == "avatars"]
    assert any(e.status == "started" for e in avatar_events)
    assert any(e.status == "completed" for e in avatar_events)


def test_phase_order_contains_expected_phases() -> None:
    """Verify all expected phases are present in PHASE_ORDER."""
    expected = {
        "export",
        "validate",
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
        "report",
    }
    assert expected.issubset(set(PHASE_ORDER))


async def test_discord_metadata_fetch_runs_with_skip_export(tmp_path: Path) -> None:
    """Discord metadata fetch runs even when skip_export=True."""
    from aioresponses import aioresponses

    events: list[MigrationEvent] = []
    config = _make_config(
        tmp_path,
        skip_export=True,
        discord_token="test-discord-token",
        discord_server_id=_GUILD_ID,
    )

    with aioresponses() as m:
        m.get(
            f"{_DISCORD_API}/guilds/{_GUILD_ID}",
            payload={"id": _GUILD_ID, "name": "Test", "banner": None},
        )
        m.get(
            f"{_DISCORD_API}/guilds/{_GUILD_ID}/roles",
            payload=_MOCK_ROLES,
        )
        m.get(
            f"{_DISCORD_API}/guilds/{_GUILD_ID}/channels",
            payload=_MOCK_CHANNELS,
        )
        await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)

    assert (tmp_path / "discord_metadata.json").exists()
    export_events = [e for e in events if e.phase == "export"]
    assert any(e.status == "skipped" for e in export_events)
    assert any("metadata" in e.message.lower() for e in export_events if e.status == "progress")


# ---------------------------------------------------------------------------
# Review event includes reaction_mode
# ---------------------------------------------------------------------------


async def test_review_shows_reaction_mode(tmp_path: Path) -> None:
    """Pre-creation review event detail includes reaction_mode from config."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path, reaction_mode="native")
    await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)

    review_events = [e for e in events if e.phase == "review" and e.status == "confirm"]
    assert len(review_events) == 1
    detail = review_events[0].detail
    assert detail is not None
    assert detail["reaction_mode"] == "native"


# ---------------------------------------------------------------------------
# run_retry_failed
# ---------------------------------------------------------------------------

BASE_URL = "https://stoat.test"
AUTUMN_URL = "https://autumn.test"
TOKEN = "test-token"


def _make_retry_config(tmp_path: Path, **overrides: Any) -> FerryConfig:
    """Config suitable for retry tests (no export skip, rate limits off)."""
    export_dir = tmp_path / "exports"
    export_dir.mkdir(exist_ok=True)
    defaults: dict[str, Any] = {
        "export_dir": export_dir,
        "stoat_url": BASE_URL,
        "token": TOKEN,
        "output_dir": tmp_path / "output",
        "message_rate_limit": 0.0,
        "upload_delay": 0.0,
    }
    defaults.update(overrides)
    return FerryConfig(**defaults)


def _write_dce_json(export_dir: Path, channel_id: str, messages: list[dict[str, Any]]) -> Path:
    """Write a minimal valid DCE JSON file and return its path."""
    data = {
        "guild": {"id": "guild1", "name": "Test Guild", "iconUrl": ""},
        "channel": {
            "id": channel_id,
            "type": 0,
            "name": f"channel-{channel_id}",
            "categoryId": "",
            "category": "",
            "topic": "",
        },
        "dateRange": {"after": None, "before": None},
        "exportedAt": "2024-01-01T00:00:00+00:00",
        "messageCount": len(messages),
        "messages": messages,
    }
    path = export_dir / f"Test - channel-{channel_id} [{channel_id}].json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _dce_msg_dict(msg_id: str, content: str = "hello") -> dict[str, Any]:
    """Build a minimal DCE message JSON dict."""
    return {
        "id": msg_id,
        "type": "Default",
        "timestamp": "2024-01-15T12:00:00+00:00",
        "timestampEdited": None,
        "callEndedTimestamp": None,
        "isPinned": False,
        "content": content,
        "author": {
            "id": "auth1",
            "name": "alice",
            "discriminator": "0000",
            "nickname": "Alice",
            "color": None,
            "isBot": False,
            "roles": [],
            "avatarUrl": "",
        },
        "attachments": [],
        "embeds": [],
        "stickers": [],
        "reactions": [],
        "mentions": [],
    }


def _make_exports_from_dir(export_dir: Path) -> list[DCEExport]:
    """Parse all JSON files in export_dir into DCEExport objects with json_path set."""
    from discord_ferry.parser.dce_parser import parse_export_directory

    return parse_export_directory(export_dir, metadata_only=True)


async def test_retry_failed_empty_list(tmp_path: Path) -> None:
    """Empty failed_messages completes immediately."""
    config = _make_retry_config(tmp_path)
    state = MigrationState()
    events: list[MigrationEvent] = []
    await run_retry_failed(config, state, [], events.append)
    assert any("No failed messages" in e.message for e in events)
    assert any(e.status == "completed" for e in events)


async def test_retry_failed_missing_export_dir(tmp_path: Path) -> None:
    """Missing export directory aborts with error event."""
    config = _make_retry_config(tmp_path, export_dir=tmp_path / "nonexistent")
    state = MigrationState(
        failed_messages=[FailedMessage(discord_msg_id="m1", stoat_channel_id="ch1", error="fail")]
    )
    events: list[MigrationEvent] = []
    await run_retry_failed(config, state, [], events.append)
    assert any("export directory not found" in e.message for e in events)
    assert len(state.failed_messages) == 1  # Unchanged


async def test_retry_failed_success_removes_from_list(tmp_path: Path) -> None:
    """Successfully retried message is removed from failed_messages."""
    config = _make_retry_config(tmp_path)
    config.output_dir.mkdir(parents=True, exist_ok=True)

    # Write a DCE export with the message we want to retry
    _write_dce_json(config.export_dir, "ch1", [_dce_msg_dict("msg_retry", "retry me")])

    exports = _make_exports_from_dir(config.export_dir)

    state = MigrationState(
        channel_map={"ch1": "stoat_ch1"},
        autumn_url=AUTUMN_URL,
        failed_messages=[
            FailedMessage(discord_msg_id="msg_retry", stoat_channel_id="stoat_ch1", error="timeout")
        ],
    )

    events: list[MigrationEvent] = []
    with aioresponses() as m:
        m.post(
            f"{BASE_URL}/channels/stoat_ch1/messages",
            payload={"_id": "stoat_retried"},
        )
        await run_retry_failed(config, state, exports, events.append)

    assert len(state.failed_messages) == 0
    assert "msg_retry" in state.message_map
    assert any("1 succeeded" in e.message for e in events)


async def test_retry_failed_still_failing_increments_count(tmp_path: Path) -> None:
    """A message that fails again increments retry_count and stays in the list."""
    config = _make_retry_config(tmp_path)
    config.output_dir.mkdir(parents=True, exist_ok=True)

    _write_dce_json(config.export_dir, "ch1", [_dce_msg_dict("msg_fail")])
    exports = _make_exports_from_dir(config.export_dir)

    state = MigrationState(
        channel_map={"ch1": "stoat_ch1"},
        autumn_url=AUTUMN_URL,
        failed_messages=[
            FailedMessage(discord_msg_id="msg_fail", stoat_channel_id="stoat_ch1", error="err")
        ],
    )

    events: list[MigrationEvent] = []

    async def always_fail(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("still broken")

    with patch("discord_ferry.core.engine._process_message", side_effect=always_fail):
        await run_retry_failed(config, state, exports, events.append)

    assert len(state.failed_messages) == 1
    assert state.failed_messages[0].retry_count == 1
    assert any("0 succeeded" in e.message and "1 still failed" in e.message for e in events)


async def test_retry_failed_message_not_found_in_exports(tmp_path: Path) -> None:
    """A message ID not found in any export emits a warning and is not removed."""
    config = _make_retry_config(tmp_path)
    config.output_dir.mkdir(parents=True, exist_ok=True)

    # Write an export with a DIFFERENT message ID
    _write_dce_json(config.export_dir, "ch1", [_dce_msg_dict("other_msg")])
    exports = _make_exports_from_dir(config.export_dir)

    state = MigrationState(
        channel_map={"ch1": "stoat_ch1"},
        autumn_url=AUTUMN_URL,
        failed_messages=[
            FailedMessage(
                discord_msg_id="missing_msg", stoat_channel_id="stoat_ch1", error="timeout"
            )
        ],
    )

    events: list[MigrationEvent] = []
    with aioresponses():
        await run_retry_failed(config, state, exports, events.append)

    assert len(state.failed_messages) == 1  # Not removed
    assert any("not found in exports" in e.message for e in events)


async def test_retry_failed_saves_state(tmp_path: Path) -> None:
    """State is saved after retry completes."""
    config = _make_retry_config(tmp_path)
    config.output_dir.mkdir(parents=True, exist_ok=True)

    _write_dce_json(config.export_dir, "ch1", [_dce_msg_dict("msg_save")])
    exports = _make_exports_from_dir(config.export_dir)

    state = MigrationState(
        channel_map={"ch1": "stoat_ch1"},
        autumn_url=AUTUMN_URL,
        failed_messages=[
            FailedMessage(discord_msg_id="msg_save", stoat_channel_id="stoat_ch1", error="err")
        ],
    )

    events: list[MigrationEvent] = []
    with aioresponses() as m:
        m.post(
            f"{BASE_URL}/channels/stoat_ch1/messages",
            payload={"_id": "stoat_saved"},
        )
        await run_retry_failed(config, state, exports, events.append)

    assert (config.output_dir / "state.json").exists()


async def test_retry_failed_mixed_results(tmp_path: Path) -> None:
    """Retry with one success and one not-found gives correct counts."""
    config = _make_retry_config(tmp_path)
    config.output_dir.mkdir(parents=True, exist_ok=True)

    # Only msg_ok exists in the export
    _write_dce_json(config.export_dir, "ch1", [_dce_msg_dict("msg_ok")])
    exports = _make_exports_from_dir(config.export_dir)

    state = MigrationState(
        channel_map={"ch1": "stoat_ch1"},
        autumn_url=AUTUMN_URL,
        failed_messages=[
            FailedMessage(discord_msg_id="msg_ok", stoat_channel_id="stoat_ch1", error="e"),
            FailedMessage(discord_msg_id="msg_gone", stoat_channel_id="stoat_ch1", error="e"),
        ],
    )

    events: list[MigrationEvent] = []
    with aioresponses() as m:
        m.post(
            f"{BASE_URL}/channels/stoat_ch1/messages",
            payload={"_id": "stoat_ok"},
        )
        await run_retry_failed(config, state, exports, events.append)

    # msg_ok succeeded → removed; msg_gone not found → still in list
    assert len(state.failed_messages) == 1
    assert state.failed_messages[0].discord_msg_id == "msg_gone"
    assert any("1 succeeded" in e.message and "1 still failed" in e.message for e in events)


# ---------------------------------------------------------------------------
# Post-migration validation (S7)
# ---------------------------------------------------------------------------

STOAT_URL = "https://api.test"
STOAT_SERVER_ID = "stoat_server_123"


async def test_validation_passes_when_counts_match(tmp_path: Path) -> None:
    """Validation emits 'completed' when channel and role counts match."""
    events: list[MigrationEvent] = []

    async def set_server_id(
        config: FerryConfig,
        state: MigrationState,
        exports: list,
        emit: EventCallback,
    ) -> None:
        state.stoat_server_id = STOAT_SERVER_ID
        state.channel_map = {"d1": "s1", "d2": "s2"}
        state.role_map = {"r1": "sr1"}

    config = _make_config(tmp_path, validate_after=True)
    overrides = {**_NOOP_OVERRIDES, "connect": set_server_id}

    with aioresponses() as m:
        m.get(
            f"{STOAT_URL}/servers/{STOAT_SERVER_ID}",
            payload={
                "channels": ["s1", "s2"],
                "roles": {"sr1": {"name": "role1"}},
            },
        )
        state = await run_migration(config, events.append, phase_overrides=overrides)

    val_events = [e for e in events if e.phase == "validate_migration"]
    assert any(e.status == "started" for e in val_events)
    assert any(e.status == "completed" and "passed" in e.message.lower() for e in val_events)
    assert state.validation_results["passed"] is True
    assert state.validation_results["channels_expected"] == 2
    assert state.validation_results["channels_found"] == 2
    assert state.validation_results["roles_expected"] == 1
    assert state.validation_results["roles_found"] == 1


async def test_validation_warns_on_mismatch(tmp_path: Path) -> None:
    """Validation emits 'warning' when channel or role counts don't match."""
    events: list[MigrationEvent] = []

    async def set_server_id(
        config: FerryConfig,
        state: MigrationState,
        exports: list,
        emit: EventCallback,
    ) -> None:
        state.stoat_server_id = STOAT_SERVER_ID
        state.channel_map = {"d1": "s1", "d2": "s2", "d3": "s3"}
        state.role_map = {"r1": "sr1"}

    config = _make_config(tmp_path, validate_after=True)
    overrides = {**_NOOP_OVERRIDES, "connect": set_server_id}

    with aioresponses() as m:
        m.get(
            f"{STOAT_URL}/servers/{STOAT_SERVER_ID}",
            payload={
                "channels": ["s1", "s2"],  # expected 3, found 2
                "roles": {"sr1": {"name": "role1"}},
            },
        )
        state = await run_migration(config, events.append, phase_overrides=overrides)

    val_events = [e for e in events if e.phase == "validate_migration"]
    assert any(e.status == "warning" for e in val_events)
    assert state.validation_results["passed"] is False
    assert state.validation_results["channels_expected"] == 3
    assert state.validation_results["channels_found"] == 2


async def test_validation_skipped_when_disabled(tmp_path: Path) -> None:
    """No validation events when validate_after is False."""
    events: list[MigrationEvent] = []

    async def set_server_id(
        config: FerryConfig,
        state: MigrationState,
        exports: list,
        emit: EventCallback,
    ) -> None:
        state.stoat_server_id = STOAT_SERVER_ID

    config = _make_config(tmp_path, validate_after=False)
    overrides = {**_NOOP_OVERRIDES, "connect": set_server_id}

    await run_migration(config, events.append, phase_overrides=overrides)

    val_events = [e for e in events if e.phase == "validate_migration"]
    assert len(val_events) == 0


async def test_validation_skips_on_api_failure(tmp_path: Path) -> None:
    """API failure during validation emits a warning, doesn't crash."""
    events: list[MigrationEvent] = []

    async def set_server_id(
        config: FerryConfig,
        state: MigrationState,
        exports: list,
        emit: EventCallback,
    ) -> None:
        state.stoat_server_id = STOAT_SERVER_ID

    config = _make_config(tmp_path, validate_after=True)
    overrides = {**_NOOP_OVERRIDES, "connect": set_server_id}

    with aioresponses() as m:
        m.get(
            f"{STOAT_URL}/servers/{STOAT_SERVER_ID}",
            status=500,
        )
        state = await run_migration(config, events.append, phase_overrides=overrides)

    val_events = [e for e in events if e.phase == "validate_migration"]
    assert any(e.status == "warning" and "skipped" in e.message.lower() for e in val_events)
    # Migration should still complete
    assert state.completed_at != ""


async def test_validation_results_stored_in_state(tmp_path: Path) -> None:
    """Validation results are persisted in state.validation_results and state.json."""
    events: list[MigrationEvent] = []

    async def set_server_id(
        config: FerryConfig,
        state: MigrationState,
        exports: list,
        emit: EventCallback,
    ) -> None:
        state.stoat_server_id = STOAT_SERVER_ID
        state.channel_map = {"d1": "s1"}
        state.role_map = {"r1": "sr1"}

    config = _make_config(tmp_path, validate_after=True)
    overrides = {**_NOOP_OVERRIDES, "connect": set_server_id}

    with aioresponses() as m:
        m.get(
            f"{STOAT_URL}/servers/{STOAT_SERVER_ID}",
            payload={
                "channels": ["s1"],
                "roles": {"sr1": {"name": "role1"}},
            },
        )
        state = await run_migration(config, events.append, phase_overrides=overrides)

    assert state.validation_results != {}
    assert state.validation_results["passed"] is True
    assert state.validation_results["failed_messages"] == 0

    # Verify it's persisted to state.json
    state_path = tmp_path / "state.json"
    assert state_path.exists()
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["validation_results"]["passed"] is True


# ---------------------------------------------------------------------------
# S6: Thread filtering by minimum message count
# ---------------------------------------------------------------------------


def _write_thread_json(
    export_dir: Path,
    *,
    parent_channel: str = "general",
    thread_name: str = "my-thread",
    thread_id: str = "900000000000000001",
    message_count: int = 3,
) -> Path:
    """Write a DCE JSON file with a three-segment (thread) filename."""
    msgs = [_dce_msg_dict(f"m{i}") for i in range(message_count)]
    data = {
        "guild": {"id": "guild1", "name": "Test Guild", "iconUrl": ""},
        "channel": {
            "id": thread_id,
            "type": 11,
            "name": thread_name,
            "categoryId": "",
            "category": "",
            "topic": "",
        },
        "dateRange": {"after": None, "before": None},
        "exportedAt": "2024-01-01T00:00:00+00:00",
        "messageCount": message_count,
        "messages": msgs,
    }
    # Three-segment filename triggers is_thread=True in the parser
    path = export_dir / f"Test Guild - {parent_channel} - {thread_name} [{thread_id}].json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _write_channel_json(
    export_dir: Path,
    *,
    channel_name: str = "general",
    channel_id: str = "800000000000000001",
    message_count: int = 10,
) -> Path:
    """Write a DCE JSON file with a two-segment (regular channel) filename."""
    msgs = [_dce_msg_dict(f"m{i}") for i in range(message_count)]
    data = {
        "guild": {"id": "guild1", "name": "Test Guild", "iconUrl": ""},
        "channel": {
            "id": channel_id,
            "type": 0,
            "name": channel_name,
            "categoryId": "",
            "category": "",
            "topic": "",
        },
        "dateRange": {"after": None, "before": None},
        "exportedAt": "2024-01-01T00:00:00+00:00",
        "messageCount": message_count,
        "messages": msgs,
    }
    path = export_dir / f"Test Guild - {channel_name} [{channel_id}].json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


async def test_thread_below_threshold_excluded(tmp_path: Path) -> None:
    """Thread with fewer messages than threshold is excluded from exports."""
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    output_dir = tmp_path / "output"

    _write_channel_json(
        export_dir, channel_name="general", channel_id="800000000000000001", message_count=20
    )
    _write_thread_json(
        export_dir,
        parent_channel="general",
        thread_name="small-thread",
        thread_id="900000000000000001",
        message_count=3,
    )

    events: list[MigrationEvent] = []
    config = _make_config(
        output_dir,
        export_dir=export_dir,
        min_thread_messages=5,
    )
    await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)

    # The review event should show the thread was filtered
    review_events = [e for e in events if e.phase == "review" and e.status == "confirm"]
    assert len(review_events) == 1
    detail = review_events[0].detail
    assert detail is not None
    assert detail["threads_filtered"] == 1

    # Filtered thread warning event should be emitted
    filter_warnings = [
        e
        for e in events
        if e.phase == "validate" and e.status == "warning" and "filtered out" in e.message
    ]
    assert len(filter_warnings) == 1
    assert "small-thread" in filter_warnings[0].message


async def test_regular_channel_never_filtered(tmp_path: Path) -> None:
    """Regular channels are never filtered regardless of message count or threshold."""
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    output_dir = tmp_path / "output"

    # Regular channel with only 1 message — should NOT be filtered even with high threshold
    _write_channel_json(
        export_dir, channel_name="quiet", channel_id="800000000000000002", message_count=1
    )

    events: list[MigrationEvent] = []
    config = _make_config(
        output_dir,
        export_dir=export_dir,
        min_thread_messages=100,
    )
    await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)

    # No thread filtering warnings
    filter_warnings = [
        e
        for e in events
        if e.phase == "validate" and e.status == "warning" and "filtered out" in e.message
    ]
    assert len(filter_warnings) == 0

    # Review shows 0 threads filtered
    review_events = [e for e in events if e.phase == "review" and e.status == "confirm"]
    assert len(review_events) == 1
    assert review_events[0].detail is not None
    assert review_events[0].detail["threads_filtered"] == 0


async def test_min_thread_messages_zero_includes_all(tmp_path: Path) -> None:
    """Default min_thread_messages=0 includes all threads regardless of message count."""
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    output_dir = tmp_path / "output"

    _write_channel_json(
        export_dir, channel_name="general", channel_id="800000000000000003", message_count=10
    )
    _write_thread_json(
        export_dir,
        parent_channel="general",
        thread_name="tiny-thread",
        thread_id="900000000000000002",
        message_count=1,
    )

    events: list[MigrationEvent] = []
    config = _make_config(
        output_dir,
        export_dir=export_dir,
        min_thread_messages=0,
    )
    await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)

    # No filtering warnings
    filter_warnings = [
        e
        for e in events
        if e.phase == "validate" and e.status == "warning" and "filtered out" in e.message
    ]
    assert len(filter_warnings) == 0

    # Review should show the thread is present (thread_count >= 1)
    review_events = [e for e in events if e.phase == "review" and e.status == "confirm"]
    assert len(review_events) == 1
    assert review_events[0].detail is not None
    assert review_events[0].detail["threads"] >= 1
    assert review_events[0].detail["threads_filtered"] == 0


async def test_filtered_threads_logged_to_warnings(tmp_path: Path) -> None:
    """Filtered threads are recorded in state.warnings with the correct structure."""
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    output_dir = tmp_path / "output"

    _write_channel_json(
        export_dir, channel_name="general", channel_id="800000000000000004", message_count=10
    )
    _write_thread_json(
        export_dir,
        parent_channel="general",
        thread_name="low-activity",
        thread_id="900000000000000003",
        message_count=2,
    )

    events: list[MigrationEvent] = []
    config = _make_config(
        output_dir,
        export_dir=export_dir,
        min_thread_messages=5,
    )
    state = await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)

    # Find the thread_filtered warning in state
    thread_warnings = [w for w in state.warnings if w.get("type") == "thread_filtered"]
    assert len(thread_warnings) == 1
    w = thread_warnings[0]
    assert w["phase"] == "validate"
    assert "low-activity" in w["message"]
    assert "2 messages" in w["message"]
    assert "< 5 threshold" in w["message"]


# ---------------------------------------------------------------------------
# Semaphore initialization
# ---------------------------------------------------------------------------


async def test_semaphore_initialized_during_migration(tmp_path: Path) -> None:
    """run_migration calls init_request_semaphore with max_concurrent_requests."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path, max_concurrent_requests=7)

    with patch("discord_ferry.core.engine.init_request_semaphore") as mock_init:
        await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)

    mock_init.assert_called_once_with(7)


# ---------------------------------------------------------------------------
# S15: Forum index rebuild in REPORT phase
# ---------------------------------------------------------------------------


async def test_forum_index_rebuild_called_when_forum_members_set(tmp_path: Path) -> None:
    """_rebuild_forum_indexes is invoked during REPORT when forum_channel_members is non-empty."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path, dry_run=False)

    with patch("discord_ferry.core.engine._rebuild_forum_indexes") as mock_rebuild:

        async def _channels_with_forum(
            config: FerryConfig,
            state: MigrationState,
            exports: list[Any],
            emit: EventCallback,
        ) -> None:
            state.forum_channel_members["forum-posts"] = ["ch1"]
            state.forum_category_names["forum-posts"] = "Posts"
            state.channel_map["ch1"] = "stoat-ch1"
            state.channel_map["forum-index-forum-posts"] = "stoat-idx-1"

        overrides = dict(_NOOP_OVERRIDES)
        overrides["channels"] = _channels_with_forum
        await run_migration(config, events.append, phase_overrides=overrides)

    mock_rebuild.assert_called_once()


async def test_forum_index_rebuild_skipped_when_dry_run(tmp_path: Path) -> None:
    """_rebuild_forum_indexes is NOT called in dry_run mode."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path, dry_run=True)

    with patch("discord_ferry.core.engine._rebuild_forum_indexes") as mock_rebuild:

        async def _channels_with_forum(
            config: FerryConfig,
            state: MigrationState,
            exports: list[Any],
            emit: EventCallback,
        ) -> None:
            state.forum_channel_members["forum-posts"] = ["ch1"]

        overrides = dict(_NOOP_OVERRIDES)
        overrides["channels"] = _channels_with_forum
        await run_migration(config, events.append, phase_overrides=overrides)

    mock_rebuild.assert_not_called()


async def test_forum_index_rebuild_uses_actual_message_counts(tmp_path: Path) -> None:
    """_rebuild_forum_indexes uses state.channel_message_counts for per-post counts."""
    from discord_ferry.core.engine import _rebuild_forum_indexes

    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(
        stoat_server_id="stoat-srv",
        forum_channel_members={"forum-news": ["disc-ch1", "disc-ch2"]},
        forum_category_names={"forum-news": "News"},
        channel_map={
            "disc-ch1": "stoat-ch1",
            "disc-ch2": "stoat-ch2",
            "forum-index-forum-news": "stoat-idx-news",
        },
        channel_message_counts={"disc-ch1": 42, "disc-ch2": 7},
    )

    with aioresponses() as mock:
        mock.post(
            "https://api.test/channels/stoat-idx-news/messages",
            payload={"_id": "new-idx-msg"},
        )
        mock.put(
            "https://api.test/channels/stoat-idx-news/messages/new-idx-msg/pin",
            payload={},
        )
        import aiohttp

        async with aiohttp.ClientSession() as session:
            config.session = session
            await _rebuild_forum_indexes(config, state, events.append)

    sent_content = [
        e.message for e in events if e.phase == "report" and "Rebuilt" in (e.message or "")
    ]
    assert len(sent_content) == 1
    assert "News" in sent_content[0]


# ---------------------------------------------------------------------------
# S16: Orphan Autumn upload cleanup
# ---------------------------------------------------------------------------


async def test_cleanup_orphans_logs_unreferenced_uploads(tmp_path: Path) -> None:
    """When cleanup_orphans=True, unreferenced uploads are added to state.warnings."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path, cleanup_orphans=True, dry_run=False)

    async def _messages_with_orphans(
        config: FerryConfig,
        state: MigrationState,
        exports: list[Any],
        emit: EventCallback,
    ) -> None:
        state.autumn_uploads = {"aut1": "att1", "aut2": "att2", "aut3": "att3"}
        state.referenced_autumn_ids = {"aut1"}  # aut2 and aut3 are orphans

    overrides = dict(_NOOP_OVERRIDES)
    overrides["messages"] = _messages_with_orphans
    state = await run_migration(config, events.append, phase_overrides=overrides)

    orphan_warnings = [w for w in state.warnings if w.get("type") == "orphan_detected"]
    assert len(orphan_warnings) == 2
    orphan_ids = {w["message"].split(": ")[-1] for w in orphan_warnings}
    assert orphan_ids == {"aut2", "aut3"}


async def test_cleanup_orphans_skipped_when_flag_false(tmp_path: Path) -> None:
    """When cleanup_orphans=False (default), no orphan warnings are emitted."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path, cleanup_orphans=False)

    async def _messages_with_orphans(
        config: FerryConfig,
        state: MigrationState,
        exports: list[Any],
        emit: EventCallback,
    ) -> None:
        state.autumn_uploads = {"aut1": "att1"}
        state.referenced_autumn_ids = set()

    overrides = dict(_NOOP_OVERRIDES)
    overrides["messages"] = _messages_with_orphans
    state = await run_migration(config, events.append, phase_overrides=overrides)

    orphan_warnings = [w for w in state.warnings if w.get("type") == "orphan_detected"]
    assert len(orphan_warnings) == 0


async def test_cleanup_orphans_emits_cleanup_event(tmp_path: Path) -> None:
    """cleanup_orphans emits a 'cleanup' phase event with orphan count."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path, cleanup_orphans=True, dry_run=False)

    async def _messages_with_orphans(
        config: FerryConfig,
        state: MigrationState,
        exports: list[Any],
        emit: EventCallback,
    ) -> None:
        state.autumn_uploads = {"aut1": "att1", "aut2": "att2"}
        state.referenced_autumn_ids = set()

    overrides = dict(_NOOP_OVERRIDES)
    overrides["messages"] = _messages_with_orphans
    await run_migration(config, events.append, phase_overrides=overrides)

    cleanup_events = [e for e in events if e.phase == "cleanup"]
    assert len(cleanup_events) == 1
    assert "2" in cleanup_events[0].message


# ---------------------------------------------------------------------------
# S17: Migration lock
# ---------------------------------------------------------------------------


async def test_migration_lock_acquired_on_existing_server(tmp_path: Path) -> None:
    """Lock is acquired (api_edit_server called) when server_id is provided."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path, server_id="srv-001", dry_run=False)

    with (
        patch("discord_ferry.core.engine.api_fetch_server") as mock_fetch,
        patch("discord_ferry.core.engine.api_edit_server") as mock_edit,
    ):
        mock_fetch.return_value = {"_id": "srv-001", "description": ""}
        await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)

    # api_edit_server should be called at least once to acquire the lock
    assert mock_edit.call_count >= 1


async def test_migration_lock_not_acquired_without_server_id(tmp_path: Path) -> None:
    """Lock is NOT acquired when no server_id is configured."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path, server_id=None, dry_run=False)

    with patch("discord_ferry.core.engine.api_edit_server") as mock_edit:
        await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)

    # api_edit_server should not be called for lock purposes (no server_id)
    mock_edit.assert_not_called()


async def test_migration_lock_raises_on_active_lock(tmp_path: Path) -> None:
    """MigrationError raised when a live lock is found and force_unlock=False."""
    from discord_ferry.errors import MigrationError

    from discord_ferry.core.engine import _acquire_migration_lock

    import aiohttp
    import time

    events: list[MigrationEvent] = []
    config = _make_config(tmp_path, server_id="srv-001", force_unlock=False)
    state = MigrationState()

    fresh_ts = int(time.time()) - 60  # lock from 60 seconds ago (< 24h)
    lock_desc = f"[FERRY_LOCK:{fresh_ts}:other-host]"

    with aioresponses() as mock:
        mock.get(
            "https://api.test/servers/srv-001",
            payload={"_id": "srv-001", "description": lock_desc},
        )
        async with aiohttp.ClientSession() as session:
            with pytest.raises(MigrationError, match="Another migration is in progress"):
                await _acquire_migration_lock(config, state, session, events.append)


async def test_migration_lock_overrides_expired_lock(tmp_path: Path) -> None:
    """Expired lock (>24h) is overridden with a warning."""
    import aiohttp
    import time

    from discord_ferry.core.engine import _acquire_migration_lock

    events: list[MigrationEvent] = []
    config = _make_config(tmp_path, server_id="srv-001", force_unlock=False)
    state = MigrationState()

    old_ts = int(time.time()) - (25 * 3600)  # lock from 25 hours ago
    lock_desc = f"[FERRY_LOCK:{old_ts}:old-host]"

    with aioresponses() as mock:
        mock.get(
            "https://api.test/servers/srv-001",
            payload={"_id": "srv-001", "description": lock_desc},
        )
        mock.patch(
            "https://api.test/servers/srv-001",
            payload={"_id": "srv-001"},
        )
        async with aiohttp.ClientSession() as session:
            result = await _acquire_migration_lock(config, state, session, events.append)

    assert result is True
    lock_warnings = [w for w in state.warnings if w.get("type") == "lock_expired"]
    assert len(lock_warnings) == 1
