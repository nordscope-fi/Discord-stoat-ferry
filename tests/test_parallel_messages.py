"""Tests for parallel cross-channel message sends (S4)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from aioresponses import aioresponses

from discord_ferry.config import FerryConfig
from discord_ferry.migrator.messages import (
    ChannelResult,
    _merge_channel_result,
    run_messages,
)
from discord_ferry.parser.models import (
    DCEAttachment,
    DCEAuthor,
    DCEChannel,
    DCEEmoji,
    DCEExport,
    DCEGuild,
    DCEMessage,
    DCEReaction,
)
from discord_ferry.state import FailedMessage, MigrationState

if TYPE_CHECKING:
    from pathlib import Path

    from discord_ferry.core.events import MigrationEvent

BASE_URL = "https://stoat.test"
AUTUMN_URL = "https://autumn.test"
TOKEN = "test-token"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path, **overrides: Any) -> FerryConfig:
    defaults: dict[str, Any] = {
        "export_dir": tmp_path,
        "stoat_url": BASE_URL,
        "token": TOKEN,
        "message_rate_limit": 0.0,
        "upload_delay": 0.0,
        "resume": False,
        "max_concurrent_channels": 3,
    }
    defaults.update(overrides)
    return FerryConfig(**defaults)


def _make_state(**overrides: Any) -> MigrationState:
    defaults: dict[str, Any] = {
        "autumn_url": AUTUMN_URL,
    }
    defaults.update(overrides)
    return MigrationState(**defaults)


def _make_guild() -> DCEGuild:
    return DCEGuild(id="guild1", name="Test Guild")


def _make_channel(channel_id: str = "ch1", name: str = "general") -> DCEChannel:
    return DCEChannel(id=channel_id, type=0, name=name)


def _make_export(
    channel_id: str = "ch1",
    name: str = "general",
    messages: list[DCEMessage] | None = None,
) -> DCEExport:
    return DCEExport(
        guild=_make_guild(),
        channel=_make_channel(channel_id=channel_id, name=name),
        messages=messages or [],
    )


def _make_author(id: str = "auth1", name: str = "Alice", **overrides: Any) -> DCEAuthor:
    defaults: dict[str, Any] = {
        "id": id,
        "name": name,
        "nickname": "",
        "color": None,
        "is_bot": False,
        "avatar_url": "",
    }
    defaults.update(overrides)
    return DCEAuthor(**defaults)


def _make_message(
    id: str = "msg1",
    content: str = "hello",
    msg_type: str = "Default",
    timestamp: str = "2024-01-15T12:00:00+00:00",
    **overrides: Any,
) -> DCEMessage:
    defaults: dict[str, Any] = {
        "id": id,
        "type": msg_type,
        "timestamp": timestamp,
        "content": content,
        "author": _make_author(),
        "is_pinned": False,
        "attachments": [],
        "embeds": [],
        "stickers": [],
        "reactions": [],
        "reference": None,
    }
    defaults.update(overrides)
    return DCEMessage(**defaults)


def _collect_events(events: list[MigrationEvent]) -> Any:
    def callback(event: MigrationEvent) -> None:
        events.append(event)

    return callback


@pytest.fixture
def mock_aiohttp() -> aioresponses:
    with aioresponses() as m:
        yield m


# ---------------------------------------------------------------------------
# test_parallel_channels_all_complete
# ---------------------------------------------------------------------------


async def test_parallel_channels_all_complete(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """3 channels processed in parallel, all messages sent and mapped correctly."""
    # Set up 3 channels with 1 message each.
    channel_ids = ["ch1", "ch2", "ch3"]
    stoat_ids = ["stoat_ch1", "stoat_ch2", "stoat_ch3"]

    for stoat_id in stoat_ids:
        mock_aiohttp.post(
            f"{BASE_URL}/channels/{stoat_id}/messages",
            payload={"_id": f"stoat_msg_{stoat_id}"},
        )

    channel_map = dict(zip(channel_ids, stoat_ids))
    state = _make_state(channel_map=channel_map)
    config = _make_config(tmp_path, max_concurrent_channels=3)

    exports = [
        _make_export(
            channel_id=ch_id,
            name=f"channel-{ch_id}",
            messages=[
                _make_message(
                    id=f"msg_{ch_id}",
                    content=f"hello from {ch_id}",
                    timestamp=f"2024-01-15T12:0{i}:00+00:00",
                )
            ],
        )
        for i, ch_id in enumerate(channel_ids)
    ]

    events: list[MigrationEvent] = []
    await run_messages(config, state, exports, _collect_events(events))

    # All 3 messages should be mapped.
    for ch_id in channel_ids:
        assert f"msg_{ch_id}" in state.message_map, f"Message for {ch_id} not in message_map"

    # All 3 channels should be marked complete.
    for ch_id in channel_ids:
        assert ch_id in state.completed_channel_ids

    # Started and completed events emitted.
    statuses = [e.status for e in events]
    assert "started" in statuses
    assert "completed" in statuses


# ---------------------------------------------------------------------------
# test_channel_result_merged_correctly
# ---------------------------------------------------------------------------


def test_channel_result_merged_correctly() -> None:
    """ChannelResult accumulators are merged into state correctly."""
    state = _make_state()
    state.warnings = [{"phase": "existing", "type": "x", "message": "old"}]
    state.attachments_uploaded = 5
    state.attachments_skipped = 1

    result = ChannelResult(
        channel_id="ch1",
        warnings=[{"phase": "messages", "type": "test", "message": "new warning"}],
        errors=[{"phase": "messages", "type": "err", "message": "new error"}],
        failed_messages=[
            FailedMessage(
                discord_msg_id="fm1",
                stoat_channel_id="stoat_ch1",
                error="fail",
            )
        ],
        message_map_updates={"msg1": "stoat_msg1", "msg2": "stoat_msg2"},
        pending_pins=[("stoat_ch1", "stoat_msg1")],
        pending_reactions=[{"channel_id": "stoat_ch1", "message_id": "stoat_msg1", "emoji": "x"}],
        attachments_uploaded=3,
        attachments_skipped=2,
        referenced_autumn_ids={"aut1", "aut2"},
    )

    _merge_channel_result(state, result)

    # Warnings merged (existing + new).
    assert len(state.warnings) == 2
    assert state.warnings[1]["message"] == "new warning"

    # Errors merged.
    assert len(state.errors) == 1

    # Failed messages merged.
    assert len(state.failed_messages) == 1
    assert state.failed_messages[0].discord_msg_id == "fm1"

    # Message map merged.
    assert state.message_map["msg1"] == "stoat_msg1"
    assert state.message_map["msg2"] == "stoat_msg2"

    # Pins and reactions merged.
    assert ("stoat_ch1", "stoat_msg1") in state.pending_pins
    assert len(state.pending_reactions) == 1

    # Counters accumulated.
    assert state.attachments_uploaded == 8  # 5 + 3
    assert state.attachments_skipped == 3  # 1 + 2

    # Autumn IDs merged.
    assert state.referenced_autumn_ids == {"aut1", "aut2"}


# ---------------------------------------------------------------------------
# test_error_in_one_channel_others_continue
# ---------------------------------------------------------------------------


async def test_error_in_one_channel_others_continue(tmp_path: Path) -> None:
    """An error in one channel worker does not prevent other channels from completing."""
    call_count = 0

    async def selective_fail(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if channel_id == "stoat_ch_bad":
            raise RuntimeError("Channel-specific API failure")
        return {"_id": f"stoat_msg_{call_count}"}

    channel_map = {
        "ch_good1": "stoat_ch_good1",
        "ch_bad": "stoat_ch_bad",
        "ch_good2": "stoat_ch_good2",
    }
    state = _make_state(channel_map=channel_map)
    config = _make_config(tmp_path, max_concurrent_channels=3)

    exports = [
        _make_export(
            channel_id="ch_good1",
            name="good1",
            messages=[_make_message(id="msg_g1", content="ok1")],
        ),
        _make_export(
            channel_id="ch_bad",
            name="bad",
            messages=[_make_message(id="msg_bad", content="fail")],
        ),
        _make_export(
            channel_id="ch_good2",
            name="good2",
            messages=[
                _make_message(
                    id="msg_g2",
                    content="ok2",
                    timestamp="2024-01-15T12:01:00+00:00",
                )
            ],
        ),
    ]

    events: list[MigrationEvent] = []
    with patch("discord_ferry.migrator.messages.api_send_message", selective_fail):
        await run_messages(config, state, exports, _collect_events(events))

    # Good channels should have their messages mapped.
    assert "msg_g1" in state.message_map
    assert "msg_g2" in state.message_map

    # Bad channel message should NOT be mapped (it failed).
    assert "msg_bad" not in state.message_map

    # Error should be recorded for the bad message (as a failed_message via channel result).
    assert len(state.failed_messages) == 1
    assert state.failed_messages[0].discord_msg_id == "msg_bad"

    # Both good channels should be completed.
    assert "ch_good1" in state.completed_channel_ids
    assert "ch_good2" in state.completed_channel_ids

    # Bad channel should also be completed (the worker finished, it just had a failed msg).
    assert "ch_bad" in state.completed_channel_ids


# ---------------------------------------------------------------------------
# test_single_channel_works
# ---------------------------------------------------------------------------


async def test_single_channel_works(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """Single channel backward compatibility — same behavior as before parallelism."""
    mock_aiohttp.post(
        f"{BASE_URL}/channels/stoat_ch1/messages",
        payload={"_id": "stoat_msg_1"},
    )
    mock_aiohttp.post(
        f"{BASE_URL}/channels/stoat_ch1/messages",
        payload={"_id": "stoat_msg_2"},
    )

    state = _make_state(
        channel_map={"ch1": "stoat_ch1"},
        emoji_map={"emoji1": "stoat_emoji1"},
    )
    config = _make_config(tmp_path, reaction_mode="native", max_concurrent_channels=1)

    reaction = DCEReaction(emoji=DCEEmoji(id="emoji1", name="fire"), count=2)
    msg1 = _make_message(
        id="msg1",
        content="first",
        timestamp="2024-01-15T10:00:00+00:00",
        is_pinned=True,
    )
    msg2 = _make_message(
        id="msg2",
        content="second",
        timestamp="2024-01-15T11:00:00+00:00",
        reactions=[reaction],
    )
    export = _make_export(messages=[msg1, msg2])

    events: list[MigrationEvent] = []
    await run_messages(config, state, [export], _collect_events(events))

    # Both messages mapped.
    assert state.message_map["msg1"] == "stoat_msg_1"
    assert state.message_map["msg2"] == "stoat_msg_2"

    # Pin queued for msg1.
    assert ("stoat_ch1", "stoat_msg_1") in state.pending_pins

    # Reaction queued for msg2.
    assert len(state.pending_reactions) == 1
    assert state.pending_reactions[0]["emoji"] == "stoat_emoji1"

    # Channel marked as completed.
    assert "ch1" in state.completed_channel_ids

    # Progress events emitted.
    statuses = [e.status for e in events]
    assert "started" in statuses
    assert "completed" in statuses


# ---------------------------------------------------------------------------
# test_cancel_event_stops_all_workers
# ---------------------------------------------------------------------------


async def test_cancel_event_stops_all_workers(tmp_path: Path) -> None:
    """Setting cancel_event stops all channel workers."""
    cancel_event = asyncio.Event()
    sent_count = 0

    async def counting_send(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        nonlocal sent_count
        sent_count += 1
        # Cancel after the first message is sent.
        if sent_count >= 1:
            cancel_event.set()
        return {"_id": f"stoat_msg_{sent_count}"}

    channel_map = {"ch1": "stoat_ch1", "ch2": "stoat_ch2"}
    state = _make_state(channel_map=channel_map)
    config = _make_config(
        tmp_path,
        max_concurrent_channels=1,  # Sequential so cancellation is predictable.
        cancel_event=cancel_event,
    )

    # Create 2 channels, each with many messages.
    exports = []
    for ch_id in ["ch1", "ch2"]:
        msgs = [
            _make_message(
                id=f"msg_{ch_id}_{i}",
                content=f"message {i}",
                timestamp=f"2024-01-15T12:{i:02d}:00+00:00",
            )
            for i in range(10)
        ]
        exports.append(
            _make_export(channel_id=ch_id, name=f"channel-{ch_id}", messages=msgs)
        )

    with patch("discord_ferry.migrator.messages.api_send_message", counting_send):
        # CancelledError from _rate_limit_with_pause will propagate.
        # The gather handles exceptions, so run_messages should complete.
        await run_messages(config, state, exports, lambda e: None)

    # Not all messages should have been sent — cancellation stopped early.
    assert sent_count < 20, f"Expected early stop, but {sent_count} messages were sent"


# ---------------------------------------------------------------------------
# test_max_concurrent_channels_respected
# ---------------------------------------------------------------------------


async def test_max_concurrent_channels_respected(tmp_path: Path) -> None:
    """Channel semaphore limits concurrent channel processing."""
    max_concurrent_seen = 0
    current_concurrent = 0
    lock = asyncio.Lock()

    async def tracking_send(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        nonlocal max_concurrent_seen, current_concurrent
        async with lock:
            current_concurrent += 1
            if current_concurrent > max_concurrent_seen:
                max_concurrent_seen = current_concurrent
        # Small delay to allow overlap detection.
        await asyncio.sleep(0.01)
        async with lock:
            current_concurrent -= 1
        return {"_id": f"stoat_msg_{channel_id}"}

    channel_map = {f"ch{i}": f"stoat_ch{i}" for i in range(5)}
    state = _make_state(channel_map=channel_map)
    config = _make_config(tmp_path, max_concurrent_channels=2)

    exports = [
        _make_export(
            channel_id=f"ch{i}",
            name=f"channel-{i}",
            messages=[_make_message(id=f"msg_ch{i}", content=f"hello {i}")],
        )
        for i in range(5)
    ]

    with patch("discord_ferry.migrator.messages.api_send_message", tracking_send):
        await run_messages(config, state, exports, lambda e: None)

    # Semaphore should have limited to 2.
    assert max_concurrent_seen <= 2, (
        f"Expected max 2 concurrent channels, saw {max_concurrent_seen}"
    )
