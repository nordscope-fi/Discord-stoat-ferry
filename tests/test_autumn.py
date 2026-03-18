"""Tests for the Autumn file uploader."""

from pathlib import Path

import aiohttp
import pytest
from aioresponses import aioresponses

from discord_ferry.errors import AutumnUploadError
from discord_ferry.uploader.autumn import upload_to_autumn, upload_with_cache

AUTUMN_URL = "https://autumn.test"
TOKEN = "test-token-abc"


@pytest.fixture
def mock_aiohttp() -> aioresponses:
    with aioresponses() as m:
        yield m


@pytest.fixture
async def session() -> aiohttp.ClientSession:
    async with aiohttp.ClientSession() as s:
        yield s


# ---------------------------------------------------------------------------
# upload_to_autumn
# ---------------------------------------------------------------------------


async def test_upload_to_autumn_success(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """A valid file upload returns the Autumn file ID from the JSON response."""
    file = tmp_path / "test.png"
    file.write_bytes(b"x" * 100)

    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": "file123"})

    async with aiohttp.ClientSession() as session:
        result = await upload_to_autumn(session, AUTUMN_URL, "attachments", file, TOKEN)

    assert result == "file123"


async def test_upload_to_autumn_file_not_found(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """Passing a non-existent file raises AutumnUploadError immediately."""
    missing = tmp_path / "ghost.png"

    async with aiohttp.ClientSession() as session:
        with pytest.raises(AutumnUploadError, match="File not found"):
            await upload_to_autumn(session, AUTUMN_URL, "attachments", missing, TOKEN)


async def test_upload_to_autumn_file_too_large(tmp_path: Path) -> None:
    """A file exceeding the tag size limit raises AutumnUploadError before any HTTP call."""
    oversized = tmp_path / "big_emoji.png"
    # emojis limit is 500 KB; write 501 KB
    oversized.write_bytes(b"x" * (501 * 1024))

    async with aiohttp.ClientSession() as session:
        with pytest.raises(AutumnUploadError, match="exceeds the emojis limit"):
            await upload_to_autumn(session, AUTUMN_URL, "emojis", oversized, TOKEN)


async def test_upload_to_autumn_invalid_tag(tmp_path: Path) -> None:
    """An unrecognised tag raises AutumnUploadError before any HTTP call."""
    file = tmp_path / "file.bin"
    file.write_bytes(b"data")

    async with aiohttp.ClientSession() as session:
        with pytest.raises(AutumnUploadError, match="Unknown Autumn tag"):
            await upload_to_autumn(session, AUTUMN_URL, "invalid_tag", file, TOKEN)


async def test_upload_to_autumn_429_retry(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """A 429 response causes a retry after the retry_after delay; second attempt succeeds."""
    file = tmp_path / "img.png"
    file.write_bytes(b"y" * 200)

    # First request: 429 with 100 ms retry_after
    mock_aiohttp.post(
        f"{AUTUMN_URL}/attachments",
        status=429,
        payload={"retry_after": 100},
    )
    # Second request: success
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": "file123"})

    async with aiohttp.ClientSession() as session:
        result = await upload_to_autumn(session, AUTUMN_URL, "attachments", file, TOKEN)

    assert result == "file123"


async def test_upload_to_autumn_server_error_retry(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """A 502 response is retried; a subsequent 200 returns the file ID."""
    file = tmp_path / "doc.pdf"
    file.write_bytes(b"z" * 512)

    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", status=502)
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": "abc999"})

    async with aiohttp.ClientSession() as session:
        result = await upload_to_autumn(session, AUTUMN_URL, "attachments", file, TOKEN)

    assert result == "abc999"


async def test_upload_to_autumn_retries_exhausted(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """Three consecutive 502 responses exhaust retries and raise AutumnUploadError."""
    file = tmp_path / "data.bin"
    file.write_bytes(b"a" * 256)

    for _ in range(3):
        mock_aiohttp.post(f"{AUTUMN_URL}/attachments", status=502)

    async with aiohttp.ClientSession() as session:
        with pytest.raises(AutumnUploadError, match="Upload failed after"):
            await upload_to_autumn(session, AUTUMN_URL, "attachments", file, TOKEN)


async def test_upload_to_autumn_413_specific_message(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """HTTP 413 produces error message with file size and limit."""
    file = tmp_path / "big.png"
    file.write_bytes(b"x" * 100)
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", status=413, body=b"Payload Too Large")
    async with aiohttp.ClientSession() as session:
        with pytest.raises(AutumnUploadError, match="File too large"):
            await upload_to_autumn(session, AUTUMN_URL, "attachments", file, TOKEN)


# ---------------------------------------------------------------------------
# upload_with_cache
# ---------------------------------------------------------------------------


async def test_upload_with_cache_hit(tmp_path: Path) -> None:
    """A pre-populated cache entry is returned without making any HTTP request."""
    file = tmp_path / "cached.png"
    file.write_bytes(b"c" * 100)

    cache: dict[str, str] = {str(file): "cached_id_xyz"}

    async with aiohttp.ClientSession() as session:
        result = await upload_with_cache(
            session, AUTUMN_URL, "attachments", file, TOKEN, cache, delay=0
        )

    assert result == "cached_id_xyz"
    # Cache size unchanged — no new entry added
    assert len(cache) == 1


async def test_upload_with_cache_miss(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """A cache miss triggers an upload and stores the result in the cache dict."""
    file = tmp_path / "fresh.png"
    file.write_bytes(b"f" * 100)

    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": "new_file_id"})

    cache: dict[str, str] = {}

    async with aiohttp.ClientSession() as session:
        result = await upload_with_cache(
            session, AUTUMN_URL, "attachments", file, TOKEN, cache, delay=0
        )

    assert result == "new_file_id"
    assert cache[str(file)] == "new_file_id"
