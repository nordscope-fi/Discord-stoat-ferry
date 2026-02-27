"""Tests for the emoji migration phase (Phase 7)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from discord_ferry.config import FerryConfig
from discord_ferry.migrator.emoji import _extract_emoji_from_content, run_emoji
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

    # Only one emoji should be created.
    assert state.emoji_map == {"999": "stoat_emoji_1"}


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
    assert state.emoji_map["555"] == "stoat_555"

    # Warning about animation loss should be emitted.
    warning_events = [e for e in events if e.status == "warning"]
    assert any("animated" in e.message.lower() for e in warning_events)
    assert any("animated" in w["message"].lower() for w in state.warnings)


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

    assert state.emoji_map["666"] == "stoat_666"
    # No animation warnings.
    assert not any("animated" in w["message"].lower() for w in state.warnings)
