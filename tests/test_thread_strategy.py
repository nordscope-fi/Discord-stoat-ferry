"""Tests for --thread-strategy flag: flatten, merge, archive modes."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from aioresponses import aioresponses

from discord_ferry.config import FerryConfig
from discord_ferry.core.security import SecureTokenStore
from discord_ferry.errors import DuplicateSendError, MigrationError
from discord_ferry.migrator.messages import run_messages
from discord_ferry.migrator.structure import run_channels
from discord_ferry.parser.models import (
    DCEAuthor,
    DCEChannel,
    DCEExport,
    DCEForwardedMessage,
    DCEGuild,
    DCEMessage,
    DCEReference,
)
from discord_ferry.state import FailedMessage, MigrationState

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
    **overrides: object,
) -> DCEMessage:
    defaults: dict[str, object] = {
        "id": msg_id,
        "type": "Default",
        "timestamp": "2024-01-15T12:00:00+00:00",
        "content": content,
        "author": _make_author(),
    }
    defaults.update(overrides)
    return DCEMessage(**defaults)  # type: ignore[arg-type]


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

    from unittest.mock import patch

    keys: list[str] = []

    with patch("discord_ferry.migrator.messages.api_send_message", _capture_keys(keys)):
        await run_messages(config, state, [parent, thread], events.append)

    # The separator message was actually sent (idempotency_key ferry-thread-sep-{channel_id}).
    # Pins the separator payload at the send level — a broken separator now fails this test.
    assert "ferry-thread-sep-200" in keys

    # Existing event-level assertion preserved.
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


# ---------------------------------------------------------------------------
# Batch 7 S1 — thread-merge failed-message self-heal (#76 pattern ported)
# ---------------------------------------------------------------------------


def _fail_on(fail_ids: set[str], keys: list[str] | None = None) -> object:
    """Patch target for api_send_message: raise for merge POSTs whose key maps to a
    msg.id in fail_ids; else record the idempotency_key + return a fake id.

    Mirrors _capture_keys but injects a per-message send failure.
    """

    async def _send(
        session: object, stoat_url: object, token: object, channel_id: object, **kwargs: object
    ) -> dict[str, object]:
        key = str(kwargs.get("idempotency_key", ""))
        mid = ""
        if key.startswith("ferry-merge-"):
            mid = key[len("ferry-merge-") :].split("_p")[0]
        if mid and mid in fail_ids:
            raise RuntimeError(f"simulated send failure for {mid}")
        if keys is not None:
            keys.append(key)
        return {"_id": f"stoat-{key}"}

    return _send


async def test_merge_success_records_no_failure(tmp_path: Path) -> None:
    """SC-1: a fully-successful merge run records zero FailedMessage (non-regression)."""
    from unittest.mock import patch

    config, state, parent, thread = _merge_setup(
        tmp_path, ["10", "20", "30"], marker=None, incremental=False
    )
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_keys([])):
        await run_messages(config, state, [parent, thread], lambda e: None)
    assert state.failed_messages == []
    assert state.channel_high_water["200"] == "30"


async def test_merge_failure_recorded_plain_mode(tmp_path: Path) -> None:
    """SC-2: a merge POST failure is recorded as a FailedMessage even in plain mode."""
    from unittest.mock import patch

    config, state, parent, thread = _merge_setup(
        tmp_path, ["10", "20", "30"], marker=None, incremental=False
    )
    with patch("discord_ferry.migrator.messages.api_send_message", _fail_on({"20"})):
        await run_messages(config, state, [parent, thread], lambda e: None)
    failed = [fm for fm in state.failed_messages if fm.discord_msg_id == "20"]
    assert len(failed) == 1
    assert failed[0].stoat_channel_id == "stoat-ch-100"
    assert failed[0].content_preview  # non-empty preview from built content
    assert state.channel_high_water["200"] == "30"  # marker still max(all ids)


async def test_merge_incremental_reattempts_prior_failed_id(tmp_path: Path) -> None:
    """SC-3: a prior-failed id below the marker is re-attempted on --incremental."""
    from unittest.mock import patch

    config, state, parent, thread = _merge_setup(tmp_path, ["10", "20", "30"], marker=None)
    with patch("discord_ferry.migrator.messages.api_send_message", _fail_on({"20"})):
        await run_messages(config, state, [parent, thread], lambda e: None)
    assert state.channel_high_water["200"] == "30"
    assert any(fm.discord_msg_id == "20" for fm in state.failed_messages)
    # run 2: reuse state, all succeed; "20" must be re-attempted despite <= marker.
    config2, _s2, parent2, thread2 = _merge_setup(tmp_path, ["10", "20", "30"], marker=None)
    keys2: list[str] = []
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_keys(keys2)):
        await run_messages(config2, state, [parent2, thread2], lambda e: None)
    assert "ferry-merge-20" in keys2
    assert "ferry-merge-10" not in keys2  # already-copied, not failed -> skipped


async def test_merge_reattempt_success_drops_entry(tmp_path: Path) -> None:
    """SC-4: a successful re-attempt drops the entry from failed_messages."""
    from unittest.mock import patch

    config, state, parent, thread = _merge_setup(tmp_path, ["10", "20", "30"], marker=None)
    with patch("discord_ferry.migrator.messages.api_send_message", _fail_on({"20"})):
        await run_messages(config, state, [parent, thread], lambda e: None)
    config2, _s2, parent2, thread2 = _merge_setup(tmp_path, ["10", "20", "30"], marker=None)
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_keys([])):
        await run_messages(config2, state, [parent2, thread2], lambda e: None)
    assert not any(fm.discord_msg_id == "20" for fm in state.failed_messages)


async def test_merge_reattempt_refail_collapses_to_one(tmp_path: Path) -> None:
    """SC-5: a re-failing id stays as exactly one entry (carried+re-fail collapsed)."""
    from unittest.mock import patch

    config, state, parent, thread = _merge_setup(tmp_path, ["10", "20", "30"], marker=None)
    with patch("discord_ferry.migrator.messages.api_send_message", _fail_on({"20"})):
        await run_messages(config, state, [parent, thread], lambda e: None)
    config2, _s2, parent2, thread2 = _merge_setup(tmp_path, ["10", "20", "30"], marker=None)
    with patch("discord_ferry.migrator.messages.api_send_message", _fail_on({"20"})):
        await run_messages(config2, state, [parent2, thread2], lambda e: None)
    assert len([fm for fm in state.failed_messages if fm.discord_msg_id == "20"]) == 1


async def test_merge_plain_mode_ignores_marker_no_failrecord_on_success(tmp_path: Path) -> None:
    """SC-6: plain mode (incremental=False) ignores the marker (no skip), no FailedMessage."""
    from unittest.mock import patch

    config, state, parent, thread = _merge_setup(
        tmp_path, ["10", "20", "30"], marker="20", incremental=False
    )
    keys: list[str] = []
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_keys(keys)):
        await run_messages(config, state, [parent, thread], lambda e: None)
    # No incremental skip in plain mode: every message is (re-)posted, no FailedMessage.
    assert {"ferry-merge-10", "ferry-merge-20", "ferry-merge-30"}.issubset(set(keys))
    assert state.failed_messages == []


async def test_merge_multipart_failure_records_one(tmp_path: Path) -> None:
    """SC-7: a multi-part message with a failing part records exactly one FailedMessage."""
    from unittest.mock import patch

    long_text = "a" * 5000  # > 2000 -> _split_message yields multiple parts
    config = _make_config(
        tmp_path, thread_strategy="merge", message_rate_limit=0.0, incremental=False
    )
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)
    state.channel_map["100"] = "stoat-ch-100"
    parent = _make_export(channel_id="100", channel_name="general")
    thread = _make_export(
        channel_id="200",
        channel_name="my-thread",
        is_thread=True,
        parent_channel_name="general",
        messages=[_make_message("50", long_text)],
        message_count=1,
    )

    async def _send(
        session: object, stoat_url: object, token: object, channel_id: object, **kwargs: object
    ) -> dict[str, object]:
        key = str(kwargs.get("idempotency_key", ""))
        if key == "ferry-merge-50_p2":
            raise RuntimeError("part 2 fails")
        return {"_id": f"stoat-{key}"}

    with patch("discord_ferry.migrator.messages.api_send_message", _send):
        await run_messages(config, state, [parent, thread], lambda e: None)
    assert len([fm for fm in state.failed_messages if fm.discord_msg_id == "50"]) == 1


async def test_merge_cross_thread_sibling_safety(tmp_path: Path) -> None:
    """SC-8: thread B completion does not drop/mangle thread A's still-failed entry."""
    from unittest.mock import patch

    config = _make_config(
        tmp_path, thread_strategy="merge", message_rate_limit=0.0, incremental=True
    )
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)
    state.channel_map["100"] = "stoat-ch-100"
    state.channel_high_water["200"] = "30"
    state.channel_high_water["201"] = "31"
    state.failed_messages.append(
        FailedMessage(
            discord_msg_id="20", stoat_channel_id="stoat-ch-100", error="prior", content_preview="x"
        )
    )
    parent = _make_export(channel_id="100", channel_name="general")
    thread_a = _make_export(
        channel_id="200",
        channel_name="thread-a",
        is_thread=True,
        parent_channel_name="general",
        messages=[_make_message(m, f"m{m}") for m in ["10", "20", "30"]],
        message_count=3,
    )
    thread_b = _make_export(
        channel_id="201",
        channel_name="thread-b",
        is_thread=True,
        parent_channel_name="general",
        messages=[_make_message(m, f"m{m}") for m in ["11", "21", "31"]],
        message_count=3,
    )
    # B succeeds; A's "20" re-fails -> A keeps exactly one entry, B adds none, none mangled.
    with patch("discord_ferry.migrator.messages.api_send_message", _fail_on({"20"})):
        await run_messages(config, state, [parent, thread_a, thread_b], lambda e: None)
    assert len([fm for fm in state.failed_messages if fm.discord_msg_id == "20"]) == 1


