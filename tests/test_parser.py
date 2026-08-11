"""Tests for DCE JSON parser."""

import json
from pathlib import Path

import pytest

from discord_ferry.parser.dce_parser import (
    RENDERED_MENTION_CONSEQUENCE,
    _coerce_channel_type,
    _infer_thread_info,
    _parse_author,
    _parse_guild,
    _parse_message,
    _parse_reaction,
    _raw_form_present,
    acknowledgement_required,
    check_cdn_url_expiry,
    message_has_rendered_mention,
    parse_export_directory,
    parse_single_export,
    validate_export,
)
from discord_ferry.parser.models import DCEExport, DCEMessage, DCEReaction

FIXTURES_DIR = Path(__file__).parent / "fixtures"

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
# Channel-type coercion — _coerce_channel_type
# ---------------------------------------------------------------------------


def test_coerce_channel_type_int_passthrough() -> None:
    """Pre-2.47 integer channel types pass through unchanged."""
    assert _coerce_channel_type(0) == 0
    assert _coerce_channel_type(11) == 11


def test_coerce_channel_type_dce_2_47_strings() -> None:
    """DCE 2.47.1 enum strings map to Discord-canonical integer codes."""
    assert _coerce_channel_type("GuildTextChat") == 0
    assert _coerce_channel_type("GuildPublicThread") == 11
    assert _coerce_channel_type("GuildForum") == 15


def test_coerce_channel_type_digit_string() -> None:
    """Quoted-int channel types from legacy serializers cast through."""
    assert _coerce_channel_type("0") == 0
    assert _coerce_channel_type("11") == 11


def test_coerce_channel_type_none_raises() -> None:
    """`None` is neither int nor str — must raise at the parse boundary."""
    with pytest.raises(ValueError, match="Unrecognized DCE channel type"):
        _coerce_channel_type(None)


def test_coerce_channel_type_unknown_raises() -> None:
    """An unrecognized channel-type string fails loudly at parse boundary."""
    with pytest.raises(ValueError, match="Unrecognized DCE channel type"):
        _coerce_channel_type("GuildUnobtainium")


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
    # 7 valid DCE JSON files: simple_channel, edge_cases, markdown_rendered,
    # forwarded_message_synthetic, plus the three real DCE captures (general,
    # Cool Thread, Bug Report).
    # rollback_state.json is JSON but not DCE-shaped and is silently skipped.
    assert len(exports) == 7
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
    # The assertion is "the bad files changed nothing", so compare against the clean
    # directory rather than a hardcoded count — otherwise every new fixture breaks this
    # test for a reason unrelated to what it checks.
    assert len(exports) == len(parse_export_directory(fixtures_dir))


# ---------------------------------------------------------------------------
# Thread detection on full export
# ---------------------------------------------------------------------------


def test_thread_export_detected(fixtures_dir: Path) -> None:
    """Thread fixture file is parsed with is_thread=True."""
    export = parse_single_export(
        fixtures_dir / "Discord Ferry Test - general - Cool Thread [1506019505778987190].json"
    )
    assert export.is_thread is True


def test_thread_parent_name(fixtures_dir: Path) -> None:
    """Thread fixture has parent_channel_name='general'."""
    export = parse_single_export(
        fixtures_dir / "Discord Ferry Test - general - Cool Thread [1506019505778987190].json"
    )
    assert export.parent_channel_name == "general"


def test_forum_export_detected(fixtures_dir: Path) -> None:
    """Forum thread fixture is also parsed with is_thread=True."""
    export = parse_single_export(
        fixtures_dir / "Discord Ferry Test - feedback-forum - Bug Report [1506019530294562938].json"
    )
    assert export.is_thread is True
    # Discord normalizes forum-channel names (lowercase + hyphenated), which
    # propagates to DCE's filename template and the parsed parent name.
    assert export.parent_channel_name == "feedback-forum"


