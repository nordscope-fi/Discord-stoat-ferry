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


def test_icons_limit_matches_stoat_autumn_config() -> None:
    """Autumn enforces a flat 2_500_000 for icons (Revolt.toml), not 2560*1024."""
    from discord_ferry.uploader.autumn import TAG_SIZE_LIMITS

    assert TAG_SIZE_LIMITS["icons"] == 2_500_000


# ---------------------------------------------------------------------------
# Batch 10 / S2 — upload_with_cache single-flight concurrency
# ---------------------------------------------------------------------------


async def test_upload_with_cache_coalesces_same_key(tmp_path: Path) -> None:
    """SC-5: concurrent same-key calls upload exactly once and share the id."""
    import asyncio
    from unittest.mock import patch

    f = tmp_path / "a.png"
    f.write_bytes(b"x" * 10)
    cache: dict[str, str] = {}
    calls = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _slow(*args: object, **kwargs: object) -> str:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return "id-1"

    with patch("discord_ferry.uploader.autumn.upload_to_autumn", _slow):
        async with aiohttp.ClientSession() as session:
            task = asyncio.gather(
                upload_with_cache(session, AUTUMN_URL, "avatars", f, TOKEN, cache, delay=0),
                upload_with_cache(session, AUTUMN_URL, "avatars", f, TOKEN, cache, delay=0),
            )
            await entered.wait()  # leader registered the in-flight future + entered upload
            await asyncio.sleep(0)  # let the follower reach `await inflight`
            release.set()
            results = await task

    assert calls == 1
    assert results == ["id-1", "id-1"]
    assert cache[str(f)] == "id-1"


async def test_upload_with_cache_distinct_keys_concurrent(tmp_path: Path) -> None:
    """SC-6: distinct keys upload independently (no global serialization)."""
    import asyncio
    from unittest.mock import patch

    f1 = tmp_path / "a.png"
    f2 = tmp_path / "b.png"
    f1.write_bytes(b"x" * 10)
    f2.write_bytes(b"y" * 10)
    cache: dict[str, str] = {}
    calls = 0

    async def _up(*args: object, **kwargs: object) -> str:
        nonlocal calls
        calls += 1
        return f"id-{calls}"

    with patch("discord_ferry.uploader.autumn.upload_to_autumn", _up):
        async with aiohttp.ClientSession() as session:
            results = await asyncio.gather(
                upload_with_cache(session, AUTUMN_URL, "avatars", f1, TOKEN, cache, delay=0),
                upload_with_cache(session, AUTUMN_URL, "avatars", f2, TOKEN, cache, delay=0),
            )

    assert calls == 2
    assert len(set(results)) == 2


async def test_upload_with_cache_hit_skips_upload_and_inflight(tmp_path: Path) -> None:
    """SC-7: a cache hit returns without uploading or registering an in-flight future."""
    from unittest.mock import AsyncMock, patch

    from discord_ferry.uploader.autumn import _inflight_uploads

    f = tmp_path / "a.png"
    f.write_bytes(b"x" * 10)
    cache = {str(f): "cached-id"}
    mock = AsyncMock(return_value="should-not-be-used")
    with patch("discord_ferry.uploader.autumn.upload_to_autumn", mock):
        async with aiohttp.ClientSession() as session:
            result = await upload_with_cache(
                session, AUTUMN_URL, "avatars", f, TOKEN, cache, delay=0
            )

    assert result == "cached-id"
    assert mock.call_count == 0
    assert str(f) not in _inflight_uploads


async def test_upload_with_cache_failure_does_not_poison_key(tmp_path: Path) -> None:
    """SC-8: a failed upload pops the key; a later call retries; no stray warning."""
    from unittest.mock import AsyncMock, patch

    from discord_ferry.uploader.autumn import _inflight_uploads

    f = tmp_path / "a.png"
    f.write_bytes(b"x" * 10)
    cache: dict[str, str] = {}
    mock = AsyncMock(side_effect=[AutumnUploadError("boom"), "id-2"])
    with patch("discord_ferry.uploader.autumn.upload_to_autumn", mock):
        async with aiohttp.ClientSession() as session:
            with pytest.raises(AutumnUploadError):
                await upload_with_cache(session, AUTUMN_URL, "avatars", f, TOKEN, cache, delay=0)
            assert str(f) not in _inflight_uploads  # not poisoned
            result = await upload_with_cache(
                session, AUTUMN_URL, "avatars", f, TOKEN, cache, delay=0
            )

    assert result == "id-2"
    assert mock.call_count == 2


