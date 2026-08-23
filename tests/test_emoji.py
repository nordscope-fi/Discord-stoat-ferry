"""Tests for the emoji migration phase (Phase 7)."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from discord_ferry.config import FerryConfig
from discord_ferry.migrator.emoji import (
    _extract_emoji_from_content,
    find_emoji_in_exports,
    messages_using_emoji,
    run_emoji,
    upload_and_create_emoji,
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
from discord_ferry.state import MigrationState

# Default emoji limit matching FerryConfig.max_emoji
MAX_EMOJI_DEFAULT = 100

BASE_URL = "https://api.test"
TOKEN = "test-token"
AUTUMN_URL = "https://autumn.test"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_author() -> DCEAuthor:
    return DCEAuthor(id="user1", name="TestUser")


def _make_message(
    msg_id: str = "msg1",
    content: str = "",
    reactions: list[DCEReaction] | None = None,
) -> DCEMessage:
    return DCEMessage(
        id=msg_id,
        type="Default",
        timestamp="2024-01-01T00:00:00Z",
        content=content,
        author=_make_author(),
        reactions=reactions or [],
    )


def _make_export(messages: list[DCEMessage] | None = None) -> DCEExport:
    return DCEExport(
        guild=DCEGuild(id="guild1", name="Test", icon_url=""),
        channel=DCEChannel(id="ch1", type=0, category_id="", category="", name="general", topic=""),
        messages=messages or [],
    )


def _make_config(export_dir: Path) -> FerryConfig:
    return FerryConfig(
        export_dir=export_dir,
        stoat_url=BASE_URL,
        token=TOKEN,
        upload_delay=0.0,
    )


def _make_state() -> MigrationState:
    state = MigrationState()
    state.stoat_server_id = "srv1"
    state.autumn_url = AUTUMN_URL
    return state


# ---------------------------------------------------------------------------
# _extract_emoji_from_content
# ---------------------------------------------------------------------------


def test_extract_emoji_from_content_static() -> None:
    """Parses standard custom emoji syntax <:name:id>."""
    results = _extract_emoji_from_content("Hello <:party:123> world")
    assert len(results) == 1
    assert results[0] == ("123", "party", False)


def test_extract_emoji_from_content_animated() -> None:
    """Parses animated emoji syntax <a:name:id>."""
    results = _extract_emoji_from_content("Look <a:spin:456>!")
    assert len(results) == 1
    assert results[0] == ("456", "spin", True)


def test_extract_emoji_from_content_multiple() -> None:
    """Extracts multiple emoji from a single content string."""
    results = _extract_emoji_from_content("<:foo:111> text <a:bar:222>")
    assert len(results) == 2
    ids = {r[0] for r in results}
    assert ids == {"111", "222"}


def test_extract_emoji_from_content_empty() -> None:
    """Returns empty list for content with no custom emoji."""
    assert _extract_emoji_from_content("Just plain text") == []


def test_extract_emoji_from_content_unicode_not_matched() -> None:
    """Standard Unicode emoji are not matched."""
    assert _extract_emoji_from_content("Hello \U0001f44d") == []


# ---------------------------------------------------------------------------
# run_emoji — unit tests
# ---------------------------------------------------------------------------


async def test_run_emoji_empty_exports() -> None:
    """Returns early with 'completed' event when no exports have emoji."""
    events: list[Any] = []
    config = _make_config(Path("/tmp"))
    state = _make_state()
    exports = [_make_export()]  # export with no messages

    await run_emoji(config, state, exports, events.append)

    statuses = [e.status for e in events]
    assert "completed" in statuses
    assert state.emoji_map == {}


async def test_run_emoji_deduplication(tmp_path: Path) -> None:
    """Same emoji ID appearing in both reactions and content is stored only once."""
    # Create a dummy emoji file.
    emoji_file = tmp_path / "emoji.png"
    emoji_file.write_bytes(b"PNG")

    msg = _make_message(
        content="<:wave:999>",
        reactions=[
            DCEReaction(emoji=DCEEmoji(id="999", name="wave", image_url="emoji.png"), count=1)
        ],
    )
    exports = [_make_export([msg])]
    config = _make_config(tmp_path)
    state = _make_state()
    events: list[Any] = []

    with (
        patch(
            "discord_ferry.migrator.emoji.upload_with_cache",
            new=AsyncMock(return_value="autumn_file_1"),
        ),
        patch(
            "discord_ferry.migrator.emoji.api_create_emoji",
            new=AsyncMock(return_value={"_id": "stoat_emoji_1"}),
        ),
        patch("discord_ferry.migrator.emoji.asyncio.sleep", new=AsyncMock()),
    ):
        await run_emoji(config, state, exports, events.append)

    # Only one emoji should be created — value is the Autumn file ID.
    assert state.emoji_map == {"999": "autumn_file_1"}


async def test_run_emoji_limit_warning(tmp_path: Path) -> None:
    """Emits a warning and truncates when more than MAX_EMOJI_DEFAULT are discovered."""
    # Build MAX_EMOJI_DEFAULT + 5 reactions with unique IDs.
    reactions = [
        DCEReaction(emoji=DCEEmoji(id=str(i), name=f"emoji{i}", image_url=f"e{i}.png"), count=1)
        for i in range(MAX_EMOJI_DEFAULT + 5)
    ]
    # Create dummy files for every emoji.
    for i in range(MAX_EMOJI_DEFAULT + 5):
        (tmp_path / f"e{i}.png").write_bytes(b"PNG")

    msg = _make_message(reactions=reactions)
    exports = [_make_export([msg])]
    config = _make_config(tmp_path)
    state = _make_state()
    events: list[Any] = []

    with (
        patch(
            "discord_ferry.migrator.emoji.upload_with_cache",
            new=AsyncMock(return_value="autumn_id"),
        ),
        patch(
            "discord_ferry.migrator.emoji.api_create_emoji",
            new=AsyncMock(return_value={"_id": "stoat_id"}),
        ),
        patch("discord_ferry.migrator.emoji.asyncio.sleep", new=AsyncMock()),
    ):
        await run_emoji(config, state, exports, events.append)

    warning_events = [e for e in events if e.status == "warning"]
    assert warning_events, "Expected at least one warning event for truncation"
    assert any("truncat" in e.message.lower() for e in warning_events)
    assert len(state.warnings) >= 1


async def test_run_emoji_resume_skip(tmp_path: Path) -> None:
    """Skips an emoji that is already in state.emoji_map (resume support)."""
    emoji_file = tmp_path / "wave.png"
    emoji_file.write_bytes(b"PNG")

    reactions = [DCEReaction(emoji=DCEEmoji(id="111", name="wave", image_url="wave.png"), count=1)]
    msg = _make_message(reactions=reactions)
    exports = [_make_export([msg])]
    config = _make_config(tmp_path)
    state = _make_state()
    state.emoji_map["111"] = "already_migrated"
    events: list[Any] = []

    mock_create = AsyncMock(return_value={"_id": "new_id"})
    with (
        patch("discord_ferry.migrator.emoji.upload_with_cache", new=AsyncMock()),
        patch("discord_ferry.migrator.emoji.api_create_emoji", new=mock_create),
        patch("discord_ferry.migrator.emoji.asyncio.sleep", new=AsyncMock()),
    ):
        await run_emoji(config, state, exports, events.append)

    mock_create.assert_not_called()
    # Map should still contain original value.
    assert state.emoji_map["111"] == "already_migrated"


async def test_run_emoji_http_image_url_skipped(tmp_path: Path) -> None:
    """Skips emoji whose image_url starts with http (not downloaded)."""
    reactions = [
        DCEReaction(
            emoji=DCEEmoji(
                id="222",
                name="cloud",
                image_url="https://cdn.discordapp.com/emojis/222.png",
            ),
            count=1,
        )
    ]
    msg = _make_message(reactions=reactions)
    exports = [_make_export([msg])]
    config = _make_config(tmp_path)
    state = _make_state()
    events: list[Any] = []

    mock_create = AsyncMock(return_value={"_id": "id"})
    with (
        patch("discord_ferry.migrator.emoji.upload_with_cache", new=AsyncMock()),
        patch("discord_ferry.migrator.emoji.api_create_emoji", new=mock_create),
        patch("discord_ferry.migrator.emoji.asyncio.sleep", new=AsyncMock()),
    ):
        await run_emoji(config, state, exports, events.append)

    mock_create.assert_not_called()
    assert "222" not in state.emoji_map
    warning_messages = [w["message"] for w in state.warnings]
    assert any("222" in m or "cloud" in m for m in warning_messages)


async def test_run_emoji_missing_file_skipped(tmp_path: Path) -> None:
    """Skips emoji whose image file does not exist on disk."""
    reactions = [
        DCEReaction(emoji=DCEEmoji(id="333", name="ghost", image_url="missing.png"), count=1)
    ]
    msg = _make_message(reactions=reactions)
    exports = [_make_export([msg])]
    config = _make_config(tmp_path)
    state = _make_state()
    events: list[Any] = []

    mock_create = AsyncMock(return_value={"_id": "id"})
    with (
        patch("discord_ferry.migrator.emoji.upload_with_cache", new=AsyncMock()),
        patch("discord_ferry.migrator.emoji.api_create_emoji", new=mock_create),
        patch("discord_ferry.migrator.emoji.asyncio.sleep", new=AsyncMock()),
    ):
        await run_emoji(config, state, exports, events.append)

    mock_create.assert_not_called()
    assert "333" not in state.emoji_map


async def test_run_emoji_api_error_logged(tmp_path: Path) -> None:
    """Logs to state.errors when api_create_emoji raises, and continues."""
    emoji_file = tmp_path / "boom.png"
    emoji_file.write_bytes(b"PNG")

    reactions = [DCEReaction(emoji=DCEEmoji(id="444", name="boom", image_url="boom.png"), count=1)]
    msg = _make_message(reactions=reactions)
    exports = [_make_export([msg])]
    config = _make_config(tmp_path)
    state = _make_state()
    events: list[Any] = []

    with (
        patch(
            "discord_ferry.migrator.emoji.upload_with_cache",
            new=AsyncMock(return_value="autumn_id"),
        ),
        patch(
            "discord_ferry.migrator.emoji.api_create_emoji",
            new=AsyncMock(side_effect=Exception("API exploded")),
        ),
        patch("discord_ferry.migrator.emoji.asyncio.sleep", new=AsyncMock()),
    ):
        await run_emoji(config, state, exports, events.append)

    assert len(state.errors) == 1
    assert "API exploded" in state.errors[0]["message"]
    error_events = [e for e in events if e.status == "error"]
    assert error_events


async def test_run_emoji_full_happy_path(tmp_path: Path) -> None:
    """Full run: emoji from content and reaction both migrate successfully."""
    emoji_file_a = tmp_path / "partyA.png"
    emoji_file_a.write_bytes(b"PNG")
    emoji_file_b = tmp_path / "partyB.png"
    emoji_file_b.write_bytes(b"PNG")

    # Reaction message first so image_url is populated before content scanning.
    # Content reference to emoji ID "10" is then a duplicate that won't overwrite image_url.
    msg_reaction = _make_message(
        msg_id="m1",
        reactions=[
            DCEReaction(emoji=DCEEmoji(id="10", name="partyA", image_url="partyA.png"), count=1),
            DCEReaction(emoji=DCEEmoji(id="20", name="partyB", image_url="partyB.png"), count=2),
        ],
    )
    msg_content = _make_message(msg_id="m2", content="<:partyA:10>")

    exports = [_make_export([msg_reaction, msg_content])]
    config = _make_config(tmp_path)
    state = _make_state()
    events: list[Any] = []

    call_count = 0

    async def fake_create(
        session: Any,
        stoat_url: Any,
        token: Any,
        server_id: Any,
        name: Any,
        parent: Any,
    ) -> dict[str, str]:
        nonlocal call_count
        call_count += 1
        return {"_id": f"stoat_{call_count}"}

    with (
        patch(
            "discord_ferry.migrator.emoji.upload_with_cache",
            new=AsyncMock(return_value="autumn_id"),
        ),
        patch("discord_ferry.migrator.emoji.api_create_emoji", new=fake_create),
        patch("discord_ferry.migrator.emoji.asyncio.sleep", new=AsyncMock()),
    ):
        await run_emoji(config, state, exports, events.append)

    assert len(state.emoji_map) == 2
    completed = [e for e in events if e.status == "completed"]
    assert completed


# ---------------------------------------------------------------------------
# Bug 6: Animated emoji warning
# ---------------------------------------------------------------------------


async def test_run_emoji_animated_warning(tmp_path: Path) -> None:
    """Animated emoji triggers a warning event and state.warnings entry."""
    emoji_file = tmp_path / "spin.gif"
    emoji_file.write_bytes(b"GIF89a")

    reactions = [
        DCEReaction(
            emoji=DCEEmoji(id="555", name="spin", is_animated=True, image_url="spin.gif"),
            count=1,
        )
    ]
    msg = _make_message(reactions=reactions)
    exports = [_make_export([msg])]
    config = _make_config(tmp_path)
    state = _make_state()
    events: list[Any] = []

    with (
        patch(
            "discord_ferry.migrator.emoji.upload_with_cache",
            new=AsyncMock(return_value="autumn_id"),
        ),
        patch(
            "discord_ferry.migrator.emoji.api_create_emoji",
            new=AsyncMock(return_value={"_id": "stoat_555"}),
        ),
        patch("discord_ferry.migrator.emoji.asyncio.sleep", new=AsyncMock()),
    ):
        await run_emoji(config, state, exports, events.append)

    # Emoji should still be created.
    assert state.emoji_map["555"] == "autumn_id"

    # Warning about animation loss should be emitted.
    warning_events = [e for e in events if e.status == "warning"]
    assert any("animated" in e.message.lower() for e in warning_events)
    assert any("animated" in w["message"].lower() for w in state.warnings)


# ---------------------------------------------------------------------------
# Embed emoji discovery (Source 3)
# ---------------------------------------------------------------------------


def test_emoji_discovered_in_embed_description() -> None:
    """Emoji in embed description is extracted by _extract_emoji_from_content."""
    results = _extract_emoji_from_content("Check <:custom:12345> here")
    assert len(results) == 1
    assert results[0] == ("12345", "custom", False)


def test_emoji_discovered_in_embed_field_value() -> None:
    """Emoji in an embed field value is extracted by _extract_emoji_from_content."""
    results = _extract_emoji_from_content("Value: <:star:67890>")
    assert len(results) == 1
    assert results[0] == ("67890", "star", False)


def test_animated_emoji_in_embed() -> None:
    """Animated emoji in embed text is detected with is_animated=True."""
    results = _extract_emoji_from_content("Dance! <a:dance:98765>")
    assert len(results) == 1
    emoji_id, name, is_animated = results[0]
    assert emoji_id == "98765"
    assert name == "dance"
    assert is_animated is True


async def test_run_emoji_discovers_embed_description(tmp_path: Path) -> None:
    """run_emoji discovers emoji in embed description fields."""
    emoji_file = tmp_path / "embed_emoji.png"
    emoji_file.write_bytes(b"PNG")

    msg = _make_message(msg_id="m1", content="")
    # Manually attach an embed with an emoji in description.
    msg.embeds.append(
        {
            "description": "<:embed_emoji:11111>",
            "title": "",
            "fields": [],
        }
    )

    exports = [_make_export([msg])]
    config = _make_config(tmp_path)
    state = _make_state()
    events: list[Any] = []

    # emoji discovered via embed has no image_url — expect it to be skipped (warning).
    with (
        patch("discord_ferry.migrator.emoji.upload_with_cache", new=AsyncMock()),
        patch(
            "discord_ferry.migrator.emoji.api_create_emoji",
            new=AsyncMock(return_value={"_id": "stoat_embed"}),
        ),
        patch("discord_ferry.migrator.emoji.asyncio.sleep", new=AsyncMock()),
    ):
        await run_emoji(config, state, exports, events.append)

    # The emoji should have been discovered (seen in warnings as skipped).
    warning_messages = " ".join(w["message"] for w in state.warnings)
    assert "embed_emoji" in warning_messages or "11111" in warning_messages


async def test_run_emoji_discovers_embed_field_value(tmp_path: Path) -> None:
    """run_emoji discovers emoji that appear inside embed field values."""
    msg = _make_message(msg_id="m1", content="")
    msg.embeds.append(
        {
            "description": "",
            "title": "",
            "fields": [{"name": "Score", "value": "<:trophy:22222>", "inline": False}],
        }
    )

    exports = [_make_export([msg])]
    config = _make_config(tmp_path)
    state = _make_state()
    events: list[Any] = []

    with (
        patch("discord_ferry.migrator.emoji.upload_with_cache", new=AsyncMock()),
        patch(
            "discord_ferry.migrator.emoji.api_create_emoji",
            new=AsyncMock(return_value={"_id": "stoat_embed2"}),
        ),
        patch("discord_ferry.migrator.emoji.asyncio.sleep", new=AsyncMock()),
    ):
        await run_emoji(config, state, exports, events.append)

    warning_messages = " ".join(w["message"] for w in state.warnings)
    assert "trophy" in warning_messages or "22222" in warning_messages


async def test_run_emoji_static_no_animation_warning(tmp_path: Path) -> None:
    """Static emoji does NOT trigger an animation warning."""
    emoji_file = tmp_path / "smile.png"
    emoji_file.write_bytes(b"PNG")

    reactions = [
        DCEReaction(
            emoji=DCEEmoji(id="666", name="smile", is_animated=False, image_url="smile.png"),
            count=1,
        )
    ]
    msg = _make_message(reactions=reactions)
    exports = [_make_export([msg])]
    config = _make_config(tmp_path)
    state = _make_state()
    events: list[Any] = []

    with (
        patch(
            "discord_ferry.migrator.emoji.upload_with_cache",
            new=AsyncMock(return_value="autumn_id"),
        ),
        patch(
            "discord_ferry.migrator.emoji.api_create_emoji",
            new=AsyncMock(return_value={"_id": "stoat_666"}),
        ),
        patch("discord_ferry.migrator.emoji.asyncio.sleep", new=AsyncMock()),
    ):
        await run_emoji(config, state, exports, events.append)

    assert state.emoji_map["666"] == "autumn_id"
    # No animation warnings.
    assert not any("animated" in w["message"].lower() for w in state.warnings)


# ---------------------------------------------------------------------------
# Batch 4 (S2): discovery upgrade-in-place + (S3) usage-ranked truncation
# ---------------------------------------------------------------------------


def _emoji_patches() -> Any:
    """The standard run_emoji mock trio (upload + create + sleep)."""
    return (
        patch(
            "discord_ferry.migrator.emoji.upload_with_cache",
            new=AsyncMock(return_value="autumn_id"),
        ),
        patch(
            "discord_ferry.migrator.emoji.api_create_emoji",
            new=AsyncMock(return_value={"_id": "stoat_id"}),
        ),
        patch("discord_ferry.migrator.emoji.asyncio.sleep", new=AsyncMock()),
    )


async def test_emoji_content_first_reaction_later_upgrades(tmp_path: Path) -> None:
    """SC-6: a content-first emoji (image_url='') is upgraded by a later reaction with a real
    path, so it uploads and enters emoji_map."""
    (tmp_path / "smile.png").write_bytes(b"PNG")
    msg1 = _make_message(msg_id="m1", content="<:smile:123>")  # content source -> image_url=''
    msg2 = _make_message(
        msg_id="m2",
        reactions=[
            DCEReaction(emoji=DCEEmoji(id="123", name="smile", image_url="smile.png"), count=1)
        ],
    )
    exports = [_make_export([msg1, msg2])]
    config = _make_config(tmp_path)
    state = _make_state()
    up, cr, sl = _emoji_patches()
    with up, cr, sl:
        await run_emoji(config, state, exports, [].append)
    assert "123" in state.emoji_map  # upgraded -> uploaded -> mapped


async def test_emoji_reaction_first_content_later_no_downgrade(tmp_path: Path) -> None:
    """SC-7: a reaction-first emoji (real path) is NOT downgraded by a later content occurrence."""
    (tmp_path / "smile.png").write_bytes(b"PNG")
    msg1 = _make_message(
        msg_id="m1",
        reactions=[
            DCEReaction(emoji=DCEEmoji(id="123", name="smile", image_url="smile.png"), count=1)
        ],
    )
    msg2 = _make_message(msg_id="m2", content="<:smile:123>")  # later, image_url=''
    exports = [_make_export([msg1, msg2])]
    config = _make_config(tmp_path)
    state = _make_state()
    up, cr, sl = _emoji_patches()
    with up, cr, sl:
        await run_emoji(config, state, exports, [].append)
    assert "123" in state.emoji_map  # real path retained, no downgrade


async def test_emoji_content_only_still_skipped(tmp_path: Path) -> None:
    """SC-8: an emoji that only ever appears in content (image_url='') is still skipped."""
    msg = _make_message(content="<:ghost:123>")  # never a reaction -> no real path
    exports = [_make_export([msg])]
    config = _make_config(tmp_path)
    state = _make_state()
    up, cr, sl = _emoji_patches()
    with up, cr, sl:
        await run_emoji(config, state, exports, [].append)
    assert "123" not in state.emoji_map


async def test_emoji_cap_ranks_by_usage(tmp_path: Path) -> None:
    """SC-9: the cap keeps the MOST-used emoji, not the lexicographic-first."""
    for fn in ("a.png", "b.png", "c.png"):
        (tmp_path / fn).write_bytes(b"PNG")
    msgs = [
        _make_message(msg_id="m1", content="<:dup:999> <:dup:999>"),  # 999 used 2x (content)
        _make_message(
            msg_id="m2",
            reactions=[
                DCEReaction(emoji=DCEEmoji(id="999", name="dup", image_url="a.png"), count=1)
            ],
        ),  # 999 -> 3 uses + real path
        _make_message(
            msg_id="m3",
            reactions=[DCEReaction(emoji=DCEEmoji(id="100", name="b", image_url="b.png"), count=1)],
        ),
        _make_message(
            msg_id="m4",
            reactions=[DCEReaction(emoji=DCEEmoji(id="200", name="c", image_url="c.png"), count=1)],
        ),
    ]
    exports = [_make_export(msgs)]
    config = dataclasses.replace(_make_config(tmp_path), max_emoji=2)
    state = _make_state()
    up, cr, sl = _emoji_patches()
    with up, cr, sl:
        await run_emoji(config, state, exports, [].append)
    assert "999" in state.emoji_map  # most-used survives despite sorting last lexicographically
    assert "200" not in state.emoji_map  # least-used (tie-break loser) dropped


async def test_emoji_cap_ranking_deterministic(tmp_path: Path) -> None:
    """SC-10: repeated runs keep the same subset (stable tie-break)."""
    for fn in ("a.png", "b.png", "c.png"):
        (tmp_path / fn).write_bytes(b"PNG")

    def _msgs() -> list[Any]:
        return [
            _make_message(msg_id="m1", content="<:dup:999> <:dup:999>"),
            _make_message(
                msg_id="m2",
                reactions=[
                    DCEReaction(emoji=DCEEmoji(id="999", name="dup", image_url="a.png"), count=1)
                ],
            ),
            _make_message(
                msg_id="m3",
                reactions=[
                    DCEReaction(emoji=DCEEmoji(id="100", name="b", image_url="b.png"), count=1)
                ],
            ),
            _make_message(
                msg_id="m4",
                reactions=[
                    DCEReaction(emoji=DCEEmoji(id="200", name="c", image_url="c.png"), count=1)
                ],
            ),
        ]

    config = dataclasses.replace(_make_config(tmp_path), max_emoji=2)
    keys = []
    for _ in range(2):
        state = _make_state()
        up, cr, sl = _emoji_patches()
        with up, cr, sl:
            await run_emoji(config, state, [_make_export(_msgs())], [].append)
        keys.append(sorted(state.emoji_map))
    assert keys[0] == keys[1]


async def test_emoji_cap_non_numeric_id_safe(tmp_path: Path) -> None:
    """SC-11: a non-numeric emoji id does not crash the usage sort (NEW fixture)."""
    (tmp_path / "w.png").write_bytes(b"PNG")
    (tmp_path / "n.png").write_bytes(b"PNG")
    msgs = [
        _make_message(
            msg_id="m1",
            reactions=[
                DCEReaction(emoji=DCEEmoji(id="weird_id", name="w", image_url="w.png"), count=1)
            ],
        ),
        _make_message(
            msg_id="m2",
            reactions=[
                DCEReaction(emoji=DCEEmoji(id="weird_id", name="w", image_url="w.png"), count=1)
            ],
        ),  # weird_id used 2x
        _make_message(
            msg_id="m3",
            reactions=[DCEReaction(emoji=DCEEmoji(id="100", name="n", image_url="n.png"), count=1)],
        ),  # 100 used 1x
    ]
    exports = [_make_export(msgs)]
    config = dataclasses.replace(_make_config(tmp_path), max_emoji=1)
    state = _make_state()
    up, cr, sl = _emoji_patches()
    with up, cr, sl:
        await run_emoji(config, state, exports, [].append)  # must NOT raise
    assert "weird_id" in state.emoji_map  # higher-used kept; sort handled mixed ids


async def test_emoji_truncation_warning_names_dropped(tmp_path: Path) -> None:
    """SC-12: the truncation warning surfaces the dropped emoji name."""
    (tmp_path / "k.png").write_bytes(b"PNG")
    (tmp_path / "d.png").write_bytes(b"PNG")
    msgs = [
        _make_message(
            msg_id="m1",
            reactions=[
                DCEReaction(emoji=DCEEmoji(id="100", name="keepme", image_url="k.png"), count=1)
            ],
        ),
        _make_message(
            msg_id="m2",
            reactions=[
                DCEReaction(emoji=DCEEmoji(id="100", name="keepme", image_url="k.png"), count=1)
            ],
        ),  # keepme used 2x
        _make_message(
            msg_id="m3",
            reactions=[
                DCEReaction(emoji=DCEEmoji(id="200", name="dropme", image_url="d.png"), count=1)
            ],
        ),  # dropme used 1x
    ]
    exports = [_make_export(msgs)]
    config = dataclasses.replace(_make_config(tmp_path), max_emoji=1)
    state = _make_state()
    events: list[Any] = []
    up, cr, sl = _emoji_patches()
    with up, cr, sl:
        await run_emoji(config, state, exports, events.append)
    warn = " ".join(w["message"] for w in state.warnings)
    assert "dropme" in warn  # the dropped emoji's name is surfaced


async def test_emoji_cap_prefers_uploadable_over_high_use_assetless(tmp_path: Path) -> None:
    """SC-9b (code-review #1): an asset-less high-use emoji (content-only, image_url='') must
    NOT take a kept slot ahead of an uploadable lower-use emoji — else the slot is wasted
    (skipped at upload) and a creatable emoji is dropped."""
    (tmp_path / "k.png").write_bytes(b"PNG")
    msgs = [
        # 999: content-only (no asset), used 3x — high uses but NOT uploadable.
        _make_message(msg_id="m1", content="<:big:999> <:big:999> <:big:999>"),
        # 100: a real reaction asset, used 1x — uploadable.
        _make_message(
            msg_id="m2",
            reactions=[DCEReaction(emoji=DCEEmoji(id="100", name="k", image_url="k.png"), count=1)],
        ),
    ]
    exports = [_make_export(msgs)]
    config = dataclasses.replace(_make_config(tmp_path), max_emoji=1)
    state = _make_state()
    up, cr, sl = _emoji_patches()
    with up, cr, sl:
        await run_emoji(config, state, exports, [].append)
    assert "100" in state.emoji_map  # uploadable kept despite fewer uses
    assert "999" not in state.emoji_map  # asset-less high-use one dropped (would be unuploadable)


async def test_upload_and_create_emoji_uploads_then_creates(tmp_path: Path) -> None:
    """SC-1.2 (unit): the shared helper uploads to Autumn then registers the emoji."""
    img = tmp_path / "emo.png"
    img.write_bytes(b"x")
    config = _make_config(tmp_path)
    state = _make_state()
    with (
        patch(
            "discord_ferry.migrator.emoji.upload_with_cache",
            new=AsyncMock(return_value="new_autumn"),
        ) as up,
        patch(
            "discord_ferry.migrator.emoji.api_create_emoji",
            new=AsyncMock(return_value={"_id": "new_autumn", "name": "smile"}),
        ) as cr,
    ):
        new_id, emoji_name = await upload_and_create_emoji(
            None, config, state, file_path=img, name="smile", used_names={}
        )
    assert new_id == "new_autumn"
    assert emoji_name == "smile"
    up.assert_awaited_once()
    cr.assert_awaited_once()
    # api_create_emoji(session, stoat_url, token, autumn_id, name, server_id)
    assert cr.await_args.args[3] == "new_autumn"
    assert cr.await_args.args[5] == "srv1"


def test_messages_using_emoji_finds_content_not_reaction() -> None:
    """SC-2.1/2.5: content uses are found; a reaction-only use is excluded."""
    m1 = _make_message(msg_id="m1", content="hi <:smile:123> there")
    m2 = _make_message(msg_id="m2", content="plain text")
    m3 = _make_message(
        msg_id="m3",
        reactions=[DCEReaction(emoji=DCEEmoji(id="123", name="smile", image_url="e.png"), count=1)],
    )
    exp = _make_export([m1, m2, m3])
    hits = list(messages_using_emoji([exp], "123"))
    assert [(chan, m.id) for chan, m in hits] == [("ch1", "m1")]


def test_messages_using_emoji_ignores_other_emoji() -> None:
    """A message using a different custom emoji is not yielded."""
    m1 = _make_message(msg_id="m1", content="<:other:999>")
    exp = _make_export([m1])
    assert list(messages_using_emoji([exp], "123")) == []


def test_find_emoji_in_exports_recovers_name_and_image() -> None:
    """The recreate step needs the name and image the id map never stored."""
    m = _make_message(
        msg_id="m1",
        content="<:smile:123>",
        reactions=[
            DCEReaction(emoji=DCEEmoji(id="123", name="smile", image_url="smile.png"), count=1)
        ],
    )
    exp = _make_export([m])
    rec = find_emoji_in_exports([exp], "123")
    assert rec is not None
    assert rec["name"] == "smile"
    assert rec["image_url"] == "smile.png"
    assert rec["is_animated"] is False


def test_find_emoji_in_exports_none_when_absent() -> None:
    exp = _make_export([_make_message(content="plain text")])
    assert find_emoji_in_exports([exp], "123") is None



async def test_run_emoji_records_server_echoed_name(tmp_path: Path) -> None:
    """The emoji name is recorded from the server response, not the input name."""
    img = tmp_path / "smile.png"
    img.write_bytes(b"PNG")
    msg = _make_message(
        reactions=[
            DCEReaction(
                emoji=DCEEmoji(id="999", name="smile", image_url="smile.png"), count=1
            )
        ]
    )
    exports = [_make_export([msg])]
    config = _make_config(tmp_path)
    state = _make_state()

    with (
        patch(
            "discord_ferry.migrator.emoji.upload_with_cache",
            new=AsyncMock(return_value="autumn_file_1"),
        ),
        patch(
            "discord_ferry.migrator.emoji.api_create_emoji",
            new=AsyncMock(return_value={"_id": "autumn_file_1", "name": "party"}),
        ),
        patch("discord_ferry.migrator.emoji.asyncio.sleep", new=AsyncMock()),
    ):
        await run_emoji(config, state, exports, lambda e: None)

    assert state.emoji_map == {"999": "autumn_file_1"}
    assert state.created_emoji_names == {"999": "party"}


async def test_run_emoji_falls_back_to_sanitized_name(tmp_path: Path) -> None:
    """When the server response carries no name, the sanitized input name is recorded."""
    img = tmp_path / "smile.png"
    img.write_bytes(b"PNG")
    msg = _make_message(
        reactions=[
            DCEReaction(
                emoji=DCEEmoji(id="999", name="smile", image_url="smile.png"), count=1
            )
        ]
    )
    exports = [_make_export([msg])]
    config = _make_config(tmp_path)
    state = _make_state()

    with (
        patch(
            "discord_ferry.migrator.emoji.upload_with_cache",
            new=AsyncMock(return_value="autumn_file_1"),
        ),
        patch(
            "discord_ferry.migrator.emoji.api_create_emoji",
            new=AsyncMock(return_value={"_id": "autumn_file_1"}),
        ),
        patch("discord_ferry.migrator.emoji.asyncio.sleep", new=AsyncMock()),
    ):
        await run_emoji(config, state, exports, lambda e: None)

    assert state.created_emoji_names == {"999": "smile"}