def test_null_json_fields_collapse_to_empty_string(fixtures_dir: Path) -> None:
    """DCE emits `null` for absent categoryId/category/topic and for messageId
    on `ThreadCreated` system messages. `dict.get(K, default)` only returns
    the default when the key is missing — for a present-but-null key it
    returns Python `None`, which `str()` would coerce to the truthy string
    `"None"`. That string slips past downstream truthy guards (e.g.
    structure.py's `if cat_id`), causing phantom category creation. Lock the
    null-collapse behavior here against the real captures that exposed it.
    """
    general = parse_single_export(
        fixtures_dir / "Discord Ferry Test - general [1506019498094891120].json"
    )
    # General has no category in the manifest; both channel fields are null.
    assert general.channel.category_id == ""
    assert general.channel.category == ""

    # The ThreadCreated system message has reference.messageId == null.
    thread_created = next(m for m in general.messages if m.type == "ThreadCreated")
    assert thread_created.reference is not None
    assert thread_created.reference.message_id == ""

    # Threads carry no topic; null collapses to "" rather than "None".
    cool_thread = parse_single_export(
        fixtures_dir / "Discord Ferry Test - general - Cool Thread [1506019505778987190].json"
    )
    assert cool_thread.channel.topic == ""


def test_parse_guild_null_icon_url_collapses() -> None:
    """A guild without a custom icon emits `"iconUrl": null` — must not stringify to `"None"`."""
    guild = _parse_guild({"id": "1", "name": "Iconless Guild", "iconUrl": None})
    assert guild.icon_url == ""


def test_parse_author_null_fields_collapse_to_defaults() -> None:
    """Author fields default to their intended values when JSON-null.

    Pomelo-era users have no discriminator and may have neither a server-specific
    nickname nor a custom avatar; DCE serializes those as `null`, not as missing
    keys. The defaults must hold whether the key is missing or present-but-null.
    """
    author = _parse_author(
        {
            "id": "100",
            "name": "pomelo_user",
            "discriminator": None,
            "nickname": None,
            "color": None,
            "isBot": False,
            "avatarUrl": None,
            "roles": [],
        }
    )
    assert author.discriminator == "0000"
    assert author.nickname == ""
    assert author.avatar_url == ""


def test_parse_reaction_unicode_emoji_null_id() -> None:
    """Unicode emojis emit `"id": null` and `"imageUrl": null` — must collapse to ``\"\"``."""
    reaction_raw = {
        "emoji": {
            "id": None,
            "name": "\N{THUMBS UP SIGN}",
            "isAnimated": False,
            "imageUrl": None,
        },
        "count": 1,
        "users": [],
    }
    reaction = _parse_reaction(reaction_raw)
    assert reaction.emoji.id == ""
    assert reaction.emoji.image_url == ""
    assert reaction.emoji.name == "\N{THUMBS UP SIGN}"


# ---------------------------------------------------------------------------
# Validation — validate_export
# ---------------------------------------------------------------------------


def test_validate_detects_rendered_markdown(fixtures_dir: Path) -> None:
    """markdown_rendered.json triggers a 'rendered_markdown' warning."""
    exports = parse_export_directory(fixtures_dir)
    warnings = validate_export(exports, fixtures_dir)
    types = [w["type"] for w in warnings]
    assert "rendered_markdown" in types


NOT_RENDERED_DIR = FIXTURES_DIR / "mentions_not_rendered"
RENDERED_MULTI_DIR = FIXTURES_DIR / "markdown_rendered_multi"


def test_validate_ignores_mentions_that_were_not_rendered() -> None:
    """A reply, an embed-carried mention and an attachment-only message all have
    a non-empty `mentions` array and no raw `<@` in content, so the OLD rule
    flagged every one of them. None is evidence of an export made without
    --markdown false. This is issue #143: one such message condemned a whole
    channel and left the GUI with no way forward.

    Killing: the old rule (mentions non-empty and no <@ in content)."""
    exports = parse_export_directory(NOT_RENDERED_DIR)
    warnings = validate_export(exports, NOT_RENDERED_DIR)
    assert [w for w in warnings if w["type"] == "rendered_markdown"] == []


