"""Tests for incremental/delta migration mode (S19)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest

from discord_ferry.config import FerryConfig
from discord_ferry.core.engine import PhaseFunction, run_migration
from discord_ferry.state import FailedMessage, MigrationState, save_state
from tests.stoat_ids import SERVER_ID
from tests.test_engine import migration_preflight_unit_boundary as migration_preflight_unit_boundary

if TYPE_CHECKING:
    from discord_ferry.core.events import EventCallback, MigrationEvent

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


@pytest.mark.usefixtures("migration_preflight_unit_boundary")
async def test_incremental_loads_prior_state(tmp_path: Path) -> None:
    """Incremental mode carries forward ID maps and offsets from prior state."""
    prior = _make_prior_state(
        tmp_path,
        channel_map={"111": "01J5T000000000000000000111", "222": "01J5T000000000000000000222"},
        role_map={"810": "01J5T000000000000000000810"},
        category_map={"830": "stoat-cat-1"},
        emoji_map={"850": "autumn-em-1"},
        avatar_cache={"user1": "autumn-av-1"},
        stoat_server_id=SERVER_ID,
        autumn_url="https://autumn.test",
        channel_message_offsets={"111": "99999"},
        attachments_uploaded=50,
        attachments_skipped=5,
    )
    # Add a fake message to the message_map so prior_messages_total is non-zero.
    prior.message_map["860"] = "01J5T000000000000000000860"
    save_state(prior, tmp_path)

    events: list[MigrationEvent] = []
    config = _make_config(tmp_path, incremental=True)
    state = await run_migration(config, events.append, phase_overrides=_NOOP_OVERRIDES)

    # ID maps are carried over
    assert state.channel_map == {
        "111": "01J5T000000000000000000111",
        "222": "01J5T000000000000000000222",
    }
    assert state.role_map == {"810": "01J5T000000000000000000810"}
    assert state.category_map == {"830": "stoat-cat-1"}
    assert state.emoji_map == {"850": "autumn-em-1"}
    assert state.avatar_cache == {"user1": "autumn-av-1"}
    assert state.stoat_server_id == SERVER_ID
    assert state.autumn_url == "https://autumn.test"

    # Offsets carried over (channels not completed, so they can receive new messages)
    assert state.channel_message_offsets.get("111") == "99999"
    assert state.completed_channel_ids == set()

    # Cumulative counters carried forward
    assert state.attachments_uploaded >= 50
    assert state.attachments_skipped >= 5

    # prior_messages_total is set from prior run
    assert state.prior_messages_total == 1  # one message in prior message_map

    # Incremental event emitted
    progress_events = [e for e in events if e.status == "progress" and "Incremental" in e.message]
    assert len(progress_events) >= 1


@pytest.mark.usefixtures("migration_preflight_unit_boundary")
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


@pytest.mark.usefixtures("migration_preflight_unit_boundary")
async def test_incremental_skips_old_messages(tmp_path: Path) -> None:
    """SC-19: incremental carries the durable marker the real phase skips against.

    The previous version used a spy messages phase that re-implemented the filter and
    injected a channel_message_offsets value that would not survive a real completion,
    so it passed whether or not the production gate existed. This version asserts the
    durable channel_high_water marker is carried; the real run_messages skip behaviour
    is guarded directly in tests/test_messages.py (SC-1/SC-8).
    """
    _make_prior_state(
        tmp_path,
        channel_map={"870": "01J5T000000000000000000870"},
        channel_high_water={"870": "200"},
        channel_message_offsets={},
    )
    config = _make_config(tmp_path, incremental=True)
    state = await run_migration(config, lambda e: None, phase_overrides=_NOOP_OVERRIDES)
    # The carried durable marker is what a real messages phase would skip against.
    assert state.channel_high_water == {"870": "200"}
    assert state.completed_channel_ids == set()


@pytest.mark.usefixtures("migration_preflight_unit_boundary")
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

    # Prior state only knows about "880", not "new-ch"
    _make_prior_state(
        tmp_path,
        channel_map={"880": "01J5T000000000000000000880"},
        channel_message_offsets={"880": "999"},
    )

    overrides = {**_NOOP_OVERRIDES, "messages": spy_messages_phase}
    config = _make_config(tmp_path, incremental=True)
    await run_migration(config, lambda e: None, phase_overrides=overrides)

    # new-ch has no offset — all its messages should be processed
    assert "100" in processed_ids
    assert "200" in processed_ids
    assert "300" in processed_ids


@pytest.mark.usefixtures("migration_preflight_unit_boundary")
async def test_incremental_carries_forum_state_and_counts(tmp_path: Path) -> None:
    """SC-8: incremental carry-over copies forum fields + counts + category maps.

    Regression: on main only the ID maps are carried, so forum_channel_members,
    forum_category_names, forum_index_message_ids, channel_message_counts,
    category_names, and channel_categories are silently reset to empty — which
    makes the forum-index rebuild re-POST (instead of PATCH) and report delta
    (instead of cumulative) counts on the 2nd run.
    """
    forum_key = "forum-my-forum"
    prior = _make_prior_state(
        tmp_path,
        channel_map={
            "890": "01J5T000000000000000000890",
            f"forum-index-{forum_key}": "01J5T000000000000000000891",
        },
        category_map={forum_key: "stoat-forumcat", "830": "stoat-cat1"},
        category_names={forum_key: "my-forum", "830": "General"},
        forum_channel_members={forum_key: ["890"]},
        forum_category_names={forum_key: "my-forum"},
        forum_index_message_ids={forum_key: "01J5T000000000000000000892"},
        channel_message_counts={"890": 800},
        channel_categories={"890": forum_key, "871": "830"},
        native_fidelity_counts={"slowmode": 2, "user_limit": 1},
    )
    # Non-empty message_map so prior_messages_total is meaningful.
    prior.message_map["861"] = "01J5T000000000000000000861"
    save_state(prior, tmp_path)

    config = _make_config(tmp_path, incremental=True)
    with patch(
        "discord_ferry.core.engine._rebuild_forum_indexes",
        new_callable=AsyncMock,
    ) as rebuild:
        state = await run_migration(config, lambda e: None, phase_overrides=_NOOP_OVERRIDES)

    rebuild.assert_awaited_once()

    # All forum-rebuild + membership fields carried verbatim from the prior run.
    assert state.forum_channel_members == {forum_key: ["890"]}
    assert state.forum_category_names == {forum_key: "my-forum"}
    assert state.forum_index_message_ids == {forum_key: "01J5T000000000000000000892"}
    assert state.channel_message_counts == {"890": 800}
    assert state.category_names == {forum_key: "my-forum", "830": "General"}
    assert state.channel_categories == {"890": forum_key, "871": "830"}
    # native_fidelity_counts carried cumulatively so the report reflects all runs.
    assert state.native_fidelity_counts == {"slowmode": 2, "user_limit": 1}
    # Carried lists are copies, not aliases of the prior structures.
    assert state.forum_channel_members[forum_key] is not prior.forum_channel_members[forum_key]


@pytest.mark.usefixtures("migration_preflight_unit_boundary")
async def test_incremental_delta_stats_in_report(tmp_path: Path) -> None:
    """Report includes delta stats: this_run, cumulative, prior_run_total."""
    from discord_ferry.reporter import generate_report

    prior = _make_prior_state(tmp_path)
    # Simulate 10 messages were migrated in the prior run
    for i in range(10):
        prior.message_map[f"{900 + i}"] = f"01J5T{i:021d}"
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


@pytest.mark.usefixtures("migration_preflight_unit_boundary")
async def test_incremental_carries_channel_high_water(tmp_path: Path) -> None:
    """Incremental carry-over copies channel_high_water and resets completed_channel_ids."""
    _make_prior_state(
        tmp_path,
        channel_map={"871": "01J5T000000000000000000871"},
        channel_high_water={"871": "300"},
    )
    config = _make_config(tmp_path, incremental=True)
    state = await run_migration(config, lambda e: None, phase_overrides=_NOOP_OVERRIDES)
    assert state.channel_high_water == {"871": "300"}
    assert state.completed_channel_ids == set()


@pytest.mark.usefixtures("migration_preflight_unit_boundary")
async def test_incremental_carries_failed_messages(tmp_path: Path) -> None:
    """SC-1: incremental carry-over copies prior.failed_messages forward."""
    _make_prior_state(
        tmp_path,
        channel_map={"871": "01J5T000000000000000000871"},
        failed_messages=[FailedMessage("200", "01J5T000000000000000000871", "boom")],
    )
    config = _make_config(tmp_path, incremental=True)
    state = await run_migration(config, lambda e: None, phase_overrides=_NOOP_OVERRIDES)
    assert [fm.discord_msg_id for fm in state.failed_messages] == ["200"]


@pytest.mark.usefixtures("migration_preflight_unit_boundary")
async def test_incremental_carried_failed_messages_are_independent_copies(tmp_path: Path) -> None:
    """SC-2: mutating a carried FailedMessage must not affect the prior state object."""
    prior = _make_prior_state(
        tmp_path,
        channel_map={"871": "01J5T000000000000000000871"},
        failed_messages=[FailedMessage("200", "01J5T000000000000000000871", "boom", retry_count=2)],
    )
    config = _make_config(tmp_path, incremental=True)
    state = await run_migration(config, lambda e: None, phase_overrides=_NOOP_OVERRIDES)
    state.failed_messages[0].retry_count = 99
    assert prior.failed_messages[0].retry_count == 2  # no aliasing
    assert state.failed_messages[0] is not prior.failed_messages[0]


@pytest.mark.usefixtures("migration_preflight_unit_boundary")
async def test_incremental_carries_pending_emoji_rewrites(tmp_path: Path) -> None:
    """#307: a repair resume record survives an incremental run.

    emoji_map is carried, so a recreated emoji reads as present. Dropping its
    resume record would strand the messages that still hold the old id, and no
    check would catch it. The record must ride along beside emoji_map.
    """
    _make_prior_state(
        tmp_path,
        emoji_map={"851": "new_stoat"},
        pending_emoji_rewrites={"851": {"old": "old_stoat", "new": "new_stoat"}},
    )
    config = _make_config(tmp_path, incremental=True)
    state = await run_migration(config, lambda e: None, phase_overrides=_NOOP_OVERRIDES)
    assert state.pending_emoji_rewrites == {"851": {"old": "old_stoat", "new": "new_stoat"}}


@pytest.mark.usefixtures("migration_preflight_unit_boundary")
async def test_incremental_carries_roles_finalized(tmp_path: Path) -> None:
    """SC-19: incremental carry-over copies prior.roles_finalized (C1)."""
    _make_prior_state(
        tmp_path,
        role_map={"810": "01J5T000000000000000000810"},
        roles_finalized={"810"},
    )
    config = _make_config(tmp_path, incremental=True)
    state = await run_migration(config, lambda e: None, phase_overrides=_NOOP_OVERRIDES)
    assert state.roles_finalized == {"810"}


@pytest.mark.usefixtures("migration_preflight_unit_boundary")
async def test_incremental_seeds_then_carries_roles_finalized_for_old_state(tmp_path: Path) -> None:
    """SC-19 (seed+carry): a completed prior state with no roles_finalized is seeded."""
    # _make_prior_state sets completed_at; roles_finalized defaults empty -> simulates an old ferry.
    _make_prior_state(
        tmp_path,
        role_map={"810": "01J5T000000000000000000810", "811": "01J5T000000000000000000811"},
    )
    config = _make_config(tmp_path, incremental=True)
    state = await run_migration(config, lambda e: None, phase_overrides=_NOOP_OVERRIDES)
    assert state.roles_finalized == {"810", "811"}
