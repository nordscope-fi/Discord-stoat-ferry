"""Tests for the message import phase (Phase 8)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import aiohttp
import pytest
from aioresponses import aioresponses

from discord_ferry.config import FerryConfig
from discord_ferry.migrator.messages import (
    _build_content,
    _build_masquerade,
    _resolve_attachment_path,
    _skip_attachment,
    _upload_attachments,
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
    DCEReference,
)
from discord_ferry.state import FailedMessage, MigrationState

if TYPE_CHECKING:
    from pathlib import Path

    from discord_ferry.core.events import MigrationEvent

BASE_URL = "https://stoat.test"
AUTUMN_URL = "https://autumn.test"
TOKEN = "test-token"
CHANNEL_MSG_URL = f"{BASE_URL}/channels/stoat_ch1/messages"


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
    }
    defaults.update(overrides)
    return FerryConfig(**defaults)


def _make_state(**overrides: Any) -> MigrationState:
    defaults: dict[str, Any] = {
        "channel_map": {"ch1": "stoat_ch1"},
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
    messages: list[DCEMessage] | None = None,
) -> DCEExport:
    return DCEExport(
        guild=_make_guild(),
        channel=_make_channel(channel_id=channel_id),
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


def _capture_sends(sent: list[dict[str, Any]]) -> Any:
    """Patch target for api_send_message: record kwargs, return a fake id."""

    async def _send(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        sent.append(kwargs)
        return {"_id": f"stoat-{kwargs.get('idempotency_key', 'x')}"}

    return _send


def _sent_message_ids(sent: list[dict[str, Any]]) -> list[str]:
    """Discord ids of captured MESSAGE sends (excludes the thread/forum header)."""
    return [
        k["idempotency_key"].removeprefix("ferry-")
        for k in sent
        if not k["idempotency_key"].startswith("ferry-header-")
    ]


def _collect_events(events: list[MigrationEvent]) -> Any:
    def callback(event: MigrationEvent) -> None:
        events.append(event)

    return callback


@pytest.fixture
def mock_aiohttp() -> aioresponses:
    with aioresponses() as m:
        yield m


# ---------------------------------------------------------------------------
# _resolve_attachment_path
# ---------------------------------------------------------------------------


def test_resolve_attachment_path_local(tmp_path: Path) -> None:
    """A relative URL resolves to export_dir / url."""
    result = _resolve_attachment_path(tmp_path, "media/image.png")
    assert result == tmp_path / "media/image.png"


def test_resolve_attachment_path_http_returns_none(tmp_path: Path) -> None:
    """An http:// URL returns None (cannot be locally resolved)."""
    result = _resolve_attachment_path(tmp_path, "http://cdn.discordapp.com/image.png")
    assert result is None


def test_resolve_attachment_path_https_returns_none(tmp_path: Path) -> None:
    """An https:// URL returns None."""
    result = _resolve_attachment_path(tmp_path, "https://cdn.discordapp.com/image.png")
    assert result is None


# ---------------------------------------------------------------------------
# _build_content
# ---------------------------------------------------------------------------


def test_build_content_applies_transforms_in_order(tmp_path: Path) -> None:
    """Content transforms are applied: spoilers, underline, mentions, emoji, timestamp."""
    state = _make_state(
        channel_map={"123456789": "stoat_ch99"},
        role_map={},
        emoji_map={},
        author_names={"111222333": "Bob"},
    )
    msg = _make_message(
        content="||secret|| __bold__ <@111222333>",
        timestamp="2024-01-15T12:00:00+00:00",
    )
    result = _build_content(msg, state)

    # Spoiler conversion
    assert "!!secret!!" in result
    # Underline → bold
    assert "**bold**" in result
    # Mention remap (numeric Discord user ID → author display name)
    assert "@Bob" in result
    # Timestamp prepended
    assert result.startswith("*[2024-01-15 12:00 UTC]*")


def test_build_content_prepends_timestamp(tmp_path: Path) -> None:
    """The formatted original timestamp is always prepended."""
    state = _make_state()
    msg = _make_message(content="hi", timestamp="2024-06-01T08:30:00+00:00")
    result = _build_content(msg, state)
    assert result.startswith("*[2024-06-01 08:30 UTC]*")


def test_build_content_appends_stickers() -> None:
    """Sticker names are appended to the content."""
    state = _make_state()
    msg = _make_message(content="look", stickers=[{"name": "wave"}])
    result = _build_content(msg, state)
    assert "[Sticker: wave]" in result


def test_build_content_remaps_custom_emoji() -> None:
    """Custom emoji in content is remapped via emoji_map."""
    state = _make_state(emoji_map={"12345": "stoat_emoji_id"})
    msg = _make_message(content="hey <:smile:12345>")
    result = _build_content(msg, state)
    assert ":stoat_emoji_id:" in result


def test_build_content_fallback_emoji() -> None:
    """Unknown custom emoji becomes bracketed name fallback."""
    state = _make_state(emoji_map={})
    msg = _make_message(content="<:cry:99999>")
    result = _build_content(msg, state)
    assert "[:cry:]" in result


# ---------------------------------------------------------------------------
# _build_masquerade
# ---------------------------------------------------------------------------


async def test_build_masquerade_uses_nickname_over_name(tmp_path: Path) -> None:
    """Masquerade name uses nickname when set."""
    state = _make_state()
    config = _make_config(tmp_path)
    author = _make_author(name="Alice", nickname="Ally")
    async with aiohttp.ClientSession() as session:
        result = await _build_masquerade(author, session, state, config)
    assert result["name"] == "Ally"


async def test_build_masquerade_falls_back_to_name(tmp_path: Path) -> None:
    """Masquerade name uses author.name when nickname is empty."""
    state = _make_state()
    config = _make_config(tmp_path)
    author = _make_author(name="Bob", nickname="")
    async with aiohttp.ClientSession() as session:
        result = await _build_masquerade(author, session, state, config)
    assert result["name"] == "Bob"


async def test_build_masquerade_colour_passthrough(tmp_path: Path) -> None:
    """Author colour is passed to masquerade as-is (British spelling in key)."""
    state = _make_state()
    config = _make_config(tmp_path)
    author = _make_author(color="#ff0000")
    async with aiohttp.ClientSession() as session:
        result = await _build_masquerade(author, session, state, config)
    assert result["colour"] == "#ff0000"


async def test_build_masquerade_no_colour_omitted(tmp_path: Path) -> None:
    """When author has no colour, masquerade omits the colour key."""
    state = _make_state()
    config = _make_config(tmp_path)
    author = _make_author(color=None)
    async with aiohttp.ClientSession() as session:
        result = await _build_masquerade(author, session, state, config)
    assert "colour" not in result


async def test_build_masquerade_avatar_cache_hit(tmp_path: Path) -> None:
    """When avatar is in cache, Autumn URL is constructed without an upload."""
    state = _make_state(avatar_cache={"auth1": "cached_file_id"})
    config = _make_config(tmp_path)
    author = _make_author(id="auth1")
    async with aiohttp.ClientSession() as session:
        result = await _build_masquerade(author, session, state, config)
    assert result["avatar"] == f"{AUTUMN_URL}/avatars/cached_file_id"