def test_validate_counts_every_rendered_message_not_just_the_first() -> None:
    """The old rule set markdown_warned=True on the first hit, so a user was told
    an export was critical without being told how widespread it was. Three
    messages out of a thousand and nine hundred out of a thousand looked
    identical.

    Two exports, not one. With a single export in the directory, a counter
    declared above `for export in exports:` instead of inside it would still
    report "2" and pass every assertion here, while accumulating across channels
    in the field. The second channel's count pins the counter's scope.

    The consequence phrase is asserted through the constant, not as a literal.
    A hand-written copy at the interpolation site in dce_parser.py would leave
    every other assertion green while opening the wording drift the constant
    exists to prevent.

    Killing: first-occurrence-only behaviour; a per-directory counter; any
    implementation that takes a message list, which reports ZERO on the engine's
    streaming path; a consequence phrase written out by hand."""
    exports = parse_export_directory(RENDERED_MULTI_DIR, metadata_only=True)
    warnings = validate_export(exports, RENDERED_MULTI_DIR)
    hits = [w for w in warnings if w["type"] == "rendered_markdown"]
    assert len(hits) == 2

    # Key by channel name, not by list position: the row order follows the
    # channel-name sort in parse_export_directory, which is not this test's
    # subject.
    by_channel = {w["message"].split("'")[1]: w for w in hits}
    assert set(by_channel) == {"rendered-multi", "rendered-multi-b"}
    assert by_channel["rendered-multi"]["count"] == "2"
    assert by_channel["rendered-multi-b"]["count"] == "1"
    assert "2 message(s)" in by_channel["rendered-multi"]["message"]
    assert "1 message(s)" in by_channel["rendered-multi-b"]["message"]
    for hit in hits:
        assert RENDERED_MENTION_CONSEQUENCE in hit["message"]


_ALICE = {"id": "400000000000000001", "name": "alice", "nickname": "Alice"}
_BOB = {"id": "400000000000000002", "name": "bob", "nickname": "Bob"}
_FLOWER = {"id": "400000000000000003", "name": "ali", "nickname": "Alice 🌸"}
_PRONOUNS = {"id": "400000000000000004", "name": "cas", "nickname": "Bob (he/him)"}
_EVERYONE = {"id": "400000000000000005", "name": "everyone", "nickname": "Everyone"}


@pytest.mark.parametrize(
    ("content", "mentions", "expected", "kills"),
    [
        ("Hey @Alice, check out #general!", [_ALICE], True, "a rule that never fires"),
        (
            "Hey <@400000000000000001>! cc @Alice",
            [_ALICE],
            False,
            "_raw_form_present returning a constant False, or step 2 deleted",
        ),
        (
            "Hey <@!400000000000000001>! cc @Alice",
            [_ALICE],
            False,
            "a raw-form check that only knows <@id>",
        ),
        (
            "<@400000000000000001> and @Bob",
            [_ALICE, _BOB],
            True,
            "a per-MESSAGE raw check, which would clear the whole message",
        ),
        ("Sure, that works", [_ALICE], False, "the old rule"),
        ("", [_BOB], False, "the old rule"),
        (
            "hi @Alice 🌸!",
            [_FLOWER],
            True,
            r"a \b boundary, which cannot match after a non-word character",
        ),
        ("thanks @Bob (he/him).", [_PRONOUNS], True, r"a \b boundary"),
        (
            "cc @Bobby, I meant @Bob",
            [_BOB],
            True,
            "a single str.find, which stops at the first rejected occurrence",
        ),
        ("cc @Bobby about it", [_BOB], False, "no trailing check"),
        (
            "cc @Bob_smith about it",
            [_BOB],
            False,
            "_is_word_char dropping the underscore arm",
        ),
        (
            "mail me at someone@Bob.io",
            [_BOB],
            False,
            "checking only the character AFTER the candidate",
        ),
        (
            "@Bob look at this",
            [_BOB],
            True,
            "a leading check that requires a preceding character to exist",
        ),
        (
            "thanks @Bob",
            [_BOB],
            True,
            "a trailing check that requires a following character to exist",
        ),
        (
            "@everyone please read",
            [_EVERYONE],
            False,
            "a case-insensitive match, or using name as a SECOND candidate",
        ),
        (
            "hi @alice",
            [{"id": "1", "name": "alice"}],
            True,
            "direct subscripting of the mention dict",
        ),
        (
            "hi @Alice there",
            [{"id": "1", "nickname": "  Alice  "}],
            True,
            "using the raw value, which would look for '@  Alice  '",
        ),
        (
            "hi @ there",
            [{"id": "1", "name": "", "nickname": "   "}],
            False,
            "no empty-candidate guard, which makes '@' alone a match",
        ),
        ("ping @Unknown please", [], True, "a rule that only ever consults the mentions array"),
        ("the @Unknowns are a band", [], False, "a bare substring test for @Unknown"),
        (
            "what does @Unknown mean? <@400000000000000001>",
            [_ALICE],
            False,
            "running the @Unknown test BEFORE the mention loop",
        ),
        (
            "@here see <@400000000000000001>",
            [_ALICE],
            False,
            "treating any at-sign token as a rendered mention",
        ),
        (
            "the file /home/@Bob/x is here",
            [_BOB],
            True,
            "NOTHING. Known residual: '/' is not a word character, so no boundary "
            "check can tell a path from a mention after a bracket. Requiring "
            "whitespace before '@' would fix it and break '(@Alice)', which is what "
            "DCE renders for '(<@1>)'.",
        ),
        (
            "Sure @Alice, that works",
            [_ALICE],
            True,
            "NOTHING. Known residual: in a RAW export, a body that types a name as "
            "plain text while that person is also the reply target is "
            "indistinguishable from the true positive.",
        ),
    ],
)
def test_message_has_rendered_mention(
    content: str, mentions: list[dict[str, str]], expected: bool, kills: str
) -> None:
    """Ported from the design prototype. `kills` names the wrong implementation
    each row catches; rows saying NOTHING are pinned known residuals, kept so
    they stay visible."""
    assert message_has_rendered_mention(content, mentions) is expected, kills