async def test_merge_fully_failed_message_is_recorded_for_retry(tmp_path: Path) -> None:
    """SC-9: a fully-failed merge message is recorded so ferry retry can recover it.

    KNOWN LIMITATION (documented, design §S1 I1): the incremental re-attempt path
    (SC-3/4/5) is the asserted-correct recovery and is idempotency-safe. `ferry retry`
    recovers a FULLY-failed merge message cleanly, but recovering a PARTIAL-SUCCESS
    multi-part merge message via `ferry retry` may duplicate already-sent parts (the
    `ferry-merge-` vs `ferry-` idempotency-namespace mismatch). We do NOT assert the
    duplicate as desired; this test only asserts the recordable surface.
    """
    from unittest.mock import patch

    config, state, parent, thread = _merge_setup(tmp_path, ["10"], marker=None, incremental=False)
    with patch("discord_ferry.migrator.messages.api_send_message", _fail_on({"10"})):
        await run_messages(config, state, [parent, thread], lambda e: None)
    failed = [fm for fm in state.failed_messages if fm.discord_msg_id == "10"]
    assert len(failed) == 1
    assert failed[0].stoat_channel_id == "stoat-ch-100"  # retry posts to the parent channel


async def test_merge_failure_sanitizes_token(tmp_path: Path) -> None:
    """SC-10: a token in the exception is absent from FailedMessage.error AND warnings."""
    from unittest.mock import patch

    token = "SECRET-TOKEN-12345"
    config = _make_config(
        tmp_path, thread_strategy="merge", message_rate_limit=0.0, incremental=False
    )
    config.token_store = SecureTokenStore({"discord": token})
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)
    state.channel_map["100"] = "stoat-ch-100"
    parent = _make_export(channel_id="100", channel_name="general")
    thread = _make_export(
        channel_id="200",
        channel_name="my-thread",
        is_thread=True,
        parent_channel_name="general",
        messages=[_make_message("60", "hi")],
        message_count=1,
    )

    async def _send(
        session: object, stoat_url: object, token_: object, channel_id: object, **kwargs: object
    ) -> dict[str, object]:
        raise RuntimeError(f"boom {token}")

    with patch("discord_ferry.migrator.messages.api_send_message", _send):
        await run_messages(config, state, [parent, thread], lambda e: None)
    assert all(token not in (fm.error or "") for fm in state.failed_messages)
    assert all(token not in str(w.get("message", "")) for w in state.warnings)


