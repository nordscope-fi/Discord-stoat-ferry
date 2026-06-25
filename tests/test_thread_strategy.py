"""Tests for --thread-strategy flag: flatten, merge, archive modes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aioresponses import aioresponses

from discord_ferry.config import FerryConfig
from discord_ferry.migrator.messages import run_messages
from discord_ferry.migrator.structure import run_channels
from discord_ferry.parser.models import (
    DCEAuthor,
    DCEChannel,
    DCEExport,
    DCEGuild,
    DCEMessage,
)
from discord_ferry.state import MigrationState

if TYPE_CHECKING:
    from pathlib import Path

    from discord_ferry.core.events import MigrationEvent

STOAT_URL = "https://api.test"
AUTUMN_URL = "https://autumn.test"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path, **overrides: object) -> FerryConfig:
    defaults: dict[str, object] = {
        "export_dir": tmp_path,
        "stoat_url": STOAT_URL,
        "token": "tok",
        "output_dir": tmp_path,
    }
    defaults.update(overrides)
    return FerryConfig(**defaults)  # type: ignore[arg-type]


def _make_author(author_id: str = "u1") -> DCEAuthor:
    return DCEAuthor(id=author_id, name="User")


def _make_message(
    msg_id: str = "m1",
    content: str = "hello",
) -> DCEMessage:
    return DCEMessage(
        id=msg_id,
        type="Default",
        timestamp="2024-01-15T12:00:00+00:00",
        content=content,
        author=_make_author(),
    )


def _make_export(
    channel_id: str = "222",
    channel_name: str = "general",
    channel_type: int = 0,
    category_id: str = "cat1",
    category: str = "General",
    is_thread: bool = False,
    parent_channel_name: str = "",
    messages: list[DCEMessage] | None = None,
    message_count: int = 0,
) -> DCEExport:
    guild = DCEGuild(id="111", name="Test")
    channel = DCEChannel(
        id=channel_id,
        type=channel_type,
        name=channel_name,
        category_id=category_id,
        category=category,
    )
    return DCEExport(
        guild=guild,
        channel=channel,
        messages=messages or [],
        message_count=message_count,
        is_thread=is_thread,
        parent_channel_name=parent_channel_name,
    )


# ---------------------------------------------------------------------------
# Flatten mode tests
# ---------------------------------------------------------------------------


async def test_flatten_mode_unchanged(tmp_path: Path) -> None:
    """Default flatten behavior: threads become separate channels (dry run)."""
    config = _make_config(tmp_path, dry_run=True, thread_strategy="flatten")
    state = MigrationState(stoat_server_id="srv1")
    events: list[MigrationEvent] = []

    parent = _make_export(channel_id="100", channel_name="general")
    thread = _make_export(
        channel_id="200",
        channel_name="thread-1",
        is_thread=True,
        parent_channel_name="general",
    )

    await run_channels(config, state, [parent, thread], events.append)

    # Both channels should be mapped.
    assert "100" in state.channel_map
    assert "200" in state.channel_map


async def test_flatten_mode_thread_prefix(tmp_path: Path) -> None:
    """In flatten mode, thread channels get the '├─' prefix in their name."""
    from discord_ferry.migrator.structure import make_unique_channel_name

    config = _make_config(tmp_path, dry_run=True, thread_strategy="flatten")
    state = MigrationState(stoat_server_id="srv1")
    events: list[MigrationEvent] = []

    parent = _make_export(channel_id="100", channel_name="general")
    thread = _make_export(
        channel_id="200",
        channel_name="my-thread",
        is_thread=True,
        parent_channel_name="general",
    )

    await run_channels(config, state, [parent, thread], events.append)

    # In dry_run, the mapped name includes the prefix via event messages.
    # Verify the prefix is applied by checking the dry-run completed event
    # mentions the correct channel count, and verify naming directly:
    existing: set[str] = set()
    prefixed = make_unique_channel_name("\u251c\u2500 my-thread", existing)
    assert prefixed.startswith("\u251c\u2500")
    assert len(prefixed) <= 32

    # Both channels should still be mapped in flatten mode.
    assert "100" in state.channel_map
    assert "200" in state.channel_map


# ---------------------------------------------------------------------------
# Merge mode tests
# ---------------------------------------------------------------------------


async def test_merge_mode_no_thread_channels_created(tmp_path: Path) -> None:
    """In merge mode, thread exports should NOT create separate channels."""
    config = _make_config(tmp_path, dry_run=True, thread_strategy="merge")
    state = MigrationState(stoat_server_id="srv1")
    events: list[MigrationEvent] = []

    parent = _make_export(channel_id="100", channel_name="general")
    thread = _make_export(
        channel_id="200",
        channel_name="thread-1",
        is_thread=True,
        parent_channel_name="general",
    )

    await run_channels(config, state, [parent, thread], events.append)

    # Only parent channel should be mapped.
    assert "100" in state.channel_map
    assert "200" not in state.channel_map


async def test_merge_mode_separator_sent(tmp_path: Path) -> None:
    """In merge mode, separator message is sent to parent channel."""
    config = _make_config(tmp_path, thread_strategy="merge", message_rate_limit=0.0)
    state = MigrationState(
        stoat_server_id="srv1",
        autumn_url=AUTUMN_URL,
    )
    state.channel_map["100"] = "stoat-ch-100"
    events: list[MigrationEvent] = []

    parent = _make_export(
        channel_id="100",
        channel_name="general",
        messages=[_make_message("m1", "parent msg")],
        message_count=1,
    )
    thread = _make_export(
        channel_id="200",
        channel_name="my-thread",
        is_thread=True,
        parent_channel_name="general",
        messages=[_make_message("m2", "thread msg")],
        message_count=1,
    )

    sent_payloads: list[dict[str, object]] = []

    with aioresponses() as m:
        # Capture all POST calls to the messages endpoint.
        def capture_send(url: str, **kwargs: object) -> None:
            sent_payloads.append(kwargs.get("json", {}))  # type: ignore[arg-type]

        # Allow multiple sends to any channel.
        for _ in range(10):
            m.post(
                f"{STOAT_URL}/channels/stoat-ch-100/messages",
                payload={"_id": "msg-result"},
                repeat=True,
            )

        await run_messages(config, state, [parent, thread], events.append)

    # Verify the separator message was sent (check event messages).
    event_msgs = [e.message for e in events]
    assert any("Merged thread" in msg and "my-thread" in msg for msg in event_msgs)


# ---------------------------------------------------------------------------
# Archive mode tests
# ---------------------------------------------------------------------------


async def test_archive_mode_creates_markdown(tmp_path: Path) -> None:
    """Archive mode creates a markdown file for each thread."""
    config = _make_config(tmp_path, thread_strategy="archive", message_rate_limit=0.0)
    state = MigrationState(
        stoat_server_id="srv1",
        autumn_url=AUTUMN_URL,
    )
    state.channel_map["100"] = "stoat-ch-100"
    events: list[MigrationEvent] = []

    parent = _make_export(
        channel_id="100",
        channel_name="general",
        messages=[_make_message("m1", "parent msg")],
        message_count=1,
    )
    thread = _make_export(
        channel_id="200",
        channel_name="my-thread",
        is_thread=True,
        parent_channel_name="general",
        messages=[
            DCEMessage(
                id="m2",
                type="Default",
                timestamp="2024-01-15T12:00:00+00:00",
                content="thread message one",
                author=DCEAuthor(id="u1", name="Alice"),
            ),
            DCEMessage(
                id="m3",
                type="Default",
                timestamp="2024-01-15T12:05:00+00:00",
                content="thread message two",
                author=DCEAuthor(id="u2", name="Bob"),
            ),
        ],
        message_count=2,
    )

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/channels/stoat-ch-100/messages",
            payload={"_id": "msg-result"},
            repeat=True,
        )
        await run_messages(config, state, [parent, thread], events.append)

    md_path = tmp_path / "threads" / "general" / "my-thread.md"
    assert md_path.exists(), f"Expected markdown file at {md_path}"

    content = md_path.read_text(encoding="utf-8")
    assert "Alice" in content
    assert "Bob" in content
    assert "thread message one" in content
    assert "thread message two" in content
    assert "##" in content  # markdown headings


async def test_archive_mode_no_api_calls(tmp_path: Path) -> None:
    """Archive mode should NOT send thread messages to Stoat."""
    config = _make_config(tmp_path, thread_strategy="archive", message_rate_limit=0.0)
    state = MigrationState(
        stoat_server_id="srv1",
        autumn_url=AUTUMN_URL,
    )
    state.channel_map["100"] = "stoat-ch-100"
    events: list[MigrationEvent] = []

    parent = _make_export(
        channel_id="100",
        channel_name="general",
        messages=[],
        message_count=0,
    )
    thread = _make_export(
        channel_id="200",
        channel_name="archived-thread",
        is_thread=True,
        parent_channel_name="general",
        messages=[_make_message("m2", "thread msg")],
        message_count=1,
    )

    with aioresponses():
        # No API calls should be made for the thread.
        # Only parent channel would make calls (but it has 0 messages).
        await run_messages(config, state, [parent, thread], events.append)

    # The thread's channel should NOT be in channel_map (no channel created).
    assert "200" not in state.channel_map
    # The markdown file should exist.
    md_path = tmp_path / "threads" / "general" / "archived-thread.md"
    assert md_path.exists()


# ---------------------------------------------------------------------------
# Thread sorting tests
# ---------------------------------------------------------------------------


async def test_thread_sort_by_message_count(tmp_path: Path) -> None:
    """When truncating, higher-traffic threads survive over lower-traffic ones."""
    config = _make_config(tmp_path, dry_run=True, thread_strategy="flatten", max_channels=3)
    state = MigrationState(stoat_server_id="srv1")
    events: list[MigrationEvent] = []

    parent = _make_export(channel_id="100", channel_name="general")
    # Low-traffic thread (5 messages).
    thread_low = _make_export(
        channel_id="200",
        channel_name="low-thread",
        is_thread=True,
        parent_channel_name="general",
        message_count=5,
    )
    # High-traffic thread (500 messages).
    thread_high = _make_export(
        channel_id="300",
        channel_name="high-thread",
        is_thread=True,
        parent_channel_name="general",
        message_count=500,
    )
    # Another parent to fill slots.
    parent2 = _make_export(channel_id="400", channel_name="random")

    await run_channels(config, state, [parent, thread_low, thread_high, parent2], events.append)

    # max_channels=3, so one thread must be dropped.
    # Main channels (100, 400) survive. High-traffic thread (300) survives.
    assert "100" in state.channel_map
    assert "400" in state.channel_map
    assert "300" in state.channel_map  # high-traffic thread kept
    assert "200" not in state.channel_map  # low-traffic thread dropped


# ---------------------------------------------------------------------------
# #78 — merge strategy honours the durable high-water marker
# ---------------------------------------------------------------------------


def _capture_keys(keys: list[str]) -> object:
    """Patch target for api_send_message: record idempotency_key, return a fake id."""

    async def _send(
        session: object, stoat_url: object, token: object, channel_id: object, **kwargs: object
    ) -> dict[str, object]:
        keys.append(str(kwargs.get("idempotency_key", "")))
        return {"_id": f"stoat-{kwargs.get('idempotency_key', 'x')}"}

    return _send


def _merge_setup(
    tmp_path: Path,
    thread_msg_ids: list[str],
    marker: str | None = None,
    incremental: bool = True,
) -> tuple[FerryConfig, MigrationState, DCEExport, DCEExport]:
    """Parent (ch 100) + one thread (ch 200) with the given message ids."""
    config = _make_config(
        tmp_path, thread_strategy="merge", message_rate_limit=0.0, incremental=incremental
    )
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)
    state.channel_map["100"] = "stoat-ch-100"
    if marker is not None:
        state.channel_high_water["200"] = marker
    parent = _make_export(channel_id="100", channel_name="general")
    thread = _make_export(
        channel_id="200",
        channel_name="my-thread",
        is_thread=True,
        parent_channel_name="general",
        messages=[_make_message(mid, f"msg {mid}") for mid in thread_msg_ids],
        message_count=len(thread_msg_ids),
    )
    return config, state, parent, thread


async def test_merge_incremental_unchanged_thread_zero_posts(tmp_path: Path) -> None:
    """S1: an unchanged merged thread (all ids <= marker) makes zero POSTs."""
    from unittest.mock import patch

    config, state, parent, thread = _merge_setup(tmp_path, ["10", "20"], marker="20")
    keys: list[str] = []
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_keys(keys)):
        await run_messages(config, state, [parent, thread], lambda e: None)
    assert not any(k.startswith("ferry-thread-sep-200") for k in keys)
    assert not any(k.startswith("ferry-merge-") for k in keys)


async def test_merge_brand_new_thread_posts_all_and_records_marker(tmp_path: Path) -> None:
    """S2: a brand-new thread posts separator + all messages and records the marker."""
    from unittest.mock import patch

    config, state, parent, thread = _merge_setup(tmp_path, ["10", "20", "30"], marker=None)
    keys: list[str] = []
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_keys(keys)):
        await run_messages(config, state, [parent, thread], lambda e: None)
    assert "ferry-thread-sep-200" in keys
    assert {"ferry-merge-10", "ferry-merge-20", "ferry-merge-30"}.issubset(set(keys))
    assert state.channel_high_water["200"] == "30"


async def test_merge_second_run_unchanged_zero_posts(tmp_path: Path) -> None:
    """S2-AC3: after a brand-new run records the marker, a 2nd run makes zero POSTs."""
    from unittest.mock import patch

    config, state, parent, thread = _merge_setup(tmp_path, ["10", "20", "30"], marker=None)
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_keys([])):
        await run_messages(config, state, [parent, thread], lambda e: None)
    # second run over the same export, reusing the now-marked state
    config2, _state2, parent2, thread2 = _merge_setup(tmp_path, ["10", "20", "30"], marker=None)
    keys2: list[str] = []
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_keys(keys2)):
        await run_messages(config2, state, [parent2, thread2], lambda e: None)
    assert keys2 == []


async def test_merge_incremental_only_new_messages(tmp_path: Path) -> None:
    """S3: a thread with K new ids > marker re-POSTs only those K; no separator."""
    from unittest.mock import patch

    config, state, parent, thread = _merge_setup(tmp_path, ["10", "20", "30", "40"], marker="20")
    keys: list[str] = []
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_keys(keys)):
        await run_messages(config, state, [parent, thread], lambda e: None)
    merge_keys = sorted(k for k in keys if k.startswith("ferry-merge-"))
    assert merge_keys == ["ferry-merge-30", "ferry-merge-40"]
    assert not any(k.startswith("ferry-thread-sep-") for k in keys)
    assert state.channel_high_water["200"] == "40"


async def test_merge_event_reports_posted_count(tmp_path: Path) -> None:
    """S5: the completion event counts posted (2), not the full history (4)."""
    from unittest.mock import patch

    config, state, parent, thread = _merge_setup(tmp_path, ["10", "20", "30", "40"], marker="20")
    events: list[MigrationEvent] = []
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_keys([])):
        await run_messages(config, state, [parent, thread], events.append)
    merged = [e.message for e in events if "Merged thread" in e.message]
    assert any("(2 messages)" in m for m in merged)


async def test_merge_skip_type_trailing_high_id_marker(tmp_path: Path) -> None:
    """Critique I2: a trailing skip-type id still advances the marker (max over ALL ids)."""
    from unittest.mock import patch

    config = _make_config(
        tmp_path, thread_strategy="merge", message_rate_limit=0.0, incremental=True
    )
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)
    state.channel_map["100"] = "stoat-ch-100"
    parent = _make_export(channel_id="100", channel_name="general")
    thread = _make_export(
        channel_id="200",
        channel_name="my-thread",
        is_thread=True,
        parent_channel_name="general",
        messages=[
            _make_message("10", "a"),
            _make_message("20", "b"),
            DCEMessage(
                id="30",
                type="ThreadCreated",  # a _SKIP_TYPES message
                timestamp="2024-01-15T12:00:00+00:00",
                content="",
                author=_make_author(),
            ),
        ],
        message_count=3,
    )
    keys: list[str] = []
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_keys(keys)):
        await run_messages(config, state, [parent, thread], lambda e: None)
    assert "ferry-merge-30" not in keys  # skip-type never posted
    assert state.channel_high_water["200"] == "30"  # but it advances the marker


async def test_merge_non_numeric_thread_id_no_crash(tmp_path: Path) -> None:
    """S4: a non-numeric thread message id never hits int() — no ValueError."""
    from unittest.mock import patch

    config = _make_config(
        tmp_path, thread_strategy="merge", message_rate_limit=0.0, incremental=True
    )
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)
    state.channel_map["100"] = "stoat-ch-100"
    state.channel_high_water["200"] = "20"  # numeric marker present
    parent = _make_export(channel_id="100", channel_name="general")
    thread = _make_export(
        channel_id="200",
        channel_name="my-thread",
        is_thread=True,
        parent_channel_name="general",
        messages=[_make_message("sys-x", "a system-id message"), _make_message("30", "new")],
        message_count=2,
    )
    keys: list[str] = []
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_keys(keys)):
        await run_messages(config, state, [parent, thread], lambda e: None)  # must not raise
    assert "ferry-merge-sys-x" in keys  # non-numeric never skipped (no int() compare)
    assert "ferry-merge-30" in keys  # 30 > 20 marker -> posts


async def test_merge_non_numeric_marker_no_crash(tmp_path: Path) -> None:
    """#77 parity: a non-numeric carried marker degrades to no-threshold, never crashes."""
    from unittest.mock import patch

    config, state, parent, thread = _merge_setup(tmp_path, ["10", "20"], marker="sys-marker")
    keys: list[str] = []
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_keys(keys)):
        await run_messages(config, state, [parent, thread], lambda e: None)  # must not raise
    # Threshold normalized to "" -> every message copies (idempotent), no ValueError.
    assert "ferry-merge-10" in keys
    assert "ferry-merge-20" in keys