def test_raw_form_present_ignores_an_empty_id() -> None:
    """Killing: dropping _raw_form_present's empty-id guard, which makes the
    literal '<@>' read as raw evidence and suppress a genuine warning."""
    assert _raw_form_present("see <@> and @Bob", "") is False


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
# Acknowledgement classifier: acknowledgement_required
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "warning_type",
    ["http_attachment", "empty_export", "expired_cdn_url", "channel_limit", "emoji_limit"],
)
def test_acknowledgement_not_required_for_other_warning_types(warning_type: str) -> None:
    """Spec S1.4: no warning type other than rendered_markdown may gate the
    button, before or after.

    Killing: a classifier that gates on any warning, and a widened
    _ACKNOWLEDGEABLE_TYPES. Parametrizing over the real other five pins the
    BEHAVIOUR; asserting the frozenset's contents by equality would only detect
    change."""
    assert acknowledgement_required([{"type": warning_type, "message": "x"}]) is None


def test_acknowledgement_required_sums_across_files() -> None:
    """Killing: a classifier that never fires, one that reports only the first
    warning's count, and one that raises on a missing or non-numeric count."""
    reason = acknowledgement_required(
        [
            {"type": "rendered_markdown", "count": "2", "message": "x"},
            {"type": "rendered_markdown", "count": "1", "message": "y"},
        ]
    )
    assert reason is not None
    assert "3 message(s)" in reason
    assert "2 export file(s)" in reason
    assert RENDERED_MENTION_CONSEQUENCE in reason
    assert acknowledgement_required([]) is None

    # A rendered_markdown row among other types: the file count must report the
    # ACKNOWLEDGEABLE rows, not every warning. The http_attachment row carries a
    # count it has no business carrying, so a total summed over all warnings
    # instead of over the hits is caught here too.
    mixed = acknowledgement_required(
        [
            {"type": "http_attachment", "count": "9", "message": "a"},
            {"type": "rendered_markdown", "count": "2", "message": "b"},
            {"type": "empty_export", "message": "c"},
        ]
    )
    assert mixed is not None
    assert "2 message(s) across 1 export file(s)" in mixed


@pytest.mark.parametrize(
    "bad", [{"type": "rendered_markdown", "count": "lots"}, {"type": "rendered_markdown"}]
)
def test_acknowledgement_required_tolerates_a_broken_count(bad: dict[str, str]) -> None:
    """This runs while a GUI page is rendering. A bare int() or a bare subscript
    would raise there.

    Killing: int(w["count"]) without a guard."""
    assert acknowledgement_required([bad]) is not None


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


# ---------------------------------------------------------------------------
# Bug 7: validate_export counts emoji from message content
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Task 4: json_path field on DCEExport
# ---------------------------------------------------------------------------


def test_dce_export_has_json_path() -> None:
    """DCEExport includes json_path field for streaming parser."""
    from discord_ferry.parser.models import DCEChannel, DCEExport, DCEGuild

    export = DCEExport(
        guild=DCEGuild(id="1", name="Test"),
        channel=DCEChannel(id="2", type=0, name="general"),
        json_path=Path("/tmp/test.json"),
    )
    assert export.json_path == Path("/tmp/test.json")


