"""Tests for the reactions migration phase (Phase 9)."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from discord_ferry.config import FerryConfig
from discord_ferry.migrator.reactions import run_reactions
from discord_ferry.state import MigrationState

BASE_URL = "https://api.test"
TOKEN = "test-token"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(output_dir: Path) -> FerryConfig:
    return FerryConfig(
        export_dir=Path("/tmp"),
        stoat_url=BASE_URL,
        token=TOKEN,
        output_dir=output_dir,
    )


def _make_state(pending: list[dict[str, object]] | None = None) -> MigrationState:
    state = MigrationState()
    state.stoat_server_id = "srv1"
    if pending is not None:
        state.pending_reactions = pending
    return state


def _reaction(
    channel_id: str = "ch1",
    message_id: str = "msg1",
    emoji: str = "\U0001f44d",
) -> dict[str, object]:
    return {"channel_id": channel_id, "message_id": message_id, "emoji": emoji}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_run_reactions_empty_pending(tmp_path: Path) -> None:
    """Emits 'completed' immediately when there are no pending reactions."""
    events: list[Any] = []
    config = _make_config(tmp_path)
    state = _make_state(pending=[])

    await run_reactions(config, state, [], events.append)

    statuses = [e.status for e in events]
    assert "completed" in statuses
    assert state.reactions_applied == 0


async def test_run_reactions_unicode_emoji(tmp_path: Path) -> None:
    """Applies a Unicode emoji reaction successfully."""
    events: list[Any] = []
    config = _make_config(tmp_path)
    state = _make_state(pending=[_reaction(emoji="\U0001f44d")])

    mock_add = AsyncMock(return_value={})
    with (
        patch("discord_ferry.migrator.reactions.api_add_reaction", new=mock_add),
        patch("discord_ferry.migrator.reactions.asyncio.sleep", new=AsyncMock()),
    ):
        await run_reactions(config, state, [], events.append)

    mock_add.assert_awaited_once()
    assert state.reactions_applied == 1


async def test_run_reactions_custom_emoji(tmp_path: Path) -> None:
    """Applies a custom emoji ID reaction successfully."""
    events: list[Any] = []
    config = _make_config(tmp_path)
    state = _make_state(pending=[_reaction(emoji="customEmojiId")])

    mock_add = AsyncMock(return_value={})
    with (
        patch("discord_ferry.migrator.reactions.api_add_reaction", new=mock_add),
        patch("discord_ferry.migrator.reactions.asyncio.sleep", new=AsyncMock()),
    ):
        await run_reactions(config, state, [], events.append)

    mock_add.assert_awaited_once()
    assert state.reactions_applied == 1


async def test_run_reactions_per_message_limit(tmp_path: Path) -> None:
    """Stops adding reactions once a message reaches 20 (Stoat hard limit)."""
    events: list[Any] = []
    config = _make_config(tmp_path)
    # 25 reactions all on the same message.
    pending = [_reaction(message_id="msg1", emoji=f"e{i}") for i in range(25)]
    state = _make_state(pending=pending)

    mock_add = AsyncMock(return_value={})
    with (
        patch("discord_ferry.migrator.reactions.api_add_reaction", new=mock_add),
        patch("discord_ferry.migrator.reactions.asyncio.sleep", new=AsyncMock()),
    ):
        await run_reactions(config, state, [], events.append)

    # Only 20 should be applied.
    assert state.reactions_applied == 20
    assert mock_add.await_count == 20


async def test_run_reactions_error_handling(tmp_path: Path) -> None:
    """Failed reaction stays in pending; successful one is consumed."""
    events: list[Any] = []
    config = _make_config(tmp_path)
    pending = [_reaction(message_id="msg1"), _reaction(message_id="msg2")]  # 1 fails, 1 ok
    state = _make_state(pending=pending)

    call_count = 0

    async def side_effect(*args: Any, **kwargs: Any) -> dict[str, object]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("network error")
        return {}

    with (
        patch("discord_ferry.migrator.reactions.api_add_reaction", new=side_effect),
        patch("discord_ferry.migrator.reactions.asyncio.sleep", new=AsyncMock()),
    ):
        await run_reactions(config, state, [], events.append)

    assert len(state.errors) == 1
    assert state.reactions_applied == 1
    # The failed entry is retained for resume; the successful one consumed.
    assert state.pending_reactions == [_reaction(message_id="msg1")]


async def test_run_reactions_counter_increment(tmp_path: Path) -> None:
    """reactions_applied increments correctly for each successful reaction."""
    events: list[Any] = []
    config = _make_config(tmp_path)
    pending = [_reaction(message_id=f"msg{i}") for i in range(5)]
    state = _make_state(pending=pending)

    mock_add = AsyncMock(return_value={})
    with (
        patch("discord_ferry.migrator.reactions.api_add_reaction", new=mock_add),
        patch("discord_ferry.migrator.reactions.asyncio.sleep", new=AsyncMock()),
    ):
        await run_reactions(config, state, [], events.append)

    assert state.reactions_applied == 5
    completed = [e for e in events if e.status == "completed"]
    assert completed
    assert "5" in completed[0].message


async def test_run_reactions_clears_pending_on_success(tmp_path: Path) -> None:
    """A fully successful run empties pending → reactions_total == applied → 100%."""
    from discord_ferry.stats import summarize_state

    events: list[Any] = []
    config = _make_config(tmp_path)
    state = _make_state(pending=[_reaction(message_id=f"msg{i}") for i in range(3)])

    with (
        patch("discord_ferry.migrator.reactions.api_add_reaction", new=AsyncMock(return_value={})),
        patch("discord_ferry.migrator.reactions.asyncio.sleep", new=AsyncMock()),
    ):
        await run_reactions(config, state, [], events.append)

    assert state.reactions_applied == 3
    assert state.pending_reactions == []
    assert summarize_state(state).fidelity.reactions == 100.0


async def test_run_reactions_resume_no_double_count(tmp_path: Path) -> None:
    """3-of-5 applied then crash; resume applies the remaining 2 → applied == 5, not 8."""
    from discord_ferry.state import load_state

    config = _make_config(tmp_path)
    state = _make_state(pending=[_reaction(message_id=f"msg{i}") for i in range(5)])

    crash_after = 0

    async def crashing(*args: Any, **kwargs: Any) -> dict[str, object]:
        nonlocal crash_after
        crash_after += 1
        if crash_after > 3:
            raise KeyboardInterrupt  # simulate hard crash after 3 applied
        return {}

    with (
        patch("discord_ferry.migrator.reactions.api_add_reaction", new=crashing),
        patch("discord_ferry.migrator.reactions.asyncio.sleep", new=AsyncMock()),
        # Force a checkpoint save after every entry so the crash persists progress.
    ):
        config.checkpoint_interval = 1
        # Make the 5s throttle a no-op: values spaced 10 apart so every checkpoint fires.
        monotonic_patch = patch(
            "discord_ferry.migrator.reactions.time.monotonic",
            side_effect=range(0, 1000, 10),
        )
        with monotonic_patch, contextlib.suppress(KeyboardInterrupt):
            await run_reactions(config, state, [], [].append)

    reloaded = load_state(tmp_path)
    assert reloaded.reactions_applied == 3
    assert len(reloaded.pending_reactions) == 2  # only the un-applied remain

    with (
        patch("discord_ferry.migrator.reactions.api_add_reaction", new=AsyncMock(return_value={})),
        patch("discord_ferry.migrator.reactions.asyncio.sleep", new=AsyncMock()),
    ):
        await run_reactions(config, reloaded, [], [].append)

    assert reloaded.reactions_applied == 5
    assert reloaded.pending_reactions == []


async def test_run_reactions_cap_persists_across_resume(tmp_path: Path) -> None:
    """A message already at the 20 cap is not re-applied past 20 after a resume."""
    config = _make_config(tmp_path)
    state = _make_state(pending=[])
    state.reaction_message_counts = {"msgX": 20}  # already capped in a prior run
    state.pending_reactions = [_reaction(message_id="msgX", emoji=f"e{i}") for i in range(3)]

    mock_add = AsyncMock(return_value={})
    with (
        patch("discord_ferry.migrator.reactions.api_add_reaction", new=mock_add),
        patch("discord_ferry.migrator.reactions.asyncio.sleep", new=AsyncMock()),
    ):
        await run_reactions(config, state, [], [].append)

    assert mock_add.await_count == 0  # all 3 skipped — already at cap
    assert state.pending_reactions == []  # cap-skipped consumed, not retained


async def test_run_reactions_dry_run_reports_full(tmp_path: Path) -> None:
    """Dry-run counts all pending as applied and clears pending → 100%, no API calls."""
    from discord_ferry.stats import summarize_state

    config = _make_config(tmp_path)
    config.dry_run = True
    state = _make_state(pending=[_reaction(message_id=f"msg{i}") for i in range(4)])

    mock_add = AsyncMock(return_value={})
    with patch("discord_ferry.migrator.reactions.api_add_reaction", new=mock_add):
        await run_reactions(config, state, [], [].append)

    assert mock_add.await_count == 0
    assert state.reactions_applied == 4
    assert state.pending_reactions == []
    assert summarize_state(state).fidelity.reactions == 100.0
