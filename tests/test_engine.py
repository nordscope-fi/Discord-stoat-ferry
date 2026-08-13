"""Tests for the migration engine orchestrator."""

import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
from aioresponses import aioresponses

from discord_ferry.config import FerryConfig
from discord_ferry.core import engine as engine_module
from discord_ferry.core.engine import (
    PHASE_ORDER,
    PhaseFunction,
    run_migration,
    run_repair,
    run_retry_failed,
)
from discord_ferry.core.events import EventCallback, MigrationEvent
from discord_ferry.errors import CheckError, DuplicateSendError
from discord_ferry.migrator.verify import CheckReport
from discord_ferry.parser.models import DCEChannel, DCEExport, DCEGuild
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


async def test_run_migration_validates_exports(tmp_path: Path, proxy_env: Any) -> None:
    """Engine parses exports and emits validate events.

    Wrapped in proxy_env() with no arguments: an ambient proxy variable on the
    developer's machine (set by a corp VPN, Docker Desktop, etc.) makes
    format_proxy_notices() append a preflight entry to state.warnings with no
    matching validate-phase warning event, which would otherwise make this
    assertion fail off CI while passing on a clean runner.
    """
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    with proxy_env():
        state = await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)
    validate_events = [e for e in events if e.phase == "validate"]
    assert any(e.status == "started" for e in validate_events)
    assert any(e.status == "completed" for e in validate_events)
    # Validation warnings should be stored in state for the report
    warning_events = [e for e in validate_events if e.status == "warning"]
    assert len(state.warnings) == len(warning_events)
    # Author names should be populated from the fixture exports
    assert len(state.author_names) > 0


async def test_rendered_markdown_warning_does_not_gate_the_migration(tmp_path: Path) -> None:
    """The GUI blocked on this warning and the engine never did, which is the
    real defect in #143. Pin the engine side so nobody closes the gap from the
    wrong end.

    Filters state.warnings by type rather than by length or position: the
    proxy_env note on test_run_migration_validates_exports records that an
    ambient proxy variable adds an unrelated preflight entry.

    The observable is state.current_phase (state.py:102, set at engine.py:690
    and again at :525 once the phase loop returns). state.completed_phases does
    not exist. An engine that stopped at the warning would leave it "".

    Killing: a gate added to the engine after engine.py:401, or one added to
    validate_export that raises instead of returning a warning row."""
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    shutil.copy(FIXTURES_DIR / "markdown_rendered.json", export_dir / "rendered.json")

    events: list[MigrationEvent] = []
    config = _make_config(tmp_path, export_dir=export_dir)
    state = await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)

    types = [w.get("type") for w in state.warnings]
    assert "rendered_markdown" in types
    # The run reached the phase loop and came out the far side.
    assert state.current_phase != ""
    assert any(e.phase == "connect" and e.status == "started" for e in events)


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


async def test_run_migration_resume_after_validate_does_not_crash(tmp_path: Path) -> None:
    """Resume must not crash when current_phase is a terminal value outside PHASE_ORDER.

    Regression: run_migration persists current_phase="validate_migration" when
    validate_after is set and a stoat_server_id exists. A later --resume then called
    PHASE_ORDER.index("validate_migration") -> ValueError before any phase ran. A
    terminal phase means the whole pipeline already completed, so every runnable phase
    should be skipped rather than crashing.
    """
    from discord_ferry.state import save_state

    prior_state = MigrationState(
        current_phase="validate_migration", started_at="2024-01-01T00:00:00+00:00"
    )
    save_state(prior_state, tmp_path)

    events: list[MigrationEvent] = []
    config = _make_config(tmp_path, resume=True)
    # Must not raise ValueError("'validate_migration' is not in list").
    await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)

    # Terminal current_phase => all runnable pipeline phases are already complete.
    for phase in ["connect", "server", "roles", "categories", "channels", "messages"]:
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


async def test_run_migration_sets_source_messages_total(tmp_path: Path) -> None:
    """source_messages_total is set from the parsed exports on a fresh run."""
    from discord_ferry.parser.dce_parser import parse_export_directory

    config = _make_config(tmp_path)
    expected = sum(
        e.message_count for e in parse_export_directory(config.export_dir, metadata_only=True)
    )
    state = await run_migration(config, lambda e: None, phase_overrides=_NOOP_OVERRIDES)
    assert expected > 0
    assert state.source_messages_total == expected


async def test_run_migration_resume_repopulates_source_messages_total(tmp_path: Path) -> None:
    """A resume run re-derives source_messages_total even though MESSAGES is skipped."""
    from discord_ferry.state import load_state, save_state

    prior = MigrationState()
    prior.current_phase = "report"
    prior.source_messages_total = 0  # legacy/never-set
    save_state(prior, tmp_path)

    config = _make_config(tmp_path, resume=True)
    state = await run_migration(config, lambda e: None, phase_overrides=_NOOP_OVERRIDES)
    assert state.source_messages_total > 0
    _ = load_state  # silence unused import if not needed


async def test_run_migration_source_messages_total_post_filter(tmp_path: Path) -> None:
    """source_messages_total reflects only exports that survive the thread filter (R1 guard).

    Fixtures have two thread exports (msg_count=1 and msg_count=2) and four non-thread
    exports. Setting min_thread_messages=5 filters both threads, so the post-filter total
    must be strictly less than the unfiltered total.
    """
    from discord_ferry.parser.dce_parser import parse_export_directory

    all_exports = parse_export_directory(FIXTURES_DIR, metadata_only=True)
    unfiltered_total = sum(e.message_count for e in all_exports)
    # min_thread_messages=5 filters threads with msg_count < 5 (both thread fixtures)
    post_filter_total = sum(
        e.message_count for e in all_exports if not (e.is_thread and e.message_count < 5)
    )
    assert post_filter_total < unfiltered_total, "fixture sanity: filter must actually remove msgs"

    config = _make_config(tmp_path, min_thread_messages=5)
    state = await run_migration(config, lambda e: None, phase_overrides=_NOOP_OVERRIDES)
    assert state.source_messages_total == post_filter_total


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