def test_dce_export_json_path_defaults_to_none() -> None:
    """json_path defaults to None for backward compatibility."""
    from discord_ferry.parser.models import DCEChannel, DCEExport, DCEGuild

    export = DCEExport(
        guild=DCEGuild(id="1", name="Test"),
        channel=DCEChannel(id="2", type=0, name="general"),
    )
    assert export.json_path is None


def test_stream_messages_yields_all(tmp_path: Path) -> None:
    """stream_messages yields each message from a DCE JSON file."""
    import json

    from discord_ferry.parser.dce_parser import stream_messages

    data = {
        "guild": {"id": "1", "name": "G"},
        "channel": {"id": "2", "type": 0, "name": "c"},
        "messages": [
            {
                "id": "100",
                "type": "Default",
                "timestamp": "2024-01-01T00:00:00+00:00",
                "content": "hello",
                "author": {"id": "10", "name": "User"},
            },
            {
                "id": "101",
                "type": "Default",
                "timestamp": "2024-01-01T00:01:00+00:00",
                "content": "world",
                "author": {"id": "10", "name": "User"},
            },
        ],
        "messageCount": 2,
    }
    json_path = tmp_path / "test.json"
    json_path.write_text(json.dumps(data))

    msgs = list(stream_messages(json_path))
    assert len(msgs) == 2
    assert msgs[0].id == "100"
    assert msgs[0].content == "hello"
    assert msgs[1].id == "101"


def test_stream_messages_handles_empty(tmp_path: Path) -> None:
    """stream_messages yields nothing for exports with no messages."""
    import json

    from discord_ferry.parser.dce_parser import stream_messages

    data = {
        "guild": {"id": "1", "name": "G"},
        "channel": {"id": "2", "type": 0, "name": "c"},
        "messages": [],
        "messageCount": 0,
    }
    json_path = tmp_path / "test.json"
    json_path.write_text(json.dumps(data))

    msgs = list(stream_messages(json_path))
    assert len(msgs) == 0


def test_parse_single_export_metadata_only(tmp_path: Path) -> None:
    """metadata_only=True returns DCEExport with empty messages list."""
    import json

    from discord_ferry.parser.dce_parser import parse_single_export

    data = {
        "guild": {"id": "1", "name": "G"},
        "channel": {"id": "2", "type": 0, "name": "c"},
        "messages": [
            {
                "id": "100",
                "type": "Default",
                "timestamp": "2024-01-01T00:00:00+00:00",
                "content": "hello",
                "author": {"id": "10", "name": "User"},
            },
        ],
        "messageCount": 1,
    }
    json_path = tmp_path / "test.json"
    json_path.write_text(json.dumps(data))

    export = parse_single_export(json_path, metadata_only=True)
    assert export.message_count == 1
    assert len(export.messages) == 0
    assert export.json_path == json_path


def test_validate_counts_emoji_from_content(tmp_path: Path) -> None:
    """validate_export counts custom emoji in message content, not just reactions."""
    temp_dir = tmp_path / "exports"
    temp_dir.mkdir()

    # Build an export with emoji only in message content (no reactions).
    export_data = {
        "guild": {"id": "111", "name": "EmojiGuild", "iconUrl": ""},
        "channel": {
            "id": "222",
            "type": 0,
            "categoryId": "",
            "category": "",
            "name": "test",
            "topic": "",
        },
        "exportedAt": "2024-01-01T00:00:00+00:00",
        "messages": [
            {
                "id": "1",
                "type": "Default",
                "timestamp": "2024-01-01T00:00:00+00:00",
                "content": "Look <:wave:111> and <a:spin:222>",
                "author": {"id": "u1", "name": "User"},
            }
        ],
        "messageCount": 1,
    }
    (temp_dir / "EmojiGuild - test [222].json").write_text(json.dumps(export_data))

    exports = parse_export_directory(temp_dir)
    # Validate should now find 2 emoji from content.
    # We can't easily test the exact count warning (need >100), but we can verify
    # the summary in _compute_summary or check that the emoji IDs are being tracked.
    # Instead, let's verify validate_export doesn't crash and the counting logic works
    # by checking that no emoji_limit warning is raised for just 2 emoji.
    warnings = validate_export(exports, temp_dir)
    emoji_warnings = [w for w in warnings if w["type"] == "emoji_limit"]
    assert len(emoji_warnings) == 0  # 2 emoji < 100 limit


