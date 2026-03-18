"""Tests for the avatar pre-flight phase (Phase 7.5)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import aiohttp
from aioresponses import aioresponses

from discord_ferry.config import FerryConfig
from discord_ferry.migrator.avatars import run_avatars
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
        "upload_delay": 0.0,
        "output_dir": tmp_path / "output",
    }
    defaults.update(overrides)
    return FerryConfig(**defaults)


def _make_state() -> MigrationState:
    state = MigrationState()
    state.stoat_server_id = "srv1"
    state.autumn_url = AUTUMN_URL
    return state


def _make_author(
    author_id: str = "user1",
    name: str = "Alice",
    avatar_url: str = "",
) -> DCEAuthor:
    return DCEAuthor(id=author_id, name=name, avatar_url=avatar_url)


def _make_message(
    msg_id: str = "msg1",
    author: DCEAuthor | None = None,
) -> DCEMessage:
    return DCEMessage(
        id=msg_id,
        type="Default",
        timestamp="2024-01-01T00:00:00Z",
        content="hello",
        author=author or _make_author(),
    )


def _make_export(messages: list[DCEMessage] | None = None) -> DCEExport:
    return DCEExport(
        guild=DCEGuild(id="guild1", name="Test"),
        channel=DCEChannel(id="ch1", type=0, name="general"),
        messages=messages or [],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_local_avatars_uploaded_and_cached(tmp_path: Path) -> None:
    """Local avatar files are uploaded to Autumn and cached in state.avatar_cache."""
    # Create two local avatar files
    avatar1 = tmp_path / "avatar_user1.webp"
    avatar1.write_bytes(b"RIFF\x00\x00\x00\x00WEBP")
    avatar2 = tmp_path / "avatar_user2.webp"
    avatar2.write_bytes(b"RIFF\x00\x00\x00\x00WEBP")

    author1 = _make_author("user1", "Alice", avatar_url="avatar_user1.webp")
    author2 = _make_author("user2", "Bob", avatar_url="avatar_user2.webp")

    export1 = _make_export([_make_message("m1", author1)])
    export2 = _make_export([_make_message("m2", author2)])

    config = _make_config(tmp_path)
    state = _make_state()
    events: list[MigrationEvent] = []

    with patch(
        "discord_ferry.migrator.avatars.upload_with_cache",
        new=AsyncMock(side_effect=["autumn_av1", "autumn_av2"]),
    ) as mock_upload:
        await run_avatars(config, state, [export1, export2], events.append)

    assert state.avatar_cache == {"user1": "autumn_av1", "user2": "autumn_av2"}
    assert mock_upload.call_count == 2
    completed = [e for e in events if e.status == "completed"]
    assert completed
    assert "2" in completed[-1].message  # "Uploaded 2 of 2"


async def test_remote_avatar_downloaded_and_uploaded(tmp_path: Path) -> None:
    """Remote avatar URL is downloaded, then uploaded to Autumn, then cached."""
    author = _make_author("user1", "Alice", avatar_url="https://cdn.example.com/avatars/abc.webp")
    export = _make_export([_make_message("m1", author)])
    config = _make_config(tmp_path)
    state = _make_state()
    events: list[MigrationEvent] = []

    async with aiohttp.ClientSession() as session:
        config.session = session
        with aioresponses() as mocked:
            # Mock the remote avatar GET
            mocked.get(
                "https://cdn.example.com/avatars/abc.webp",
                body=b"RIFF\x00\x00\x00\x00WEBP",
                content_type="image/webp",
            )
            with patch(
                "discord_ferry.migrator.avatars.upload_with_cache",
                new=AsyncMock(return_value="autumn_av1"),
            ) as mock_upload:
                await run_avatars(config, state, [export], events.append)

    assert state.avatar_cache == {"user1": "autumn_av1"}
    assert mock_upload.call_count == 1

    # Verify the downloaded file exists
    dl_dir = tmp_path / "output" / "avatars"
    assert dl_dir.exists()
    downloaded_files = list(dl_dir.iterdir())
    assert len(downloaded_files) == 1
    assert "user1" in downloaded_files[0].name


async def test_remote_non_image_content_type_rejected(tmp_path: Path) -> None:
    """Remote URL returning non-image Content-Type is rejected (not cached)."""
    author = _make_author("user1", "Alice", avatar_url="https://cdn.example.com/error.html")
    export = _make_export([_make_message("m1", author)])
    config = _make_config(tmp_path)
    state = _make_state()
    events: list[MigrationEvent] = []

    async with aiohttp.ClientSession() as session:
        config.session = session
        with aioresponses() as mocked:
            mocked.get(
                "https://cdn.example.com/error.html",
                body=b"<html>Error</html>",
                content_type="text/html",
            )
            with patch(
                "discord_ferry.migrator.avatars.upload_with_cache",
                new=AsyncMock(return_value="autumn_av1"),
            ) as mock_upload:
                await run_avatars(config, state, [export], events.append)

    assert "user1" not in state.avatar_cache
    mock_upload.assert_not_called()
    # Should have a warning
    assert len(state.warnings) >= 1
    assert any(
        "content" in w["message"].lower() or "image" in w["message"].lower() for w in state.warnings
    )


async def test_remote_download_timeout_nonfatal(tmp_path: Path) -> None:
    """Timeout during remote avatar download logs warning, phase continues."""
    author1 = _make_author("user1", "Alice", avatar_url="https://cdn.example.com/slow.webp")
    author2 = _make_author("user2", "Bob", avatar_url="avatar_user2.webp")
    # Create local file for second author
    avatar2 = tmp_path / "avatar_user2.webp"
    avatar2.write_bytes(b"RIFF\x00\x00\x00\x00WEBP")

    export = _make_export([_make_message("m1", author1), _make_message("m2", author2)])
    config = _make_config(tmp_path)
    state = _make_state()
    events: list[MigrationEvent] = []

    async with aiohttp.ClientSession() as session:
        config.session = session
        with aioresponses() as mocked:
            mocked.get(
                "https://cdn.example.com/slow.webp",
                exception=TimeoutError("Connection timed out"),
            )
            with patch(
                "discord_ferry.migrator.avatars.upload_with_cache",
                new=AsyncMock(return_value="autumn_av2"),
            ):
                await run_avatars(config, state, [export], events.append)

    # user1 failed (timeout) but user2 succeeded
    assert "user1" not in state.avatar_cache
    assert state.avatar_cache.get("user2") == "autumn_av2"
    # Phase completed (did not crash)
    assert any(e.status == "completed" for e in events)
    # Warning logged for user1
    assert len(state.warnings) >= 1


async def test_already_cached_avatars_skipped(tmp_path: Path) -> None:
    """Authors already in state.avatar_cache are not re-uploaded."""
    avatar1 = tmp_path / "avatar_user1.webp"
    avatar1.write_bytes(b"RIFF\x00\x00\x00\x00WEBP")

    author = _make_author("user1", "Alice", avatar_url="avatar_user1.webp")
    export = _make_export([_make_message("m1", author)])
    config = _make_config(tmp_path)
    state = _make_state()
    state.avatar_cache["user1"] = "already_cached_id"
    events: list[MigrationEvent] = []

    with patch(
        "discord_ferry.migrator.avatars.upload_with_cache",
        new=AsyncMock(return_value="new_id"),
    ) as mock_upload:
        await run_avatars(config, state, [export], events.append)

    mock_upload.assert_not_called()
    assert state.avatar_cache["user1"] == "already_cached_id"


async def test_empty_avatar_url_filtered(tmp_path: Path) -> None:
    """Authors with empty avatar_url are filtered out and not processed."""
    author = _make_author("user1", "Alice", avatar_url="")
    export = _make_export([_make_message("m1", author)])
    config = _make_config(tmp_path)
    state = _make_state()
    events: list[MigrationEvent] = []

    with patch(
        "discord_ferry.migrator.avatars.upload_with_cache",
        new=AsyncMock(return_value="autumn_av1"),
    ) as mock_upload:
        await run_avatars(config, state, [export], events.append)

    mock_upload.assert_not_called()
    assert "user1" not in state.avatar_cache
    # Phase should still complete
    completed = [e for e in events if e.status == "completed"]
    assert completed


async def test_all_avatars_fail_completes_with_summary(tmp_path: Path) -> None:
    """When all remote downloads fail, phase completes with '0 of N' summary."""
    author1 = _make_author("user1", "Alice", avatar_url="https://cdn.example.com/a.webp")
    author2 = _make_author("user2", "Bob", avatar_url="https://cdn.example.com/b.webp")
    export = _make_export([_make_message("m1", author1), _make_message("m2", author2)])
    config = _make_config(tmp_path)
    state = _make_state()
    events: list[MigrationEvent] = []

    async with aiohttp.ClientSession() as session:
        config.session = session
        with aioresponses() as mocked:
            mocked.get(
                "https://cdn.example.com/a.webp",
                exception=TimeoutError("timeout"),
            )
            mocked.get(
                "https://cdn.example.com/b.webp",
                exception=TimeoutError("timeout"),
            )
            await run_avatars(config, state, [export], events.append)

    assert len(state.avatar_cache) == 0
    completed = [e for e in events if e.status == "completed"]
    assert completed
    assert "0" in completed[-1].message  # "Uploaded 0 of 2"
    assert "2" in completed[-1].message


async def test_empty_export_no_error(tmp_path: Path) -> None:
    """Export with zero messages causes no error — phase completes immediately."""
    export = _make_export([])
    config = _make_config(tmp_path)
    state = _make_state()
    events: list[MigrationEvent] = []

    await run_avatars(config, state, [export], events.append)

    completed = [e for e in events if e.status == "completed"]
    assert completed
    assert "no unique avatars" in completed[-1].message.lower() or "0" in completed[-1].message


async def test_duplicate_authors_across_exports(tmp_path: Path) -> None:
    """Same author appearing in multiple exports is uploaded only once."""
    avatar1 = tmp_path / "avatar_user1.webp"
    avatar1.write_bytes(b"RIFF\x00\x00\x00\x00WEBP")

    author = _make_author("user1", "Alice", avatar_url="avatar_user1.webp")
    export1 = _make_export([_make_message("m1", author)])
    export2 = _make_export([_make_message("m2", author)])
    config = _make_config(tmp_path)
    state = _make_state()
    events: list[MigrationEvent] = []

    with patch(
        "discord_ferry.migrator.avatars.upload_with_cache",
        new=AsyncMock(return_value="autumn_av1"),
    ) as mock_upload:
        await run_avatars(config, state, [export1, export2], events.append)

    assert mock_upload.call_count == 1
    assert state.avatar_cache == {"user1": "autumn_av1"}
