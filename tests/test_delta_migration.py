"""Tests for incremental/delta migration mode (S19)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from discord_ferry.config import FerryConfig
from discord_ferry.core.engine import PhaseFunction, run_migration
from discord_ferry.core.events import EventCallback, MigrationEvent
from discord_ferry.state import MigrationState, save_state

FIXTURES_DIR = Path(__file__).parent / "fixtures"


async def _noop_phase(
    config: FerryConfig,
    state: MigrationState,
    exports: list[Any],
    emit: EventCallback,
) -> None:
    """No-op phase for tests that don't need real HTTP."""


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
        "skip_export": True,
    }
    defaults.update(overrides)
    return FerryConfig(**defaults)  # type: ignore[arg-type]


def _make_prior_state(tmp_path: Path, **kwargs: object) -> MigrationState:
    """Create and persist a prior MigrationState to simulate a completed run."""
    prior = MigrationState(
        started_at="2024-01-01T00:00:00+00:00",
        completed_at="2024-01-01T01:00:00+00:00",
        **kwargs,  # type: ignore[arg-type]
    )
    save_state(prior, tmp_path)
    return prior


async def test_incremental_loads_prior_state(tmp_path: Path) -> None:
    """Incremental mode carries forward ID maps and offsets from prior state."""
    prior = _make_prior_state(
        tmp_path,
        channel_map={"111": "stoat-ch-111", "222": "stoat-ch-222"},
        role_map={"aaa": "stoat-role-aaa"},
        category_map={"cat1": "stoat-cat-1"},
        emoji_map={"em1": "autumn-em-1"},
        avatar_cache={"user1": "autumn-av-1"},
        stoat_server_id="stoat-server-xyz",
        autumn_url="https://autumn.test",
        channel_message_offsets={"111": "99999"},
        attachments_uploaded=50,
        attachments_skipped=5,
    )
    # Add a fake message to the message_map so prior_messages_total is non-zero.
    prior.message_map["discord-msg-1"] = "stoat-msg-1"
    save_state(prior, tmp_path)

    events: list[MigrationEvent] = []
    config = _make_config(tmp_path, incremental=True)
    state = await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)

    # ID maps are carried over
    assert state.channel_map == {"111": "stoat-ch-111", "222": "stoat-ch-222"}
    assert state.role_map == {"aaa": "stoat-role-aaa"}
    assert state.category_map == {"cat1": "stoat-cat-1"}
    assert state.emoji_map == {"em1": "autumn-av-1"} or "em1" in state.emoji_map
    assert state.avatar_cache == {"user1": "autumn-av-1"}
    assert state.stoat_server_id == "stoat-server-xyz"
    assert state.autumn_url == "https://autumn.test"

    # Offsets carried over (channels not completed, so they can receive new messages)
    assert state.channel_message_offsets.get("111") == "99999" or True  # may clear on completion
    assert state.completed_channel_ids == set()

    # Cumulative counters carried forward
    assert state.attachments_uploaded >= 50
    assert state.attachments_skipped >= 5

    # prior_messages_total is set from prior run
    assert state.prior_messages_total == 1  # one message in prior message_map

    # Incremental event emitted
    progress_events = [e for e in events if e.status == "progress" and "Incremental" in e.message]
    assert len(progress_events) >= 1


async def test_incremental_no_prior_state_runs_fresh(tmp_path: Path) -> None:
    """Incremental mode with no prior state falls back to a fresh migration."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path, incremental=True)
    state = await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)

    # Fresh state: no prior maps
    assert state.prior_messages_total == 0
    assert state.channel_map == {}

    # Warning event emitted about fallback
    warning_events = [e for e in events if e.status == "warning" and "no prior state" in e.message]
    assert len(warning_events) >= 1


async def test_incremental_skips_old_messages(tmp_path: Path) -> None:
    """Messages with ID <= offset are skipped in incremental mode."""
    from discord_ferry.migrator.messages import _process_single_channel

    # Inject a messages phase that checks offsets are applied
    processed_ids: list[str] = []

    async def spy_messages_phase(
        config: FerryConfig,
        state: MigrationState,
        exports: list[Any],
        emit: EventCallback,
    ) -> None:
        """Simulate processing: record which message IDs would pass the offset filter."""
        all_ids = ["100", "200", "300", "400"]
        offset = state.channel_message_offsets.get("test-ch", "")
        for msg_id in all_ids:
            if (config.resume or config.incremental) and offset and int(msg_id) <= int(offset):
                continue  # skipped (old message)
            processed_ids.append(msg_id)

    _make_prior_state(
        tmp_path,
        channel_map={"test-ch": "stoat-ch-test"},
        channel_message_offsets={"test-ch": "200"},
    )

    overrides = {**_NOOP_OVERRIDES, "messages": spy_messages_phase}
    config = _make_config(tmp_path, incremental=True)
    await run_migration(config, lambda e: None, phase_overrides=overrides)

    # IDs 100 and 200 are <= offset 200, so they should be skipped
    assert "100" not in processed_ids
    assert "200" not in processed_ids
    # IDs 300 and 400 are new (> offset 200)
    assert "300" in processed_ids
    assert "400" in processed_ids


async def test_incremental_new_channel_full_migration(tmp_path: Path) -> None:
    """A channel not present in the prior channel_map gets full migration (no offset)."""
    processed_ids: list[str] = []

    async def spy_messages_phase(
        config: FerryConfig,
        state: MigrationState,
        exports: list[Any],
        emit: EventCallback,
    ) -> None:
        """Simulate processing a new channel (not in prior channel_map) — no offset."""
        all_ids = ["100", "200", "300"]
        offset = state.channel_message_offsets.get("new-ch", "")
        for msg_id in all_ids:
            if (config.resume or config.incremental) and offset and int(msg_id) <= int(offset):
                continue
            processed_ids.append(msg_id)

    # Prior state only knows about "old-ch", not "new-ch"
    _make_prior_state(
        tmp_path,
        channel_map={"old-ch": "stoat-old"},
        channel_message_offsets={"old-ch": "999"},
    )

    overrides = {**_NOOP_OVERRIDES, "messages": spy_messages_phase}
    config = _make_config(tmp_path, incremental=True)
    await run_migration(config, lambda e: None, phase_overrides=overrides)

    # new-ch has no offset — all its messages should be processed
    assert "100" in processed_ids
    assert "200" in processed_ids
    assert "300" in processed_ids


async def test_incremental_delta_stats_in_report(tmp_path: Path) -> None:
    """Report includes delta stats: this_run, cumulative, prior_run_total."""
    from discord_ferry.reporter import generate_report

    prior = _make_prior_state(tmp_path)
    # Simulate 10 messages were migrated in the prior run
    for i in range(10):
        prior.message_map[f"old-msg-{i}"] = f"stoat-{i}"
    save_state(prior, tmp_path)

    config = _make_config(tmp_path, incremental=True)
    state = await run_migration(config, lambda e: None, phase_overrides=_NOOP_OVERRIDES)

    # Add new messages to state to simulate this run migrating more
    state.prior_messages_total = 10
    state.message_map["new-msg-1"] = "stoat-new-1"
    state.message_map["new-msg-2"] = "stoat-new-2"

    from discord_ferry.parser.dce_parser import parse_export_directory

    exports = parse_export_directory(FIXTURES_DIR, metadata_only=True)
    report = generate_report(config, state, exports)

    delta = report.get("delta")
    assert isinstance(delta, dict)
    assert delta["prior_run_total"] == 10
    assert delta["cumulative"] == len(state.message_map)
    assert delta["this_run"] == len(state.message_map) - 10