async def test_merge_empty_thread_no_marker_no_failure(tmp_path: Path) -> None:
    """SC-11: an empty merge thread writes no marker and records no failure."""
    from unittest.mock import patch

    config, state, parent, thread = _merge_setup(tmp_path, [], marker=None, incremental=False)
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_keys([])):
        await run_messages(config, state, [parent, thread], lambda e: None)
    assert state.failed_messages == []
    assert "200" not in state.channel_high_water


async def test_merge_separator_failure_not_recorded_as_failed(tmp_path: Path) -> None:
    """SC-12: a separator failure is warn-only, never a FailedMessage."""
    from unittest.mock import patch

    config, state, parent, thread = _merge_setup(tmp_path, ["10", "20"], marker=None)

    async def _send(
        session: object, stoat_url: object, token: object, channel_id: object, **kwargs: object
    ) -> dict[str, object]:
        key = str(kwargs.get("idempotency_key", ""))
        if key.startswith("ferry-thread-sep-"):
            raise RuntimeError("sep fails")
        return {"_id": f"stoat-{key}"}

    with patch("discord_ferry.migrator.messages.api_send_message", _send):
        await run_messages(config, state, [parent, thread], lambda e: None)
    assert state.failed_messages == []
    assert any(w.get("type") == "merge_separator_failed" for w in state.warnings)


async def test_merge_non_numeric_id_failure_recorded(tmp_path: Path) -> None:
    """SC-13: a non-numeric merge id that fails is recorded; numeric marker unaffected."""
    from unittest.mock import patch

    config, state, parent, thread = _merge_setup(
        tmp_path, ["sys-x", "30"], marker=None, incremental=False
    )
    with patch("discord_ferry.migrator.messages.api_send_message", _fail_on({"sys-x"})):
        await run_messages(config, state, [parent, thread], lambda e: None)
    assert any(fm.discord_msg_id == "sys-x" for fm in state.failed_messages)
    assert state.channel_high_water["200"] == "30"  # max over numeric ids only


# ---------------------------------------------------------------------------
# Forwarded messages must be recovered on EVERY path that renders a message,
# not only the main send path.
# ---------------------------------------------------------------------------


def _forwarded_msg(msg_id: str = "fwd1") -> DCEMessage:
    return _make_message(
        msg_id,
        content="",
        reference=DCEReference(message_id="orig1", type="Forward"),
        forwarded_message=DCEForwardedMessage(content="recovered thread text"),
    )


async def test_merge_strategy_recovers_forwarded_content(tmp_path: Path) -> None:
    """A forward inside a merged thread must not arrive empty.

    `_merge_threads` builds content directly and never enters `_process_message`, so the
    recovery has to be applied on this path too — otherwise `--thread-strategy merge`
    silently sends an empty message and the feature's headline claim is false for it.
    """
    from unittest.mock import patch

    config = _make_config(tmp_path, thread_strategy="merge", message_rate_limit=0.0)
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)
    state.channel_map["100"] = "stoat-ch-100"

    parent = _make_export(
        channel_id="100", channel_name="general", messages=[_make_message("m1")], message_count=1
    )
    thread = _make_export(
        channel_id="200",
        channel_name="my-thread",
        is_thread=True,
        parent_channel_name="general",
        messages=[_forwarded_msg()],
        message_count=1,
    )

    sent: list[str] = []

    async def _capture(
        session: object, stoat_url: object, token: object, channel_id: object, **kwargs: object
    ) -> dict[str, object]:
        sent.append(str(kwargs.get("content", "")))
        return {"_id": "stoat-x"}

    with patch("discord_ferry.migrator.messages.api_send_message", _capture):
        await run_messages(config, state, [parent, thread], lambda e: None)

    merged = "\n".join(sent)
    assert "recovered thread text" in merged
    assert "[forwarded]" in merged


async def test_archive_strategy_writes_forwarded_content(tmp_path: Path) -> None:
    """Archive writes messages to markdown, so a forward must not archive as empty."""
    config = _make_config(tmp_path, thread_strategy="archive", message_rate_limit=0.0)
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)
    state.channel_map["100"] = "stoat-ch-100"

    parent = _make_export(
        channel_id="100", channel_name="general", messages=[_make_message("m1")], message_count=1
    )
    thread = _make_export(
        channel_id="200",
        channel_name="my-thread",
        is_thread=True,
        parent_channel_name="general",
        messages=[_forwarded_msg()],
        message_count=1,
    )

    await run_messages(config, state, [parent, thread], lambda e: None)

    md = (tmp_path / "threads" / "general" / "my-thread.md").read_text()
    assert "recovered thread text" in md


# ---------------------------------------------------------------------------
# Batch 6 (#107) — merge-path durability
# ---------------------------------------------------------------------------


class _Crash(BaseException):
    """Simulates the process dying mid-merge.

    Deliberately a BaseException: the merge loop catches ``Exception`` per message
    and records a failure, which is the opposite of the scenario under test. Only
    something outside that net models "the process went away".
    """


def _crash_on_nth(keys: list[str], n: int) -> object:
    """api_send_message stand-in that records keys and dies on the nth call."""

    async def _send(
        session: object, stoat_url: object, token: object, channel_id: object, **kwargs: object
    ) -> dict[str, object]:
        key = str(kwargs.get("idempotency_key", ""))
        if len(keys) >= n:
            raise _Crash(f"process died before sending {key}")
        keys.append(key)
        return {"_id": f"stoat-{key}"}

    return _send


def _two_thread_setup(
    tmp_path: Path,
    *,
    resume: bool = False,
    incremental: bool = False,
    checkpoint_interval: int = 50,
) -> tuple[FerryConfig, MigrationState, list[DCEExport]]:
    """Parent (ch 100) plus two threads (ch 200, ch 300), four messages each."""
    config = _make_config(
        tmp_path,
        thread_strategy="merge",
        message_rate_limit=0.0,
        resume=resume,
        incremental=incremental,
        checkpoint_interval=checkpoint_interval,
    )
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)
    state.channel_map["100"] = "stoat-ch-100"
    parent = _make_export(channel_id="100", channel_name="general")
    threads = [
        _make_export(
            channel_id=cid,
            channel_name=f"thread-{cid}",
            is_thread=True,
            parent_channel_name="general",
            messages=[_make_message(mid, f"msg {mid}") for mid in ids],
            message_count=len(ids),
        )
        for cid, ids in (("200", ["10", "20", "30", "40"]), ("300", ["50", "60", "70", "80"]))
    ]
    return config, state, [parent, *threads]


