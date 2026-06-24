"""Tests for the pins migration phase (Phase 10)."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from discord_ferry.config import FerryConfig
from discord_ferry.migrator.pins import run_pins
from discord_ferry.state import MigrationState

BASE_URL = "https://api.test"
TOKEN = "test-token"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(output_dir: Path) -> FerryConfig:
    return FerryConfig(
        export_dir=Path("/tmp"), stoat_url=BASE_URL, token=TOKEN, output_dir=output_dir
    )


def _make_state(pending: list[tuple[str, str]] | None = None) -> MigrationState:
    state = MigrationState()
    state.stoat_server_id = "srv1"
    if pending is not None:
        state.pending_pins = pending
    return state


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_run_pins_empty_pending(tmp_path: Path) -> None:
    """Emits 'completed' immediately when there are no pending pins."""
    events: list[Any] = []
    config = _make_config(tmp_path)
    state = _make_state(pending=[])

    await run_pins(config, state, [], events.append)

    statuses = [e.status for e in events]
    assert "completed" in statuses
    assert state.pins_applied == 0


async def test_run_pins_successful_pin(tmp_path: Path) -> None:
    """Pins a message successfully and increments the counter."""
    events: list[Any] = []
    config = _make_config(tmp_path)
    state = _make_state(pending=[("ch1", "msg1")])

    mock_pin = AsyncMock(return_value={})
    with (
        patch("discord_ferry.migrator.pins.api_pin_message", new=mock_pin),
        patch("discord_ferry.migrator.pins.asyncio.sleep", new=AsyncMock()),
    ):
        await run_pins(config, state, [], events.append)

    mock_pin.assert_awaited_once_with(
        mock_pin.call_args[0][0],  # session
        BASE_URL,
        TOKEN,
        "ch1",
        "msg1",
    )
    assert state.pins_applied == 1


async def test_run_pins_error_handling(tmp_path: Path) -> None:
    """Failed pin stays in pending; successful one is consumed."""
    events: list[Any] = []
    config = _make_config(tmp_path)
    state = _make_state(pending=[("ch1", "msg1"), ("ch1", "msg2")])  # 1 fails, 1 ok

    call_count = 0

    async def side_effect(*args: Any, **kwargs: Any) -> dict[str, object]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("pin failed")
        return {}

    with (
        patch("discord_ferry.migrator.pins.api_pin_message", new=side_effect),
        patch("discord_ferry.migrator.pins.asyncio.sleep", new=AsyncMock()),
    ):
        await run_pins(config, state, [], events.append)

    assert len(state.errors) == 1
    assert state.pins_applied == 1
    assert state.pending_pins == [("ch1", "msg1")]


async def test_run_pins_counter_increment(tmp_path: Path) -> None:
    """pins_applied increments correctly for each successful pin."""
    events: list[Any] = []
    config = _make_config(tmp_path)
    pending = [("ch1", f"msg{i}") for i in range(4)]
    state = _make_state(pending=pending)

    mock_pin = AsyncMock(return_value={})
    with (
        patch("discord_ferry.migrator.pins.api_pin_message", new=mock_pin),
        patch("discord_ferry.migrator.pins.asyncio.sleep", new=AsyncMock()),
    ):
        await run_pins(config, state, [], events.append)

    assert state.pins_applied == 4
    completed = [e for e in events if e.status == "completed"]
    assert completed
    assert "4" in completed[0].message


async def test_run_pins_emits_progress_events(tmp_path: Path) -> None:
    """Progress events are emitted with correct current/total values."""
    events: list[Any] = []
    config = _make_config(tmp_path)
    pending = [("ch1", "msgA"), ("ch2", "msgB"), ("ch3", "msgC")]
    state = _make_state(pending=pending)

    mock_pin = AsyncMock(return_value={})
    with (
        patch("discord_ferry.migrator.pins.api_pin_message", new=mock_pin),
        patch("discord_ferry.migrator.pins.asyncio.sleep", new=AsyncMock()),
    ):
        await run_pins(config, state, [], events.append)

    progress_events = [e for e in events if e.status == "progress"]
    assert len(progress_events) == 3
    assert progress_events[0].current == 1
    assert progress_events[0].total == 3
    assert progress_events[2].current == 3


async def test_run_pins_clears_pending_on_success(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    state = _make_state(pending=[("ch1", f"msg{i}") for i in range(4)])

    with (
        patch("discord_ferry.migrator.pins.api_pin_message", new=AsyncMock(return_value={})),
        patch("discord_ferry.migrator.pins.asyncio.sleep", new=AsyncMock()),
    ):
        await run_pins(config, state, [], [].append)

    assert state.pins_applied == 4
    assert state.pending_pins == []


async def test_run_pins_resume_no_double_count(tmp_path: Path) -> None:
    from discord_ferry.state import load_state

    config = _make_config(tmp_path)
    config.checkpoint_interval = 1
    state = _make_state(pending=[("ch1", f"msg{i}") for i in range(5)])

    n = 0

    async def crashing(*args: Any, **kwargs: Any) -> dict[str, object]:
        nonlocal n
        n += 1
        if n > 3:
            raise KeyboardInterrupt
        return {}

    with (
        patch("discord_ferry.migrator.pins.api_pin_message", new=crashing),
        patch("discord_ferry.migrator.pins.asyncio.sleep", new=AsyncMock()),
        patch("discord_ferry.migrator.pins.time.monotonic", side_effect=range(0, 1000, 10)),
        contextlib.suppress(KeyboardInterrupt),
    ):
        await run_pins(config, state, [], [].append)

    reloaded = load_state(tmp_path)
    assert reloaded.pins_applied == 3
    assert len(reloaded.pending_pins) == 2

    with (
        patch("discord_ferry.migrator.pins.api_pin_message", new=AsyncMock(return_value={})),
        patch("discord_ferry.migrator.pins.asyncio.sleep", new=AsyncMock()),
    ):
        await run_pins(config, reloaded, [], [].append)

    assert reloaded.pins_applied == 5
    assert reloaded.pending_pins == []


async def test_run_pins_dry_run_reports_full(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config.dry_run = True
    state = _make_state(pending=[("ch1", f"msg{i}") for i in range(3)])

    mock_pin = AsyncMock(return_value={})
    with patch("discord_ferry.migrator.pins.api_pin_message", new=mock_pin):
        await run_pins(config, state, [], [].append)

    assert mock_pin.await_count == 0
    assert state.pins_applied == 3
    assert state.pending_pins == []