async def test_retry_failed_populates_the_token_store(tmp_path: Path) -> None:
    """run_retry_failed must build the token store, as both its siblings do.

    ``_ensure_token_store`` is called by ``run_migration`` and ``run_rollback``
    and by nothing else. Without it ``config.token_store`` stays None for the
    whole run, and ``_process_message``'s handler calls
    ``safe_sanitize(config.token_store, str(exc))``, which is an identity no-op
    with a None store. A Stoat token in an exception therefore reaches
    ``state.failed_messages`` and, through it, report.json unredacted.

    The defect was latent only because no command could reach this coroutine.
    ``ferry retry`` makes it reachable, so this is that command's precondition.

    ASSERTS THE CALL AND ITS EFFECT, never that a token is absent from a
    message. An absence assertion passes against a token that simply never
    appeared in that string, and this project shipped unredacted Stoat tokens
    for two releases behind exactly that shape.
    """
    config = _make_retry_config(tmp_path)
    config.output_dir.mkdir(parents=True, exist_ok=True)
    config.token_store = None

    _write_dce_json(config.export_dir, "ch1", [_dce_msg_dict("100000000000000001")])
    exports = _make_exports_from_dir(config.export_dir)

    state = MigrationState(
        channel_map={"800000000000000001": "01JSTOATCHN000000000OLD"},
        autumn_url=AUTUMN_URL,
        failed_messages=[
            FailedMessage(
                discord_msg_id="100000000000000001",
                stoat_channel_id="01JSTOATCHN000000000OLD",
                error="timeout",
            )
        ],
    )

    calls: list[str] = []
    real = engine_module._ensure_token_store

    def _spy(cfg: FerryConfig) -> None:
        calls.append("ensure_token_store")
        real(cfg)

    with (
        patch.object(engine_module, "_ensure_token_store", _spy),
        aioresponses() as m,
    ):
        m.post(
            f"{BASE_URL}/channels/01JSTOATCHN000000000OLD/messages",
            payload={"_id": "01JSTOATMSG0000000000AA"},
        )
        await run_retry_failed(config, state, exports, lambda _e: None)

    assert calls == ["ensure_token_store"], (
        "run_retry_failed did not call _ensure_token_store, so every safe_sanitize "
        "on this path is an identity no-op"
    )
    assert config.token_store is not None, "the call left token_store unset"


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


async def test_retry_failed_deterministic_failure_terminates(tmp_path: Path) -> None:
    """SC-1: a deterministically-failing retry TERMINATES (no mutate-during-iteration hang)
    via the REAL _process_message append path; the message stays failed with retry_count+1.

    M2: patch api_send_message (one level BELOW _process_message), NOT _process_message — so
    the real append to state.failed_messages happens. The pre-fix code (silent return + a
    live-list loop) hangs forever; asyncio.wait_for turns that into a TimeoutError.
    """
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

    async def always_fail_send(*args: Any, **kwargs: Any) -> dict[str, Any]:
        # Yield + throttle: a true await suspension lets asyncio.wait_for enforce its
        # timeout (a synchronous raise would never yield, so a regression would hang
        # UNBOUNDED instead of timing out); the small sleep bounds a regression's growth.
        await asyncio.sleep(0.01)
        raise RuntimeError("still broken")

    events: list[MigrationEvent] = []
    with patch("discord_ferry.migrator.messages.api_send_message", always_fail_send):
        await asyncio.wait_for(run_retry_failed(config, state, exports, events.append), timeout=5.0)

    assert len(state.failed_messages) == 1
    assert state.failed_messages[0].discord_msg_id == "msg_fail"
    assert state.failed_messages[0].retry_count == 1
    assert any("0 succeeded" in e.message and "1 still failed" in e.message for e in events)