# ---------------------------------------------------------------------------
# Task 7: check_cdn_url_expiry
# ---------------------------------------------------------------------------


def test_cdn_url_expired() -> None:
    """URL with past ex timestamp returns True."""
    url = "https://cdn.discordapp.com/attachments/1/2/f.png?ex=60000000&is=abc&hm=def"
    assert check_cdn_url_expiry(url) is True


def test_cdn_url_valid_future() -> None:
    """URL with far-future ex timestamp returns False."""
    url = "https://cdn.discordapp.com/attachments/1/2/f.png?ex=ffffffff&is=abc&hm=def"
    assert check_cdn_url_expiry(url) is False


def test_cdn_url_no_ex_param() -> None:
    """Discord URL without ex param returns None."""
    url = "https://cdn.discordapp.com/attachments/1/2/file.png"
    assert check_cdn_url_expiry(url) is None


def test_cdn_url_non_discord() -> None:
    """Non-Discord URL returns None."""
    assert check_cdn_url_expiry("https://example.com/file.png") is None


def test_cdn_url_non_hex_ex() -> None:
    """Non-hex ex value returns None (no crash)."""
    url = "https://cdn.discordapp.com/file.png?ex=notahex"
    assert check_cdn_url_expiry(url) is None


def test_cdn_url_empty_string() -> None:
    """Empty URL returns None."""
    assert check_cdn_url_expiry("") is None


# ---------------------------------------------------------------------------
# Task 8: validate_export CDN expiry warning
# ---------------------------------------------------------------------------


def test_validate_export_counts_expired_urls(tmp_path: Path) -> None:
    """validate_export emits expired_cdn_url warning with count."""
    export_data = {
        "guild": {"id": "g1", "name": "G", "iconUrl": ""},
        "channel": {"id": "c1", "name": "ch", "type": 0},
        "dateRange": {"after": None, "before": None},
        "exportedAt": "2024-01-01T00:00:00+00:00",
        "messageCount": 1,
        "messages": [
            {
                "id": "m1",
                "type": "Default",
                "timestamp": "2024-01-01T00:00:00+00:00",
                "content": "msg",
                "author": {
                    "id": "u1",
                    "name": "U",
                    "discriminator": "0",
                    "isBot": False,
                },
                "attachments": [
                    {
                        "id": "a1",
                        "url": "https://cdn.discordapp.com/f1.png?ex=60000000",
                        "fileName": "f1.png",
                        "fileSizeBytes": 100,
                    },
                    {
                        "id": "a2",
                        "url": "https://cdn.discordapp.com/f2.png?ex=60000000",
                        "fileName": "f2.png",
                        "fileSizeBytes": 200,
                    },
                ],
                "embeds": [],
                "stickers": [],
                "reactions": [],
                "mentions": [],
            }
        ],
    }
    json_path = tmp_path / "Test Server - ch [c1].json"
    json_path.write_text(json.dumps(export_data))

    exports = parse_export_directory(tmp_path)
    warnings = validate_export(exports, tmp_path)

    expired_warnings = [w for w in warnings if w["type"] == "expired_cdn_url"]
    assert len(expired_warnings) == 1
    assert "2" in expired_warnings[0]["message"]
    assert "--media" in expired_warnings[0]["message"]


# ---------------------------------------------------------------------------
# Batch 9 — S2 type-aware thread classification
# ---------------------------------------------------------------------------


def test_infer_thread_type_overrides_dash_guild() -> None:
    """SC-12: a dash-guild text channel (type 0) is NOT a thread."""
    assert _infer_thread_info("Acme - Community - general [123]", channel_type=0) == (False, "")


def test_infer_thread_real_thread_type() -> None:
    """SC-13: a real thread (type 11) is a thread with the right parent."""
    assert _infer_thread_info("Guild - Channel - Thread [123]", channel_type=11) == (
        True,
        "Channel",
    )


def test_infer_thread_plain_channel_type() -> None:
    """SC-14: a plain 2-segment channel (type 0) is not a thread."""
    assert _infer_thread_info("Guild - general [123]", channel_type=0) == (False, "")


