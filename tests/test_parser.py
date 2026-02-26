"""Tests for DCE JSON parser."""

import json
from pathlib import Path

from discord_ferry.parser.dce_parser import (
    _infer_thread_info,
    parse_export_directory,
    parse_single_export,
    validate_export,
)
from discord_ferry.parser.models import DCEExport, DCEMessage, DCEReaction

# ---------------------------------------------------------------------------
# Parsing — parse_single_export
# ---------------------------------------------------------------------------


def test_parse_single_export_basic(fixtures_dir: Path) -> None:
    """Parse simple_channel.json and verify top-level guild/channel/message count."""
    export = parse_single_export(fixtures_dir / "simple_channel.json")
    assert isinstance(export, DCEExport)
    assert export.guild.id == "111111111111111111"
    assert export.guild.name == "Test Server"
    assert export.channel.id == "222222222222222222"
    assert export.channel.name == "general"
    assert export.message_count == 5
    assert len(export.messages) == 5
    assert export.exported_at == "2024-06-15T10:30:00+00:00"


def test_parse_single_export_messages_sorted(fixtures_dir: Path) -> None:
    """Messages must be sorted by timestamp ascending."""
    export = parse_single_export(fixtures_dir / "simple_channel.json")
    timestamps = [m.timestamp for m in export.messages]
    assert timestamps == sorted(timestamps)


def test_parse_message_fields(fixtures_dir: Path) -> None:
    """Verify all scalar fields on the first message."""
    export = parse_single_export(fixtures_dir / "simple_channel.json")
    msg = export.messages[0]
    assert isinstance(msg, DCEMessage)
    assert msg.id == "900000000000000001"
    assert msg.type == "Default"
    assert msg.timestamp == "2024-01-15T12:00:00+00:00"
    assert msg.content == "Hello everyone!"
    assert msg.is_pinned is False
    assert msg.timestamp_edited is None
    assert msg.attachments == []
    assert msg.embeds == []
    assert msg.stickers == []
    assert msg.reactions == []
    assert msg.mentions == []
    assert msg.reference is None


def test_parse_reply_message(fixtures_dir: Path) -> None:
    """Reply message has a populated reference object."""
    export = parse_single_export(fixtures_dir / "simple_channel.json")
    # Second message is a Reply
    msg = export.messages[1]
    assert msg.type == "Reply"
    assert msg.reference is not None
    assert msg.reference.message_id == "900000000000000001"
    assert msg.reference.channel_id == "222222222222222222"
    assert msg.reference.guild_id == "111111111111111111"


def test_parse_pinned_message(fixtures_dir: Path) -> None:
    """isPinned=true in JSON maps to is_pinned=True on DCEMessage."""
    export = parse_single_export(fixtures_dir / "simple_channel.json")
    # Third message is pinned
    pinned = export.messages[2]
    assert pinned.id == "900000000000000003"
    assert pinned.is_pinned is True


def test_parse_attachment(fixtures_dir: Path) -> None:
    """Attachment fields are mapped correctly (camelCase → snake_case)."""
    export = parse_single_export(fixtures_dir / "simple_channel.json")
    # Fourth message has an attachment
    msg = export.messages[3]
    assert len(msg.attachments) == 1
    att = msg.attachments[0]
    assert att.id == "600000000000000001"
    assert att.url == "media/attachments/document.pdf"
    assert att.file_name == "document.pdf"
    assert att.file_size_bytes == 1048576


def test_parse_author_with_roles(fixtures_dir: Path) -> None:
    """Author with multiple roles is parsed into DCERole list."""
    export = parse_single_export(fixtures_dir / "simple_channel.json")
    # Second message author (bob) has two roles
    author = export.messages[1].author
    assert author.id == "400000000000000002"
    assert author.name == "bob"
    assert author.nickname == "Bob"
    assert author.is_bot is False
    assert len(author.roles) == 2
    role = author.roles[0]
    assert role.id == "500000000000000001"
    assert role.name == "Member"
    assert role.color == "#3498DB"
    assert role.position == 1