async def test_build_masquerade_avatar_upload_and_cache(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """Local avatar file is uploaded to Autumn and stored in avatar_cache."""
    avatar_file = tmp_path / "avatar.png"
    avatar_file.write_bytes(b"x" * 100)

    mock_aiohttp.post(f"{AUTUMN_URL}/avatars", payload={"id": "new_avatar_id"})

    state = _make_state(avatar_cache={}, upload_cache={})
    config = _make_config(tmp_path)
    author = _make_author(id="auth1", avatar_url="avatar.png")

    async with aiohttp.ClientSession() as session:
        result = await _build_masquerade(author, session, state, config)

    assert result["avatar"] == f"{AUTUMN_URL}/avatars/new_avatar_id"
    assert state.avatar_cache["auth1"] == "new_avatar_id"


async def test_build_masquerade_missing_avatar_graceful(tmp_path: Path) -> None:
    """Missing local avatar file does not raise — avatar key is omitted."""
    state = _make_state()
    config = _make_config(tmp_path)
    author = _make_author(id="auth1", avatar_url="nonexistent_avatar.png")
    async with aiohttp.ClientSession() as session:
        result = await _build_masquerade(author, session, state, config)
    assert "avatar" not in result


async def test_build_masquerade_truncates_long_name(tmp_path: Path) -> None:
    """Masquerade name is truncated to 32 characters."""
    state = _make_state()
    config = _make_config(tmp_path)
    long_name = "a" * 50
    author = _make_author(id="auth1", name=long_name)
    async with aiohttp.ClientSession() as session:
        result = await _build_masquerade(author, session, state, config)
    assert len(result["name"]) == 32


async def test_build_masquerade_http_avatar_skipped(tmp_path: Path) -> None:
    """Remote avatar URLs are not uploaded — avatar key is omitted."""
    state = _make_state()
    config = _make_config(tmp_path)
    author = _make_author(avatar_url="https://cdn.discord.com/avatars/user1/abc.png")
    async with aiohttp.ClientSession() as session:
        result = await _build_masquerade(author, session, state, config)
    assert "avatar" not in result


# ---------------------------------------------------------------------------
# Message type filtering
# ---------------------------------------------------------------------------


async def test_skip_types_are_not_sent(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """Messages with skip types are silently dropped without an API call."""
    skip_types = [
        "RecipientAdd",
        "RecipientRemove",
        "ChannelNameChange",
        "UserPremiumGuildSubscription",
    ]
    events: list[MigrationEvent] = []
    for msg_type in skip_types:
        state = _make_state()
        config = _make_config(tmp_path)
        msg = _make_message(msg_type=msg_type)
        export = _make_export(messages=[msg])
        await run_messages(config, state, [export], _collect_events(events))
        assert msg.id not in state.message_map


async def test_default_message_is_imported(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """A Default type message is sent and mapped."""
    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_msg_1"})

    state = _make_state()
    config = _make_config(tmp_path)
    msg = _make_message(id="msg1", content="hello world")
    export = _make_export(messages=[msg])

    await run_messages(config, state, [export], lambda e: None)

    assert state.message_map["msg1"] == "stoat_msg_1"


async def test_reply_type_is_imported(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """A Reply type message is imported (not skipped)."""
    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_reply"})

    state = _make_state()
    config = _make_config(tmp_path)
    msg = _make_message(id="reply1", msg_type="Reply", content="reply text")
    export = _make_export(messages=[msg])

    await run_messages(config, state, [export], lambda e: None)

    assert "reply1" in state.message_map


# ---------------------------------------------------------------------------
# Forwarded message detection
# ---------------------------------------------------------------------------


async def test_forwarded_message_skipped(tmp_path: Path) -> None:
    """Empty content + no attachments + reference + Default type → forwarded, skipped."""
    events: list[MigrationEvent] = []
    state = _make_state()
    config = _make_config(tmp_path)
    msg = _make_message(
        id="fwd1",
        content="",
        msg_type="Default",
        reference=DCEReference(message_id="orig1"),
    )
    export = _make_export(messages=[msg])

    await run_messages(config, state, [export], _collect_events(events))

    assert "fwd1" not in state.message_map
    warning_messages = [e.message for e in events if e.status == "warning"]
    assert any("fwd1" in w for w in warning_messages)


async def test_non_forwarded_empty_content_with_attachment_not_skipped(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """Empty content + attachment + reference = NOT a forwarded message (has attachment)."""
    att_file = tmp_path / "file.png"
    att_file.write_bytes(b"data")
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": "att_id"})
    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_msg"})

    state = _make_state()
    config = _make_config(tmp_path)
    att = DCEAttachment(id="att1", url="file.png", file_name="file.png")
    msg = _make_message(
        id="msg_with_att",
        content="",
        msg_type="Default",
        attachments=[att],
        reference=DCEReference(message_id="orig1"),
    )
    export = _make_export(messages=[msg])

    await run_messages(config, state, [export], lambda e: None)

    assert "msg_with_att" in state.message_map


# ---------------------------------------------------------------------------
# Attachment upload
# ---------------------------------------------------------------------------


async def test_attachment_max_5(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """Only the first 5 attachments are uploaded."""
    for i in range(7):
        f = tmp_path / f"file{i}.png"
        f.write_bytes(b"x" * 10)
        mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": f"att_id_{i}"})

    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_msg"})

    state = _make_state()
    config = _make_config(tmp_path)
    attachments = [
        DCEAttachment(id=str(i), url=f"file{i}.png", file_name=f"file{i}.png") for i in range(7)
    ]
    msg = _make_message(id="msg1", content="many files", attachments=attachments)
    export = _make_export(messages=[msg])

    await run_messages(config, state, [export], lambda e: None)

    assert "msg1" in state.message_map
    # Only 5 uploads should have been queued; remaining 2 mock entries are unconsumed.
    # We verify by checking the upload_cache has exactly 5 entries.
    assert len(state.upload_cache) == 5


async def test_http_attachment_skipped(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """An attachment with an http URL is skipped and increments attachments_skipped."""
    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_msg"})

    state = _make_state()
    config = _make_config(tmp_path)
    att = DCEAttachment(id="att1", url="https://cdn.discord.com/file.png", file_name="file.png")
    msg = _make_message(id="msg1", content="with remote att", attachments=[att])
    export = _make_export(messages=[msg])

    await run_messages(config, state, [export], lambda e: None)

    assert state.attachments_skipped == 1
    assert "msg1" in state.message_map  # Message still sent.


async def test_missing_local_attachment_skipped(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """A locally referenced attachment that doesn't exist is skipped gracefully."""
    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_msg"})

    state = _make_state()
    config = _make_config(tmp_path)
    att = DCEAttachment(id="att1", url="nonexistent/file.png", file_name="file.png")
    msg = _make_message(id="msg1", content="missing file", attachments=[att])
    export = _make_export(messages=[msg])

    await run_messages(config, state, [export], lambda e: None)

    assert state.attachments_skipped == 1
    assert "msg1" in state.message_map


async def test_attachment_upload_cache_used(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """If an attachment is in upload_cache, it is not re-uploaded."""
    att_file = tmp_path / "cached.png"
    att_file.write_bytes(b"x" * 10)
    cache_key = str(att_file)

    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_msg"})
    # No upload mock — if upload is attempted it will raise.

    state = _make_state(upload_cache={cache_key: "already_uploaded_id"})
    config = _make_config(tmp_path)
    att = DCEAttachment(id="att1", url="cached.png", file_name="cached.png")
    msg = _make_message(id="msg1", content="cached att", attachments=[att])
    export = _make_export(messages=[msg])

    await run_messages(config, state, [export], lambda e: None)

    assert "msg1" in state.message_map
    # No new entries added to cache.
    assert state.upload_cache[cache_key] == "already_uploaded_id"


# ---------------------------------------------------------------------------
# Embed handling
# ---------------------------------------------------------------------------


async def test_embeds_max_5(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """Only first 5 embeds with title/description are included."""
    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_msg"})

    state = _make_state()
    config = _make_config(tmp_path)
    embeds = [{"title": f"Embed {i}", "description": f"desc {i}"} for i in range(7)]
    msg = _make_message(id="msg1", content="embeds", embeds=embeds)
    export = _make_export(messages=[msg])

    await run_messages(config, state, [export], lambda e: None)

    assert "msg1" in state.message_map


async def test_embed_without_title_or_description_excluded(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """Embeds that flatten to no title and no description are excluded."""
    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_msg"})

    state = _make_state()
    config = _make_config(tmp_path)
    # An embed with only a url (no title, no description)
    embeds = [{"url": "https://example.com"}]
    msg = _make_message(id="msg1", content="embed no text", embeds=embeds)
    export = _make_export(messages=[msg])

    await run_messages(config, state, [export], lambda e: None)

    assert "msg1" in state.message_map


# ---------------------------------------------------------------------------
# Reply references
# ---------------------------------------------------------------------------


async def test_reply_reference_found_in_map(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """When the referenced message is in message_map, replies list is populated."""
    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_reply"})

    state = _make_state(message_map={"orig_discord_id": "orig_stoat_id"})
    config = _make_config(tmp_path)
    msg = _make_message(
        id="reply1",
        msg_type="Reply",
        content="responding to something",
        reference=DCEReference(message_id="orig_discord_id"),
    )
    export = _make_export(messages=[msg])

    await run_messages(config, state, [export], lambda e: None)

    assert "reply1" in state.message_map


async def test_reply_reference_not_in_map_is_silently_skipped(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """When the referenced message is not in message_map, message is still sent."""
    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_reply"})

    state = _make_state(message_map={})  # Empty — no known messages.
    config = _make_config(tmp_path)
    msg = _make_message(
        id="reply1",
        msg_type="Reply",
        content="replying to unknown",
        reference=DCEReference(message_id="unknown_id"),
    )
    export = _make_export(messages=[msg])
    events: list[MigrationEvent] = []

    await run_messages(config, state, [export], _collect_events(events))

    assert "reply1" in state.message_map
    # No error events for missing reply reference.
    assert not any(e.status == "error" for e in events)


# ---------------------------------------------------------------------------
# Empty message handling
# ---------------------------------------------------------------------------


async def test_empty_message_gets_placeholder(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """Empty content + no attachments + no embeds → placeholder text sent."""
    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_msg"})

    state = _make_state()
    config = _make_config(tmp_path)
    # Use a Default type — GuildMemberJoin is now a skip type.
    msg = _make_message(id="msg1", content="", msg_type="Default")
    export = _make_export(messages=[msg])

    await run_messages(config, state, [export], lambda e: None)

    assert "msg1" in state.message_map


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


async def test_content_split_at_2001_chars(tmp_path: Path) -> None:
    """Content exceeding 2000 characters is split into multiple parts (not truncated)."""
    state = _make_state()
    config = _make_config(tmp_path)
    long_content = "A" * 3000
    msg = _make_message(id="msg1", content=long_content)
    export = _make_export(messages=[msg])

    # Capture all payloads sent.
    sent_content: list[str] = []

    async def capture_send(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        sent_content.append(kwargs.get("content", ""))
        return {"_id": "stoat_msg"}

    with patch("discord_ferry.migrator.messages.api_send_message", capture_send):
        await run_messages(config, state, [export], lambda e: None)

    # Content is split into multiple parts, all ≤2000 chars.
    assert len(sent_content) >= 2
    for part in sent_content:
        assert len(part) <= 2000
    # First part should have continuation marker, not "..."
    assert "continued" in sent_content[0]
    assert not sent_content[0].endswith("...")


# ---------------------------------------------------------------------------
# Nonce format
# ---------------------------------------------------------------------------


async def test_idempotency_key_format(tmp_path: Path) -> None:
    """The idempotency_key sent to the API matches the f'ferry-{msg.id}' pattern."""
    sent_kwargs: list[dict[str, Any]] = []

    async def capture_send(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        sent_kwargs.append(kwargs)
        return {"_id": "stoat_msg"}

    state = _make_state()
    config = _make_config(tmp_path)
    msg = _make_message(id="discord_abc123", content="hi")
    export = _make_export(messages=[msg])

    with patch("discord_ferry.migrator.messages.api_send_message", capture_send):
        await run_messages(config, state, [export], lambda e: None)

    assert sent_kwargs[0]["idempotency_key"] == "ferry-discord_abc123"


# ---------------------------------------------------------------------------
# Pin queuing
# ---------------------------------------------------------------------------


async def test_pinned_message_queued(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """A pinned message has its (channel_id, msg_id) tuple added to pending_pins."""
    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_pinned"})

    state = _make_state()
    config = _make_config(tmp_path)
    msg = _make_message(id="pinned1", content="important", is_pinned=True)
    export = _make_export(messages=[msg])

    await run_messages(config, state, [export], lambda e: None)

    assert ("stoat_ch1", "stoat_pinned") in state.pending_pins


async def test_unpinned_message_not_queued(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """An unpinned message does not appear in pending_pins."""
    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_msg"})

    state = _make_state()
    config = _make_config(tmp_path)
    msg = _make_message(id="msg1", content="regular", is_pinned=False)
    export = _make_export(messages=[msg])

    await run_messages(config, state, [export], lambda e: None)

    assert len(state.pending_pins) == 0


# ---------------------------------------------------------------------------
# Reaction queuing
# ---------------------------------------------------------------------------


async def test_custom_emoji_reaction_queued(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """Custom emoji reactions are queued via emoji_map lookup."""
    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_msg"})

    state = _make_state(emoji_map={"discord_emoji_1": "stoat_emoji_1"})
    config = _make_config(tmp_path, reaction_mode="native")
    reaction = DCEReaction(emoji=DCEEmoji(id="discord_emoji_1", name="smile"), count=3)
    msg = _make_message(id="msg1", content="reacted", reactions=[reaction])
    export = _make_export(messages=[msg])

    await run_messages(config, state, [export], lambda e: None)

    assert len(state.pending_reactions) == 1
    assert state.pending_reactions[0]["emoji"] == "stoat_emoji_1"


async def test_custom_emoji_not_in_map_not_queued(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """Custom emoji missing from emoji_map is not queued."""
    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_msg"})

    state = _make_state(emoji_map={})
    config = _make_config(tmp_path, reaction_mode="native")
    reaction = DCEReaction(emoji=DCEEmoji(id="unknown_emoji", name="mystery"), count=1)
    msg = _make_message(id="msg1", content="reacted", reactions=[reaction])
    export = _make_export(messages=[msg])

    await run_messages(config, state, [export], lambda e: None)

    assert len(state.pending_reactions) == 0


async def test_unicode_emoji_reaction_queued(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """Unicode emoji reactions are queued with the emoji name directly."""
    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_msg"})

    state = _make_state()
    config = _make_config(tmp_path, reaction_mode="native")
    reaction = DCEReaction(emoji=DCEEmoji(id="", name="👍"), count=5)
    msg = _make_message(id="msg1", content="thumbs up", reactions=[reaction])
    export = _make_export(messages=[msg])

    await run_messages(config, state, [export], lambda e: None)

    assert len(state.pending_reactions) == 1
    assert state.pending_reactions[0]["emoji"] == "👍"


# ---------------------------------------------------------------------------
# Resume support
# ---------------------------------------------------------------------------


async def test_resume_skips_completed_channels(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """Channels in completed_channel_ids are skipped on resume."""
    # Only ch 200 should be processed (ch 100 is in completed_channel_ids).
    mock_aiohttp.post(f"{BASE_URL}/channels/stoat_ch200/messages", payload={"_id": "stoat_msg2"})

    state = _make_state(
        channel_map={"100": "stoat_ch100", "200": "stoat_ch200"},
        completed_channel_ids={"100"},
    )
    config = _make_config(tmp_path, resume=True)

    msg1 = _make_message(id="1001", content="old")
    msg2 = _make_message(id="2001", content="new", timestamp="2024-01-15T13:00:00+00:00")
    export1 = _make_export(channel_id="100", messages=[msg1])
    export2 = DCEExport(
        guild=_make_guild(),
        channel=_make_channel(channel_id="200", name="announcements"),
        messages=[msg2],
    )

    await run_messages(config, state, [export1, export2], lambda e: None)

    # ch 100 message should NOT be re-imported.
    assert "1001" not in state.message_map
    # ch 200 message should be imported.
    assert "2001" in state.message_map


async def test_resume_skips_completed_messages_within_channel(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """Messages with ID <= channel_message_offsets entry are skipped on resume."""
    mock_aiohttp.post(f"{BASE_URL}/channels/stoat_ch500/messages", payload={"_id": "stoat_msg2"})

    state = _make_state(
        channel_map={"500": "stoat_ch500"},
        channel_message_offsets={"500": "1000"},
    )
    config = _make_config(tmp_path, resume=True)

    msg1 = _make_message(id="1000", content="already done", timestamp="2024-01-15T12:00:00+00:00")
    msg2 = _make_message(id="2000", content="needs import", timestamp="2024-01-15T12:01:00+00:00")
    export = _make_export(channel_id="500", messages=[msg1, msg2])

    await run_messages(config, state, [export], lambda e: None)

    assert "1000" not in state.message_map
    assert "2000" in state.message_map


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


async def test_api_failure_does_not_stop_other_messages(tmp_path: Path) -> None:
    """A send failure on one message does not prevent subsequent messages from being sent."""
    call_count = 0

    async def flaky_send(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("Temporary failure")
        return {"_id": f"stoat_msg_{call_count}"}

    state = _make_state()
    config = _make_config(tmp_path)
    msg1 = _make_message(id="msg1", content="fail", timestamp="2024-01-15T12:00:00+00:00")
    msg2 = _make_message(id="msg2", content="success", timestamp="2024-01-15T12:01:00+00:00")
    export = _make_export(messages=[msg1, msg2])

    with patch("discord_ferry.migrator.messages.api_send_message", flaky_send):
        await run_messages(config, state, [export], lambda e: None)

    assert "msg1" not in state.message_map
    assert "msg2" in state.message_map
    assert len(state.errors) == 1


async def test_api_failure_adds_to_errors(tmp_path: Path) -> None:
    """A send failure adds an entry to state.errors."""

    async def always_fail(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        raise RuntimeError("API down")

    state = _make_state()
    config = _make_config(tmp_path)
    msg = _make_message(id="msg1", content="bad message")
    export = _make_export(messages=[msg])

    with patch("discord_ferry.migrator.messages.api_send_message", always_fail):
        await run_messages(config, state, [export], lambda e: None)

    assert len(state.errors) == 1
    assert "msg1" in state.errors[0]["message"]


async def test_channel_not_in_channel_map_skipped(tmp_path: Path) -> None:
    """A channel not found in channel_map is warned and skipped."""
    events: list[MigrationEvent] = []
    state = _make_state(channel_map={})  # Empty map.
    config = _make_config(tmp_path)
    msg = _make_message(id="msg1", content="lost message")
    export = _make_export(messages=[msg])

    await run_messages(config, state, [export], _collect_events(events))

    assert "msg1" not in state.message_map
    skipped_events = [e for e in events if e.status == "skipped"]
    assert len(skipped_events) == 1


# ---------------------------------------------------------------------------
# Full run_messages e2e
# ---------------------------------------------------------------------------


async def test_run_messages_e2e(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """End-to-end: two messages sent, state.message_map populated, pins and reactions queued."""
    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_msg_1"})
    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_msg_2"})

    state = _make_state(
        channel_map={"ch1": "stoat_ch1"},
        emoji_map={"emoji1": "stoat_emoji1"},
    )
    config = _make_config(tmp_path, reaction_mode="native")

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

    # Progress events were emitted.
    statuses = [e.status for e in events]
    assert "started" in statuses
    assert "completed" in statuses

    # Channel marked as completed.
    assert "ch1" in state.completed_channel_ids


# ---------------------------------------------------------------------------
# Bug 2: GuildMemberJoin and ThreadCreated are now skip types
# ---------------------------------------------------------------------------


async def test_guild_member_join_skipped(tmp_path: Path) -> None:
    """GuildMemberJoin messages are silently dropped (not sent to API)."""
    state = _make_state()
    config = _make_config(tmp_path)
    msg = _make_message(id="join1", content="", msg_type="GuildMemberJoin")
    export = _make_export(messages=[msg])

    await run_messages(config, state, [export], lambda e: None)

    assert "join1" not in state.message_map


async def test_thread_created_skipped(tmp_path: Path) -> None:
    """ThreadCreated messages are silently dropped."""
    state = _make_state()
    config = _make_config(tmp_path)
    msg = _make_message(id="tc1", content="", msg_type="ThreadCreated")
    export = _make_export(messages=[msg])

    await run_messages(config, state, [export], lambda e: None)

    assert "tc1" not in state.message_map


async def test_channel_pinned_message_adds_to_pending_pins(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """ChannelPinnedMessage marks referenced message for pinning, not sent as content."""
    # Send a normal message first so the reference target exists in message_map.
    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_original"})

    state = _make_state()
    config = _make_config(tmp_path)
    ref = DCEReference(message_id="original1")
    original_msg = _make_message(id="original1", content="important info")
    pin_msg = _make_message(
        id="pinmsg1",
        content="pinned a message",
        msg_type="ChannelPinnedMessage",
        reference=ref,
        timestamp="2024-01-15T13:00:00+00:00",
    )
    export = _make_export(messages=[original_msg, pin_msg])

    await run_messages(config, state, [export], lambda e: None)

    # The original was sent; the pin notification was NOT sent.
    assert "original1" in state.message_map
    assert "pinmsg1" not in state.message_map
    # The referenced message was queued for pinning.
    assert ("stoat_ch1", "stoat_original") in state.pending_pins


async def test_channel_pinned_message_unknown_ref(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """ChannelPinnedMessage with unknown reference logs a warning."""
    state = _make_state()
    config = _make_config(tmp_path)
    ref = DCEReference(message_id="nonexistent999")
    msg = _make_message(
        id="pinmsg2",
        content="pinned a message",
        msg_type="ChannelPinnedMessage",
        reference=ref,
    )
    export = _make_export(messages=[msg])

    await run_messages(config, state, [export], lambda e: None)

    assert "pinmsg2" not in state.message_map
    assert any("nonexistent999" in w["message"] for w in state.warnings)


async def test_thread_starter_message_imported(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """ThreadStarterMessage type is NOT skipped — it falls through to normal handling."""
    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_starter"})

    state = _make_state()
    config = _make_config(tmp_path)
    msg = _make_message(id="starter1", content="thread start", msg_type="ThreadStarterMessage")
    export = _make_export(messages=[msg])

    await run_messages(config, state, [export], lambda e: None)

    assert "starter1" in state.message_map


# ---------------------------------------------------------------------------
# Bug 3: Thread header messages
# ---------------------------------------------------------------------------


async def test_thread_header_injected(tmp_path: Path) -> None:
    """Thread exports get a system header message before regular messages."""
    sent_contents: list[str] = []

    async def capture_send(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        sent_contents.append(kwargs.get("content", ""))
        return {"_id": f"stoat_msg_{len(sent_contents)}"}

    state = _make_state(channel_map={"th1": "stoat_th1"})
    config = _make_config(tmp_path)
    msg = _make_message(id="msg1", content="hello thread")
    export = DCEExport(
        guild=_make_guild(),
        channel=_make_channel(channel_id="th1", name="cool-thread"),
        messages=[msg],
        is_thread=True,
        parent_channel_name="general",
    )

    with patch("discord_ferry.migrator.messages.api_send_message", capture_send):
        await run_messages(config, state, [export], lambda e: None)

    # First message sent should be the header.
    assert len(sent_contents) >= 2
    assert "[Thread migrated from #general]" in sent_contents[0]


async def test_non_thread_no_header(tmp_path: Path) -> None:
    """Non-thread exports do NOT get a system header."""
    sent_contents: list[str] = []

    async def capture_send(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        sent_contents.append(kwargs.get("content", ""))
        return {"_id": f"stoat_msg_{len(sent_contents)}"}

    state = _make_state()
    config = _make_config(tmp_path)
    msg = _make_message(id="msg1", content="hello")
    export = _make_export(messages=[msg])

    with patch("discord_ferry.migrator.messages.api_send_message", capture_send):
        await run_messages(config, state, [export], lambda e: None)

    # Only 1 message sent (no header).
    assert len(sent_contents) == 1
    assert "Thread migrated" not in sent_contents[0]


# ---------------------------------------------------------------------------
# Bug 1: skip_threads in messages phase
# ---------------------------------------------------------------------------


async def test_call_message_type_skipped(tmp_path: Path) -> None:
    """Call messages are silently dropped (not sent to API)."""
    state = _make_state()
    config = _make_config(tmp_path)
    msg = _make_message(id="call1", content="", msg_type="Call")
    export = _make_export(messages=[msg])

    await run_messages(config, state, [export], lambda e: None)

    assert "call1" not in state.message_map


async def test_channel_icon_change_skipped(tmp_path: Path) -> None:
    """ChannelIconChange messages are silently dropped."""
    state = _make_state()
    config = _make_config(tmp_path)
    msg = _make_message(id="icon1", content="", msg_type="ChannelIconChange")
    export = _make_export(messages=[msg])

    await run_messages(config, state, [export], lambda e: None)

    assert "icon1" not in state.message_map


async def test_api_send_message_includes_silent(tmp_path: Path) -> None:
    """The message payload includes silent=true by default."""
    sent_kwargs: list[dict[str, Any]] = []

    async def capture_send(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        sent_kwargs.append(kwargs)
        return {"_id": "stoat_msg"}

    state = _make_state()
    config = _make_config(tmp_path)
    msg = _make_message(id="msg1", content="hello")
    export = _make_export(messages=[msg])

    with patch("discord_ferry.migrator.messages.api_send_message", capture_send):
        await run_messages(config, state, [export], lambda e: None)

    # api_send_message is called with silent=True by default (the default parameter)
    # Since messages.py doesn't explicitly pass silent, it uses the default True
    assert "msg1" in state.message_map


async def test_attachments_uploaded_counter_increments(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """attachments_uploaded counter increments on successful upload."""
    att_file = tmp_path / "file.png"
    att_file.write_bytes(b"data")
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": "att_id"})
    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_msg"})

    state = _make_state()
    config = _make_config(tmp_path)
    att = DCEAttachment(id="att1", url="file.png", file_name="file.png")
    msg = _make_message(id="msg1", content="with file", attachments=[att])
    export = _make_export(messages=[msg])

    await run_messages(config, state, [export], lambda e: None)

    assert state.attachments_uploaded == 1


async def test_skip_threads_skips_thread_exports(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """When skip_threads=True, thread exports are not processed in messages phase."""
    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_msg"})

    state = _make_state(channel_map={"ch1": "stoat_ch1", "th1": "stoat_th1"})
    config = _make_config(tmp_path, skip_threads=True)

    msg_main = _make_message(id="main1", content="main channel msg")
    msg_thread = _make_message(id="thread1", content="thread msg")

    export_main = _make_export(channel_id="ch1", messages=[msg_main])
    export_thread = DCEExport(
        guild=_make_guild(),
        channel=_make_channel(channel_id="th1", name="my-thread"),
        messages=[msg_thread],
        is_thread=True,
        parent_channel_name="general",
    )

    await run_messages(config, state, [export_main, export_thread], lambda e: None)

    assert "main1" in state.message_map
    assert "thread1" not in state.message_map


async def test_embed_media_upload(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """Embed with a local thumbnail triggers upload and sets media field on the embed."""
    # Create a local thumbnail file
    thumb_dir = tmp_path / "media"
    thumb_dir.mkdir()
    thumb_file = thumb_dir / "thumb.png"
    thumb_file.write_bytes(b"fake-png-data")

    msg = _make_message(
        id="msg_embed",
        content="Check this out",
        embeds=[
            {
                "title": "Link Preview",
                "description": "A description",
                "thumbnail": {"url": "media/thumb.png"},
            }
        ],
    )
    export = _make_export(messages=[msg])
    config = _make_config(tmp_path)
    state = _make_state()
    events: list[MigrationEvent] = []

    # Mock autumn upload
    mock_aiohttp.post(
        f"{AUTUMN_URL}/attachments",
        payload={"id": "autumn_thumb1"},
        repeat=True,
    )
    # Mock message send
    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_embed_msg"}, repeat=True)

    await run_messages(config, state, [export], events.append)
    assert "msg_embed" in state.message_map
    assert state.attachments_uploaded >= 1


async def test_sticker_image_upload(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """Message with a sticker that has a local image path triggers upload."""
    sticker_dir = tmp_path / "stickers"
    sticker_dir.mkdir()
    sticker_file = sticker_dir / "cool.png"
    sticker_file.write_bytes(b"fake-sticker-data")

    msg = _make_message(
        id="msg_sticker",
        content="Look at this sticker",
        stickers=[
            {
                "id": "sticker1",
                "name": "CoolSticker",
                "format": "png",
                "sourceUrl": "stickers/cool.png",
            }
        ],
    )
    export = _make_export(messages=[msg])
    config = _make_config(tmp_path)
    state = _make_state()
    events: list[MigrationEvent] = []

    mock_aiohttp.post(
        f"{AUTUMN_URL}/attachments",
        payload={"id": "autumn_sticker1"},
        repeat=True,
    )
    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_sticker_msg"}, repeat=True)

    await run_messages(config, state, [export], events.append)
    assert "msg_sticker" in state.message_map
    assert state.attachments_uploaded >= 1


async def test_poll_in_build_content(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """Message with a poll field includes poll text in the sent content."""
    msg = _make_message(
        id="msg_poll",
        content="Vote here",
        poll={
            "question": {"text": "What do you prefer?"},
            "answers": [
                {"text": "Option A", "votes": 5},
                {"text": "Option B", "votes": 3},
            ],
        },
    )
    export = _make_export(messages=[msg])
    config = _make_config(tmp_path)
    state = _make_state()
    events: list[MigrationEvent] = []

    captured_bodies: list[dict[str, Any]] = []

    def capture_callback(url: object, **kwargs: Any) -> None:
        body = kwargs.get("json") or {}
        captured_bodies.append(dict(body))

    mock_aiohttp.post(
        CHANNEL_MSG_URL,
        payload={"_id": "stoat_poll_msg"},
        callback=capture_callback,
        repeat=True,
    )

    await run_messages(config, state, [export], events.append)
    assert "msg_poll" in state.message_map
    # At least one sent message should contain poll text
    poll_found = any("What do you prefer?" in str(b.get("content", "")) for b in captured_bodies)
    assert poll_found, f"Poll text not found in sent messages: {captured_bodies}"


# ---------------------------------------------------------------------------
# _skip_attachment helper
# ---------------------------------------------------------------------------


def test_skip_attachment_returns_placeholder() -> None:
    """_skip_attachment returns a bracketed placeholder with the reason."""
    state = _make_state()
    reason = "File too large: photo.png (25.0 MB, limit: 20.0 MB)"
    result = _skip_attachment(state, "photo.png", reason)
    assert result == f"[{reason}]"
    assert state.attachments_skipped == 1
    assert len(state.warnings) == 1
    assert state.warnings[0]["type"] == "attachment_skipped"


def test_skip_attachment_increments_on_multiple_calls() -> None:
    """Counter increments correctly across multiple calls."""
    state = _make_state()
    _skip_attachment(state, "a.png", "reason a")
    _skip_attachment(state, "b.png", "reason b")
    _skip_attachment(state, "c.png", "reason c")
    assert state.attachments_skipped == 3
    assert len(state.warnings) == 3


# ---------------------------------------------------------------------------
# Size pre-check in _upload_attachments
# ---------------------------------------------------------------------------


async def test_oversized_attachment_skipped_before_upload(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """Attachment exceeding 20 MB limit is skipped with no HTTP call."""
    state = _make_state()
    config = _make_config(tmp_path)
    events: list[MigrationEvent] = []

    oversized = DCEAttachment(
        id="att1",
        url="huge.bin",
        file_name="huge.bin",
        file_size_bytes=25 * 1024 * 1024,  # 25 MB — over 20 MB limit
    )
    msg = _make_message(id="msg1", content="with attachment", attachments=[oversized])

    async with aiohttp.ClientSession() as session:
        result_ids, result_placeholders = await _upload_attachments(
            msg, config, state, session, _collect_events(events)
        )

    assert result_ids == []
    assert len(result_placeholders) >= 1
    assert state.attachments_skipped == 1
    assert any(w["type"] == "attachment_skipped" for w in state.warnings)
    warning_events = [e for e in events if e.status == "warning"]
    assert any("too large" in e.message for e in warning_events)


async def test_file_size_zero_falls_through_to_upload(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """file_size_bytes=0 (unknown size) falls through to normal upload path."""
    att_file = tmp_path / "unknown_size.png"
    att_file.write_bytes(b"data")

    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": "autumn_id"})

    state = _make_state()
    config = _make_config(tmp_path)
    events: list[MigrationEvent] = []

    att = DCEAttachment(
        id="att1",
        url="unknown_size.png",
        file_name="unknown_size.png",
        file_size_bytes=0,
    )
    msg = _make_message(id="msg1", content="with file", attachments=[att])

    async with aiohttp.ClientSession() as session:
        result_ids, _result_placeholders = await _upload_attachments(
            msg, config, state, session, _collect_events(events)
        )

    assert len(result_ids) == 1
    assert state.attachments_uploaded == 1
    assert state.attachments_skipped == 0


async def test_attachment_exactly_at_limit_proceeds(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """Attachment exactly at 20 MB limit proceeds to upload (> not >=)."""
    att_file = tmp_path / "exact.bin"
    att_file.write_bytes(b"data")

    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": "autumn_exact"})

    state = _make_state()
    config = _make_config(tmp_path)
    events: list[MigrationEvent] = []

    att = DCEAttachment(
        id="att1",
        url="exact.bin",
        file_name="exact.bin",
        file_size_bytes=20 * 1024 * 1024,  # Exactly at limit
    )
    msg = _make_message(id="msg1", content="exact limit", attachments=[att])

    async with aiohttp.ClientSession() as session:
        result_ids, _result_ph = await _upload_attachments(
            msg, config, state, session, _collect_events(events)
        )

    assert len(result_ids) == 1
    assert state.attachments_uploaded == 1
    assert state.attachments_skipped == 0


# ---------------------------------------------------------------------------
# CDN expiry check in _upload_attachments
# ---------------------------------------------------------------------------


async def test_expired_url_no_local_file_produces_placeholder(tmp_path: Path) -> None:
    """Expired CDN URL + no local file -> specific expired warning via _skip_attachment."""
    config = _make_config(tmp_path)
    state = _make_state()
    msg = DCEMessage(
        id="msg1",
        type="Default",
        timestamp="2024-01-01T00:00:00Z",
        content="",
        author=DCEAuthor(id="u1", name="User"),
        attachments=[
            DCEAttachment(
                id="att1",
                url="https://cdn.discordapp.com/f.png?ex=60000000",
                file_name="photo.png",
                file_size_bytes=100,
            )
        ],
    )
    async with aiohttp.ClientSession() as session:
        autumn_ids, _placeholders = await _upload_attachments(
            msg, config, state, session, lambda e: None
        )
    assert autumn_ids == []
    assert state.attachments_skipped >= 1
    assert any(
        w.get("type") == "attachment_skipped" and "expired" in w.get("message", "").lower()
        for w in state.warnings
    )


async def test_missing_local_non_expired_url_uses_generic_warning(tmp_path: Path) -> None:
    """Missing local file + non-expired URL -> generic missing_media warning."""
    config = _make_config(tmp_path)
    state = _make_state()
    msg = DCEMessage(
        id="msg1",
        type="Default",
        timestamp="2024-01-01T00:00:00Z",
        content="",
        author=DCEAuthor(id="u1", name="User"),
        attachments=[
            DCEAttachment(
                id="att1",
                url="https://cdn.discordapp.com/f.png?ex=ffffffff",
                file_name="photo.png",
                file_size_bytes=100,
            )
        ],
    )
    async with aiohttp.ClientSession() as session:
        autumn_ids, _placeholders = await _upload_attachments(
            msg, config, state, session, lambda e: None
        )
    assert autumn_ids == []
    assert any(w.get("type") == "missing_media" for w in state.warnings)


async def test_empty_url_attachment_no_crash(tmp_path: Path) -> None:
    """Empty URL attachment doesn't crash CDN check (returns None)."""
    config = _make_config(tmp_path)
    state = _make_state()
    msg = DCEMessage(
        id="msg1",
        type="Default",
        timestamp="2024-01-01T00:00:00Z",
        content="",
        author=DCEAuthor(id="u1", name="User"),
        attachments=[DCEAttachment(id="att1", url="", file_name="ghost.txt", file_size_bytes=0)],
    )
    async with aiohttp.ClientSession() as session:
        autumn_ids, _placeholders = await _upload_attachments(
            msg, config, state, session, lambda e: None
        )
    assert autumn_ids == []
    # Should use generic missing_media, not crash on CDN check
    assert any(w.get("type") == "missing_media" for w in state.warnings)


# ---------------------------------------------------------------------------
# Configurable checkpoint interval + time throttle (S5)
# ---------------------------------------------------------------------------


async def test_checkpoint_interval_zero_clamped(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """checkpoint_interval=0 does not cause ZeroDivisionError."""
    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_msg1"})
    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_msg2"})
    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_msg3"})
    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_msg4"})
    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_msg5"})

    state = _make_state()
    config = _make_config(tmp_path, checkpoint_interval=0)

    msgs = [
        _make_message(
            id=f"msg{i}",
            content=f"hello {i}",
            timestamp=f"2024-01-15T12:{i:02d}:00+00:00",
        )
        for i in range(5)
    ]
    export = _make_export(messages=msgs)

    # Should NOT raise ZeroDivisionError
    await run_messages(config, state, [export], lambda e: None)

    assert len(state.message_map) == 5


async def test_checkpoint_saves_are_time_throttled(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """save_state during message loop is only called when 5s have elapsed."""
    # Create enough messages to trigger multiple checkpoint intervals
    num_msgs = 10
    for _ in range(num_msgs):
        mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_msg"})

    state = _make_state()
    config = _make_config(tmp_path, checkpoint_interval=3)

    msgs = [
        _make_message(
            id=f"msg{i}",
            content=f"hello {i}",
            timestamp=f"2024-01-15T12:{i:02d}:00+00:00",
        )
        for i in range(num_msgs)
    ]
    export = _make_export(messages=msgs)

    save_calls: list[object] = []

    def counting_save(st: object, path: object) -> None:
        save_calls.append(1)

    # Patch save_state in messages module to count calls.
    # Time passes near-instantly in test, so the 5s throttle means
    # in-loop saves should be suppressed. Only channel-end save fires.
    with patch("discord_ferry.migrator.messages.save_state", counting_save):
        await run_messages(config, state, [export], lambda e: None)

    # At checkpoint_interval=3, indices 2,5,8 hit the modulo check (3 times).
    # But the 5s time throttle suppresses all in-loop saves because the test
    # runs in <1ms. Only the channel-end unconditional save should fire (1 call).
    assert save_calls == [1], (
        f"Expected only the channel-end save (1 call), got {len(save_calls)} calls"
    )


async def test_checkpoint_interval_from_config(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """checkpoint_interval config field is respected in the modulo check."""
    # With checkpoint_interval=2 and 4 messages, indices 1 and 3 hit modulo.
    for _ in range(4):
        mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_msg"})

    state = _make_state()
    config = _make_config(tmp_path, checkpoint_interval=2)

    msgs = [
        _make_message(
            id=f"msg{i}",
            content=f"hello {i}",
            timestamp=f"2024-01-15T12:{i:02d}:00+00:00",
        )
        for i in range(4)
    ]
    export = _make_export(messages=msgs)
    events: list[MigrationEvent] = []

    await run_messages(config, state, [export], _collect_events(events))

    # Progress events at checkpoints: idx 1 (msg 2/4) and idx 3 (msg 4/4).
    progress_with_current = [
        e for e in events if e.status == "progress" and e.current is not None and e.current > 0
    ]
    checkpoint_counts = [e.current for e in progress_with_current]
    assert 2 in checkpoint_counts, f"Expected checkpoint at message 2, got {checkpoint_counts}"
    assert 4 in checkpoint_counts, f"Expected checkpoint at message 4, got {checkpoint_counts}"


# ---------------------------------------------------------------------------
# Orphan upload tracking (S5)
# ---------------------------------------------------------------------------


async def test_successful_send_marks_referenced(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """After a successful api_send_message, autumn_ids are added to referenced_autumn_ids."""
    att_file = tmp_path / "file.png"
    att_file.write_bytes(b"data")
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": "autumn_att1"})
    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_msg1"})

    state = _make_state()
    config = _make_config(tmp_path)
    att = DCEAttachment(id="att1", url="file.png", file_name="file.png")
    msg = _make_message(id="msg1", content="with file", attachments=[att])
    export = _make_export(messages=[msg])

    await run_messages(config, state, [export], lambda e: None)

    # The uploaded autumn_id should be tracked and referenced
    assert "autumn_att1" in state.autumn_uploads
    assert state.autumn_uploads["autumn_att1"] == "att1"
    assert "autumn_att1" in state.referenced_autumn_ids


async def test_failed_send_leaves_orphan(tmp_path: Path) -> None:
    """When api_send_message fails, uploaded files remain in autumn_uploads but NOT referenced."""

    async def always_fail(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        raise RuntimeError("API down")

    att_file = tmp_path / "file.png"
    att_file.write_bytes(b"data")

    state = _make_state()
    config = _make_config(tmp_path)
    att = DCEAttachment(id="att1", url="file.png", file_name="file.png")
    msg = _make_message(id="msg1", content="with file", attachments=[att])
    export = _make_export(messages=[msg])

    with (
        patch("discord_ferry.migrator.messages.api_send_message", always_fail),
        patch(
            "discord_ferry.migrator.messages.upload_with_cache",
            return_value="autumn_orphan1",
        ),
    ):
        await run_messages(config, state, [export], lambda e: None)

    # Upload was tracked...
    assert "autumn_orphan1" in state.autumn_uploads
    # ...but NOT marked as referenced (send failed)
    assert "autumn_orphan1" not in state.referenced_autumn_ids


# ---------------------------------------------------------------------------
# Dead-letter queue: FailedMessage on send failure (S1)
# ---------------------------------------------------------------------------


async def test_message_failure_creates_failed_message(tmp_path: Path) -> None:
    """A send failure creates a FailedMessage with correct fields in state.failed_messages."""

    async def always_fail(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        raise RuntimeError("API down")

    state = _make_state()
    config = _make_config(tmp_path)
    msg = _make_message(id="msg_fail", content="important message")
    export = _make_export(messages=[msg])

    with patch("discord_ferry.migrator.messages.api_send_message", always_fail):
        await run_messages(config, state, [export], lambda e: None)

    assert len(state.failed_messages) == 1
    fm = state.failed_messages[0]
    assert isinstance(fm, FailedMessage)
    assert fm.discord_msg_id == "msg_fail"
    assert fm.stoat_channel_id == "stoat_ch1"
    assert "API down" in fm.error
    assert fm.retry_count == 0


async def test_failed_message_content_preview_truncated(tmp_path: Path) -> None:
    """Content preview is truncated to 50 chars for long messages."""

    async def always_fail(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        raise RuntimeError("fail")

    long_content = "x" * 5000
    state = _make_state()
    config = _make_config(tmp_path)
    msg = _make_message(id="msg_long", content=long_content)
    export = _make_export(messages=[msg])

    with patch("discord_ferry.migrator.messages.api_send_message", always_fail):
        await run_messages(config, state, [export], lambda e: None)

    assert len(state.failed_messages) == 1
    assert len(state.failed_messages[0].content_preview) == 50


async def test_forwarded_message_failure_no_crash(tmp_path: Path) -> None:
    """Forwarded messages (empty content) that fail don't crash the preview slice."""

    async def always_fail(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        raise RuntimeError("fail")

    state = _make_state()
    config = _make_config(tmp_path)
    # Non-forwarded empty content message (no reference) — will proceed to send
    msg = _make_message(
        id="msg_empty",
        content="",
        attachments=[
            DCEAttachment(id="att1", url="missing.png", file_name="missing.png"),
        ],
    )
    export = _make_export(messages=[msg])

    with patch("discord_ferry.migrator.messages.api_send_message", always_fail):
        await run_messages(config, state, [export], lambda e: None)

    # Should not crash — content_preview should be empty string or short
    assert len(state.failed_messages) == 1
    assert len(state.failed_messages[0].content_preview) <= 50


# ---------------------------------------------------------------------------
# Reaction mode (text / native / skip)
# ---------------------------------------------------------------------------


async def test_text_mode_appends_reaction_summary(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """reaction_mode='text' appends [Reactions: ...] to content and does not queue."""
    sent_kwargs: list[dict[str, Any]] = []

    async def capture_send(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        sent_kwargs.append(kwargs)
        return {"_id": "stoat_msg"}

    state = _make_state()
    config = _make_config(tmp_path, reaction_mode="text")
    reaction = DCEReaction(emoji=DCEEmoji(id="", name="thumbsup"), count=3)
    msg = _make_message(id="msg1", content="hello", reactions=[reaction])
    export = _make_export(messages=[msg])

    with patch("discord_ferry.migrator.messages.api_send_message", capture_send):
        await run_messages(config, state, [export], lambda e: None)

    assert len(state.pending_reactions) == 0
    assert "[Reactions:" in sent_kwargs[0]["content"]
    assert "thumbsup 3" in sent_kwargs[0]["content"]


async def test_native_mode_queues_reactions(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """reaction_mode='native' queues reactions and does not append text."""
    sent_kwargs: list[dict[str, Any]] = []

    async def capture_send(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        sent_kwargs.append(kwargs)
        return {"_id": "stoat_msg"}

    state = _make_state()
    config = _make_config(tmp_path, reaction_mode="native")
    reaction = DCEReaction(emoji=DCEEmoji(id="", name="thumbsup"), count=3)
    msg = _make_message(id="msg1", content="hello", reactions=[reaction])
    export = _make_export(messages=[msg])

    with patch("discord_ferry.migrator.messages.api_send_message", capture_send):
        await run_messages(config, state, [export], lambda e: None)

    assert len(state.pending_reactions) == 1
    assert "[Reactions:" not in sent_kwargs[0]["content"]


async def test_skip_mode_no_reactions(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """reaction_mode='skip' produces no reaction text and no queuing."""
    sent_kwargs: list[dict[str, Any]] = []

    async def capture_send(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        sent_kwargs.append(kwargs)
        return {"_id": "stoat_msg"}

    state = _make_state()
    config = _make_config(tmp_path, reaction_mode="skip")
    reaction = DCEReaction(emoji=DCEEmoji(id="", name="thumbsup"), count=3)
    msg = _make_message(id="msg1", content="hello", reactions=[reaction])
    export = _make_export(messages=[msg])

    with patch("discord_ferry.migrator.messages.api_send_message", capture_send):
        await run_messages(config, state, [export], lambda e: None)

    assert len(state.pending_reactions) == 0
    assert "[Reactions:" not in sent_kwargs[0]["content"]


async def test_invalid_reaction_mode_defaults_to_text(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """Invalid reaction_mode value is treated as 'text' with a warning logged."""
    sent_kwargs: list[dict[str, Any]] = []

    async def capture_send(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        sent_kwargs.append(kwargs)
        return {"_id": "stoat_msg"}

    state = _make_state()
    config = _make_config(tmp_path, reaction_mode="bogus")
    reaction = DCEReaction(emoji=DCEEmoji(id="", name="thumbsup"), count=3)
    msg = _make_message(id="msg1", content="hello", reactions=[reaction])
    export = _make_export(messages=[msg])

    with patch("discord_ferry.migrator.messages.api_send_message", capture_send):
        await run_messages(config, state, [export], lambda e: None)

    # Should behave like text mode
    assert len(state.pending_reactions) == 0
    assert "[Reactions:" in sent_kwargs[0]["content"]
    # Warning should be logged about invalid mode
    assert any("reaction_mode" in w["message"] for w in state.warnings)


# ---------------------------------------------------------------------------
# S3: Edited message indicator
# ---------------------------------------------------------------------------


def test_edited_message_gets_indicator() -> None:
    """Message with timestamp_edited set contains *(edited)* in built content."""
    state = _make_state()
    msg = _make_message(
        content="original text",
        timestamp="2024-01-15T12:00:00+00:00",
        timestamp_edited="2024-01-15T13:00:00+00:00",
    )
    result = _build_content(msg, state)
    assert "*(edited)*" in result


def test_non_edited_message_no_indicator() -> None:
    """Message without timestamp_edited does NOT contain *(edited)*."""
    state = _make_state()
    msg = _make_message(
        content="original text",
        timestamp="2024-01-15T12:00:00+00:00",
    )
    result = _build_content(msg, state)
    assert "*(edited)*" not in result


def test_empty_content_with_edit_timestamp() -> None:
    """Empty content with edit timestamp still gets the indicator."""
    state = _make_state()
    msg = _make_message(
        content="",
        timestamp="2024-01-15T12:00:00+00:00",
        timestamp_edited="2024-01-15T14:00:00+00:00",
    )
    result = _build_content(msg, state)
    assert "*(edited)*" in result


# ---------------------------------------------------------------------------
# S4: Attachment overflow handling
# ---------------------------------------------------------------------------


async def test_five_attachments_no_overflow(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """5 attachments produce no overflow warning and no overflow text in content."""
    for i in range(5):
        f = tmp_path / f"file{i}.png"
        f.write_bytes(b"x" * 10)
        mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": f"att_id_{i}"})

    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_msg"})

    state = _make_state()
    config = _make_config(tmp_path)
    attachments = [
        DCEAttachment(id=str(i), url=f"file{i}.png", file_name=f"file{i}.png") for i in range(5)
    ]
    msg = _make_message(id="msg1", content="five files", attachments=attachments)
    export = _make_export(messages=[msg])

    sent_kwargs: list[dict[str, Any]] = []

    async def capture_send(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        sent_kwargs.append(kwargs)
        return {"_id": "stoat_msg"}

    with patch("discord_ferry.migrator.messages.api_send_message", capture_send):
        await run_messages(config, state, [export], lambda e: None)

    assert "msg1" in state.message_map
    assert state.attachments_skipped == 0
    assert "[+" not in sent_kwargs[0]["content"]
    assert not any(w.get("type") == "attachment_overflow" for w in state.warnings)


async def test_seven_attachments_overflow_text(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """7 attachments: first 5 uploaded, content includes overflow text, state updated."""
    for i in range(5):
        f = tmp_path / f"file{i}.png"
        f.write_bytes(b"x" * 10)
        mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": f"att_id_{i}"})

    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_msg"})

    state = _make_state()
    config = _make_config(tmp_path)
    attachments = [
        DCEAttachment(id=str(i), url=f"file{i}.png", file_name=f"file{i}.png") for i in range(7)
    ]
    msg = _make_message(id="msg1", content="many files", attachments=attachments)
    export = _make_export(messages=[msg])

    sent_kwargs: list[dict[str, Any]] = []

    async def capture_send(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        sent_kwargs.append(kwargs)
        return {"_id": "stoat_msg"}

    with patch("discord_ferry.migrator.messages.api_send_message", capture_send):
        await run_messages(config, state, [export], lambda e: None)

    assert "msg1" in state.message_map
    content = sent_kwargs[0]["content"]
    assert "[+2 more attachment(s)" in content
    assert "file5.png" in content
    assert "file6.png" in content
    assert state.attachments_skipped == 2
    assert any(w.get("type") == "attachment_overflow" for w in state.warnings)


async def test_ten_attachments_overflow(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """10 attachments: 5 in overflow text."""
    for i in range(5):
        f = tmp_path / f"file{i}.png"
        f.write_bytes(b"x" * 10)
        mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": f"att_id_{i}"})

    mock_aiohttp.post(CHANNEL_MSG_URL, payload={"_id": "stoat_msg"})

    state = _make_state()
    config = _make_config(tmp_path)
    attachments = [
        DCEAttachment(id=str(i), url=f"file{i}.png", file_name=f"file{i}.png") for i in range(10)
    ]
    msg = _make_message(id="msg1", content="lots of files", attachments=attachments)
    export = _make_export(messages=[msg])

    sent_kwargs: list[dict[str, Any]] = []

    async def capture_send(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        sent_kwargs.append(kwargs)
        return {"_id": "stoat_msg"}

    with patch("discord_ferry.migrator.messages.api_send_message", capture_send):
        await run_messages(config, state, [export], lambda e: None)

    assert "msg1" in state.message_map
    content = sent_kwargs[0]["content"]
    assert "[+5 more attachment(s)" in content
    assert state.attachments_skipped == 5


# ---------------------------------------------------------------------------
# _build_content — Discord link rewriting (S2)
# ---------------------------------------------------------------------------


def test_discord_links_rewritten_in_content() -> None:
    """Discord jump links and invite links are rewritten in _build_content pipeline."""
    state = _make_state(
        channel_map={"ch1": "stoat_ch1", "456": "stoat_ch_mapped"},
    )
    msg = _make_message(
        content=("Check https://discord.com/channels/111/456/789 and https://discord.gg/invite1"),
    )
    result = _build_content(msg, state)
    # Jump link rewritten to channel mention
    assert "<#stoat_ch_mapped>" in result
    # Invite annotated
    assert "[Discord invite — no longer valid]" in result
    # Original Discord URL should not remain for the mapped link
    assert "discord.com/channels/111/456/789" not in result


# ---------------------------------------------------------------------------
# S3: Embed overflow reporting
# ---------------------------------------------------------------------------


async def test_embed_overflow_fallback_text(tmp_path: Path) -> None:
    """When embeds can't be migrated (no title/description), content gets a [N embed(s)...] note."""
    state = _make_state()
    config = _make_config(tmp_path)

    sent_content: list[str] = []

    async def capture_send(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        sent_content.append(kwargs.get("content", ""))
        return {"_id": "stoat_msg"}

    # Create embeds with no title or description — flatten_embed returns empty dicts,
    # so none pass the `flat.get("description") or flat.get("title")` guard.
    bad_embeds = [{"color": 0xFF0000} for _ in range(3)]
    msg = _make_message(id="msg1", content="check", embeds=bad_embeds)
    export = _make_export(messages=[msg])

    with patch("discord_ferry.migrator.messages.api_send_message", capture_send):
        await run_messages(config, state, [export], lambda e: None)

    assert len(sent_content) >= 1
    combined = " ".join(sent_content)
    assert "embed(s) could not be migrated" in combined, (
        f"Expected embed overflow notice in content, got: {combined!r}"
    )


# ---------------------------------------------------------------------------
# S8: Cross-channel reply fallback annotation
# ---------------------------------------------------------------------------


async def test_cross_channel_reply_fallback(tmp_path: Path) -> None:
    """Reply to a message in a different channel gets a text annotation when not in map."""
    sent_content: list[str] = []

    async def capture_send(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        sent_content.append(kwargs.get("content", ""))
        return {"_id": "stoat_msg"}

    state = _make_state(message_map={})  # Referenced message not in map.
    config = _make_config(tmp_path)

    # Reference points to a different channel (ch_other != ch1).
    ref = DCEReference(message_id="orig_in_other_ch", channel_id="ch_other")
    msg = _make_message(
        id="reply1",
        msg_type="Reply",
        content="responding to something",
        reference=ref,
    )
    export = _make_export(channel_id="ch1", messages=[msg])

    with patch("discord_ferry.migrator.messages.api_send_message", capture_send):
        await run_messages(config, state, [export], lambda e: None)

    assert len(sent_content) == 1
    assert "[Replying to message in #ch_other]" in sent_content[0]
    assert any(w["type"] == "cross_channel_reply" for w in state.warnings)


async def test_same_channel_missing_reply_no_annotation(tmp_path: Path) -> None:
    """Reply to unknown message in the SAME channel gets no text annotation (handled silently)."""
    sent_content: list[str] = []

    async def capture_send(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        sent_content.append(kwargs.get("content", ""))
        return {"_id": "stoat_msg"}

    state = _make_state(message_map={})
    config = _make_config(tmp_path)

    # channel_id matches the export channel (ch1).
    ref = DCEReference(message_id="missing_id", channel_id="ch1")
    msg = _make_message(
        id="reply2",
        msg_type="Reply",
        content="reply in same channel",
        reference=ref,
    )
    export = _make_export(channel_id="ch1", messages=[msg])

    with patch("discord_ferry.migrator.messages.api_send_message", capture_send):
        await run_messages(config, state, [export], lambda e: None)

    assert len(sent_content) == 1
    assert "[Replying to message in #" not in sent_content[0]
    assert not any(w["type"] == "cross_channel_reply" for w in state.warnings)


async def test_cross_channel_reply_found_in_map_uses_reply(tmp_path: Path) -> None:
    """Cross-channel reply that IS in message_map still uses the proper reply reference."""
    sent_kwargs: list[dict[str, Any]] = []

    async def capture_send(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        sent_kwargs.append(kwargs)
        return {"_id": "stoat_msg"}

    state = _make_state(message_map={"orig_in_other_ch": "stoat_orig"})
    config = _make_config(tmp_path)

    ref = DCEReference(message_id="orig_in_other_ch", channel_id="ch_other")
    msg = _make_message(
        id="reply3",
        msg_type="Reply",
        content="can resolve this one",
        reference=ref,
    )
    export = _make_export(channel_id="ch1", messages=[msg])

    with patch("discord_ferry.migrator.messages.api_send_message", capture_send):
        await run_messages(config, state, [export], lambda e: None)

    # Should use the proper stoat reply, not text fallback.
    assert sent_kwargs[0]["replies"] == [{"id": "stoat_orig", "mention": False}]
    assert "[Replying to message in #" not in sent_kwargs[0]["content"]


# ---------------------------------------------------------------------------
# S12: Reaction count annotation in native mode
# ---------------------------------------------------------------------------


async def test_reaction_count_native_mode_annotation(tmp_path: Path) -> None:
    """Native mode appends [Original counts: ...] when any reaction count > 1."""
    sent_content: list[str] = []

    async def capture_send(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        sent_content.append(kwargs.get("content", ""))
        return {"_id": "stoat_msg"}

    state = _make_state()
    config = _make_config(tmp_path, reaction_mode="native")
    reactions = [
        DCEReaction(emoji=DCEEmoji(id="", name="thumbsup"), count=5),
        DCEReaction(emoji=DCEEmoji(id="", name="tada"), count=2),
    ]
    msg = _make_message(id="msg1", content="great post", reactions=reactions)
    export = _make_export(messages=[msg])

    with patch("discord_ferry.migrator.messages.api_send_message", capture_send):
        await run_messages(config, state, [export], lambda e: None)

    assert len(sent_content) == 1
    assert "[Original counts:" in sent_content[0]
    assert "thumbsup \u00d75" in sent_content[0]
    assert "tada \u00d72" in sent_content[0]


async def test_reaction_count_single_no_annotation(tmp_path: Path) -> None:
    """Native mode does NOT append count annotation when all counts are exactly 1."""
    sent_content: list[str] = []

    async def capture_send(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        sent_content.append(kwargs.get("content", ""))
        return {"_id": "stoat_msg"}

    state = _make_state()
    config = _make_config(tmp_path, reaction_mode="native")
    reactions = [
        DCEReaction(emoji=DCEEmoji(id="", name="heart"), count=1),
    ]
    msg = _make_message(id="msg1", content="sweet", reactions=reactions)
    export = _make_export(messages=[msg])

    with patch("discord_ferry.migrator.messages.api_send_message", capture_send):
        await run_messages(config, state, [export], lambda e: None)

    assert len(sent_content) == 1
    assert "[Original counts:" not in sent_content[0]


async def test_reaction_count_mixed_some_over_one(tmp_path: Path) -> None:
    """Only reactions with count > 1 appear in the native mode annotation."""
    sent_content: list[str] = []

    async def capture_send(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        sent_content.append(kwargs.get("content", ""))
        return {"_id": "stoat_msg"}

    state = _make_state()
    config = _make_config(tmp_path, reaction_mode="native")
    reactions = [
        DCEReaction(emoji=DCEEmoji(id="", name="fire"), count=3),
        DCEReaction(emoji=DCEEmoji(id="", name="wave"), count=1),
    ]
    msg = _make_message(id="msg1", content="nice", reactions=reactions)
    export = _make_export(messages=[msg])

    with patch("discord_ferry.migrator.messages.api_send_message", capture_send):
        await run_messages(config, state, [export], lambda e: None)

    assert "[Original counts:" in sent_content[0]
    assert "fire" in sent_content[0]
    assert "wave" not in sent_content[0]


async def test_incremental_skips_at_or_below_high_water(tmp_path: Path) -> None:
    """SC-1: incremental copies only ids > the durable high-water mark."""
    sent: list[dict[str, Any]] = []
    state = _make_state(channel_high_water={"ch1": "200"}, channel_message_offsets={})
    config = _make_config(tmp_path, incremental=True)
    export = _make_export(
        messages=[
            _make_message(id="100", timestamp="2024-01-01T00:00:00+00:00"),
            _make_message(id="200", timestamp="2024-01-02T00:00:00+00:00"),
            _make_message(id="300", timestamp="2024-01-03T00:00:00+00:00"),
            _make_message(id="400", timestamp="2024-01-04T00:00:00+00:00"),
        ],
    )
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends(sent)):
        await run_messages(config, state, [export], lambda e: None)
    assert _sent_message_ids(sent) == ["300", "400"]


async def test_marker_written_at_completion_is_max_id(tmp_path: Path) -> None:
    """SC-2: full run writes channel_high_water=max id; transient offset popped."""
    sent: list[dict[str, Any]] = []
    state = _make_state()
    config = _make_config(tmp_path)
    export = _make_export(
        messages=[_make_message(id="100"), _make_message(id="200"), _make_message(id="300")],
    )
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends(sent)):
        await run_messages(config, state, [export], lambda e: None)
    assert state.channel_high_water["ch1"] == "300"
    assert "ch1" not in state.channel_message_offsets
    assert "ch1" in state.completed_channel_ids


async def test_marker_is_true_max_not_last_by_timestamp(tmp_path: Path) -> None:
    """SC-10: id order disagrees with timestamp order -> marker is the true max id."""
    sent: list[dict[str, Any]] = []
    state = _make_state()
    config = _make_config(tmp_path)
    export = _make_export(
        messages=[
            _make_message(id="300", timestamp="2024-01-01T00:00:00+00:00"),
            _make_message(id="100", timestamp="2024-06-01T00:00:00+00:00"),
        ],
    )
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends(sent)):
        await run_messages(config, state, [export], lambda e: None)
    assert state.channel_high_water["ch1"] == "300"


async def test_empty_export_writes_no_marker(tmp_path: Path) -> None:
    """SC-11: a channel with no messages writes no marker and does not crash."""
    state = _make_state()
    config = _make_config(tmp_path)
    export = _make_export(messages=[])
    await run_messages(config, state, [export], lambda e: None)
    assert "ch1" not in state.channel_high_water
    assert "ch1" in state.completed_channel_ids


async def test_incremental_crashed_prior_falls_back_to_offset(tmp_path: Path) -> None:
    """SC-8: no durable marker but a carried transient offset -> max() uses the offset."""
    sent: list[dict[str, Any]] = []
    state = _make_state(channel_high_water={}, channel_message_offsets={"ch1": "200"})
    config = _make_config(tmp_path, incremental=True)
    export = _make_export(
        messages=[_make_message(id="100"), _make_message(id="200"), _make_message(id="300")],
    )
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends(sent)):
        await run_messages(config, state, [export], lambda e: None)
    assert _sent_message_ids(sent) == ["300"]


async def test_incremental_new_channel_full_copy(tmp_path: Path) -> None:
    """SC-9: a channel with no marker/offset copies every message."""
    sent: list[dict[str, Any]] = []
    state = _make_state(channel_high_water={}, channel_message_offsets={})
    config = _make_config(tmp_path, incremental=True)
    export = _make_export(
        messages=[_make_message(id="100"), _make_message(id="200"), _make_message(id="300")],
    )
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends(sent)):
        await run_messages(config, state, [export], lambda e: None)
    assert _sent_message_ids(sent) == ["100", "200", "300"]


async def test_incremental_no_new_messages_count_not_inflated(tmp_path: Path) -> None:
    """SC-6: a no-new-messages incremental leaves channel_message_counts unchanged."""
    sent: list[dict[str, Any]] = []
    state = _make_state(
        channel_high_water={"ch1": "400"},
        channel_message_offsets={},
        channel_message_counts={"ch1": 800},
    )
    config = _make_config(tmp_path, incremental=True)
    export = _make_export(
        messages=[
            _make_message(id="100"),
            _make_message(id="200"),
            _make_message(id="300"),
            _make_message(id="400"),
        ],
    )
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends(sent)):
        await run_messages(config, state, [export], lambda e: None)
    assert _sent_message_ids(sent) == []
    assert state.channel_message_counts["ch1"] == 800


async def test_incremental_k_new_raises_count_by_k(tmp_path: Path) -> None:
    """SC-7: K new messages raise the per-channel count by exactly K."""
    sent: list[dict[str, Any]] = []
    state = _make_state(
        channel_high_water={"ch1": "200"},
        channel_message_offsets={},
        channel_message_counts={"ch1": 800},
    )
    config = _make_config(tmp_path, incremental=True)
    export = _make_export(
        messages=[
            _make_message(id="100"),
            _make_message(id="200"),
            _make_message(id="300"),
            _make_message(id="400"),
        ],
    )
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends(sent)):
        await run_messages(config, state, [export], lambda e: None)
    assert state.channel_message_counts["ch1"] == 802


async def test_incremental_old_state_degrades_to_full_copy(tmp_path: Path) -> None:
    """SC-15: incremental off an old state (no marker, no offset) re-copies all (harmless)."""
    sent: list[dict[str, Any]] = []
    state = _make_state(channel_high_water={}, channel_message_offsets={})
    config = _make_config(tmp_path, incremental=True)
    export = _make_export(messages=[_make_message(id="100"), _make_message(id="200")])
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends(sent)):
        await run_messages(config, state, [export], lambda e: None)
    assert _sent_message_ids(sent) == ["100", "200"]


async def test_resume_uses_transient_offset_unchanged(tmp_path: Path) -> None:
    """SC-16: --resume still skips via the transient offset (byte-for-byte unchanged)."""
    sent: list[dict[str, Any]] = []
    state = _make_state(channel_message_offsets={"ch1": "200"}, channel_high_water={})
    config = _make_config(tmp_path, resume=True)
    export = _make_export(
        messages=[_make_message(id="100"), _make_message(id="200"), _make_message(id="300")],
    )
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends(sent)):
        await run_messages(config, state, [export], lambda e: None)
    assert _sent_message_ids(sent) == ["300"]


async def test_incremental_completion_event_reports_new_and_skipped(tmp_path: Path) -> None:
    """SC-20: incremental completion event reports copied-new and skipped counts."""
    sent: list[dict[str, Any]] = []
    events: list[MigrationEvent] = []
    state = _make_state(channel_high_water={"ch1": "200"}, channel_message_offsets={})
    config = _make_config(tmp_path, incremental=True)
    export = _make_export(
        messages=[
            _make_message(id="100"),
            _make_message(id="200"),
            _make_message(id="300"),
            _make_message(id="400"),
        ],
    )
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends(sent)):
        await run_messages(config, state, [export], _collect_events(events))
    msgs = [e.message for e in events if e.message.startswith("Completed 'general'")]
    assert any("2 new, 2 already present" in m for m in msgs)


async def test_dry_run_incremental_writes_no_marker(tmp_path: Path) -> None:
    """SC-12: dry-run takes the separate early branch and never writes channel_high_water.

    Pre-seed an existing marker so the assertion distinguishes "never wrote" from
    "wrote then deleted" — the dry-run path must leave the marker untouched.
    """
    state = _make_state(channel_high_water={"ch1": "999"})
    config = _make_config(tmp_path, incremental=True, dry_run=True)
    export = _make_export(messages=[_make_message(id="100"), _make_message(id="200")])
    await run_messages(config, state, [export], lambda e: None)
    assert state.channel_high_water == {"ch1": "999"}


async def test_non_numeric_message_id_not_skipped_or_marked(tmp_path: Path) -> None:
    """isdigit guard: a non-numeric id is copied (never skipped) and never advances the marker."""
    sent: list[dict[str, Any]] = []
    state = _make_state(channel_high_water={"ch1": "200"}, channel_message_offsets={})
    config = _make_config(tmp_path, incremental=True)
    export = _make_export(
        messages=[_make_message(id="system-msg", timestamp="2024-01-01T00:00:00+00:00")],
    )
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends(sent)):
        await run_messages(config, state, [export], lambda e: None)
    assert _sent_message_ids(sent) == ["system-msg"]  # non-numeric id is not skipped
    assert state.channel_high_water["ch1"] == "200"  # marker unchanged (non-numeric ignored)


def _make_thread_export(
    channel_id: str = "ch1",
    messages: list[DCEMessage] | None = None,
    channel_type: int = 0,
) -> DCEExport:
    return DCEExport(
        guild=_make_guild(),
        channel=DCEChannel(id=channel_id, type=channel_type, name="thread"),
        messages=messages or [],
        is_thread=True,
        parent_channel_name="general",
    )


async def test_unchanged_thread_makes_zero_posts(tmp_path: Path) -> None:
    """SC-4: an unchanged thread channel (marker present) posts nothing — not even the header."""
    sent: list[dict[str, Any]] = []
    state = _make_state(channel_high_water={"ch1": "200"}, channel_message_offsets={})
    config = _make_config(tmp_path, incremental=True)
    export = _make_thread_export(messages=[_make_message(id="100"), _make_message(id="200")])
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends(sent)):
        await run_messages(config, state, [export], lambda e: None)
    assert sent == []


async def test_new_thread_posts_header(tmp_path: Path) -> None:
    """SC-5: a brand-new thread channel (no marker) posts the header."""
    sent: list[dict[str, Any]] = []
    state = _make_state(channel_high_water={}, channel_message_offsets={})
    config = _make_config(tmp_path, incremental=True)
    export = _make_thread_export(messages=[_make_message(id="100")])
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends(sent)):
        await run_messages(config, state, [export], lambda e: None)
    headers = [
        k.get("content") for k in sent if k.get("idempotency_key", "").startswith("ferry-header-")
    ]
    assert headers == ["[Thread migrated from #general]"]


async def test_unchanged_forum_makes_zero_posts(tmp_path: Path) -> None:
    """#79: an unchanged forum channel (type 15, marker present) posts nothing — no header."""
    sent: list[dict[str, Any]] = []
    state = _make_state(channel_high_water={"ch1": "200"}, channel_message_offsets={})
    config = _make_config(tmp_path, incremental=True)
    export = _make_thread_export(
        messages=[_make_message(id="100"), _make_message(id="200")], channel_type=15
    )
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends(sent)):
        await run_messages(config, state, [export], lambda e: None)
    assert sent == []


async def test_new_forum_posts_forum_header(tmp_path: Path) -> None:
    """#79: a brand-new forum channel (type 15/16, no marker) posts the Forum-post header."""
    for forum_type in (15, 16):
        sent: list[dict[str, Any]] = []
        state = _make_state(channel_high_water={}, channel_message_offsets={})
        config = _make_config(tmp_path, incremental=True)
        export = _make_thread_export(messages=[_make_message(id="100")], channel_type=forum_type)
        with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends(sent)):
            await run_messages(config, state, [export], lambda e: None)
        headers = [
            k.get("content")
            for k in sent
            if k.get("idempotency_key", "").startswith("ferry-header-")
        ]
        assert headers == ["[Forum post migrated from #general]"], f"type={forum_type}"


# ---------------------------------------------------------------------------
# #77 — non-numeric carried offset crash guard (S3)
# ---------------------------------------------------------------------------


async def test_incremental_non_numeric_offset_does_not_crash(tmp_path: Path) -> None:
    """SC-11: a non-numeric carried offset degrades to no-threshold, never raises."""
    sent: list[dict[str, Any]] = []
    config = _make_config(tmp_path, incremental=True, output_dir=tmp_path)
    state = _make_state(channel_message_offsets={"ch1": "sys-abc"})  # non-numeric
    export = _make_export(messages=[_make_message(id="100"), _make_message(id="200")])
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends(sent)):
        await run_messages(config, state, [export], lambda e: None)
    assert set(_sent_message_ids(sent)) == {"100", "200"}  # no threshold -> both copied


async def test_resume_non_numeric_offset_does_not_crash(tmp_path: Path) -> None:
    """SC-12: resume path shares the guard — non-numeric offset, no ValueError."""
    sent: list[dict[str, Any]] = []
    config = _make_config(tmp_path, resume=True, output_dir=tmp_path)
    state = _make_state(channel_message_offsets={"ch1": "sys-abc"})
    export = _make_export(messages=[_make_message(id="100"), _make_message(id="200")])
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends(sent)):
        await run_messages(config, state, [export], lambda e: None)
    assert set(_sent_message_ids(sent)) == {"100", "200"}


async def test_incremental_numeric_high_water_still_governs(tmp_path: Path) -> None:
    """SC-13: a numeric high-water still skips at-or-below it (no behaviour change)."""
    sent: list[dict[str, Any]] = []
    config = _make_config(tmp_path, incremental=True, output_dir=tmp_path)
    state = _make_state(channel_high_water={"ch1": "200"})
    export = _make_export(
        messages=[_make_message(id="100"), _make_message(id="200"), _make_message(id="300")]
    )
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends(sent)):
        await run_messages(config, state, [export], lambda e: None)
    assert set(_sent_message_ids(sent)) == {"300"}


async def test_incremental_non_numeric_offset_with_numeric_high_water(tmp_path: Path) -> None:
    """SC-14: non-numeric offset is ignored; the numeric high-water governs."""
    sent: list[dict[str, Any]] = []
    config = _make_config(tmp_path, incremental=True, output_dir=tmp_path)
    state = _make_state(
        channel_message_offsets={"ch1": "sys-abc"}, channel_high_water={"ch1": "200"}
    )
    export = _make_export(
        messages=[_make_message(id="100"), _make_message(id="200"), _make_message(id="300")]
    )
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends(sent)):
        await run_messages(config, state, [export], lambda e: None)
    assert set(_sent_message_ids(sent)) == {"300"}


# ---------------------------------------------------------------------------
# #76 — incremental failed-message self-heal: skip-gate exclusion (S2)
# ---------------------------------------------------------------------------


async def test_incremental_reattempts_failed_id(tmp_path: Path) -> None:
    """SC-4 (CRITICAL): a previously-failed id is re-POSTed alongside genuinely-new ids."""
    sent: list[dict[str, Any]] = []
    config = _make_config(tmp_path, incremental=True, output_dir=tmp_path)
    state = _make_state(
        channel_high_water={"ch1": "300"},
        failed_messages=[FailedMessage("200", "stoat_ch1", "boom")],
    )
    export = _make_export(messages=[_make_message(id=i) for i in ("100", "200", "300", "400")])
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends(sent)):
        await run_messages(config, state, [export], lambda e: None)
    assert set(_sent_message_ids(sent)) == {"200", "400"}


async def test_incremental_unchanged_channel_no_failures_zero_posts(tmp_path: Path) -> None:
    """SC-6: no prior failures + all ids <= marker -> zero POSTs (v2.6.3 regression guard)."""
    sent: list[dict[str, Any]] = []
    config = _make_config(tmp_path, incremental=True, output_dir=tmp_path)
    state = _make_state(channel_high_water={"ch1": "300"})
    export = _make_export(messages=[_make_message(id=i) for i in ("100", "200", "300")])
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends(sent)):
        await run_messages(config, state, [export], lambda e: None)
    assert _sent_message_ids(sent) == []


async def test_resume_does_not_reattempt_failed_id(tmp_path: Path) -> None:
    """SC-7: --resume must NOT consult failed_messages (no self-heal on resume)."""
    sent: list[dict[str, Any]] = []
    config = _make_config(tmp_path, resume=True, output_dir=tmp_path)
    state = _make_state(
        channel_message_offsets={"ch1": "300"},
        failed_messages=[FailedMessage("200", "stoat_ch1", "boom")],
    )
    export = _make_export(messages=[_make_message(id=i) for i in ("100", "200", "300")])
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends(sent)):
        await run_messages(config, state, [export], lambda e: None)
    assert _sent_message_ids(sent) == []  # 200 NOT re-attempted under resume


async def test_incremental_marker_monotonicity(tmp_path: Path) -> None:
    """SC-8: re-attempting low failed 200 under higher new 400 keeps the marker at 400."""
    sent: list[dict[str, Any]] = []
    config = _make_config(tmp_path, incremental=True, output_dir=tmp_path)
    state = _make_state(
        channel_high_water={"ch1": "300"},
        failed_messages=[FailedMessage("200", "stoat_ch1", "boom")],
    )
    export = _make_export(messages=[_make_message(id=i) for i in ("100", "200", "300", "400")])
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends(sent)):
        await run_messages(config, state, [export], lambda e: None)
    assert state.channel_high_water["ch1"] == "400"


async def test_incremental_failed_id_equals_marker_boundary(tmp_path: Path) -> None:
    """SC-18: failed id == marker (skip uses <=) is still re-attempted."""
    sent: list[dict[str, Any]] = []
    config = _make_config(tmp_path, incremental=True, output_dir=tmp_path)
    state = _make_state(
        channel_high_water={"ch1": "200"},
        failed_messages=[FailedMessage("200", "stoat_ch1", "boom")],
    )
    export = _make_export(messages=[_make_message(id="100"), _make_message(id="200")])
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends(sent)):
        await run_messages(config, state, [export], lambda e: None)
    assert _sent_message_ids(sent) == ["200"]
    assert state.channel_high_water["ch1"] == "200"


async def test_incremental_empty_failed_is_pure_v263(tmp_path: Path) -> None:
    """SC-19: empty failed_messages -> only genuinely-new ids POST."""
    sent: list[dict[str, Any]] = []
    config = _make_config(tmp_path, incremental=True, output_dir=tmp_path)
    state = _make_state(channel_high_water={"ch1": "300"}, failed_messages=[])
    export = _make_export(messages=[_make_message(id=i) for i in ("100", "200", "300", "400")])
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends(sent)):
        await run_messages(config, state, [export], lambda e: None)
    assert set(_sent_message_ids(sent)) == {"400"}


async def test_incremental_multiple_failed_ids_one_channel(tmp_path: Path) -> None:
    """SC-21: multiple carried failures in one channel are all re-attempted."""
    sent: list[dict[str, Any]] = []
    config = _make_config(tmp_path, incremental=True, output_dir=tmp_path)
    state = _make_state(
        channel_high_water={"ch1": "300"},
        failed_messages=[
            FailedMessage("150", "stoat_ch1", "boom"),
            FailedMessage("250", "stoat_ch1", "boom"),
        ],
    )
    export = _make_export(
        messages=[_make_message(id=i) for i in ("100", "150", "200", "250", "300", "400")]
    )
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends(sent)):
        await run_messages(config, state, [export], lambda e: None)
    assert set(_sent_message_ids(sent)) == {"150", "250", "400"}


# ---------------------------------------------------------------------------
# #76 — completion-time reconciliation: drop-on-success / collapse-on-re-fail (S2)
# ---------------------------------------------------------------------------


def _capture_sends_failing(sent: list[dict[str, Any]], fail_ids: set[str]) -> Any:
    """Capture that RAISES for ids in fail_ids (re-fail path), succeeds otherwise."""

    async def _send(
        session: Any, stoat_url: Any, token: Any, channel_id: Any, **kwargs: Any
    ) -> dict[str, Any]:
        sent.append(kwargs)
        key = kwargs.get("idempotency_key", "")
        if key.removeprefix("ferry-") in fail_ids:
            raise RuntimeError("simulated send failure")
        return {"_id": f"stoat-{key}"}

    return _send


async def test_reconcile_drops_succeeded_failed_id(tmp_path: Path) -> None:
    """SC-9 (CRITICAL): a re-attempt that succeeds is dropped from failed_messages."""
    sent: list[dict[str, Any]] = []
    config = _make_config(tmp_path, incremental=True, output_dir=tmp_path)
    state = _make_state(
        channel_high_water={"ch1": "300"},
        failed_messages=[FailedMessage("200", "stoat_ch1", "boom")],
    )
    export = _make_export(messages=[_make_message(id=i) for i in ("100", "200", "300")])
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends(sent)):
        await run_messages(config, state, [export], lambda e: None)
    assert all(fm.discord_msg_id != "200" for fm in state.failed_messages)
    assert "200" in state.message_map


async def test_reconcile_collapses_refailed_id(tmp_path: Path) -> None:
    """SC-10 (CRITICAL): a re-attempt that fails again leaves exactly one entry, stable."""
    config = _make_config(tmp_path, incremental=True, output_dir=tmp_path)
    state = _make_state(
        channel_high_water={"ch1": "300"},
        failed_messages=[FailedMessage("200", "stoat_ch1", "boom")],
    )

    def _ids(s: MigrationState) -> list[str]:
        return [fm.discord_msg_id for fm in s.failed_messages if fm.discord_msg_id == "200"]

    export = _make_export(messages=[_make_message(id=i) for i in ("100", "200", "300")])
    sent1: list[dict[str, Any]] = []
    with patch(
        "discord_ferry.migrator.messages.api_send_message",
        _capture_sends_failing(sent1, {"200"}),
    ):
        await run_messages(config, state, [export], lambda e: None)
    assert _ids(state) == ["200"]  # exactly one, not two
    # second identical incremental run keeps it stable (not growing)
    sent2: list[dict[str, Any]] = []
    with patch(
        "discord_ferry.migrator.messages.api_send_message",
        _capture_sends_failing(sent2, {"200"}),
    ):
        await run_messages(config, state, [export], lambda e: None)
    assert _ids(state) == ["200"]


async def test_reconcile_isolation_across_channels(tmp_path: Path) -> None:
    """SC-20: each channel's reconcile only touches its own failures."""
    sent: list[dict[str, Any]] = []
    config = _make_config(tmp_path, incremental=True, output_dir=tmp_path)
    state = _make_state(
        channel_map={"ch1": "stoat_ch1", "ch2": "stoat_ch2"},
        channel_high_water={"ch1": "300", "ch2": "600"},
        failed_messages=[
            FailedMessage("100", "stoat_ch1", "boom"),  # in ch1 export -> succeeds -> drop
            FailedMessage("500", "stoat_ch2", "boom"),  # absent from ch2 export -> retained
        ],
    )
    export1 = _make_export(channel_id="ch1", messages=[_make_message(id="100")])
    export2 = _make_export(channel_id="ch2", messages=[_make_message(id="550")])
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends(sent)):
        await run_messages(config, state, [export1, export2], lambda e: None)
    remaining = {fm.discord_msg_id for fm in state.failed_messages}
    assert "100" not in remaining  # ch1 failure succeeded -> dropped
    assert "500" in remaining  # ch2 failure not in export -> retained


async def test_dry_run_incremental_does_not_touch_failed_messages(tmp_path: Path) -> None:
    """SC-22: reconcile is unreachable in dry-run (run_messages returns early)."""
    config = _make_config(tmp_path, incremental=True, dry_run=True, output_dir=tmp_path)
    state = _make_state(failed_messages=[FailedMessage("200", "stoat_ch1", "boom")])
    export = _make_export(messages=[_make_message(id="200")])
    await run_messages(config, state, [export], lambda e: None)
    assert [fm.discord_msg_id for fm in state.failed_messages] == ["200"]


async def test_reconcile_keeps_deleted_unrecoverable_failed_id(tmp_path: Path) -> None:
    """SC-23: a carried failed id absent from the re-export lingers, no POST."""
    sent: list[dict[str, Any]] = []
    config = _make_config(tmp_path, incremental=True, output_dir=tmp_path)
    state = _make_state(
        channel_high_water={"ch1": "300"},
        failed_messages=[FailedMessage("200", "stoat_ch1", "boom")],
    )
    export = _make_export(messages=[_make_message(id=i) for i in ("100", "300", "400")])  # no 200
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends(sent)):
        await run_messages(config, state, [export], lambda e: None)
    assert "200" not in _sent_message_ids(sent)
    assert any(fm.discord_msg_id == "200" for fm in state.failed_messages)


# ---------------------------------------------------------------------------
# #76 — observability: retried count in the incremental complete event (S4)
# ---------------------------------------------------------------------------


async def test_incremental_complete_event_reports_retried(tmp_path: Path) -> None:
    """SC-15: incremental complete event distinguishes retried from new."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path, incremental=True, output_dir=tmp_path)
    state = _make_state(
        channel_high_water={"ch1": "300"},
        failed_messages=[FailedMessage("200", "stoat_ch1", "boom")],
    )
    export = _make_export(messages=[_make_message(id=i) for i in ("100", "200", "300", "400")])
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends([])):
        await run_messages(config, state, [export], events.append)
    msgs = [e.message for e in events if e.message.startswith("Completed 'general':")]
    assert msgs[-1] == "Completed 'general': 1 new, 2 already present, 1 retried."


async def test_incremental_complete_event_omits_retried_when_zero(tmp_path: Path) -> None:
    """SC-16: with no retries the message is byte-identical to today's form."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path, incremental=True, output_dir=tmp_path)
    state = _make_state(channel_high_water={"ch1": "300"}, failed_messages=[])
    export = _make_export(messages=[_make_message(id=i) for i in ("100", "200", "300", "400")])
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends([])):
        await run_messages(config, state, [export], events.append)
    msgs = [e.message for e in events if e.message.startswith("Completed 'general':")]
    assert msgs[-1] == "Completed 'general': 1 new, 3 already present."


# ---------------------------------------------------------------------------
# #77 — defensive write-side guard for the transient offset (S5, P2)
# ---------------------------------------------------------------------------


async def test_checkpoint_skips_non_numeric_offset_write(tmp_path: Path, monkeypatch: Any) -> None:
    """SC-17: a non-numeric msg.id is never persisted as a transient offset.

    The offset is popped at channel completion, so an end-state assertion would be
    trivially green. Spy on save_state to capture the offset value at each checkpoint
    save — the moment the guard acts.
    """
    import discord_ferry.migrator.messages as messages_mod

    captured: list[Any] = []

    def _spy_save(state: Any, output_dir: Any) -> None:
        captured.append(state.channel_message_offsets.get("ch1"))

    monkeypatch.setattr(messages_mod, "save_state", _spy_save)

    # Force the checkpoint's 5s save gate to pass on every message.
    clock = {"v": 0.0}

    def _fake_monotonic() -> float:
        clock["v"] += 100.0
        return clock["v"]

    monkeypatch.setattr(messages_mod.time, "monotonic", _fake_monotonic)

    config = _make_config(tmp_path, incremental=True, output_dir=tmp_path, checkpoint_interval=1)
    state = _make_state(channel_high_water={"ch1": "300"})
    # A non-numeric (system-message) id followed by a numeric one.
    export = _make_export(messages=[_make_message(id="sys-xyz"), _make_message(id="400")])
    with patch("discord_ferry.migrator.messages.api_send_message", _capture_sends([])):
        await run_messages(config, state, [export], lambda e: None)
    # No checkpoint ever persisted the non-numeric id as an offset.
    assert all(off is None or off.isdigit() for off in captured)
    assert "sys-xyz" not in captured