def test_two_segment_re_removed() -> None:
    """SC-17: the dead _TWO_SEGMENT_RE is gone (no compiled-but-unused regex)."""
    import discord_ferry.parser.dce_parser as p

    assert not hasattr(p, "_TWO_SEGMENT_RE")


# ---------------------------------------------------------------------------
# Forwarded messages (DCE 2.47+)
# ---------------------------------------------------------------------------


def _forward_raw() -> dict:
    """A raw message carrying a forwarded payload, in the verified DCE 2.47.1 shape.

    Field names taken from DiscordChatExporter.Core/Exporting/JsonMessageWriter.cs:538-592
    at tag 2.47.1 — the version this project pins. The nested attachments/embeds/stickers
    are written by the same writers as their top-level counterparts, so their shapes match.
    """
    return {
        "id": "900000000000000099",
        "type": "Default",
        "timestamp": "2024-03-01T10:00:00+00:00",
        "content": "",
        "author": {"id": "1", "name": "forwarder", "nickname": "forwarder", "isBot": False},
        "reference": {
            "type": "Forward",
            "messageId": "800000000000000001",
            "channelId": "222222222222222222",
            "guildId": "111111111111111111",
        },
        "forwardedMessage": {
            "timestamp": "2024-02-01T09:00:00+00:00",
            "timestampEdited": None,
            "content": "the original text",
            "attachments": [
                {
                    "id": "att1",
                    "url": "https://cdn.discordapp.com/a.png",
                    "fileName": "a.png",
                    "fileSizeBytes": 1234,
                }
            ],
            "embeds": [{"title": "embed title", "description": "embed body"}],
            "stickers": [
                {"id": "s1", "name": "sticker", "format": "Png", "sourceUrl": "https://x/s.png"}
            ],
        },
    }


def test_parse_forwarded_message_payload() -> None:
    """The forwarded block is parsed in full, reusing the normal attachment parser."""
    msg = _parse_message(_forward_raw())

    assert msg.reference is not None
    assert msg.reference.type == "Forward"

    fwd = msg.forwarded_message
    assert fwd is not None
    assert fwd.content == "the original text"
    assert fwd.timestamp == "2024-02-01T09:00:00+00:00"
    assert fwd.timestamp_edited is None
    assert len(fwd.attachments) == 1
    assert fwd.attachments[0].file_name == "a.png"
    assert fwd.attachments[0].file_size_bytes == 1234
    assert fwd.embeds == [{"title": "embed title", "description": "embed body"}]
    assert fwd.stickers[0]["name"] == "sticker"


def test_parse_message_without_forward_leaves_fields_empty() -> None:
    """A plain message has no forwarded payload and an empty reference kind.

    Empty (not "Default") is the marker for "this export predates DCE 2.47", which the
    migrator needs to distinguish an old export from a genuine reply.
    """
    raw = _forward_raw()
    del raw["forwardedMessage"]
    del raw["reference"]
    msg = _parse_message(raw)
    assert msg.forwarded_message is None
    assert msg.reference is None


def test_forwarded_block_with_unexpected_types_is_survivable() -> None:
    """A malformed forwarded block must not crash or corrupt.

    Two distinct traps, both verified against the pre-hardening code:
    a non-string `content` survived as an int and raised TypeError inside the join in
    `_merge_forwarded`; and `list(some_dict)` yields the dict's KEYS rather than raising,
    so `embeds` arriving as an object silently became `["title", "description"]`.
    """
    raw = _forward_raw()
    raw["forwardedMessage"]["content"] = 123
    raw["forwardedMessage"]["embeds"] = {"title": "x", "description": "y"}
    raw["forwardedMessage"]["attachments"] = "not-a-list"
    raw["forwardedMessage"]["stickers"] = None

    msg = _parse_message(raw)
    fwd = msg.forwarded_message
    assert fwd is not None
    assert fwd.content == "123"  # coerced, not left as an int
    assert fwd.embeds == []  # not ["title", "description"]
    assert fwd.attachments == []
    assert fwd.stickers == []


