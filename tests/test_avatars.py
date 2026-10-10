"""Tests for the avatar pre-flight phase (Phase 7.5)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aioresponses import aioresponses

from discord_ferry.config import FerryConfig
from discord_ferry.migrator.avatars import _download_remote_avatar, run_avatars
from discord_ferry.parser.models import (
    DCEAuthor,
    DCEChannel,
    DCEExport,
    DCEGuild,
    DCEMessage,
)
from discord_ferry.state import MigrationState
from tests.local_cdn import ENDLESS_LIMIT, LocalCDN, RewritingSession, local_cdn

if TYPE_CHECKING:
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
    download_warnings = [
        warning for warning in state.warnings if warning.get("type") == "avatar_download_failed"
    ]
    assert len(download_warnings) == 1
    assert "non-image Content-Type 'text/html'" in download_warnings[0]["message"]


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
    download_warnings = [
        warning for warning in state.warnings if warning.get("type") == "avatar_download_failed"
    ]
    assert len(download_warnings) == 1
    assert "Connection timed out" in download_warnings[0]["message"]


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


# ---------------------------------------------------------------------------
# Orphan upload tracking (S5)
# ---------------------------------------------------------------------------


async def test_avatar_upload_tracked_and_referenced(tmp_path: Path) -> None:
    """Successful avatar upload is tracked in autumn_uploads AND marked as referenced."""
    avatar1 = tmp_path / "avatar_user1.webp"
    avatar1.write_bytes(b"RIFF\x00\x00\x00\x00WEBP")

    author = _make_author("user1", "Alice", avatar_url="avatar_user1.webp")
    export = _make_export([_make_message("m1", author)])
    config = _make_config(tmp_path)
    state = _make_state()
    events: list[MigrationEvent] = []

    with patch(
        "discord_ferry.migrator.avatars.upload_with_cache",
        new=AsyncMock(return_value="autumn_av1"),
    ):
        await run_avatars(config, state, [export], events.append)

    # Avatar is in avatar_cache
    assert state.avatar_cache == {"user1": "autumn_av1"}
    # Avatar is tracked in autumn_uploads
    assert "autumn_av1" in state.autumn_uploads
    assert state.autumn_uploads["autumn_av1"] == "user1"
    # Avatar is immediately referenced (avatars are always used via masquerade)
    assert "autumn_av1" in state.referenced_autumn_ids


# ---------------------------------------------------------------------------
# Batch 10 / S3 — time-based checkpoint cadence (replaces the %10 count-gate)
# ---------------------------------------------------------------------------


def _three_avatar_export(tmp_path: Path) -> DCEExport:
    """Three local-avatar authors in a single export (sub-10, exercises the throttle)."""
    messages = []
    for i in range(3):
        f = tmp_path / f"avatar_u{i}.webp"
        f.write_bytes(b"RIFF\x00\x00\x00\x00WEBP")
        author = _make_author(f"u{i}", f"User{i}", avatar_url=f"avatar_u{i}.webp")
        messages.append(_make_message(f"m{i}", author))
    return _make_export(messages)


async def test_avatars_time_throttle_saves_sub_ten_run(tmp_path: Path) -> None:
    """SC-10: a <10-avatar run checkpoints mid-loop once the monotonic clock crosses 5s."""
    import itertools
    from unittest.mock import Mock

    export = _three_avatar_export(tmp_path)
    config = _make_config(tmp_path)
    state = _make_state()
    save_mock = Mock()
    # Every monotonic reading jumps 100s ahead, so each per-iteration check crosses the
    # 5s threshold (robust even if other code also reads the clock).
    monotonic = Mock(side_effect=itertools.count(0, 100))

    with (
        patch(
            "discord_ferry.migrator.avatars.upload_with_cache",
            new=AsyncMock(side_effect=["a0", "a1", "a2"]),
        ),
        patch("discord_ferry.migrator.avatars.save_state", save_mock),
        patch("discord_ferry.migrator.avatars.time.monotonic", monotonic),
    ):
        await run_avatars(config, state, [export], lambda e: None)

    assert save_mock.call_count >= 1  # checkpointed mid-loop, not only at engine phase-end
    assert set(state.avatar_cache.values()) == {"a0", "a1", "a2"}


async def test_avatars_time_throttle_no_premature_save(tmp_path: Path) -> None:
    """SC-11: no mid-loop save while the clock has not advanced past 5s (no write storm)."""
    from unittest.mock import Mock

    export = _three_avatar_export(tmp_path)
    config = _make_config(tmp_path)
    state = _make_state()
    save_mock = Mock()
    monotonic = Mock(return_value=1.0)  # constant — never crosses +5.0

    with (
        patch(
            "discord_ferry.migrator.avatars.upload_with_cache",
            new=AsyncMock(side_effect=["a0", "a1", "a2"]),
        ),
        patch("discord_ferry.migrator.avatars.save_state", save_mock),
        patch("discord_ferry.migrator.avatars.time.monotonic", monotonic),
    ):
        await run_avatars(config, state, [export], lambda e: None)

    assert save_mock.call_count == 0
    assert set(state.avatar_cache.values()) == {"a0", "a1", "a2"}


# ---------------------------------------------------------------------------
# Dry-run guard (#438)
# ---------------------------------------------------------------------------


async def test_run_avatars_dry_run_maps_without_upload(tmp_path: Path) -> None:
    """Dry-run populates avatar_cache with synthetic IDs, no network calls."""
    author1 = _make_author("user1", "Alice", avatar_url="avatar_user1.webp")
    author2 = _make_author("user2", "Bob", avatar_url="avatar_user2.webp")
    export = _make_export([_make_message("m1", author1), _make_message("m2", author2)])
    config = _make_config(tmp_path, dry_run=True)
    state = _make_state()
    events: list[MigrationEvent] = []

    with patch(
        "discord_ferry.migrator.avatars.upload_with_cache",
        new=AsyncMock(return_value="should-not-be-called"),
    ) as mock_upload:
        await run_avatars(config, state, [export], events.append)

    mock_upload.assert_not_called()
    assert state.avatar_cache == {
        "user1": "dry-avatar-user1",
        "user2": "dry-avatar-user2",
    }
    assert state.autumn_uploads == {}
    assert state.referenced_autumn_ids == set()
    completed = [e for e in events if e.status == "completed"]
    assert completed
    assert "[DRY RUN]" in completed[-1].message
    assert "2" in completed[-1].message


async def test_run_avatars_dry_run_skips_cached(tmp_path: Path) -> None:
    """Dry-run does not re-map authors already in avatar_cache."""
    author1 = _make_author("user1", "Alice", avatar_url="avatar_user1.webp")
    author2 = _make_author("user2", "Bob", avatar_url="avatar_user2.webp")
    export = _make_export([_make_message("m1", author1), _make_message("m2", author2)])
    config = _make_config(tmp_path, dry_run=True)
    state = _make_state()
    state.avatar_cache["user1"] = "already-cached"
    events: list[MigrationEvent] = []

    await run_avatars(config, state, [export], events.append)

    assert state.avatar_cache["user1"] == "already-cached"
    assert state.avatar_cache["user2"] == "dry-avatar-user2"
    completed = [e for e in events if e.status == "completed"]
    assert completed
    assert "1" in completed[-1].message


# ---------------------------------------------------------------------------
# Download failure diagnostics (#438)
# ---------------------------------------------------------------------------


async def test_download_failure_includes_specific_reason(tmp_path: Path) -> None:
    """Download failure warning includes specific reason, not generic text."""
    author = _make_author("user1", "Alice", avatar_url="https://cdn.example.com/gone.webp")
    export = _make_export([_make_message("m1", author)])
    config = _make_config(tmp_path)
    state = _make_state()
    events: list[MigrationEvent] = []

    async with aiohttp.ClientSession() as session:
        config.session = session
        with aioresponses() as mocked:
            mocked.get("https://cdn.example.com/gone.webp", status=404)
            with patch(
                "discord_ferry.migrator.avatars.upload_with_cache",
                new=AsyncMock(return_value="autumn_av1"),
            ) as mock_upload:
                await run_avatars(config, state, [export], events.append)

    mock_upload.assert_not_called()
    assert state.warnings == [
        {
            "phase": "avatars",
            "type": "avatar_download_failed",
            "message": "Failed to download avatar for Alice (HTTP 404)",
        }
    ]


# ---------------------------------------------------------------------------
# Chunk 4 (#1025): avatar-phase containment and downloader author-id policy
# ---------------------------------------------------------------------------


class _FakeContent:
    async def iter_chunked(self, size: int) -> Any:
        yield b"PNG-DUMMY-BYTES"


class _FakeResp:
    status = 200
    headers = {"Content-Type": "image/png"}
    content_length = None
    content = _FakeContent()

    async def __aenter__(self) -> _FakeResp:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    def get(self, url: object, timeout: object = None, allow_redirects: bool = True) -> _FakeResp:
        return _FakeResp()


class _FakeSessionCM:
    async def __aenter__(self) -> _FakeSession:
        return _FakeSession()

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _recording_uploads(uploads: list[tuple[str, Path]]) -> Any:
    async def _upload(
        session: Any,
        autumn_url: Any,
        tag: Any,
        path: Any,
        token: Any,
        cache: Any,
        delay: Any,
        **kw: Any,
    ) -> str:
        uploads.append((tag, Path(path)))
        return f"autumn-{len(uploads)}"

    return _upload


def _export_with_author(author: DCEAuthor) -> DCEExport:
    return DCEExport(
        guild=DCEGuild(id="g1", name="G"),
        channel=DCEChannel(id="c1", type=0, name="general"),
        messages=[_make_message("m1", author=author)],
    )


async def test_avatar_phase_local_escape_warns_and_skips(tmp_path: Path) -> None:
    marker = tmp_path.parent / (tmp_path.name + "-marker.png")
    marker.write_bytes(b"MARKER")
    author = _make_author(avatar_url=f"../{marker.name}")
    config = _make_config(tmp_path)
    state = _make_state()
    uploads: list[tuple[str, Path]] = []
    events: list[MigrationEvent] = []
    with patch("discord_ferry.migrator.avatars.upload_with_cache", _recording_uploads(uploads)):
        await run_avatars(config, state, [_export_with_author(author)], events.append)

    assert uploads == []
    unsafe = [w for w in state.warnings if w.get("type") == "unsafe_media_path"]
    assert len(unsafe) == 1
    assert unsafe[0].get("phase") == "avatars"
    assert marker.read_bytes() == b"MARKER"
    completed = [e for e in events if e.status == "completed"]
    assert completed and "1 failed" in completed[0].message


async def test_avatar_phase_in_folder_control_uploads(tmp_path: Path) -> None:
    (tmp_path / "av.png").write_bytes(b"x")
    author = _make_author(avatar_url="av.png")
    config = _make_config(tmp_path)
    state = _make_state()
    uploads: list[tuple[str, Path]] = []
    with patch("discord_ferry.migrator.avatars.upload_with_cache", _recording_uploads(uploads)):
        await run_avatars(config, state, [_export_with_author(author)], lambda e: None)

    assert [tag for tag, _ in uploads] == ["avatars"]
    assert uploads[0][1] == (tmp_path / "av.png").resolve()
    assert state.avatar_cache.get("user1")


@pytest.mark.parametrize("author_id", ["../../outside-marker", "/abs/outside-marker"])
async def test_downloader_rejects_unsafe_author_id(tmp_path: Path, author_id: str) -> None:
    marker = tmp_path / "outside-marker.png"
    marker.write_bytes(b"MARKER")
    dest, reason = await _download_remote_avatar(
        _FakeSession(), "https://cdn.invalid/a.png", tmp_path, author_id
    )
    assert dest is None
    assert reason
    assert marker.read_bytes() == b"MARKER"


async def test_downloader_snowflake_destination_unchanged(tmp_path: Path) -> None:
    dest, reason = await _download_remote_avatar(
        _FakeSession(), "https://cdn.invalid/a.png", tmp_path, "1234567890"
    )
    assert reason == ""
    assert dest == (tmp_path / "avatars" / "1234567890.png").resolve()
    assert dest.read_bytes() == b"PNG-DUMMY-BYTES"


async def test_avatar_phase_crafted_id_leaves_marker_untouched(tmp_path: Path) -> None:
    marker = tmp_path / "output" / "outside-marker.png"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_bytes(b"MARKER")
    (tmp_path / "safe.png").write_bytes(b"x")
    crafted = _make_author(author_id="../../outside-marker", avatar_url="https://cdn.invalid/a.png")
    safe = _make_author(author_id="555", name="Safe", avatar_url="https://cdn.invalid/b.png")
    config = _make_config(tmp_path)
    state = _make_state()
    export = DCEExport(
        guild=DCEGuild(id="g1", name="G"),
        channel=DCEChannel(id="c1", type=0, name="general"),
        messages=[_make_message("m1", author=crafted), _make_message("m2", author=safe)],
    )
    uploads: list[tuple[str, Path]] = []
    with (
        patch("discord_ferry.migrator.avatars.get_session", lambda _cfg: _FakeSessionCM()),
        patch("discord_ferry.migrator.avatars.upload_with_cache", _recording_uploads(uploads)),
    ):
        await run_avatars(config, state, [export], lambda e: None)

    assert marker.read_bytes() == b"MARKER"
    unsafe = [w for w in state.warnings if w.get("type") == "unsafe_media_path"]
    assert len(unsafe) == 1
    assert [tag for tag, _ in uploads] == ["avatars"], "the safe author still uploads"


async def test_downloader_rejects_escaped_avatars_directory(tmp_path: Path) -> None:
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir(exist_ok=True)
    output = tmp_path / "output"
    output.mkdir(parents=True, exist_ok=True)
    (output / "avatars").symlink_to(outside, target_is_directory=True)
    dest, reason = await _download_remote_avatar(
        _FakeSession(), "https://cdn.invalid/a.png", output, "1234567890"
    )
    assert dest is None
    assert reason
    assert list(outside.iterdir()) == []


# ---------------------------------------------------------------------------
# #1108: avatar downloads stop at the Autumn avatars size limit
# ---------------------------------------------------------------------------

_AVATAR_CAP = 4_000_000  # Autumn "avatars" tag limit, stoatchat Revolt.toml


async def _fetch_avatar(
    tmp_path: Path, cdn_path: str
) -> tuple[Path | None, str, LocalCDN, RewritingSession]:
    """Run the real downloader against the local server."""
    async with local_cdn() as cdn, aiohttp.ClientSession() as real:
        session = RewritingSession(real, cdn.url(cdn_path))
        dest, reason = await asyncio.wait_for(
            _download_remote_avatar(
                session,  # type: ignore[arg-type]  # duck-typed stand-in for ClientSession
                "https://cdn.discordapp.com/avatars/42/a.png",
                tmp_path,
                "42",
            ),
            timeout=5,
        )
        await asyncio.sleep(0.2)  # let the server notice a closed connection
        return dest, reason, cdn, session


def _no_avatar_files(tmp_path: Path) -> bool:
    return not (tmp_path / "avatars").exists() or list((tmp_path / "avatars").iterdir()) == []


async def test_avatar_one_byte_over_the_cap_is_refused(tmp_path: Path) -> None:
    """Kills an implementation with no size check at all (or one off by a byte)."""
    dest, reason, _, _ = await _fetch_avatar(tmp_path, f"/chunked/{_AVATAR_CAP + 1}")
    assert dest is None
    assert "limit" in reason
    assert _no_avatar_files(tmp_path)


async def test_avatar_undeclared_oversize_stops_reading(tmp_path: Path) -> None:
    """Kills `await resp.read()` followed by a length check: that reads the whole body."""
    dest, reason, cdn, _ = await _fetch_avatar(tmp_path, "/endless")
    assert dest is None
    assert "limit" in reason
    assert cdn.sent < ENDLESS_LIMIT // 2, f"server sent {cdn.sent} bytes, the client kept reading"
    assert _no_avatar_files(tmp_path)


@pytest.mark.parametrize("route", ["/body/{n}", "/lying/{n}"])
async def test_avatar_declared_oversize_is_refused_without_reading(
    tmp_path: Path, route: str
) -> None:
    """Kills an implementation that trusts the body and ignores a declared Content-Length."""
    dest, reason, _, _ = await _fetch_avatar(tmp_path, route.format(n=_AVATAR_CAP + 1))
    assert dest is None
    assert "limit" in reason
    assert _no_avatar_files(tmp_path)


async def test_avatar_redirect_is_not_followed(tmp_path: Path) -> None:
    """Kills an implementation that still follows a 3xx to a host Ferry never chose."""
    dest, reason, cdn, _ = await _fetch_avatar(tmp_path, "/redirect")
    assert dest is None
    assert "302" in reason
    assert cdn.target_hits == 0
    assert _no_avatar_files(tmp_path)


async def test_avatar_download_requests_without_following_redirects(tmp_path: Path) -> None:
    _, _, _, session = await _fetch_avatar(tmp_path, "/body/10")
    assert session.calls[0]["allow_redirects"] is False


async def test_avatar_exactly_at_the_cap_is_accepted(tmp_path: Path) -> None:
    """Kills an off-by-one that refuses a body of exactly the limit."""
    dest, reason, _, _ = await _fetch_avatar(tmp_path, f"/chunked/{_AVATAR_CAP}")
    assert reason == ""
    assert dest is not None
    assert dest.stat().st_size == _AVATAR_CAP