async def test_crash_mid_merge_leaves_durable_record(tmp_path: Path) -> None:
    """SC-35: what reached the parent channel before the crash is on DISK afterwards.

    Asserts against state.json rather than the in-memory state — the whole point
    is what survives the process going away.
    """
    import json
    from unittest.mock import patch

    import pytest

    # checkpoint_interval=2 plus a clock that always clears the 5s floor, so the
    # transient offset is genuinely written mid-thread. With the default interval
    # of 50 and four messages per thread it never would be, and the "offset was
    # cleared" assertion below would pass whether or not anything cleared it.
    config, state, exports = _two_thread_setup(tmp_path, checkpoint_interval=2)
    keys: list[str] = []
    ticks = iter(range(0, 10_000, 100))
    # Thread 200: separator + 4 messages = 5 sends, so it completes. Die on the
    # 6th — thread 300's separator — BEFORE any later checkpoint could persist
    # thread 200's completion as a side effect. That isolates the completion save.
    with (
        patch("discord_ferry.migrator.messages.api_send_message", _crash_on_nth(keys, 5)),
        patch("discord_ferry.migrator.messages.time.monotonic", lambda: next(ticks)),
        pytest.raises(_Crash),
    ):
        await run_messages(config, state, exports, lambda e: None)

    saved = json.loads((tmp_path / "state.json").read_text())
    # Thread 200 ran to completion: its durable marker survives...
    assert saved["channel_high_water"].get("200") == "40"
    # ...and the transient offset the checkpoints wrote was cleared, because there
    # is nothing left to resume inside it. Left behind, it would suppress the
    # separator of a thread that had in fact finished.
    assert "200" not in saved.get("channel_message_offsets", {})
    # Thread 300 never started and must not claim otherwise.
    assert "300" not in saved.get("channel_high_water", {})


async def test_merge_checkpoints_periodically_inside_a_long_thread(tmp_path: Path) -> None:
    """SC-36: mid-thread progress is persisted, not just per-thread completion.

    Drives the interval-and-time gate with a clock that always reports the floor
    as satisfied, so the assertion is about the interval rather than wall time.
    """
    import json
    from unittest.mock import patch

    import pytest

    config, state, exports = _two_thread_setup(tmp_path, checkpoint_interval=2)
    keys: list[str] = []
    ticks = iter(range(0, 10_000, 100))
    with (
        patch("discord_ferry.migrator.messages.api_send_message", _crash_on_nth(keys, 4)),
        patch("discord_ferry.migrator.messages.time.monotonic", lambda: next(ticks)),
        pytest.raises(_Crash),
    ):
        await run_messages(config, state, exports, lambda e: None)

    saved = json.loads((tmp_path / "state.json").read_text())
    # Separator + msgs 10, 20 landed; the crash hit msg 30. A checkpoint must have
    # recorded progress inside thread 200, which never completed.
    assert saved.get("channel_message_offsets", {}).get("200") == "20"
    assert "200" not in saved.get("channel_high_water", {})


async def test_resume_does_not_remerge_already_sent_thread_messages(tmp_path: Path) -> None:
    """A resumed merge must not re-deliver what the crashed run already sent."""
    from unittest.mock import patch

    config, state, exports = _two_thread_setup(tmp_path, resume=True)
    # Stand-in for the state a crashed run left behind: thread 200 got as far as 20.
    state.channel_message_offsets["200"] = "20"
    keys: list[str] = []
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_keys(keys)):
        await run_messages(config, state, exports, lambda e: None)

    thread_200 = [k for k in keys if k in ("ferry-merge-10", "ferry-merge-20")]
    assert thread_200 == [], "re-sent messages the crashed run had already delivered"
    assert "ferry-merge-30" in keys
    assert "ferry-merge-40" in keys


async def test_resume_does_not_repost_the_thread_separator(tmp_path: Path) -> None:
    """The separator is posted once per thread, not once per crash."""
    from unittest.mock import patch

    config, state, exports = _two_thread_setup(tmp_path, resume=True)
    state.channel_message_offsets["200"] = "20"
    keys: list[str] = []
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_keys(keys)):
        await run_messages(config, state, exports, lambda e: None)

    assert "ferry-thread-sep-200" not in keys
    # A thread the crashed run never reached still gets its separator.
    assert "ferry-thread-sep-300" in keys


async def test_resume_keeps_a_failed_merge_message_recoverable(tmp_path: Path) -> None:
    """`_posted` advances on a FAILED send too, so the checkpoint can name a message
    that never landed — and --resume now skips past it.

    That is deliberate and matches flatten: SC-7
    (`test_resume_does_not_reattempt_failed_id`) pins resume as a pure continuation,
    because re-sending could duplicate a message that actually landed before the
    response was lost. What must hold is that the message stays RECOVERABLE: the
    failure record survives, so the report shows it and an --incremental run
    self-heals it.
    """
    from unittest.mock import patch

    config, state, exports = _two_thread_setup(tmp_path, resume=True)
    state.channel_message_offsets["200"] = "30"
    state.failed_messages.append(
        FailedMessage(
            discord_msg_id="20",
            stoat_channel_id="stoat-ch-100",
            error="boom",
            content_preview="msg 20",
        )
    )
    keys: list[str] = []
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_keys(keys)):
        await run_messages(config, state, exports, lambda e: None)

    # Not re-attempted under resume — same contract as flatten.
    assert "ferry-merge-20" not in keys
    # ...but still on the books, so it is neither silent nor permanent.
    assert [fm.discord_msg_id for fm in state.failed_messages] == ["20"]
    # Everything above the marker is still sent.
    assert "ferry-merge-40" in keys