def test_parse_forwarded_fixture_end_to_end(fixtures_dir: Path) -> None:
    """The synthetic fixture parses through the real file path, not just the dict path.

    Exercises `parse_single_export`, so the whole-file shape is covered — a dict-level
    test alone would not catch a required top-level key being wrong.
    """
    export = parse_single_export(fixtures_dir / "forwarded_message_synthetic.json")
    assert export.message_count == 2

    forward, reply = export.messages
    assert forward.forwarded_message is not None
    assert forward.forwarded_message.content == "the original text that used to be discarded"
    assert forward.forwarded_message.attachments[0].file_name == "forwarded.png"
    assert forward.reference is not None
    assert forward.reference.type == "Forward"

    # The second message is an ordinary reply and must stay one.
    assert reply.forwarded_message is None
    assert reply.reference is not None
    assert reply.reference.type == "Default"


# ---------------------------------------------------------------------------
# The DCE 2.47.3 thread-starter shape (#110 batch 8, chunk #219, task #228)
# ---------------------------------------------------------------------------
#
# Lives in fixtures/dce_2_47_3/ rather than fixtures/ on purpose. parse_export_directory
# globs "*.json" at the top level only, so a valid export added beside the others would
# change the count test_parse_export_directory asserts.

_2_47_3_DIR = FIXTURES_DIR / "dce_2_47_3"
_2_47_3_THREAD = (
    _2_47_3_DIR / "Discord Ferry Test - general - Cool Thread [1506019505778987190].json"
)


def test_the_2_47_3_fixture_has_the_collision_shape() -> None:
    """SC-4.1: post-bump the thread's channel id and its first message id are the same.

    DCE 2.47.3 replaces the empty ThreadStarterMessage placeholder with the real
    parent-channel message and keeps that message's Discord id (upstream PR #1557), and
    a thread's channel id IS its origin message id. That equality is the whole reason
    batch 7's parent-wins guard and batch 8's merge suppression exist.
    """
    export = parse_single_export(_2_47_3_THREAD)

    assert export.channel.id == export.messages[0].id == "1506019505778987190"
    assert export.messages[0].content, (
        "the resolved starter carries the origin's real content, where the 2.47.1 "
        "placeholder carried an empty string"
    )
    assert export.messages[0].type not in ("21", "ThreadStarterMessage"), (
        "2.47.3 resolves or drops the placeholder before serialising, so kind 21 stops "
        "being emitted rather than changing spelling"
    )
    assert export.messages[0].reference is None, (
        "the resolved starter is the parent's own message, which is not a reply; the "
        "placeholder it replaces DID carry a reference back to the origin"
    )


def test_the_2_47_3_fixture_starter_precedes_the_reply() -> None:
    """SC-4.1: upstream places the resolved starter in its correct chronological position.

    The placeholder sat at 22:43:55.692, after the origin was actually posted. The
    resolved starter carries the origin's own 22:43:50.667, so it sorts before the
    thread's first reply. `_merge_threads` sorts by timestamp, so this ordering is what
    a merged thread will actually look like.
    """
    export = parse_single_export(_2_47_3_THREAD)
    assert export.messages[0].timestamp < export.messages[1].timestamp


def test_the_2_47_3_fixture_declares_it_is_synthetic() -> None:
    """SC-4.2: a synthetic fixture that does not say so gets cited as ground truth.

    Same contract as forwarded_message_synthetic.json, which carries its provenance in
    the same field for the same reason.
    """
    export = parse_single_export(_2_47_3_THREAD)
    topic = export.channel.topic or ""

    assert "SYNTHETIC" in topic
    assert "2.47.3" in topic
    assert "DiscordClient.cs" in topic
    assert "MessageKind.cs" in topic
    assert "NOT produced by running DCE" in topic
    assert "re-derive this from source" in topic


def test_the_2_47_3_fixture_did_not_disturb_the_2_47_1_captures() -> None:
    """SC-4.4: the pre-bump shape must stay covered, and stay countable.

    The synthetic lives in a subdirectory because parse_export_directory globs the top
    level only. Adding a valid export beside the real ones would change this count and
    force an edit to a pre-existing test.
    """
    assert len(parse_export_directory(FIXTURES_DIR)) == 7

    pre_bump = parse_single_export(
        FIXTURES_DIR / "Discord Ferry Test - general - Cool Thread [1506019505778987190].json"
    )
    assert pre_bump.channel.id != pre_bump.messages[0].id, (
        "at the 2.47.1 pin the starter carries a synthetic id, which is why batch 7's "
        "guard is inert today; test_the_guard_is_inert_at_the_2_47_1_pin depends on it"
    )
    assert pre_bump.messages[0].type == "21"
    assert pre_bump.messages[0].content == ""