def test_parse_reactions(fixtures_dir: Path) -> None:
    """Reaction list is parsed into DCEReaction with DCEEmoji."""
    export = parse_single_export(fixtures_dir / "simple_channel.json")
    # Second message has a wave reaction
    msg = export.messages[1]
    assert len(msg.reactions) == 1
    reaction = msg.reactions[0]
    assert isinstance(reaction, DCEReaction)
    assert reaction.count == 3
    assert reaction.emoji.name == "\U0001f44b"
    assert reaction.emoji.id == ""
    assert reaction.emoji.is_animated is False


def test_parse_edited_timestamp(fixtures_dir: Path) -> None:
    """timestampEdited in JSON maps to timestamp_edited on DCEMessage."""
    export = parse_single_export(fixtures_dir / "simple_channel.json")
    # Second message was edited
    msg = export.messages[1]
    assert msg.timestamp_edited == "2024-01-15T12:06:00+00:00"


def test_parse_embed_passthrough(fixtures_dir: Path) -> None:
    """Embeds remain as raw dicts; transforms handle them later."""
    export = parse_single_export(fixtures_dir / "simple_channel.json")
    # Fifth message has an embed
    msg = export.messages[4]
    assert len(msg.embeds) == 1
    embed = msg.embeds[0]
    assert isinstance(embed, dict)
    assert embed["title"] == "Cool Website"
    assert embed["url"] == "https://example.com"


def test_parse_null_content(fixtures_dir: Path) -> None:
    """Message with null or empty content yields empty string, not None."""
    export = parse_single_export(fixtures_dir / "edge_cases.json")
    # First message (GuildMemberJoin) has empty content
    msg = export.messages[0]
    assert msg.content == ""
    assert isinstance(msg.content, str)


def test_parse_bot_author(fixtures_dir: Path) -> None:
    """isBot=true in JSON maps to is_bot=True on DCEAuthor."""
    export = parse_single_export(fixtures_dir / "edge_cases.json")
    # Fourth message (index 3) is the webhook/bot message
    bot_msg = next(m for m in export.messages if m.id == "900000000000000013")
    assert bot_msg.author.is_bot is True
    assert bot_msg.author.name == "webhook-bot"


# ---------------------------------------------------------------------------
# Thread inference — _infer_thread_info
# ---------------------------------------------------------------------------


def test_infer_thread_from_three_segments() -> None:
    """Filename with 3 dash-separated segments is identified as a thread."""
    stem = "Test Server - general - Cool Thread [888888888888888888]"
    is_thread, parent = _infer_thread_info(stem)
    assert is_thread is True
    assert parent == "general"


def test_infer_regular_from_two_segments() -> None:
    """Filename with 2 dash-separated segments is a regular channel, no parent."""
    stem = "Test Server - general [222222222222222222]"
    is_thread, parent = _infer_thread_info(stem)
    assert is_thread is False
    assert parent == ""


def test_infer_thread_info_forum() -> None:
    """Forum thread filename with 3 segments is also detected as a thread."""
    stem = "Test Server - Feedback Forum - Bug Report [999999999999999999]"
    is_thread, parent = _infer_thread_info(stem)
    assert is_thread is True
    assert parent == "Feedback Forum"


# ---------------------------------------------------------------------------
# Directory parsing — parse_export_directory
# ---------------------------------------------------------------------------


def test_parse_export_directory(fixtures_dir: Path) -> None:
    """Directory parse returns one DCEExport per valid JSON file."""
    exports = parse_export_directory(fixtures_dir)
    # All 5 fixture files are valid DCE JSON
    assert len(exports) == 5
    assert all(isinstance(e, DCEExport) for e in exports)


def test_parse_export_directory_sorted(fixtures_dir: Path) -> None:
    """Exports are sorted by channel name ascending."""
    exports = parse_export_directory(fixtures_dir)
    names = [e.channel.name for e in exports]
    assert names == sorted(names)


def test_parse_export_directory_skips_invalid(fixtures_dir: Path, tmp_path: Path) -> None:
    """Non-DCE JSON files in the directory are skipped without raising."""
    import shutil

    # Copy fixtures to a temp dir and add a bad file
    temp_dir = tmp_path / "exports"
    shutil.copytree(fixtures_dir, temp_dir)
    (temp_dir / "not_dce.json").write_text('{"foo": "bar"}')
    (temp_dir / "also_bad.json").write_text("this is not json at all{{{")

    exports = parse_export_directory(temp_dir)
    # Still 5 valid exports, bad files are silently skipped
    assert len(exports) == 5


