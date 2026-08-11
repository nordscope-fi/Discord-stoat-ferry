"""Tests for parallel cross-channel message sends (S4)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from aioresponses import aioresponses

from discord_ferry.config import FerryConfig
from discord_ferry.core.security import SecureTokenStore
from discord_ferry.migrator.messages import (
    ChannelResult,
    _merge_channel_result,
    run_messages,
)
from discord_ferry.parser.models import (
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


async def test_parallel_channels_all_complete(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """3 channels processed in parallel, all messages sent and mapped correctly."""
    # Set up 3 channels with 1 message each.
    channel_ids = ["ch1", "ch2", "ch3"]
    stoat_ids = ["stoat_ch1", "stoat_ch2", "stoat_ch3"]

    for stoat_id in stoat_ids:
        mock_aiohttp.post(
            f"{BASE_URL}/channels/{stoat_id}/messages",
            payload={"_id": f"stoat_msg_{stoat_id}"},
        )

    channel_map = dict(zip(channel_ids, stoat_ids, strict=True))
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


async def test_single_channel_works(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
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
        exports.append(_make_export(channel_id=ch_id, name=f"channel-{ch_id}", messages=msgs))

    # Batch 3 (S2): cancellation now PROPAGATES out of run_messages (intended behaviour
    # change) so the engine takes its clean-cancel path; it no longer "completes".
    with (
        patch("discord_ferry.migrator.messages.api_send_message", counting_send),
        pytest.raises(asyncio.CancelledError),
    ):
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


# ---------------------------------------------------------------------------
# Batch 3 (S2): cancel contract — run_messages must PROPAGATE CancelledError
# ---------------------------------------------------------------------------


async def test_run_messages_raises_on_cancel(tmp_path: Path) -> None:
    """SC-6: cancel during the parallel phase → run_messages raises CancelledError and
    emits NO messages 'completed' event (the engine's clean-cancel path handles it)."""
    cancel_event = asyncio.Event()
    sent = 0

    async def counting_send(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        nonlocal sent
        sent += 1
        cancel_event.set()  # cancel after the first send
        return {"_id": f"id_{sent}"}

    state = _make_state(channel_map={"ch1": "stoat_ch1", "ch2": "stoat_ch2"})
    config = _make_config(tmp_path, max_concurrent_channels=1, cancel_event=cancel_event)
    exports = [
        _make_export(
            channel_id=c,
            name=c,
            messages=[
                _make_message(
                    id=f"{c}_{i}", content="m", timestamp=f"2024-01-15T12:{i:02d}:00+00:00"
                )
                for i in range(5)
            ],
        )
        for c in ("ch1", "ch2")
    ]
    events: list[MigrationEvent] = []
    with (
        patch("discord_ferry.migrator.messages.api_send_message", counting_send),
        pytest.raises(asyncio.CancelledError),
    ):
        await run_messages(config, state, exports, _collect_events(events))

    assert not any(e.status == "completed" and e.phase == "messages" for e in events)


async def test_cancel_preserves_completed_channels(tmp_path: Path) -> None:
    """SC-8: a channel finished before the cancel is checkpointed; the cancelled channel is
    NOT marked complete."""
    cancel_event = asyncio.Event()
    sent = 0

    async def send(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        nonlocal sent
        sent += 1
        if channel_id == "stoat_ch2":
            cancel_event.set()  # cancel once ch2 (processed after ch1) starts sending
        return {"_id": f"id_{sent}"}

    state = _make_state(channel_map={"ch1": "stoat_ch1", "ch2": "stoat_ch2"})
    config = _make_config(tmp_path, max_concurrent_channels=1, cancel_event=cancel_event)
    exports = [
        _make_export(channel_id="ch1", name="ch1", messages=[_make_message(id="a1", content="m")]),
        _make_export(
            channel_id="ch2",
            name="ch2",
            messages=[_make_message(id="b1", content="m", timestamp="2024-01-15T13:00:00+00:00")],
        ),
    ]
    with (
        patch("discord_ferry.migrator.messages.api_send_message", send),
        pytest.raises(asyncio.CancelledError),
    ):
        await run_messages(config, state, exports, lambda e: None)

    assert "ch1" in state.completed_channel_ids  # finished before cancel — survives
    assert "a1" in state.message_map
    assert "ch2" not in state.completed_channel_ids  # cancelled mid-channel — not complete


async def test_cancel_before_first_send_not_marked_complete(tmp_path: Path) -> None:
    """SC-9: cancel detected at the loop-top (before any send) RAISES (not break), so the
    channel is NOT self-marked complete (:748 break→raise)."""
    cancel_event = asyncio.Event()
    cancel_event.set()  # already cancelled before the run starts

    async def send(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        raise AssertionError("should not send when cancelled before the loop starts")

    state = _make_state(channel_map={"ch1": "stoat_ch1"})
    config = _make_config(tmp_path, max_concurrent_channels=1, cancel_event=cancel_event)
    exports = [
        _make_export(channel_id="ch1", name="ch1", messages=[_make_message(id="a1", content="m")])
    ]
    with (
        patch("discord_ferry.migrator.messages.api_send_message", send),
        pytest.raises(asyncio.CancelledError),
    ):
        await run_messages(config, state, exports, lambda e: None)

    assert "ch1" not in state.completed_channel_ids
    assert "ch1" not in state.channel_high_water


# ---------------------------------------------------------------------------
# Batch 3 (S3): resume coverage — a pre-loop worker crash aborts (resumable)
# ---------------------------------------------------------------------------


def _crash_on_progress(bad_name: str) -> Any:
    """An on_event callback that raises BEFORE the per-message loop for *bad_name*.

    The first "progress" event for a channel ('Importing X...') fires at the top of
    _process_single_channel, before the message loop — raising there simulates a worker
    crashing before sending any message (an unexpected setup raise). Only the 'progress'
    status is targeted so the result-loop's later 'warning' emit is not disturbed.
    """

    def cb(event: Any) -> None:
        if event.channel_name == bad_name and event.status == "progress":
            raise RuntimeError(f"pre-loop boom in {bad_name}")

    return cb


async def test_pre_loop_crash_reraises_first_exception(tmp_path: Path) -> None:
    """SC-11/13: a non-cancel worker crash propagates out of run_messages AFTER the good
    channels have self-checkpointed (their work survives); the crashed channel is not
    marked complete and is recorded as a channel_worker_failed error."""
    state = _make_state(
        channel_map={
            "ch_good1": "stoat_good1",
            "ch_bad": "stoat_bad",
            "ch_good2": "stoat_good2",
        }
    )
    config = _make_config(tmp_path, max_concurrent_channels=3)
    exports = [
        _make_export(
            channel_id="ch_good1", name="good1", messages=[_make_message(id="g1", content="m")]
        ),
        _make_export(
            channel_id="ch_bad",
            name="bad",
            messages=[_make_message(id="b", content="m", timestamp="2024-01-15T12:01:00+00:00")],
        ),
        _make_export(
            channel_id="ch_good2",
            name="good2",
            messages=[_make_message(id="g2", content="m", timestamp="2024-01-15T12:02:00+00:00")],
        ),
    ]

    async def ok_send(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        return {"_id": f"id_{channel_id}"}

    with (
        patch("discord_ferry.migrator.messages.api_send_message", ok_send),
        pytest.raises(RuntimeError),
    ):
        await run_messages(config, state, exports, _crash_on_progress("bad"))

    # SC-13: good channels self-checkpointed before the re-raise — their work survives.
    assert "g1" in state.message_map and "g2" in state.message_map
    assert "ch_good1" in state.completed_channel_ids
    assert "ch_good2" in state.completed_channel_ids
    # SC-11: the crashed channel is NOT marked complete (so --resume re-runs it).
    assert "ch_bad" not in state.completed_channel_ids
    # SC-14 (audit signal): a channel_worker_failed error is recorded.
    assert any(e["type"] == "channel_worker_failed" for e in state.errors)


async def test_pre_loop_crash_resume_reruns_only_failed_channel(tmp_path: Path) -> None:
    """SC-12: on --resume the previously-incomplete channel is re-sent while channels already
    in completed_channel_ids are skipped (no duplicate sends)."""
    sends: list[str] = []

    async def record(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        sends.append(channel_id)
        return {"_id": f"id_{len(sends)}"}

    state = _make_state(channel_map={"ch_bad": "stoat_bad", "ch_good": "stoat_good"})
    state.completed_channel_ids.add("ch_good")  # good channel already done in a prior run
    config = _make_config(tmp_path, max_concurrent_channels=2, resume=True)
    exports = [
        _make_export(
            channel_id="ch_good", name="good", messages=[_make_message(id="g", content="m")]
        ),
        _make_export(
            channel_id="ch_bad",
            name="bad",
            messages=[_make_message(id="b", content="m", timestamp="2024-01-15T12:01:00+00:00")],
        ),
    ]
    with patch("discord_ferry.migrator.messages.api_send_message", record):
        await run_messages(config, state, exports, lambda e: None)

    assert "stoat_bad" in sends  # crashed channel re-run
    assert "stoat_good" not in sends  # completed channel skipped (resume gate)
    assert "ch_bad" in state.completed_channel_ids


async def test_channel_worker_failure_is_token_safe(tmp_path: Path) -> None:
    """SC-14: the recorded error and the emitted warning are safe_sanitize'd — no raw token."""
    secret = "stoat_secret_token_value"

    def crash_with_token(event: Any) -> None:
        if event.channel_name == "bad" and event.status == "progress":
            raise RuntimeError(f"failure leaking {secret}")

    captured: list[Any] = []

    def on_event(event: Any) -> None:
        captured.append(event)
        crash_with_token(event)

    state = _make_state(channel_map={"ch_bad": "stoat_bad"})
    config = _make_config(tmp_path, token_store=SecureTokenStore({"stoat": secret}))
    exports = [
        _make_export(channel_id="ch_bad", name="bad", messages=[_make_message(id="b", content="m")])
    ]
    with (
        patch("discord_ferry.migrator.messages.api_send_message", lambda *a, **k: None),
        pytest.raises(RuntimeError),
    ):
        await run_messages(config, state, exports, on_event)

    assert all(secret not in e["message"] for e in state.errors)
    assert all(secret not in (e.message or "") for e in captured if e.status == "warning")


async def test_cancel_precedence_over_crash(tmp_path: Path) -> None:
    """SC-15: when one worker is cancelled and another crashes in the same gather, cancel
    wins (run_messages raises CancelledError) and the crashed channel stays resumable."""
    cancel_event = asyncio.Event()
    cancel_event.set()  # the non-crashing channel will hit the loop-top cancel check

    async def noop_send(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"_id": "x"}

    state = _make_state(channel_map={"ch_cancel": "stoat_cancel", "ch_crash": "stoat_crash"})
    config = _make_config(tmp_path, max_concurrent_channels=2, cancel_event=cancel_event)
    exports = [
        _make_export(
            channel_id="ch_cancel", name="cancel", messages=[_make_message(id="c", content="m")]
        ),
        _make_export(
            channel_id="ch_crash",
            name="crash",
            messages=[_make_message(id="x", content="m", timestamp="2024-01-15T12:01:00+00:00")],
        ),
    ]
    with (
        patch("discord_ferry.migrator.messages.api_send_message", noop_send),
        pytest.raises(asyncio.CancelledError),
    ):
        await run_messages(config, state, exports, _crash_on_progress("crash"))

    assert "ch_crash" not in state.completed_channel_ids


# ---------------------------------------------------------------------------
# Batch 4 (S1): unmapped-emoji reactions are counted + warned (not silently dropped)
# ---------------------------------------------------------------------------


async def test_unmapped_emoji_reaction_dropped_counted(tmp_path: Path) -> None:
    """SC-1/3: a custom reaction whose emoji is not in emoji_map increments reactions_dropped
    (folded once from the per-channel ChannelResult) and records a structured warning."""
    state = _make_state(channel_map={"ch1": "stoat_ch1"})  # emoji_map empty
    config = _make_config(tmp_path, reaction_mode="native")
    msg = _make_message(
        id="m1",
        content="hi",
        reactions=[DCEReaction(emoji=DCEEmoji(id="123", name="party"), count=1)],
    )
    export = _make_export(channel_id="ch1", name="ch1", messages=[msg])
    with patch(
        "discord_ferry.migrator.messages.api_send_message",
        AsyncMock(return_value={"_id": "x"}),
    ):
        await run_messages(config, state, [export], lambda e: None)

    assert state.reactions_dropped == 1
    assert any(w["type"] == "unmapped_emoji_reaction" for w in state.warnings)


async def test_mapped_and_unicode_reactions_not_dropped(tmp_path: Path) -> None:
    """SC-2: a mapped custom reaction and a Unicode reaction are queued, not counted dropped."""
    state = _make_state(channel_map={"ch1": "stoat_ch1"}, emoji_map={"123": "stoat_emoji_1"})
    config = _make_config(tmp_path, reaction_mode="native")
    msg = _make_message(
        id="m1",
        content="hi",
        reactions=[
            DCEReaction(emoji=DCEEmoji(id="123", name="party"), count=1),  # mapped custom
            DCEReaction(emoji=DCEEmoji(id="", name="🔥"), count=1),  # unicode
        ],
    )
    export = _make_export(channel_id="ch1", name="ch1", messages=[msg])
    with patch(
        "discord_ferry.migrator.messages.api_send_message",
        AsyncMock(return_value={"_id": "x"}),
    ):
        await run_messages(config, state, [export], lambda e: None)

    assert state.reactions_dropped == 0
    assert len(state.pending_reactions) == 2  # both queued
    assert not any(w["type"] == "unmapped_emoji_reaction" for w in state.warnings)


async def test_unmapped_emoji_reaction_warning_token_safe(tmp_path: Path) -> None:
    """SC-5: the dropped-reaction warning is safe_sanitize'd (no registered token leaks)."""
    secret = "stoat_secret_token_value"
    state = _make_state(channel_map={"ch1": "stoat_ch1"})
    config = _make_config(
        tmp_path, reaction_mode="native", token_store=SecureTokenStore({"stoat": secret})
    )
    # Contrived: emoji id == the token, so a non-sanitised warning would leak it.
    msg = _make_message(
        id="m1",
        content="hi",
        reactions=[DCEReaction(emoji=DCEEmoji(id=secret, name="x"), count=1)],
    )
    export = _make_export(channel_id="ch1", name="ch1", messages=[msg])
    with patch(
        "discord_ferry.migrator.messages.api_send_message",
        AsyncMock(return_value={"_id": "x"}),
    ):
        await run_messages(config, state, [export], lambda e: None)

    assert state.reactions_dropped == 1
    assert all(secret not in w["message"] for w in state.warnings)


# ---------------------------------------------------------------------------
# Parent-wins on a thread-starter collision (#107 batch 7, chunk #198, task #210)
# ---------------------------------------------------------------------------
#
# Prerequisite work for batch 8 (#110). No symptom at the current DCE 2.47.1 pin.
#
# After DCE 2.47.3 a thread's starter carries the ORIGIN message's id, and a thread's
# channel id EQUALS that origin id, so the parent channel and the thread channel write
# the same message_map key. save_lock serialises the two merges but does not order them,
# and with max_concurrent_channels defaulting to 3 a small thread routinely finishes
# before its large parent. First-wins and last-wins are therefore both wrong.
#
# The discriminator: a thread's map key equals its own channel_id; a parent's never does.


def _collision_pair() -> tuple[ChannelResult, ChannelResult]:
    """A parent and a thread that both write the same key, as DCE 2.47.3 produces."""
    origin_id = "1506019505778987190"
    parent = ChannelResult(
        channel_id="parent_ch",
        message_map_updates={origin_id: "stoat_parent_copy"},
    )
    thread = ChannelResult(
        channel_id=origin_id,  # a thread's channel id IS its origin message id
        message_map_updates={origin_id: "stoat_thread_copy"},
    )
    return parent, thread


def test_parent_wins_when_the_parent_merges_first() -> None:
    """SC-3.1."""
    state = MigrationState()
    parent, thread = _collision_pair()
    _merge_channel_result(state, parent)
    _merge_channel_result(state, thread)
    assert state.message_map["1506019505778987190"] == "stoat_parent_copy"


def test_parent_wins_when_the_thread_merges_first() -> None:
    """SC-3.2. Both orders are required.

    A test running only one order cannot distinguish parent-wins from last-wins, and
    last-wins is one of the two wrong answers this guard exists to rule out.
    """
    state = MigrationState()
    parent, thread = _collision_pair()
    _merge_channel_result(state, thread)
    _merge_channel_result(state, parent)
    assert state.message_map["1506019505778987190"] == "stoat_parent_copy"


def test_a_key_already_present_but_not_the_channel_id_is_still_written() -> None:
    """SC-3.4: the skip needs BOTH halves of the condition.

    An ordinary re-merge of an existing key must not be suppressed. Only the exact
    thread-starter shape is skipped.
    """
    state = MigrationState()
    state.message_map["msg1"] = "old"
    _merge_channel_result(
        state, ChannelResult(channel_id="ch1", message_map_updates={"msg1": "new"})
    )
    assert state.message_map["msg1"] == "new"


def test_a_thread_key_is_written_when_the_parent_has_not_merged_yet() -> None:
    """SC-3.4, the other half: the skip needs the key to be PRESENT already.

    A thread whose parent has not merged is not a collision. Suppressing it would lose
    the only entry the message has.
    """
    state = MigrationState()
    origin_id = "1506019505778987190"
    _merge_channel_result(
        state,
        ChannelResult(channel_id=origin_id, message_map_updates={origin_id: "stoat_thread_copy"}),
    )
    assert state.message_map[origin_id] == "stoat_thread_copy"


# ---------------------------------------------------------------------------
# The batch 8 precondition (#107 batch 7, chunk #198, task #212)
# ---------------------------------------------------------------------------
#
# Batch 7 gates batch 8 (#110, the DCE 2.47.1 -> 2.47.3 bump) on exactly two properties:
# the guard changes nothing at the current pin, and it resolves the collision the bump
# introduces. Both are asserted here so batch 8 has evidence rather than an assurance.


def test_the_guard_is_inert_at_the_2_47_1_pin() -> None:
    """SC-5.1: at the current pin nothing collides, so nothing is skipped.

    Ground truth from the shipped fixture
    'Discord Ferry Test - general - Cool Thread [1506019505778987190].json':
    the thread's channel id is 1506019505778987190 and its starter placeholder carries
    the DIFFERENT synthetic id 1506019526855360593, under type 21.
    """
    thread_channel_id = "1506019505778987190"
    synthetic_starter_id = "1506019526855360593"
    assert thread_channel_id != synthetic_starter_id, (
        "the premise of this test: at 2.47.1 the starter id is synthetic, not the origin id"
    )

    state = MigrationState()
    parent = ChannelResult(
        channel_id="parent_ch",
        message_map_updates={synthetic_starter_id: "stoat_placeholder"},
    )
    thread = ChannelResult(
        channel_id=thread_channel_id,
        message_map_updates={thread_channel_id: "stoat_thread_first"},
    )
    _merge_channel_result(state, parent)
    _merge_channel_result(state, thread)

    # Every key written, nothing suppressed. This is what "batch 7 changes nothing
    # before the bump" means concretely.
    assert state.message_map == {
        synthetic_starter_id: "stoat_placeholder",
        thread_channel_id: "stoat_thread_first",
    }


def test_the_guard_is_active_in_the_post_bump_shape() -> None:
    """SC-5.2: after 2.47.3 the starter carries the origin id, and the parent must win.

    Built now rather than waiting for the bump. The whole point of batch 7 gating
    batch 8 is that this is proven before the pin moves.
    """
    origin_id = "1506019505778987190"  # a thread's channel id IS its origin message id

    for order in ("parent_first", "thread_first"):
        state = MigrationState()
        parent = ChannelResult(
            channel_id="parent_ch",
            message_map_updates={origin_id: "stoat_parent_copy"},
        )
        thread = ChannelResult(
            channel_id=origin_id,
            message_map_updates={origin_id: "stoat_thread_copy"},
        )
        pair = (parent, thread) if order == "parent_first" else (thread, parent)
        for result in pair:
            _merge_channel_result(state, result)

        assert state.message_map[origin_id] == "stoat_parent_copy", (
            f"the thread's copy won in the {order} order; a reply in the parent would "
            "then be sent against the wrong message, and a sent reply cannot be unsent"
        )