async def test_incremental_still_reattempts_a_failed_merge_message(tmp_path: Path) -> None:
    """The opt-in delta mode is where the self-heal lives — unchanged by batch 6."""
    from unittest.mock import patch

    config, state, exports = _two_thread_setup(tmp_path, incremental=True)
    state.channel_high_water["200"] = "30"
    state.failed_messages.append(
        FailedMessage(
            discord_msg_id="20",
            stoat_channel_id="stoat-ch-100",
            error="boom",
            content_preview="msg 20",
        )
    )
    keys: list[str] = []
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_keys(keys)):
        await run_messages(config, state, exports, lambda e: None)

    assert "ferry-merge-20" in keys, "incremental must still self-heal"
    # Re-sent successfully, so the stale failure record is reconciled away.
    assert [fm.discord_msg_id for fm in state.failed_messages] == []


# ---------------------------------------------------------------------------
# Duplicate sends at the result-discarding sites (#107 batch 7, task #202)
# ---------------------------------------------------------------------------


def _dup_on(prefix: str) -> object:
    """Patch target: raise DuplicateSendError for keys with *prefix*, else succeed."""

    async def _send(
        session: object, stoat_url: object, token: object, channel_id: object, **kwargs: object
    ) -> dict[str, object]:
        key = str(kwargs.get("idempotency_key", ""))
        if key.startswith(prefix):
            raise DuplicateSendError("already on the server")
        return {"_id": f"stoat-{key}"}

    return _send


async def test_merge_duplicate_records_no_failed_message(tmp_path: Path) -> None:
    """SC-2.5: the merge path must not record a landed message as failed.

    This is the defect itself. A FailedMessage here is what --incremental later
    re-sends, producing a real duplicate in the user's channel.
    """
    config = _make_config(tmp_path, thread_strategy="merge", message_rate_limit=0.0)
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)
    state.channel_map["100"] = "stoat-ch-100"

    parent = _make_export(channel_id="100", channel_name="general", message_count=0)
    thread = _make_export(
        channel_id="200",
        channel_name="my-thread",
        is_thread=True,
        parent_channel_name="general",
        messages=[_make_message("m2", "thread msg")],
        message_count=1,
    )

    with patch("discord_ferry.migrator.messages.api_send_message", _dup_on("ferry-merge-")):
        await run_messages(config, state, [parent, thread], lambda e: None)

    assert not state.failed_messages, (
        "a message already on the server was recorded as failed; --incremental will "
        "re-send it and create a real duplicate"
    )
    assert not [w for w in state.warnings if w.get("type") == "merge_message_failed"]


async def test_merge_separator_duplicate_records_no_warning(tmp_path: Path) -> None:
    """SC-2.8, separator half: a duplicate separator is not a failure."""
    config = _make_config(tmp_path, thread_strategy="merge", message_rate_limit=0.0)
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)
    state.channel_map["100"] = "stoat-ch-100"

    parent = _make_export(channel_id="100", channel_name="general", message_count=0)
    thread = _make_export(
        channel_id="200",
        channel_name="my-thread",
        is_thread=True,
        parent_channel_name="general",
        messages=[_make_message("m2", "thread msg")],
        message_count=1,
    )

    with patch("discord_ferry.migrator.messages.api_send_message", _dup_on("ferry-thread-sep-")):
        await run_messages(config, state, [parent, thread], lambda e: None)

    assert not [w for w in state.warnings if w.get("type") == "merge_separator_failed"]


async def test_thread_header_duplicate_records_no_warning(tmp_path: Path) -> None:
    """SC-2.8, header half: a duplicate flatten-mode header is not a failure."""
    config = _make_config(tmp_path, thread_strategy="flatten", message_rate_limit=0.0)
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)
    state.channel_map["100"] = "stoat-ch-100"
    state.channel_map["200"] = "stoat-ch-200"

    thread = _make_export(
        channel_id="200",
        channel_name="my-thread",
        is_thread=True,
        parent_channel_name="general",
        messages=[_make_message("m2", "thread msg")],
        message_count=1,
    )

    with patch("discord_ferry.migrator.messages.api_send_message", _dup_on("ferry-header-")):
        await run_messages(config, state, [thread], lambda e: None)

    assert not [w for w in state.warnings if w.get("type") == "thread_header_failed"]


# ---------------------------------------------------------------------------
# Batch 8 (#110), chunk #218: the merge duplicate suppression
# ---------------------------------------------------------------------------
#
# DCE 2.47.3 resolves a thread's empty ThreadStarterMessage placeholder into the real
# parent-channel message, KEEPING that message's Discord id (upstream PR #1557). A
# thread's Discord channel id EQUALS its origin message id, so post-bump the thread's
# first message is a message the parent export already sent.
#
# Under `merge` that lands in the parent's own Stoat channel a second time, under a
# different idempotency key (ferry-merge-* against ferry-*), so Stoat sees two distinct
# nonces and nothing deduplicates it.
#
# Every test below asserts on captured idempotency keys, never on state. The merge path
# writes no message_map either way, so a state-level assertion cannot distinguish a
# suppressed send from a delivered one.

# The real fixture's ids, so the shape under test is the shape that ships.
_ORIGIN_ID = "1506019505778987190"  # 'Cool Thread' channel id AND its origin message id
_THREAD_REPLY_ID = "1506019529476800745"


def _post_bump_pair(
    origin_content: str = "the thread's origin message",
) -> tuple[DCEExport, DCEExport]:
    """A parent export and the post-2.47.3 thread export that collides with it."""
    parent = _make_export(
        channel_id="100",
        channel_name="general",
        messages=[_make_message(_ORIGIN_ID, origin_content)],
        message_count=1,
    )
    thread = _make_export(
        channel_id=_ORIGIN_ID,  # a thread's channel id IS its origin message id
        channel_name="cool-thread",
        is_thread=True,
        parent_channel_name="general",
        messages=[
            _make_message(_ORIGIN_ID, origin_content),
            _make_message(_THREAD_REPLY_ID, "first reply in the thread"),
        ],
        message_count=2,
    )
    return parent, thread


async def test_merge_sends_a_colliding_origin_message_once(tmp_path: Path) -> None:
    """SC-3.1: the thread's resolved starter is the parent's own message, so send it once.

    Fails against the pre-batch-8 code, where the thread's copy is merged into the
    parent channel a second time.
    """
    config = _make_config(tmp_path, thread_strategy="merge", message_rate_limit=0.0)
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)
    state.channel_map["100"] = "stoat-ch-100"
    parent, thread = _post_bump_pair()

    keys: list[str] = []
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_keys(keys)):
        await run_messages(config, state, [parent, thread], lambda e: None)

    assert f"ferry-{_ORIGIN_ID}" in keys, "the parent's own send must be unaffected"
    assert f"ferry-merge-{_ORIGIN_ID}" not in keys, (
        "the thread's copy of the parent's own message was sent again into the same "
        "Stoat channel; that is the duplicate this batch exists to prevent"
    )
    assert f"ferry-merge-{_THREAD_REPLY_ID}" in keys, (
        "a thread message that does not collide must still be merged; the suppression "
        "is scoped to the collision, not to the thread"
    )