# ---------------------------------------------------------------------------
# Thread detection on full export
# ---------------------------------------------------------------------------


def test_thread_export_detected(fixtures_dir: Path) -> None:
    """Thread fixture file is parsed with is_thread=True."""
    export = parse_single_export(
        fixtures_dir / "Test Server - general - Cool Thread [888888888888888888].json"
    )
    assert export.is_thread is True


def test_thread_parent_name(fixtures_dir: Path) -> None:
    """Thread fixture has parent_channel_name='general'."""
    export = parse_single_export(
        fixtures_dir / "Test Server - general - Cool Thread [888888888888888888].json"
    )
    assert export.parent_channel_name == "general"


def test_forum_export_detected(fixtures_dir: Path) -> None:
    """Forum thread fixture is also parsed with is_thread=True."""
    export = parse_single_export(
        fixtures_dir / "Test Server - Feedback Forum - Bug Report [999999999999999999].json"
    )
    assert export.is_thread is True
    assert export.parent_channel_name == "Feedback Forum"


# ---------------------------------------------------------------------------
# Validation — validate_export
# ---------------------------------------------------------------------------


def test_validate_detects_rendered_markdown(fixtures_dir: Path) -> None:
    """markdown_rendered.json triggers a 'rendered_markdown' warning."""
    exports = parse_export_directory(fixtures_dir)
    warnings = validate_export(exports, fixtures_dir)
    types = [w["type"] for w in warnings]
    assert "rendered_markdown" in types


def test_validate_detects_http_urls(fixtures_dir: Path) -> None:
    """edge_cases.json has an HTTP attachment URL → 'http_attachment' warning."""
    exports = parse_export_directory(fixtures_dir)
    warnings = validate_export(exports, fixtures_dir)
    types = [w["type"] for w in warnings]
    assert "http_attachment" in types


def test_validate_warns_empty_export(fixtures_dir: Path, tmp_path: Path) -> None:
    """An export with 0 messages triggers an 'empty_export' warning."""
    temp_dir = tmp_path / "exports"
    temp_dir.mkdir()

    # Build a minimal DCE JSON with 0 messages
    empty_export_data = {
        "guild": {"id": "111", "name": "EmptyGuild", "iconUrl": ""},
        "channel": {
            "id": "222",
            "type": 0,
            "categoryId": "",
            "category": "",
            "name": "empty",
            "topic": "",
        },
        "exportedAt": "2024-01-01T00:00:00+00:00",
        "messages": [],
        "messageCount": 0,
    }
    (temp_dir / "EmptyGuild - empty [222].json").write_text(json.dumps(empty_export_data))

    exports = parse_export_directory(temp_dir)
    warnings = validate_export(exports, temp_dir)
    types = [w["type"] for w in warnings]
    assert "empty_export" in types


def test_validate_no_warnings_clean(fixtures_dir: Path, tmp_path: Path) -> None:
    """simple_channel.json alone produces no warnings."""
    import shutil

    temp_dir = tmp_path / "exports"
    temp_dir.mkdir()
    shutil.copy(fixtures_dir / "simple_channel.json", temp_dir / "simple_channel.json")

    exports = parse_export_directory(temp_dir)
    warnings = validate_export(exports, temp_dir)
    assert warnings == []


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_parse_system_messages(fixtures_dir: Path) -> None:
    """System message types are parsed as regular DCEMessage objects."""
    export = parse_single_export(fixtures_dir / "edge_cases.json")
    types = {m.type for m in export.messages}
    assert "GuildMemberJoin" in types
    assert "ChannelPinnedMessage" in types
    assert "RecipientAdd" in types


def test_parse_forwarded_message(fixtures_dir: Path) -> None:
    """Forwarded message pattern: empty content + non-null reference on a bot message."""
    export = parse_single_export(fixtures_dir / "edge_cases.json")
    # The bot message (id 900000000000000013) has empty content and a reference
    fwd = next(m for m in export.messages if m.id == "900000000000000013")
    assert fwd.content == ""
    assert fwd.reference is not None
    assert fwd.author.is_bot is True