async def test_retry_failed_mixed_with_real_failure_terminates(tmp_path: Path) -> None:
    """SC-4: success + real re-failure + not-found, all under a timeout (no hang).

    Uses the real _process_message append path (patches api_send_message) for the failing
    channel so the snapshot-iteration fix is genuinely exercised.
    """
    config = _make_retry_config(tmp_path)
    config.output_dir.mkdir(parents=True, exist_ok=True)
    _write_dce_json(config.export_dir, "ch_ok", [_dce_msg_dict("msg_ok")])
    _write_dce_json(config.export_dir, "ch_fail", [_dce_msg_dict("msg_fail")])
    exports = _make_exports_from_dir(config.export_dir)
    state = MigrationState(
        channel_map={"ch_ok": "stoat_ch_ok", "ch_fail": "stoat_ch_fail"},
        autumn_url=AUTUMN_URL,
        failed_messages=[
            FailedMessage(discord_msg_id="msg_ok", stoat_channel_id="stoat_ch_ok", error="e"),
            FailedMessage(discord_msg_id="msg_fail", stoat_channel_id="stoat_ch_fail", error="e"),
            FailedMessage(discord_msg_id="msg_gone", stoat_channel_id="stoat_ch_ok", error="e"),
        ],
    )

    call = 0

    async def send(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        nonlocal call
        call += 1
        await asyncio.sleep(0.01)  # yield so wait_for can bound a regression
        if channel_id == "stoat_ch_fail":
            raise RuntimeError("still broken")
        return {"_id": f"ok_{call}"}

    events: list[MigrationEvent] = []
    with patch("discord_ferry.migrator.messages.api_send_message", send):
        await asyncio.wait_for(run_retry_failed(config, state, exports, events.append), timeout=5.0)

    ids = {fm.discord_msg_id for fm in state.failed_messages}
    assert ids == {"msg_fail", "msg_gone"}
    assert "msg_ok" in state.message_map
    assert any("1 succeeded" in e.message and "2 still failed" in e.message for e in events)


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
    import time

    import aiohttp

    from discord_ferry.core.engine import _acquire_migration_lock
    from discord_ferry.errors import MigrationError

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
    import time

    import aiohttp

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


# ---------------------------------------------------------------------------
# Post-migration invite generation (T8)
# ---------------------------------------------------------------------------

from discord_ferry.core.engine import _generate_invite, _select_invite_channel  # noqa: E402


def _text_channel_export(channel_id: str, ch_type: int = 0, is_thread: bool = False) -> DCEExport:
    return DCEExport(
        guild=DCEGuild(id="g", name="G"),
        channel=DCEChannel(id=channel_id, type=ch_type, name=f"c-{channel_id}"),
        messages=[],
        message_count=0,
        is_thread=is_thread,
    )


def _exports_with_text_channel(channel_id: str) -> list[DCEExport]:
    return [_text_channel_export(channel_id, ch_type=0)]


def _exports_voice_and_forum(voice_id: str, forum_id: str) -> list[DCEExport]:
    return [
        _text_channel_export(voice_id, ch_type=2),
        _text_channel_export(forum_id, ch_type=15),
    ]


def _exports_voice_only(voice_id: str) -> list[DCEExport]:
    return [_text_channel_export(voice_id, ch_type=2)]


async def test_generate_invite_happy(tmp_path: Path) -> None:
    config = _make_config(tmp_path, create_invite=True)
    state = MigrationState(stoat_server_id="srv1", channel_map={"d_ch": "s_ch"})
    exports = _exports_with_text_channel("d_ch")
    with aioresponses() as m:
        m.get("https://api.test/", payload={"app": "https://app.test", "features": {}})
        m.post("https://api.test/channels/s_ch/invites", payload={"_id": "inv_X"})
        await _generate_invite(config, state, exports, lambda _e: None)
    assert state.invite_code == "inv_X"
    assert state.invite_url == "https://app.test/invite/inv_X"


async def test_generate_invite_bare_code_when_no_app(tmp_path: Path) -> None:
    config = _make_config(tmp_path, create_invite=True)
    state = MigrationState(stoat_server_id="srv1", channel_map={"d_ch": "s_ch"})
    exports = _exports_with_text_channel("d_ch")
    with aioresponses() as m:
        m.get("https://api.test/", payload={"features": {}})  # no "app"
        m.post("https://api.test/channels/s_ch/invites", payload={"_id": "inv_Y"})
        await _generate_invite(config, state, exports, lambda _e: None)
    assert state.invite_code == "inv_Y"
    assert state.invite_url == ""


async def test_generate_invite_idempotent(tmp_path: Path) -> None:
    """An already-populated invite_code → no POST (SC-15). The internal guard
    short-circuits, so no aioresponses mock is registered (a POST would raise)."""
    config = _make_config(tmp_path, create_invite=True)
    state = MigrationState(
        stoat_server_id="srv1", channel_map={"d_ch": "s_ch"}, invite_code="inv_done"
    )
    exports = _exports_with_text_channel("d_ch")
    with aioresponses():
        # No invite mock registered — if a POST fires, aioresponses raises.
        await _generate_invite(config, state, exports, lambda _e: None)
    assert state.invite_code == "inv_done"


async def test_generate_invite_failure_non_fatal(tmp_path: Path) -> None:
    config = _make_config(tmp_path, create_invite=True)
    state = MigrationState(stoat_server_id="srv1", channel_map={"d_ch": "s_ch"})
    exports = _exports_with_text_channel("d_ch")
    with aioresponses() as m:
        m.get("https://api.test/", payload={"app": "https://app.test"})
        m.post("https://api.test/channels/s_ch/invites", status=500, repeat=True)
        await _generate_invite(config, state, exports, lambda _e: None)  # must not raise
    assert state.invite_code == ""
    assert any(w.get("type") == "invite_failed" for w in state.warnings)


def test_select_invite_channel_prefers_forum_over_voice(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    state = MigrationState(channel_map={"d_voice": "s_voice", "d_forum": "s_forum"})
    exports = _exports_voice_and_forum("d_voice", "d_forum")
    assert _select_invite_channel(config, state, exports) == "s_forum"


def test_select_invite_channel_none_when_only_voice(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    state = MigrationState(channel_map={"d_voice": "s_voice"})
    exports = _exports_voice_only("d_voice")
    assert _select_invite_channel(config, state, exports) is None


# ---------------------------------------------------------------------------
# Batch 2 — S1: migration-lock marker plumbing (engine _acquire/_release)
# ---------------------------------------------------------------------------


async def test_acquire_raises_on_live_lock_marker(tmp_path: Path) -> None:
    """S1 SC-4: a live (unexpired) lock marker blocks a concurrent acquire."""
    from discord_ferry.core.engine import _acquire_migration_lock
    from discord_ferry.errors import MigrationError

    config = _make_config(tmp_path, server_id="srv1")
    state = MigrationState()
    with aioresponses() as m:
        # ts far in the future -> age < expiry -> treated as a live lock.
        m.get(
            "https://api.test/servers/srv1",
            payload={"_id": "srv1", "description": "x [FERRY_LOCK:9999999999:host]"},
        )
        async with aiohttp.ClientSession() as session:
            with pytest.raises(MigrationError, match="Another migration is in progress"):
                await _acquire_migration_lock(config, state, session, lambda e: None)


async def test_acquire_sets_and_release_clears_lock_marker(tmp_path: Path) -> None:
    """S1 SC-5: _acquire stashes the marker in state; _release clears it."""
    from discord_ferry.core.engine import _acquire_migration_lock, _release_migration_lock

    config = _make_config(tmp_path, server_id="srv1")
    state = MigrationState()
    with aioresponses() as m:
        m.get(
            "https://api.test/servers/srv1", payload={"_id": "srv1", "description": ""}, repeat=True
        )
        m.patch("https://api.test/servers/srv1", payload={}, repeat=True)
        async with aiohttp.ClientSession() as session:
            acquired = await _acquire_migration_lock(config, state, session, lambda e: None)
            assert acquired is True
            assert state.migration_lock_marker.startswith("[FERRY_LOCK:")
            await _release_migration_lock(config, state, session, lambda e: None)
            assert state.migration_lock_marker == ""


async def test_run_migration_messages_cancel_clean_path(tmp_path: Path) -> None:
    """SC-10: a CancelledError from the messages phase takes the engine's clean-cancel path
    (skipped event 'Cancelled during messages'), NOT phase_failed, and run_migration returns."""

    async def cancelling_messages(
        config: FerryConfig, state: MigrationState, exports: list, emit: EventCallback
    ) -> None:
        raise asyncio.CancelledError("Migration cancelled by user")

    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    overrides = {**_NOOP_OVERRIDES, "messages": cancelling_messages}
    # Must NOT raise — the engine handles CancelledError cleanly and returns.
    await run_migration(config, events.append, phase_overrides=overrides)

    msg_events = [e for e in events if e.phase == "messages"]
    assert any(e.status == "skipped" and "Cancelled" in e.message for e in msg_events)
    assert not any(e.status == "error" for e in msg_events)


async def test_run_migration_messages_crash_keeps_phase_for_resume(tmp_path: Path) -> None:
    """SC-11 (engine): a non-cancel messages-phase crash → MigrationError, with current_phase
    persisted as 'messages' so --resume re-runs it."""
    from discord_ferry.errors import MigrationError
    from discord_ferry.state import load_state

    async def crashing_messages(
        config: FerryConfig, state: MigrationState, exports: list, emit: EventCallback
    ) -> None:
        raise RuntimeError("channel worker boom")

    config = _make_config(tmp_path)
    overrides = {**_NOOP_OVERRIDES, "messages": crashing_messages}
    with pytest.raises(MigrationError, match="messages"):
        await run_migration(config, lambda e: None, phase_overrides=overrides)

    saved = load_state(tmp_path)
    assert saved.current_phase == "messages"


# ---------------------------------------------------------------------------
# Batch 10 / S1 — engine exception-sanitization sweep (_safe)
# ---------------------------------------------------------------------------


async def test_rollback_event_message_redacts_token(tmp_path: Path) -> None:
    """SC-1: an emitted rollback-delete event message redacts a token in the exc."""
    from discord_ferry.core.engine import _delete_one_channel
    from discord_ferry.core.security import SecureTokenStore
    from discord_ferry.errors import MigrationError
    from discord_ferry.state import RollbackProgress

    config = _make_config(tmp_path, token_store=SecureTokenStore({"stoat": "SEKRET-TOKEN-abcd"}))
    state = MigrationState()
    state.rollback_progress = RollbackProgress()
    events: list[MigrationEvent] = []
    sem = asyncio.BoundedSemaphore(1)

    async def _boom(*args: object, **kwargs: object) -> None:
        raise MigrationError("HTTP 500 at https://h/x?token=SEKRET-TOKEN-abcd")

    with patch("discord_ferry.core.engine.api_delete_channel", _boom):
        async with aiohttp.ClientSession() as session:
            await _delete_one_channel("42", sem, config, state, session, events.append)

    messages = [e.message or "" for e in events]
    assert not any("SEKRET-TOKEN-abcd" in m for m in messages)
    assert any("****abcd" in m for m in messages)


async def test_rollback_failure_error_redacts_token(tmp_path: Path) -> None:
    """SC-2: the persisted RollbackFailure.error redacts the token (state.json-bound)."""
    from discord_ferry.core.engine import _delete_one_channel
    from discord_ferry.core.security import SecureTokenStore
    from discord_ferry.errors import MigrationError
    from discord_ferry.state import RollbackProgress

    config = _make_config(tmp_path, token_store=SecureTokenStore({"stoat": "SEKRET-TOKEN-abcd"}))
    state = MigrationState()
    state.rollback_progress = RollbackProgress()
    sem = asyncio.BoundedSemaphore(1)

    async def _boom(*args: object, **kwargs: object) -> None:
        raise MigrationError("token=SEKRET-TOKEN-abcd")

    with patch("discord_ferry.core.engine.api_delete_channel", _boom):
        async with aiohttp.ClientSession() as session:
            await _delete_one_channel("42", sem, config, state, session, lambda e: None)

    assert state.rollback_progress is not None
    error = state.rollback_progress.failures[0].error
    assert "SEKRET-TOKEN-abcd" not in error
    assert "****abcd" in error


async def test_rollback_event_message_none_store_unchanged(tmp_path: Path) -> None:
    """SC-3: with no token store, the message is byte-identical (None-safe no-op)."""
    from discord_ferry.core.engine import _delete_one_channel
    from discord_ferry.errors import MigrationError
    from discord_ferry.state import RollbackProgress

    config = _make_config(tmp_path)  # token_store defaults to None
    assert config.token_store is None
    state = MigrationState()
    state.rollback_progress = RollbackProgress()
    events: list[MigrationEvent] = []
    sem = asyncio.BoundedSemaphore(1)

    async def _boom(*args: object, **kwargs: object) -> None:
        raise MigrationError("plain failure detail")

    with patch("discord_ferry.core.engine.api_delete_channel", _boom):
        async with aiohttp.ClientSession() as session:
            await _delete_one_channel("42", sem, config, state, session, events.append)

    assert any(e.message == "Failed to delete channel 42: plain failure detail" for e in events)


async def test_rollback_success_message_not_sanitized(tmp_path: Path) -> None:
    """SC-4: a non-exception event message is NOT wrapped (negative control).

    The success-path "Deleted channel {id}" message is deliberately left unwrapped.
    A token whose value is a substring of that literal must NOT be redacted — proving
    the sweep did not over-wrap non-exception sites.
    """
    from discord_ferry.core.engine import _delete_one_channel
    from discord_ferry.core.security import SecureTokenStore
    from discord_ferry.state import RollbackProgress

    config = _make_config(tmp_path, token_store=SecureTokenStore({"x": "Deleted"}))
    state = MigrationState()
    state.rollback_progress = RollbackProgress()
    events: list[MigrationEvent] = []
    sem = asyncio.BoundedSemaphore(1)

    async def _ok(*args: object, **kwargs: object) -> None:
        return None

    with patch("discord_ferry.core.engine.api_delete_channel", _ok):
        async with aiohttp.ClientSession() as session:
            await _delete_one_channel("42", sem, config, state, session, events.append)

    assert any(e.message == "Deleted channel 42" for e in events)


def test_ensure_token_store_populates_and_redacts(tmp_path: Path) -> None:
    """_ensure_token_store sets a store that redacts the configured token."""
    from discord_ferry.core.engine import _ensure_token_store

    config = _make_config(tmp_path, token="SEKRET-TOKEN-abcd")
    assert config.token_store is None
    _ensure_token_store(config)
    assert config.token_store is not None
    assert "SEKRET-TOKEN-abcd" not in config.token_store.sanitize("a SEKRET-TOKEN-abcd b")


def test_ensure_token_store_idempotent(tmp_path: Path) -> None:
    """_ensure_token_store does not replace an already-set store."""
    from discord_ferry.core.engine import _ensure_token_store
    from discord_ferry.core.security import SecureTokenStore

    existing = SecureTokenStore({"stoat": "other"})
    config = _make_config(tmp_path, token="SEKRET", token_store=existing)
    _ensure_token_store(config)
    assert config.token_store is existing  # unchanged


async def test_run_rollback_initializes_token_store(tmp_path: Path) -> None:
    """Ship-review fix: run_rollback wires config.token_store so rollback _safe works.

    The store is populated BEFORE input validation, so even an early validation failure
    leaves a populated store — proving the rollback path is no longer redaction-inert.
    """
    from discord_ferry.core.engine import run_rollback
    from discord_ferry.errors import MigrationError

    config = _make_config(tmp_path, token="SEKRET-TOKEN-abcd")
    assert config.token_store is None
    state = MigrationState()  # no stoat_server_id + config has no server_id → validate raises

    with pytest.raises(MigrationError):
        await run_rollback(config, state, [], lambda e: None)

    assert config.token_store is not None
    assert "SEKRET-TOKEN-abcd" not in config.token_store.sanitize("x SEKRET-TOKEN-abcd y")


async def test_preflight_emits_and_persists_a_proxy_notice(
    tmp_path: Path, proxy_env: Any, os_proxy: Any
) -> None:
    """SC-135-40. Killing: emitting an event and never appending, so report.json,
    the artefact users are asked to attach to bug reports, omits the cause."""
    events: list[MigrationEvent] = []
    with os_proxy({}), proxy_env(ALL_PROXY="socks5://sock:1080"):
        state = await run_migration(
            _make_config(tmp_path, dry_run=True), events.append, phase_overrides=_NOOP_OVERRIDES
        )

    assert any(e.status == "notice" and e.phase == "preflight" for e in events)
    assert any(w.get("type", "").startswith("proxy_") for w in state.warnings)


# ---------------------------------------------------------------------------
# Duplicate on the retry path (#107 batch 7, chunk #197, task #206)
# ---------------------------------------------------------------------------


async def test_retry_path_does_not_reraise_on_a_duplicate(tmp_path: Path) -> None:
    """SC-2.9: a duplicate means the message landed, so the retry loop must terminate.

    _process_message re-raises when channel_result is None, which is this path, because
    the retry loop relies on an exception to mark a re-failure. A DuplicateSendError is
    NOT a re-failure: the message is on the server. It never reaches that re-raise,
    because the catch sits inside the send loop.

    This test passes without a source change in this task. Its value is that it stays
    true: moving the catch from inside the loop to around it makes it fail.
    """
    config = _make_retry_config(tmp_path)
    config.output_dir.mkdir(parents=True, exist_ok=True)
    _write_dce_json(config.export_dir, "ch_dup", [_dce_msg_dict("msg_dup")])
    exports = _make_exports_from_dir(config.export_dir)
    state = MigrationState(
        channel_map={"ch_dup": "stoat_ch_dup"},
        autumn_url=AUTUMN_URL,
        failed_messages=[
            FailedMessage(discord_msg_id="msg_dup", stoat_channel_id="stoat_ch_dup", error="e"),
        ],
    )

    async def always_duplicate(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        await asyncio.sleep(0.01)  # yield so wait_for can bound a regression
        raise DuplicateSendError("already on the server")

    events: list[MigrationEvent] = []
    with patch("discord_ferry.migrator.messages.api_send_message", always_duplicate):
        await asyncio.wait_for(run_retry_failed(config, state, exports, events.append), timeout=5.0)

    assert not any(fm.discord_msg_id == "msg_dup" for fm in state.failed_messages), (
        "a message already on the server was left in failed_messages, so it stays "
        "reported as a failure and --incremental will re-send it"
    )
    assert "msg_dup" not in state.message_map, (
        "Stoat returns no id with a 409, so nothing may be mapped for it"
    )


async def test_index_rebuild_duplicate_writes_no_state_entry(tmp_path: Path) -> None:
    """SC-2.7: three id consumers here, and the state write is the one that matters.

    A bad forum_index_message_ids entry persists into state.json and is read back on
    the next run, where it drives an api_edit_message against an id that does not
    exist. Nothing may be written when Stoat returned no id.
    """
    from discord_ferry.core.engine import _rebuild_forum_indexes

    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(
        stoat_server_id="stoat-srv",
        forum_channel_members={"forum-news": ["disc-ch1"]},
        forum_category_names={"forum-news": "News"},
        channel_map={
            "disc-ch1": "stoat-ch1",
            "forum-index-forum-news": "stoat-idx-news",
        },
        channel_message_counts={"disc-ch1": 42},
    )

    pins: list[str] = []

    with aioresponses() as mock:
        mock.post(
            "https://api.test/channels/stoat-idx-news/messages",
            status=409,
            payload={"type": "DuplicateNonce", "location": "crates/x/src/lib.rs:1:1"},
        )
        # Registered so a stray pin is recorded rather than erroring out.
        mock.put(
            "https://api.test/channels/stoat-idx-news/messages/new-idx-msg/pin",
            payload={},
            callback=lambda url, **kwargs: pins.append(str(url)),  # type: ignore[misc]
        )
        import aiohttp

        async with aiohttp.ClientSession() as session:
            config.session = session
            await _rebuild_forum_indexes(config, state, events.append)

    assert pins == [], "a pin was attempted against an id that was never returned"
    assert not state.forum_index_message_ids, (
        "an index message id was recorded for a message whose id Stoat never returned; "
        "this persists into state.json and drives an edit against a nonexistent id "
        f"on the next run. got: {state.forum_index_message_ids}"
    )
    # The two above already hold, because the broad handler catches the duplicate before
    # either statement runs. This one does not: it is the reason the task exists.
    assert not [w for w in state.warnings if w.get("type") == "forum_index_rebuild_failed"], (
        "the rebuild was reported as failed, but the index message is on the server. "
        "Reporting a failure that did not happen is the same defect as the FailedMessage "
        "on the message path, in a different place"
    )


# ---------------------------------------------------------------------------
# thread_strategy is recorded on every state path (#286, SC-1.3 to SC-1.7)
# ---------------------------------------------------------------------------
#
# run_migration resolves state four ways and they all rejoin before the proxy
# preflight loop. The assignment sits at that rejoin point rather than in any
# branch, so a fifth path added later inherits it. Each path gets its own test
# because the natural implementation, setting it beside `state = MigrationState()`,
# covers the fresh path and passes every test of it.


async def test_a_fresh_run_records_the_thread_strategy(tmp_path: Path) -> None:
    """SC-1.3. Neither --resume nor --incremental."""
    config = _make_config(tmp_path, thread_strategy="merge")
    state = await run_migration(config, lambda _e: None, phase_overrides=_NOOP_OVERRIDES)
    assert state.thread_strategy == "merge"


async def test_a_resume_records_the_thread_strategy(tmp_path: Path) -> None:
    """SC-1.4. The mutant this kills is the natural implementation.

    Placing the assignment beside `state = MigrationState()` covers the fresh
    path, and every test of that path passes. Only a resume sees the omission,
    because --resume calls load_state and never constructs. A resumed migration
    would then keep the vague unverifiable wording forever.
    """
    from discord_ferry.state import save_state

    prior = MigrationState(current_phase="channels", started_at="2024-01-01T00:00:00+00:00")
    save_state(prior, tmp_path)

    config = _make_config(tmp_path, resume=True, thread_strategy="archive")
    state = await run_migration(config, lambda _e: None, phase_overrides=_NOOP_OVERRIDES)
    assert state.thread_strategy == "archive"


async def test_an_incremental_with_a_prior_records_the_thread_strategy(tmp_path: Path) -> None:
    """SC-1.5. The carry-over branch constructs, then copies fields forward.

    The strategy is NOT among them: it describes this run, not the prior one.
    """
    from discord_ferry.state import save_state

    prior = MigrationState(
        channel_map={"d-100": "01JSTOATCH00000000000AAA"},
        thread_strategy="flatten",
    )
    save_state(prior, tmp_path)

    config = _make_config(tmp_path, incremental=True, thread_strategy="merge")
    state = await run_migration(config, lambda _e: None, phase_overrides=_NOOP_OVERRIDES)
    assert state.thread_strategy == "merge"


async def test_an_incremental_with_no_prior_records_the_thread_strategy(tmp_path: Path) -> None:
    """SC-1.6. The fallback branch, which constructs fresh and warns."""
    config = _make_config(tmp_path, incremental=True, thread_strategy="archive")
    events: list[MigrationEvent] = []
    state = await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)
    assert state.thread_strategy == "archive"
    assert any("no prior state" in e.message for e in events)


async def test_a_resume_records_the_resuming_runs_strategy(tmp_path: Path) -> None:
    """SC-1.7. The field describes the MOST RECENT run, deliberately.

    A resume under a different --thread-strategy than the original therefore
    describes the resuming run while most of the content was migrated under the
    first. This spec makes that mismatch legible for the first time; it does not
    create it. verify.py's wording must not overclaim on the strength of it.
    """
    from discord_ferry.state import save_state

    prior = MigrationState(
        current_phase="channels",
        started_at="2024-01-01T00:00:00+00:00",
        thread_strategy="flatten",
    )
    save_state(prior, tmp_path)

    config = _make_config(tmp_path, resume=True, thread_strategy="merge")
    state = await run_migration(config, lambda _e: None, phase_overrides=_NOOP_OVERRIDES)
    assert state.thread_strategy == "merge"


async def test_incremental_carries_the_recorded_names(tmp_path: Path) -> None:
    """SC-5.7. Omitting these two carries degrades rename detection to silence.

    A carried channel SKIPS creation entirely: run_channels snapshots
    pre_existing_channel_ids before its create loop and reuses the mapped id
    without calling the API, and roles do the same through
    pre_existing_role_ids. So nothing re-records a name on an incremental run.

    Without the carry, every incremental run ends with an empty name map, and
    ferry check then reports ok for a renamed channel. No existing test sees it,
    which is why this one drives the ids and the names as distinct literals.
    """
    from discord_ferry.state import save_state

    prior = MigrationState(
        channel_map={"d-100": "01JSTOATCH00000000000AAA"},
        created_channel_names={"d-100": "general"},
        role_map={"d-role-1": "01JSTOATRL0000000000AAA"},
        created_role_names={"d-role-1": "mods"},
    )
    save_state(prior, tmp_path)

    config = _make_config(tmp_path, incremental=True)
    state = await run_migration(config, lambda _e: None, phase_overrides=_NOOP_OVERRIDES)

    assert state.created_channel_names == {"d-100": "general"}
    assert state.created_role_names == {"d-role-1": "mods"}


# ---------------------------------------------------------------------------
# Predicate 2: nothing dropped by the carry-over (#288, SC-5.6 to SC-5.11)
# ---------------------------------------------------------------------------


def assert_nothing_dropped(prior: MigrationState, carried: MigrationState) -> None:
    """Every name the PRIOR state held is still held, and still equal.

    NOT a completeness check, and the difference was measured on a prototype
    rather than reasoned about. A completeness predicate over the carried state
    alone cannot tell a dropped carry-over from a legitimate pre-2.17.0 upgrade:
    both leave channel_map populated and created_channel_names empty, which is
    byte-identical. Only taking the prior as an input separates them. Applying
    completeness here would report incomplete on a correct run, which is a false
    alarm against every existing user's first upgrade.

    The `key in carried.channel_map` filter is UNREACHABLE today, because the
    carry-over copies channel_map wholesale, so every prior key is present. It is
    defensive against a future carry-over that selects rather than copies. Do not
    delete it as a dead branch: a surviving mutant is not always a delete
    instruction. test_the_carried_map_filter_is_unreachable_today pins it.
    """
    for key, name in prior.created_channel_names.items():
        if key in carried.channel_map:
            assert carried.created_channel_names.get(key) == name, (
                f"channel {key}: prior recorded {name!r}, carried has "
                f"{carried.created_channel_names.get(key)!r}"
            )
    for key, name in prior.created_role_names.items():
        if key in carried.role_map:
            assert carried.created_role_names.get(key) == name, (
                f"role {key}: prior recorded {name!r}, carried has "
                f"{carried.created_role_names.get(key)!r}"
            )


async def _incremental_from(tmp_path: Path, prior: MigrationState) -> MigrationState:
    """Save *prior*, then run an --incremental migration against it."""
    from discord_ferry.state import save_state

    save_state(prior, tmp_path)
    config = _make_config(tmp_path, incremental=True)
    return await run_migration(config, lambda _e: None, phase_overrides=_NOOP_OVERRIDES)


async def test_a_2_17_prior_is_carried_intact(tmp_path: Path) -> None:
    """SC-5.6. The ordinary case: names recorded, names carried."""
    prior = MigrationState(
        channel_map={"d-100": "01JSTOATCH00000000000AAA"},
        created_channel_names={"d-100": "general"},
    )
    state = await _incremental_from(tmp_path, prior)
    assert_nothing_dropped(prior, state)
    assert state.created_channel_names == {"d-100": "general"}


async def test_an_incremental_from_a_pre_2_17_state_is_not_flagged(tmp_path: Path) -> None:
    """SC-5.8. THE mandatory discriminator, and the reason there are two predicates.

    A 2.16.1 state has channel_map entries and no names, because the fields did
    not exist. That is byte-identical to a carry-over that DROPPED the names, and
    a completeness predicate cannot separate them: it reports incomplete here,
    which is a false alarm on a completely correct run.

    Measured on the prototype at docs/plans/designs, which is gitignored: the
    completeness predicate is wrong on 2 of 8 cases and this is one of them.
    Applying it to an incremental state ships a spurious failure against every
    existing user's first upgrade, while the suite looks thorough.

    DO NOT DELETE THIS TEST, and do not "simplify" the two predicates into one.
    """
    prior = MigrationState(channel_map={"d-100": "01JSTOATCH00000000000AAA"})
    state = await _incremental_from(tmp_path, prior)

    assert_nothing_dropped(prior, state)  # passes: no names recorded, no obligation
    assert state.channel_map == {"d-100": "01JSTOATCH00000000000AAA"}
    assert state.created_channel_names == {}


async def test_a_mixed_carried_and_nameless_prior_is_not_flagged(tmp_path: Path) -> None:
    """SC-5.9. Half a pre-2.17.0 prior, half recorded under 2.17.0.

    The carried nameless entry imposes no obligation and the recorded one is
    honoured. The completeness predicate is wrong here too, which is the second
    of its two measured failures.
    """
    prior = MigrationState(
        channel_map={
            "d-100": "01JSTOATCH00000000000AAA",  # from the old run, no name
            "d-101": "01JSTOATCH00000000000BBB",  # recorded under 2.17.0
        },
        created_channel_names={"d-101": "announcements"},
    )
    state = await _incremental_from(tmp_path, prior)

    assert_nothing_dropped(prior, state)
    assert state.created_channel_names == {"d-101": "announcements"}


def test_the_carried_map_filter_is_unreachable_today() -> None:
    """SC-5.10. Diagnosis, not deletion.

    The `key in carried.channel_map` filter has no input that reaches it today,
    because the carry-over copies channel_map wholesale, so every prior key
    survives. This drives the filter directly to prove it functions, and records
    that nothing in production reaches it, so a later reader sees a diagnosed
    defensive branch rather than an uncovered one to delete.
    """
    prior = MigrationState(
        channel_map={"d-100": "01JSTOATCH00000000000AAA"},
        created_channel_names={"d-100": "general"},
    )
    carried = MigrationState()  # a carry-over that brought nothing forward
    assert_nothing_dropped(prior, carried)  # the filter excludes the absent key


def test_completeness_is_deliberately_not_applied_to_an_incremental_state() -> None:
    """SC-5.11. The omission is a decision, recorded so it is not "fixed".

    assert_names_complete lives in tests/test_structure.py and is applied after a
    FRESH structure run only. It must not be imported here. Applying it to an
    incremental state was measured to raise a false alarm on a legitimate
    pre-2.17.0 upgrade, which is what SC-5.8 above pins.

    This asserts the separation itself: the completeness helper is not reachable
    from this module.
    """
    import tests.test_engine as this_module

    assert not hasattr(this_module, "assert_names_complete"), (
        "assert_names_complete belongs to the fresh-run guard in "
        "tests/test_structure.py. Applying it to an incremental state reports a "
        "false alarm on a legitimate pre-2.17.0 upgrade. See SC-5.8."
    )


async def test_an_invalid_thread_strategy_is_recorded_as_the_effective_one(
    tmp_path: Path,
) -> None:
    """The recorded strategy must describe what RAN, not what was asked for.

    run_messages falls back to "flatten" for any value outside
    _THREAD_STRATEGIES. Recording config.thread_strategy raw would let state.json
    name a strategy the run never used, and ferry check would then report that as
    the cause of an unverifiable result, which is the exact failure this field
    exists to prevent.

    The CLI cannot reach this, because click.Choice validates there. The GUI
    reads its value back from a storage file it does not re-validate, and a
    programmatic FerryConfig is unconstrained. Found by the chunk 1 review.
    """
    config = _make_config(tmp_path, thread_strategy="not-a-strategy")
    state = await run_migration(config, lambda _e: None, phase_overrides=_NOOP_OVERRIDES)
    assert state.thread_strategy == "flatten"


# ---------------------------------------------------------------------------
# run_repair (#107 batch 10, chunk #314)
# ---------------------------------------------------------------------------

R_SERVER = "01JSTOATSRV000000000AAA"
R_D_CHANNEL = "800000000000000001"
R_S_CHANNEL = "01JSTOATCHN000000000OLD"


def _make_repair_config(tmp_path: Path, **overrides: Any) -> FerryConfig:
    """Config for repair tests, mirroring _make_retry_config above.

    Kept as its own helper rather than reusing the retry one, because repair
    reads output_dir for state and export_dir for content, and a test that
    conflated them would pass for the wrong reason.
    """
    export_dir = tmp_path / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    output_dir = tmp_path / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    defaults: dict[str, Any] = {
        "export_dir": export_dir,
        "stoat_url": BASE_URL,
        "token": TOKEN,
        "output_dir": output_dir,
        "message_rate_limit": 0.0,
        "upload_delay": 0.0,
    }
    defaults.update(overrides)
    return FerryConfig(**defaults)


async def test_repair_refuses_a_rolled_back_state_without_a_single_request(
    tmp_path: Path,
) -> None:
    """The most damaging thing this batch could get wrong.

    Rollback NEVER mutates the id maps, deliberately, to preserve the
    migration's audit trail (see RollbackProgress' docstring in state.py). So
    `ferry check` run on a rolled-back state reports channel_missing for EVERY
    channel it mapped, and a repair acting on that report would rebuild a server
    the user deliberately destroyed.

    rollback_progress is set once, at the start of run_rollback, is checkpointed
    on both success and failure, and is never cleared. Refusing whenever it is
    not None therefore also covers a rollback that failed at its very first
    delete, which is the conservative and correct reading.

    ASSERTS ZERO HTTP REQUESTS, not that an error was printed. A repair that
    fetched the server and then refused would pass a message-only assertion
    while having already spent the request that tells an operator it ran.
    """
    from discord_ferry.state import RollbackProgress

    config = _make_repair_config(tmp_path)
    state = MigrationState(
        stoat_server_id=R_SERVER,
        channel_map={R_D_CHANNEL: R_S_CHANNEL},
        rollback_progress=RollbackProgress(started_at="2026-08-13T00:00:00+00:00"),
    )
    events: list[MigrationEvent] = []

    with aioresponses() as m:
        await run_repair(config, state, [], events.append)
        assert not m.requests, (
            f"repair made {len(m.requests)} request(s) against a rolled-back state"
        )

    assert any(e.status == "error" for e in events), "the refusal was not reported"
    assert any("rollback" in e.message.lower() for e in events)


async def test_repair_runs_the_check_when_the_state_was_never_rolled_back(
    tmp_path: Path,
) -> None:
    """The other half of the guard, so the refusal cannot be unconditional.

    Without this, a run_repair that refused everything would pass the test
    above and look correct. The guard has to let an ordinary state through.
    """
    config = _make_repair_config(tmp_path)
    state = MigrationState(stoat_server_id=R_SERVER, channel_map={R_D_CHANNEL: R_S_CHANNEL})
    events: list[MigrationEvent] = []
    seen: dict[str, Any] = {}

    async def _fake_check(stoat_url: str, token: str, st: Any, on_event: Any, **_kw: Any) -> Any:
        seen["called"] = True
        return CheckReport()

    with patch("discord_ferry.migrator.verify.run_check", new=_fake_check):
        await run_repair(config, state, [], events.append)

    assert seen.get("called"), "repair refused a state that was never rolled back"


async def test_repair_populates_the_token_store_and_the_semaphore(tmp_path: Path) -> None:
    """Both asserted as CALLS, for the reason recorded on the retry path.

    A test asserting a token is absent from some string passes against a token
    that simply never appeared in it.
    """
    config = _make_repair_config(tmp_path)
    config.token_store = None
    state = MigrationState(stoat_server_id=R_SERVER)
    order: list[str] = []

    real_store = engine_module._ensure_token_store

    def _spy_store(cfg: FerryConfig) -> None:
        order.append("token_store")
        real_store(cfg)

    async def _fake_check(*_a: Any, **_k: Any) -> Any:
        order.append("check")
        return CheckReport()

    with (
        patch.object(engine_module, "_ensure_token_store", _spy_store),
        patch.object(
            engine_module,
            "init_request_semaphore",
            lambda *_a: order.append("semaphore"),
        ),
        patch("discord_ferry.migrator.verify.run_check", new=_fake_check),
    ):
        await run_repair(config, state, [], lambda _e: None)

    assert "token_store" in order, "repair never built the token store"
    assert "semaphore" in order, "repair never initialised the request semaphore"
    assert order.index("token_store") < order.index("check")
    assert order.index("semaphore") < order.index("check")
    assert config.token_store is not None


def _report_with(kind: str, status: str, *, discord_id: str = R_D_CHANNEL) -> CheckReport:
    """A one-result CheckReport, for driving the partition."""
    report = CheckReport()
    report.add(
        name=f"channel:{discord_id}",
        status=status,  # type: ignore[arg-type]
        kind=kind,
        detail="fixture",
        discord_id=discord_id,
        stoat_id=R_S_CHANNEL,
    )
    return report


def _partition_counts(events: list[MigrationEvent]) -> tuple[int, int]:
    """Read the (structure, tail) work counts out of run_repair's own event.

    Without this the request-count assertions below are INERT until a later
    chunk gives repair something to send: a mis-partition changes which lists
    fill, and with nothing consuming those lists yet no request happens either
    way. Measured, not assumed: swapping the `kind` test for a `status` one, and
    widening the tail set, both survived until these counts were asserted.
    """
    for event in reversed(events):
        match = re.search(r"(\d+) entities to recreate, (\d+) channels", event.message)
        if match:
            return int(match.group(1)), int(match.group(2))
    raise AssertionError(f"run_repair emitted no partition summary: {[e.message for e in events]}")


async def _repair_with_report(
    tmp_path: Path, report: CheckReport
) -> tuple[Any, list[Any], list[MigrationEvent]]:
    """Drive run_repair against a fixed report and hand back the request log."""
    config = _make_repair_config(tmp_path)
    state = MigrationState(stoat_server_id=R_SERVER, channel_map={R_D_CHANNEL: R_S_CHANNEL})
    events: list[MigrationEvent] = []

    async def _fake_check(*_a: Any, **_k: Any) -> CheckReport:
        return report

    with (
        patch("discord_ferry.migrator.verify.run_check", new=_fake_check),
        aioresponses() as m,
    ):
        await run_repair(config, state, [], events.append)
        requests = list(m.requests)
    return state, requests, events


@pytest.mark.parametrize(
    "kind",
    [
        "check_error",
        "tail_not_recorded",
        "tail_window_exhausted",
        "channel_not_visible",
        "category_title_unknown",
    ],
)
async def test_repair_never_acts_on_an_unverifiable_result(tmp_path: Path, kind: str) -> None:
    """The check could not look, so there is nothing to act on.

    Acting anyway is guessing at a live server. Asserts the REQUEST COUNT: an
    assertion that the report is unchanged would pass against a repair that
    sent something and then failed.
    """
    _, requests, events = await _repair_with_report(tmp_path, _report_with(kind, "unverifiable"))
    assert requests == [], f"repair acted on an unverifiable {kind}"
    assert _partition_counts(events) == (0, 0), f"an unverifiable {kind} entered the work lists"


@pytest.mark.parametrize("kind", ["channel_renamed", "role_renamed", "category_title_mismatch"])
async def test_repair_never_acts_on_a_warn_result(tmp_path: Path, kind: str) -> None:
    """A rename almost always means the operator renamed it on purpose.

    A user who asked to fix failures did not ask to have their own edits
    overruled, which is why warn is excluded and fail is not.
    """
    _, requests, events = await _repair_with_report(tmp_path, _report_with(kind, "warn"))
    assert requests == [], f"repair acted on a warn: {kind}"
    assert _partition_counts(events) == (0, 0), f"a warn {kind} entered the work lists"


@pytest.mark.parametrize(
    "kind",
    [
        "tail_present",
        "channel_empty",
        "nothing_expected",
        "tail_not_recorded",
        "tail_window_exhausted",
    ],
)
async def test_repair_never_acts_on_a_non_actionable_tail_kind(tmp_path: Path, kind: str) -> None:
    """Only tail_absent and tail_and_after_absent are repairable tails."""
    status = "fail" if kind == "channel_empty" else "ok"
    _, requests, events = await _repair_with_report(tmp_path, _report_with(kind, status))
    assert requests == [], f"repair acted on a non-actionable tail kind: {kind}"
    assert _partition_counts(events) == (0, 0), f"{kind} entered the work lists"


async def test_repair_declines_a_missing_forum_index_channel(tmp_path: Path) -> None:
    """SC-3.17. A DELIBERATE EXCLUSION, not an unimplemented case.

    The forum index writer stores its channel under a SYNTHETIC key,
    `channel_map["forum-index-{forum_key}"]`, whose value is a real Stoat
    channel id. So the check reports a deleted index as channel_missing exactly
    like any other channel, and a membership test on `kind` alone would sweep it
    into generic recreation.

    Generic recreation cannot restore it. There is no ChannelMeta for a
    synthetic id, the channel-scoped export scan finds zero messages because the
    key names no Discord channel, and nothing rebuilds the index message or
    forum_index_message_ids. _select_invite_channel already excludes the same
    prefix for the same reason. Deferred as #311.

    This is the second level of the lesson the partition comment records: a test
    on `status` would sweep in a future kind, and a test on `kind` alone sweeps
    in a channel TYPE nobody considered.
    """
    forum_key = "forum-index-800000000000000009"
    report = _report_with("channel_missing", "fail", discord_id=forum_key)
    state, requests, events = await _repair_with_report(tmp_path, report)

    assert requests == [], "repair tried to recreate a forum index channel"
    assert _partition_counts(events) == (0, 0), "the forum index entered the work lists"
    assert any(
        w.get("type") == "forum_index_not_repairable" and w.get("phase") == "repair"
        for w in state.warnings
    ), f"no warning names the declined forum index: {state.warnings}"


async def _repair_recording_saves(
    tmp_path: Path, report: CheckReport, **config_overrides: Any
) -> tuple[list[Any], list[Any], list[MigrationEvent]]:
    """Drive run_repair and record every save_state call and HTTP request."""
    config = _make_repair_config(tmp_path, **config_overrides)
    state = MigrationState(stoat_server_id=R_SERVER, channel_map={R_D_CHANNEL: R_S_CHANNEL})
    events: list[MigrationEvent] = []
    saves: list[Any] = []

    async def _fake_check(*_a: Any, **_k: Any) -> CheckReport:
        return report

    with (
        patch("discord_ferry.migrator.verify.run_check", new=_fake_check),
        patch.object(engine_module, "save_state", lambda *a, **k: saves.append(a)),
        aioresponses() as m,
    ):
        await run_repair(config, state, [], events.append)
        requests = list(m.requests)
    return saves, requests, events


async def test_repair_dry_run_never_calls_save_state_at_all(tmp_path: Path) -> None:
    """Not "called with no changes". NEVER CALLED.

    run_check raises outright on a dry-run STATE, because a dry run fills the id
    maps with `dry-` sentinels naming entities nobody created. So a repair that
    wrote sentinels into a real state file would make that state permanently
    uncheckable, which is the exact opposite of what this tool is for.

    Paired with the non-dry-run test below, which is what stops this one being
    inert: an implementation that never saved at all would satisfy this
    assertion and look correct.
    """
    saves, requests, _ = await _repair_recording_saves(
        tmp_path, _report_with("channel_missing", "fail"), dry_run=True
    )
    assert saves == [], "a dry run wrote state"
    assert requests == [], "a dry run made a write request"


async def test_repair_saves_state_once_when_not_a_dry_run(tmp_path: Path) -> None:
    """The other half, so the dry-run assertion above can actually fail.

    Without this, `save_state` removed entirely would pass the dry-run test.
    """
    saves, _, _ = await _repair_recording_saves(tmp_path, _report_with("channel_missing", "fail"))
    assert len(saves) == 1, f"expected exactly one save_state call, saw {len(saves)}"


async def test_repair_dry_run_says_what_it_would_have_done(tmp_path: Path) -> None:
    """A dry run that reports nothing is indistinguishable from a broken one."""
    _, _, events = await _repair_recording_saves(
        tmp_path, _report_with("channel_missing", "fail"), dry_run=True
    )
    assert any("dry run" in e.message.lower() for e in events), (
        f"the dry run never said it was one: {[e.message for e in events]}"
    )
    assert _partition_counts(events) == (1, 0), "the dry run did not report the work it found"


async def test_repair_does_not_swallow_a_check_error(tmp_path: Path) -> None:
    """CheckError propagates to the shell, which turns it into an exit code.

    run_check raises it on a dry-run state and on a state recording no server.
    Catching it here would leave the operator with a repair that reported
    success against a state it never managed to read.
    """
    config = _make_repair_config(tmp_path)
    state = MigrationState(stoat_server_id=R_SERVER)

    async def _raises(*_a: Any, **_k: Any) -> CheckReport:
        raise CheckError("cannot check this migration")

    with (
        patch("discord_ferry.migrator.verify.run_check", new=_raises),
        pytest.raises(CheckError),
    ):
        await run_repair(config, state, [], lambda _e: None)