async def test_merge_still_delivers_when_the_parents_send_failed(tmp_path: Path) -> None:
    """SC-3.3: the suppression must not fire on a message the parent never delivered.

    Batch 7's failure direction was a duplicate. This one's is a LOST message, which
    is worse, and this is the only test that catches an over-firing suppression.

    state.message_map is written only on a send that returned a non-empty Stoat id, so
    a parent failure leaves no entry and the merge path is the message's remaining
    route to the server.
    """
    config = _make_config(tmp_path, thread_strategy="merge", message_rate_limit=0.0)
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)
    state.channel_map["100"] = "stoat-ch-100"
    parent, thread = _post_bump_pair()

    keys: list[str] = []

    async def _fail_the_parents_send(
        session: object, stoat_url: object, token: object, channel_id: object, **kwargs: object
    ) -> dict[str, object]:
        key = str(kwargs.get("idempotency_key", ""))
        keys.append(key)
        if key == f"ferry-{_ORIGIN_ID}":
            raise MigrationError("parent send failed")
        return {"_id": f"stoat-{key}"}

    with patch("discord_ferry.migrator.messages.api_send_message", _fail_the_parents_send):
        await run_messages(config, state, [parent, thread], lambda e: None)

    assert _ORIGIN_ID not in state.message_map, (
        "premise of this test: a failed send writes no map entry, so the suppression "
        "has nothing to act on"
    )
    assert f"ferry-merge-{_ORIGIN_ID}" in keys, (
        "the suppression fired on a message the parent never delivered, so the message "
        "reached neither channel and is lost; that is strictly worse than the duplicate "
        "this batch removes"
    )


# Ground truth from the shipped fixture
# 'Discord Ferry Test - feedback-forum - Bug Report [1506019530294562938].json':
# the forum post's channel id and its first message id are the SAME, because a forum
# post is a thread whose starter is a real message rather than a placeholder.
_FORUM_POST_ID = "1506019530294562938"


async def test_merge_never_suppresses_a_forum_posts_starter(tmp_path: Path) -> None:
    """SC-3.4: MANDATORY. The only test that kills the `key == channel_id` mutant.

    That mutant is batch 7's discriminator, and it is wrong for this path. A forum
    post already satisfies `channel_id == first_message_id` at the 2.47.1 pin, so that
    variant suppresses the post's own body and loses it. The correct discriminator
    leaves it alone, because a forum CHANNEL export carries no messages of its own and
    the starter therefore never reaches state.message_map.

    Companion to test_a_forum_post_already_has_key_equal_to_its_channel_id_today
    (tests/test_parallel_messages.py), which pins the same shape for batch 7's guard.
    """
    config = _make_config(tmp_path, thread_strategy="merge", message_rate_limit=0.0)
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)
    state.channel_map["300"] = "stoat-ch-300"

    forum_channel = _make_export(
        channel_id="300",
        channel_name="feedback-forum",
        messages=[],  # a forum channel export carries no messages of its own
        message_count=0,
    )
    post = _make_export(
        channel_id=_FORUM_POST_ID,
        channel_name="Bug Report",
        is_thread=True,
        parent_channel_name="feedback-forum",
        messages=[_make_message(_FORUM_POST_ID, "the post body")],
        message_count=1,
    )

    # The premise assertion. Without it this test passes against the mutant whenever the
    # shape happens not to hold, which is exactly how batch 7's M2 survived five of six
    # probes. This line is what makes the mutant's condition actually fire.
    assert post.channel.id == post.messages[0].id, (
        "premise: a forum post's channel id equals its starter's id, so a "
        "`key == channel_id` discriminator would fire here and drop the post's body"
    )

    keys: list[str] = []
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_keys(keys)):
        await run_messages(config, state, [forum_channel, post], lambda e: None)

    assert f"ferry-merge-{_FORUM_POST_ID}" in keys, (
        "the forum post's own starter was suppressed; a `key == channel_id` "
        "discriminator does exactly this, and the post has no other copy anywhere"
    )


async def test_archive_is_unaffected_by_the_merge_suppression(tmp_path: Path) -> None:
    """SC-I6: archive writes markdown and makes no API calls, so nothing is suppressed.

    The bump gives archive a free improvement, a real starter line where it had an
    empty placeholder, and it needs no code change to get it.
    """
    config = _make_config(tmp_path, thread_strategy="archive", message_rate_limit=0.0)
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)
    state.channel_map["100"] = "stoat-ch-100"
    # Even with the collision already recorded, archive must not consult it.
    state.message_map[_ORIGIN_ID] = "stoat-parent-copy"
    parent, thread = _post_bump_pair()

    with patch("discord_ferry.migrator.messages.api_send_message", _capture_keys([])):
        await run_messages(config, state, [parent, thread], lambda e: None)

    archived = (config.output_dir / "threads" / "general" / "cool-thread.md").read_text(
        encoding="utf-8"
    )
    assert "the thread's origin message" in archived, (
        "archive must still record the starter; _archive_threads consults no message_map"
    )