async def test_upload_with_cache_self_cleans_inflight(tmp_path: Path) -> None:
    """SC-9: the in-flight entry is popped after a successful upload."""
    from unittest.mock import AsyncMock, patch

    from discord_ferry.uploader.autumn import _inflight_uploads

    f = tmp_path / "a.png"
    f.write_bytes(b"x" * 10)
    cache: dict[str, str] = {}
    mock = AsyncMock(return_value="id-1")
    with patch("discord_ferry.uploader.autumn.upload_to_autumn", mock):
        async with aiohttp.ClientSession() as session:
            await upload_with_cache(session, AUTUMN_URL, "avatars", f, TOKEN, cache, delay=0)

    assert str(f) not in _inflight_uploads
    assert cache[str(f)] == "id-1"


# ---------------------------------------------------------------------------
# S1 — malformed 200 -> AutumnUploadError
# ---------------------------------------------------------------------------


async def test_upload_non_json_200_raises_autumn_error(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """A 200 with an HTML body raises AutumnUploadError, not ContentTypeError."""
    file = tmp_path / "x.png"
    file.write_bytes(b"x" * 50)
    mock_aiohttp.post(
        f"{AUTUMN_URL}/attachments",
        status=200,
        body=b"<html>SENTINEL_BODY</html>",
        content_type="text/html",
    )
    async with aiohttp.ClientSession() as session:
        with pytest.raises(AutumnUploadError):
            await upload_to_autumn(session, AUTUMN_URL, "attachments", file, "secret-token-xyz")


async def test_upload_empty_200_body_raises_autumn_error(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """An empty 200 body (json -> None) raises AutumnUploadError."""
    file = tmp_path / "x.png"
    file.write_bytes(b"x" * 50)
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", status=200, body=b"")
    async with aiohttp.ClientSession() as session:
        with pytest.raises(AutumnUploadError):
            await upload_to_autumn(session, AUTUMN_URL, "attachments", file, TOKEN)


async def test_upload_200_missing_id_raises_autumn_error(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """A 200 whose JSON lacks 'id' raises AutumnUploadError."""
    file = tmp_path / "x.png"
    file.write_bytes(b"x" * 50)
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"size": 50})
    async with aiohttp.ClientSession() as session:
        with pytest.raises(AutumnUploadError):
            await upload_to_autumn(session, AUTUMN_URL, "attachments", file, TOKEN)


async def test_upload_error_message_omits_token_and_body(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """The raised error never contains the response body or the token (Tiger #1)."""
    file = tmp_path / "x.png"
    file.write_bytes(b"x" * 50)
    mock_aiohttp.post(
        f"{AUTUMN_URL}/attachments",
        status=200,
        body=b"<html>SENTINEL_BODY</html>",
        content_type="text/html",
    )
    async with aiohttp.ClientSession() as session:
        with pytest.raises(AutumnUploadError) as ei:
            await upload_to_autumn(session, AUTUMN_URL, "attachments", file, "secret-token-xyz")
    text = str(ei.value)
    assert "SENTINEL_BODY" not in text
    assert "secret-token-xyz" not in text


# ---------------------------------------------------------------------------
# S2 — verify_size mismatch is a failed upload (raise, never cache)
# ---------------------------------------------------------------------------


async def test_verify_size_mismatch_raises(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """verify_size=True with a server size != local size raises AutumnUploadError."""
    file = tmp_path / "x.png"
    file.write_bytes(b"x" * 100)
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": "x", "size": 999})
    async with aiohttp.ClientSession() as session:
        with pytest.raises(AutumnUploadError, match="size mismatch"):
            await upload_to_autumn(
                session, AUTUMN_URL, "attachments", file, TOKEN, verify_size=True
            )


async def test_verify_size_mismatch_not_cached(tmp_path: Path, mock_aiohttp: aioresponses) -> None:
    """A mismatch via upload_with_cache leaves NO cache entry (corrupt id never cached)."""
    file = tmp_path / "x.png"
    file.write_bytes(b"x" * 100)
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": "x", "size": 999})
    cache: dict[str, str] = {}
    async with aiohttp.ClientSession() as session:
        with pytest.raises(AutumnUploadError):
            await upload_with_cache(
                session, AUTUMN_URL, "attachments", file, TOKEN, cache, delay=0, verify_size=True
            )
    assert str(file) not in cache


async def test_verify_size_match_returns_and_caches(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """verify_size=True with matching size returns the id and caches it."""
    file = tmp_path / "x.png"
    file.write_bytes(b"x" * 100)
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": "x", "size": 100})
    cache: dict[str, str] = {}
    async with aiohttp.ClientSession() as session:
        result = await upload_with_cache(
            session, AUTUMN_URL, "attachments", file, TOKEN, cache, delay=0, verify_size=True
        )
    assert result == "x"
    assert cache[str(file)] == "x"


async def test_verify_size_no_size_field_returns_id(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """verify_size=True with no 'size' field returns the id (best-effort unchanged)."""
    file = tmp_path / "x.png"
    file.write_bytes(b"x" * 100)
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": "x"})
    async with aiohttp.ClientSession() as session:
        result = await upload_to_autumn(
            session, AUTUMN_URL, "attachments", file, TOKEN, verify_size=True
        )
    assert result == "x"


async def test_verify_size_false_ignores_mismatch(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """Default verify_size=False ignores a size mismatch (path unchanged)."""
    file = tmp_path / "x.png"
    file.write_bytes(b"x" * 100)
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": "x", "size": 999})
    async with aiohttp.ClientSession() as session:
        result = await upload_to_autumn(session, AUTUMN_URL, "attachments", file, TOKEN)
    assert result == "x"


# ---------------------------------------------------------------------------
# S3 — 429 non-JSON tolerance + Retry-After header
# ---------------------------------------------------------------------------


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record autumn-module asyncio.sleep delays without waiting."""
    calls: list[float] = []

    async def _fake_sleep(secs: float) -> None:
        calls.append(secs)

    monkeypatch.setattr("discord_ferry.uploader.autumn.asyncio.sleep", _fake_sleep)
    return calls


async def test_429_html_body_retries_then_succeeds(
    tmp_path: Path, mock_aiohttp: aioresponses, slept: list[float]
) -> None:
    """A 429 with an HTML body retries (no ContentTypeError) using the default backoff."""
    file = tmp_path / "x.png"
    file.write_bytes(b"x" * 50)
    mock_aiohttp.post(
        f"{AUTUMN_URL}/attachments", status=429, body=b"<html>429</html>", content_type="text/html"
    )
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": "ok"})
    async with aiohttp.ClientSession() as session:
        result = await upload_to_autumn(session, AUTUMN_URL, "attachments", file, TOKEN)
    assert result == "ok"
    assert slept and slept[0] == 1.0


async def test_429_empty_body_default_backoff(
    tmp_path: Path, mock_aiohttp: aioresponses, slept: list[float]
) -> None:
    """A 429 with an empty body falls back to the 1000 ms default."""
    file = tmp_path / "x.png"
    file.write_bytes(b"x" * 50)
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", status=429, body=b"")
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": "ok"})
    async with aiohttp.ClientSession() as session:
        result = await upload_to_autumn(session, AUTUMN_URL, "attachments", file, TOKEN)
    assert result == "ok"
    assert slept[0] == 1.0


async def test_429_body_retry_after_honored(
    tmp_path: Path, mock_aiohttp: aioresponses, slept: list[float]
) -> None:
    """A 429 with a body retry_after sleeps that many ms (existing behavior preserved)."""
    file = tmp_path / "x.png"
    file.write_bytes(b"x" * 50)
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", status=429, payload={"retry_after": 100})
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": "ok"})
    async with aiohttp.ClientSession() as session:
        result = await upload_to_autumn(session, AUTUMN_URL, "attachments", file, TOKEN)
    assert result == "ok"
    assert slept[0] == 0.1


async def test_429_retry_after_header_used(
    tmp_path: Path, mock_aiohttp: aioresponses, slept: list[float]
) -> None:
    """A 429 with no body field but a Retry-After header sleeps that many seconds."""
    file = tmp_path / "x.png"
    file.write_bytes(b"x" * 50)
    mock_aiohttp.post(
        f"{AUTUMN_URL}/attachments", status=429, body=b"", headers={"Retry-After": "2"}
    )
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": "ok"})
    async with aiohttp.ClientSession() as session:
        result = await upload_to_autumn(session, AUTUMN_URL, "attachments", file, TOKEN)
    assert result == "ok"
    assert slept[0] == 2.0


async def test_429_exhausted_raises(
    tmp_path: Path, mock_aiohttp: aioresponses, slept: list[float]
) -> None:
    """Three consecutive 429s exhaust retries and raise AutumnUploadError (no crash)."""
    file = tmp_path / "x.png"
    file.write_bytes(b"x" * 50)
    for _ in range(3):
        mock_aiohttp.post(f"{AUTUMN_URL}/attachments", status=429, body=b"")
    async with aiohttp.ClientSession() as session:
        with pytest.raises(AutumnUploadError, match="Upload failed after"):
            await upload_to_autumn(session, AUTUMN_URL, "attachments", file, TOKEN)