async def test_a_suppressed_message_is_not_counted_as_posted(tmp_path: Path) -> None:
    """SC-3.5: the completion event must not claim a message nobody sent.

    `_posted` feeds "Merged thread X (N messages)". A suppressed message was already
    on the server before this run touched it, so counting it overstates the merge.

    Proven able to fail: adding `_posted += 1` to the suppression branch makes this
    test fail.

    A NOTE ON WHAT THIS TEST DOES NOT COVER, because the omission is deliberate.
    The suppression also adds the id to `_succeeded_ids`, so the merge loop's
    reconciliation at messages.py:745-759 would drop a stale FailedMessage for a
    message that is on the server. No test asserts that, because it is not reachable:
    the parallel path's own reconciliation (messages.py:1089-1103) drops on
    `in state.message_map`, which is this suppression's own precondition, so the parent
    channel's worker always clears such an entry first. A mutation removing the
    `_succeeded_ids.add` survives the whole suite. That was measured, not assumed, and
    the line is kept as a documented invariant rather than as covered behaviour.
    """
    config = _make_config(
        tmp_path, thread_strategy="merge", message_rate_limit=0.0, incremental=True
    )
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)
    state.channel_map["100"] = "stoat-ch-100"
    # The parent delivered the origin on a prior run and recorded doing so.
    state.message_map[_ORIGIN_ID] = "stoat-parent-copy"

    parent = _make_export(channel_id="100", channel_name="general", messages=[], message_count=0)
    _, thread = _post_bump_pair()

    keys: list[str] = []
    events: list[MigrationEvent] = []
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_keys(keys)):
        await run_messages(config, state, [parent, thread], events.append)

    assert f"ferry-merge-{_ORIGIN_ID}" not in keys, "premise: the origin was suppressed"
    assert f"ferry-merge-{_THREAD_REPLY_ID}" in keys, "premise: the reply was merged"

    merged = [e.message or "" for e in events if "Merged thread" in (e.message or "")]
    assert any("(1 messages)" in m for m in merged), (
        f"_posted counted the suppressed message, so the completion event overstates "
        f"what this run merged: {merged}"
    )


async def test_the_merge_suppression_records_no_warning(tmp_path: Path) -> None:
    """SC-3.5: the suppression is silent, by choice.

    Post-bump it fires once per thread for every `merge` user, so a warning would be
    noise proportional to thread count rather than a signal. A `logger.debug` line
    keeps it reachable in ferry.log for anyone diagnosing a specific migration.
    """
    config = _make_config(tmp_path, thread_strategy="merge", message_rate_limit=0.0)
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)
    state.channel_map["100"] = "stoat-ch-100"
    parent, thread = _post_bump_pair()

    keys: list[str] = []
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_keys(keys)):
        await run_messages(config, state, [parent, thread], lambda e: None)

    assert f"ferry-merge-{_ORIGIN_ID}" not in keys, "premise: the origin was suppressed"
    assert not [w for w in state.warnings if _ORIGIN_ID in w.get("message", "")], (
        f"the suppression recorded a warning naming the suppressed message: {state.warnings}"
    )
    assert not state.failed_messages, "a suppressed message is not a failure"


async def test_merge_still_duplicates_after_a_parent_duplicate_nonce(tmp_path: Path) -> None:
    """SC-3.6: a KNOWN MISS, pinned so it stays visible. Not the intended outcome.

    Batch 7 chose, correctly, to write NO message_map entry when a send lands but
    Stoat answers 409 DuplicateNonce. Stoat returns no id with that response, and an
    entry with an empty value is worse than no entry: a reply resolving to '' is worse
    than one that does not resolve at all. See messages.py:1434-1435 and
    tests/test_messages.py::test_single_part_duplicate_leaves_clean_state.

    A consequence nobody noticed until batch 8's first critique round: the map is
    therefore NOT a complete record of what the parent delivered. This suppression
    reads that map, so a parent origin message delivered under a 409 is invisible to
    it, and the thread's copy is sent anyway. The channel holds it twice.

    WHAT WOULD CLOSE IT: issue #240 (spec P2 item S8), a durable MigrationState record
    of duplicate-unmapped sends. A run-scoped carrier is not enough: on --resume after
    a crash that happened before the thread was processed, the parent channel is
    skipped through completed_channel_ids, so no in-run signal exists.

    WHY IT IS NOT CLOSED HERE: the case is compound-rare. The parent's send must lose
    its response, the retry must land inside Stoat's 1000-entry idempotency LRU, that
    message must be a thread origin, and the user must run `merge`. Its outcome is one
    duplicated message, which is the pre-bump status quo for every merge user.

    Change the final assertion only when #240 is built.
    """
    config = _make_config(tmp_path, thread_strategy="merge", message_rate_limit=0.0)
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)
    state.channel_map["100"] = "stoat-ch-100"
    parent, thread = _post_bump_pair()

    keys: list[str] = []

    async def _parents_send_is_a_duplicate(
        session: object, stoat_url: object, token: object, channel_id: object, **kwargs: object
    ) -> dict[str, object]:
        key = str(kwargs.get("idempotency_key", ""))
        keys.append(key)
        if key == f"ferry-{_ORIGIN_ID}":
            raise DuplicateSendError("already on the server")
        return {"_id": f"stoat-{key}"}

    with patch("discord_ferry.migrator.messages.api_send_message", _parents_send_is_a_duplicate):
        await run_messages(config, state, [parent, thread], lambda e: None)

    assert f"ferry-{_ORIGIN_ID}" in keys, "premise: the parent attempted the send"
    assert _ORIGIN_ID not in state.message_map, (
        "premise: batch 7 deliberately writes no map entry for a send that landed "
        "under a duplicate-nonce 409, so the suppression has nothing to match on"
    )
    assert any(w.get("type") == "duplicate_send_unmapped" for w in state.warnings), (
        "premise: batch 7 does warn about it, so the information exists in the run; "
        "it is just not in a form the suppression can read"
    )
    assert f"ferry-merge-{_ORIGIN_ID}" in keys, (
        "KNOWN MISS (#240 / spec S8): the parent delivered this message under a 409 "
        "but recorded nothing, so the suppression cannot see it and the thread's copy "
        "is sent into the same channel. Do not 'fix' this assertion; build #240."
    )


# ---------------------------------------------------------------------------
# Batch 8 integration scenarios (SC-I1 to SC-I5)
# ---------------------------------------------------------------------------


async def test_flatten_then_merge_under_incremental_suppresses_everything(
    tmp_path: Path,
) -> None:
    """SC-I1: switching strategy across an --incremental run suppresses the whole thread.

    Run 1 under `flatten` puts every thread message in message_map, because each
    thread is its own Stoat channel. Run 2 under `merge` then finds all of them
    already delivered.

    This is accepted rather than incidental: those messages are on the server in the
    thread's own channel, and re-sending them into the parent would duplicate content
    across the server. Pinned because, unpinned, it looks like a bug the first time
    somebody hits it.
    """
    config = _make_config(
        tmp_path, thread_strategy="merge", message_rate_limit=0.0, incremental=True
    )
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)
    state.channel_map["100"] = "stoat-ch-100"
    # Run 1 under flatten delivered both thread messages into the thread's own channel.
    state.message_map[_ORIGIN_ID] = "stoat-thread-copy"
    state.message_map[_THREAD_REPLY_ID] = "stoat-thread-reply"

    parent = _make_export(channel_id="100", channel_name="general", messages=[], message_count=0)
    _, thread = _post_bump_pair()

    keys: list[str] = []
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_keys(keys)):
        await run_messages(config, state, [parent, thread], lambda e: None)

    assert not [k for k in keys if k.startswith("ferry-merge-")], (
        f"a strategy switch under --incremental must not re-deliver messages that are "
        f"already on the server in their own thread channel: {keys}"
    )


async def test_a_resume_interrupted_mid_thread_does_not_resend_the_separator(
    tmp_path: Path,
) -> None:
    """SC-I2: resume continues a thread without repeating its boundary marker.

    A transient offset is what a crashed run leaves behind. The separator gate reads
    it, so a resume must not add a second '── Thread: ... ──' line to the parent, and
    must skip the messages at or below the offset.
    """
    config = _make_config(tmp_path, thread_strategy="merge", message_rate_limit=0.0, resume=True)
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)
    state.channel_map["100"] = "stoat-ch-100"
    # A crash left the thread partway through, at the origin.
    state.channel_message_offsets[_ORIGIN_ID] = _ORIGIN_ID

    parent = _make_export(channel_id="100", channel_name="general", messages=[], message_count=0)
    _, thread = _post_bump_pair()

    keys: list[str] = []
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_keys(keys)):
        await run_messages(config, state, [parent, thread], lambda e: None)

    assert f"ferry-thread-sep-{_ORIGIN_ID}" not in keys, (
        "the separator was re-sent, so every crash would add another one"
    )
    assert f"ferry-merge-{_ORIGIN_ID}" not in keys, (
        "a message at or below the transient offset must not be re-sent"
    )
    assert f"ferry-merge-{_THREAD_REPLY_ID}" in keys, (
        "the tail above the offset is exactly what resume exists to deliver"
    )


async def test_a_second_incremental_pass_sends_nothing(tmp_path: Path) -> None:
    """SC-I3: a completed merge, re-run with --incremental and no new messages.

    The thread's durable high-water covers every message, so `_would_skip` fires
    before the suppression is ever consulted. This also confirms the high-water still
    advanced over the message the first run suppressed, which is why the suppression
    sits below the `_thread_max_id` tracking rather than above it.
    """
    config = _make_config(
        tmp_path, thread_strategy="merge", message_rate_limit=0.0, incremental=True
    )
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)
    state.channel_map["100"] = "stoat-ch-100"
    state.channel_high_water[_ORIGIN_ID] = _THREAD_REPLY_ID  # covers both ids
    state.message_map[_ORIGIN_ID] = "stoat-parent-copy"

    parent = _make_export(channel_id="100", channel_name="general", messages=[], message_count=0)
    _, thread = _post_bump_pair()

    keys: list[str] = []
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_keys(keys)):
        await run_messages(config, state, [parent, thread], lambda e: None)

    assert keys == [], f"an unchanged incremental re-run must make no sends at all: {keys}"


async def test_a_thread_whose_only_message_is_suppressed_still_gets_its_separator(
    tmp_path: Path,
) -> None:
    """SC-I4: a separator with nothing beneath it, and that is the accepted outcome.

    Today the same thread produces a separator plus a literal '[empty message]',
    because the 2.47.1 placeholder has no content. Post-bump it produces a separator
    and nothing, which is less odd, and it still tells the reader a thread existed.

    Pinned so a later reader does not "fix" it into a suppressed separator, which
    would remove the only trace of the thread from the parent channel.
    """
    config = _make_config(tmp_path, thread_strategy="merge", message_rate_limit=0.0)
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)
    state.channel_map["100"] = "stoat-ch-100"

    parent = _make_export(
        channel_id="100",
        channel_name="general",
        messages=[_make_message(_ORIGIN_ID, "the thread's origin message")],
        message_count=1,
    )
    thread = _make_export(
        channel_id=_ORIGIN_ID,
        channel_name="cool-thread",
        is_thread=True,
        parent_channel_name="general",
        messages=[_make_message(_ORIGIN_ID, "the thread's origin message")],
        message_count=1,
    )

    keys: list[str] = []
    events: list[MigrationEvent] = []
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_keys(keys)):
        await run_messages(config, state, [parent, thread], events.append)

    assert f"ferry-thread-sep-{_ORIGIN_ID}" in keys, (
        "the separator is the only remaining trace that this thread existed"
    )
    assert f"ferry-merge-{_ORIGIN_ID}" not in keys
    merged = [e.message or "" for e in events if "Merged thread" in (e.message or "")]
    assert any("(0 messages)" in m for m in merged), (
        f"the completion event must report zero, not one: {merged}"
    )


async def test_a_thread_with_no_resolvable_parent_is_skipped_before_the_suppression(
    tmp_path: Path,
) -> None:
    """SC-I5: the pre-existing merge_parent_not_found path is unchanged.

    Confirms the suppression sits inside the parent-resolved branch. If it had been
    added above the parent lookup, a thread with a missing parent would take a
    different route than it does today.
    """
    config = _make_config(tmp_path, thread_strategy="merge", message_rate_limit=0.0)
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)
    state.message_map[_ORIGIN_ID] = "stoat-parent-copy"

    orphan = _make_export(
        channel_id=_ORIGIN_ID,
        channel_name="cool-thread",
        is_thread=True,
        parent_channel_name="a-channel-that-is-not-in-this-export",
        messages=[_make_message(_ORIGIN_ID, "the thread's origin message")],
        message_count=1,
    )

    keys: list[str] = []
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_keys(keys)):
        await run_messages(config, state, [orphan], lambda e: None)

    assert keys == [], "nothing is sent for a thread whose parent cannot be resolved"
    assert [w for w in state.warnings if w.get("type") == "merge_parent_not_found"], (
        f"the pre-existing warning must still fire: {state.warnings}"
    )
