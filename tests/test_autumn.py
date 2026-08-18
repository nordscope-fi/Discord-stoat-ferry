"""Tests for the Autumn file uploader."""

import ssl
from pathlib import Path

import aiohttp
import pytest
from aioresponses import aioresponses

from discord_ferry.core.http import new_session
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


def test_tag_size_limits_match_the_live_instance() -> None:
    """Every limit is DECIMAL megabytes, matching what the instance advertises.

    Measured 2026-08-02 from `features.limits.default.file_upload_size_limits` on
    https://api.stoat.chat/. Autumn's config is written in decimal (20_000_000), so
    every `N * 1024 * 1024` here was too permissive: files in the gap band passed our
    pre-upload guard, were uploaded, and were rejected by Autumn with a 413.

    `icons` was already correct — it had been diagnosed and fixed in isolation, with the
    comment "Autumn enforces a flat 2.5MB (Revolt.toml), not 2560*1024", while the other
    five were left. This pin covers all six so that cannot recur. `ferry probe` now
    checks the same thing against a live instance.
    """
    from discord_ferry.uploader.autumn import TAG_SIZE_LIMITS

    assert TAG_SIZE_LIMITS == {
        "attachments": 20_000_000,
        "avatars": 4_000_000,
        "backgrounds": 6_000_000,
        "icons": 2_500_000,
        "banners": 6_000_000,
        "emojis": 500_000,
    }


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
# skip_cache — S3 attachment reuse prevention
# ---------------------------------------------------------------------------


async def test_upload_with_cache_skip_cache_gets_fresh_id(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """With skip_cache=True, a cached key is ignored and a fresh upload runs."""
    f = tmp_path / "shared.png"
    f.write_bytes(b"x" * 50)
    cache: dict[str, str] = {str(f): "cached-id-111"}

    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": "fresh-id-222"})
    async with aiohttp.ClientSession() as session:
        result = await upload_with_cache(
            session,
            AUTUMN_URL,
            "attachments",
            f,
            TOKEN,
            cache,
            delay=0,
            skip_cache=True,
        )
    assert result == "fresh-id-222"
    assert cache[str(f)] == "fresh-id-222"


async def test_upload_with_cache_skip_cache_false_still_hits(
    tmp_path: Path,
) -> None:
    """With skip_cache=False (default), the cache is consulted as before."""
    f = tmp_path / "shared.png"
    f.write_bytes(b"x" * 50)
    cache: dict[str, str] = {str(f): "cached-id-111"}

    async with aiohttp.ClientSession() as session:
        result = await upload_with_cache(
            session,
            AUTUMN_URL,
            "attachments",
            f,
            TOKEN,
            cache,
            delay=0,
        )
    assert result == "cached-id-111"


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


# ---------------------------------------------------------------------------
# X-RateLimit-Reset-After (MILLISECONDS) + delay clamp
# ---------------------------------------------------------------------------


async def test_429_x_ratelimit_reset_after_is_milliseconds(
    tmp_path: Path, mock_aiohttp: aioresponses, slept: list[float]
) -> None:
    """Autumn's X-RateLimit-Reset-After is MILLISECONDS, not the 1000 ms default.

    Autumn rides the same rate-limit middleware as the Stoat API, so a real Autumn 429
    advertises ``x-ratelimit-reset-after: 10000`` (verified live against
    cdn.stoatusercontent.com) with no body and no Retry-After. Falling through to the
    default made every Autumn 429 retry nine seconds early, into a bucket still shut.
    """
    file = tmp_path / "x.png"
    file.write_bytes(b"x" * 50)
    mock_aiohttp.post(
        f"{AUTUMN_URL}/attachments",
        status=429,
        body=b"",
        headers={"X-RateLimit-Reset-After": "10000"},
    )
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": "ok"})
    async with aiohttp.ClientSession() as session:
        result = await upload_to_autumn(session, AUTUMN_URL, "attachments", file, TOKEN)
    assert result == "ok"
    assert slept[0] == 10.0


async def test_429_negative_retry_after_floors_at_zero(
    tmp_path: Path, mock_aiohttp: aioresponses, slept: list[float]
) -> None:
    """A negative advertised delay must not disable backoff.

    asyncio.sleep() of a negative value returns immediately, which would turn the retry
    loop hot and burn all three attempts against a still-closed bucket.
    """
    file = tmp_path / "x.png"
    file.write_bytes(b"x" * 50)
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", status=429, payload={"retry_after": -5000})
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": "ok"})
    async with aiohttp.ClientSession() as session:
        result = await upload_to_autumn(session, AUTUMN_URL, "attachments", file, TOKEN)
    assert result == "ok"
    assert slept[0] == 0.0


async def test_429_absurd_retry_after_is_capped(
    tmp_path: Path, mock_aiohttp: aioresponses, slept: list[float]
) -> None:
    """An hour-long advertised delay is capped at 60 s rather than presenting as a hang."""
    file = tmp_path / "x.png"
    file.write_bytes(b"x" * 50)
    mock_aiohttp.post(
        f"{AUTUMN_URL}/attachments",
        status=429,
        body=b"",
        headers={"X-RateLimit-Reset-After": "3600000"},
    )
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": "ok"})
    async with aiohttp.ClientSession() as session:
        result = await upload_to_autumn(session, AUTUMN_URL, "attachments", file, TOKEN)
    assert result == "ok"
    assert slept[0] == 60.0


async def test_429_unparseable_reset_after_falls_back_to_default(
    tmp_path: Path, mock_aiohttp: aioresponses, slept: list[float]
) -> None:
    """A non-numeric X-RateLimit-Reset-After is ignored, not fatal."""
    file = tmp_path / "x.png"
    file.write_bytes(b"x" * 50)
    mock_aiohttp.post(
        f"{AUTUMN_URL}/attachments",
        status=429,
        body=b"",
        headers={"X-RateLimit-Reset-After": "soon"},
    )
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": "ok"})
    async with aiohttp.ClientSession() as session:
        result = await upload_to_autumn(session, AUTUMN_URL, "attachments", file, TOKEN)
    assert result == "ok"
    assert slept[0] == 1.0


async def test_429_body_retry_after_outranks_reset_after_header(
    tmp_path: Path, mock_aiohttp: aioresponses, slept: list[float]
) -> None:
    """The body stays the most specific source when both it and the ms header are present."""
    file = tmp_path / "x.png"
    file.write_bytes(b"x" * 50)
    mock_aiohttp.post(
        f"{AUTUMN_URL}/attachments",
        status=429,
        payload={"retry_after": 100},
        headers={"X-RateLimit-Reset-After": "10000"},
    )
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": "ok"})
    async with aiohttp.ClientSession() as session:
        result = await upload_to_autumn(session, AUTUMN_URL, "attachments", file, TOKEN)
    assert result == "ok"
    assert slept[0] == 0.1


async def test_429_retry_after_header_outranks_reset_after_header(
    tmp_path: Path, mock_aiohttp: aioresponses, slept: list[float]
) -> None:
    """A proxy's Retry-After (delta-SECONDS) outranks the origin's ms header.

    The two are different units on the same response -- 2 s against 10000 ms. Asserting
    2.0 rather than 10.0 proves the seconds branch ran, so a future reorder of the
    precedence chain cannot silently swap the units.
    """
    file = tmp_path / "x.png"
    file.write_bytes(b"x" * 50)
    mock_aiohttp.post(
        f"{AUTUMN_URL}/attachments",
        status=429,
        body=b"",
        headers={"Retry-After": "2", "X-RateLimit-Reset-After": "10000"},
    )
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": "ok"})
    async with aiohttp.ClientSession() as session:
        result = await upload_to_autumn(session, AUTUMN_URL, "attachments", file, TOKEN)
    assert result == "ok"
    assert slept[0] == 2.0


async def test_429_nan_reset_after_header_falls_back_to_default(
    tmp_path: Path, mock_aiohttp: aioresponses, slept: list[float]
) -> None:
    """A NaN delay must be rejected, not clamped -- the clamp cannot catch it.

    float("nan") parses fine, and every comparison against NaN is False, so it passes
    straight through min()/max(). asyncio.sleep(nan) then poisons the event loop's
    timer heap. Guards the clamp against giving false assurance.
    """
    file = tmp_path / "x.png"
    file.write_bytes(b"x" * 50)
    mock_aiohttp.post(
        f"{AUTUMN_URL}/attachments",
        status=429,
        body=b"",
        headers={"X-RateLimit-Reset-After": "nan"},
    )
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": "ok"})
    async with aiohttp.ClientSession() as session:
        result = await upload_to_autumn(session, AUTUMN_URL, "attachments", file, TOKEN)
    assert result == "ok"
    assert slept[0] == 1.0


async def test_429_infinite_reset_after_header_falls_back_to_default(
    tmp_path: Path, mock_aiohttp: aioresponses, slept: list[float]
) -> None:
    """An infinite delay is treated as garbage, not as "wait the maximum"."""
    file = tmp_path / "x.png"
    file.write_bytes(b"x" * 50)
    mock_aiohttp.post(
        f"{AUTUMN_URL}/attachments",
        status=429,
        body=b"",
        headers={"X-RateLimit-Reset-After": "inf"},
    )
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": "ok"})
    async with aiohttp.ClientSession() as session:
        result = await upload_to_autumn(session, AUTUMN_URL, "attachments", file, TOKEN)
    assert result == "ok"
    assert slept[0] == 1.0


async def test_429_nan_body_retry_after_falls_back_to_default(
    tmp_path: Path, mock_aiohttp: aioresponses, slept: list[float]
) -> None:
    """The body route reaches NaN too -- Python's json parses bare NaN by default.

    This is the more reachable of the two NaN paths: it needs only a sloppy server
    serialising a float, not a hand-written header.
    """
    file = tmp_path / "x.png"
    file.write_bytes(b"x" * 50)
    mock_aiohttp.post(
        f"{AUTUMN_URL}/attachments", status=429, payload={"retry_after": float("nan")}
    )
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": "ok"})
    async with aiohttp.ClientSession() as session:
        result = await upload_to_autumn(session, AUTUMN_URL, "attachments", file, TOKEN)
    assert result == "ok"
    assert slept[0] == 1.0


async def test_429_boolean_body_retry_after_is_ignored(
    tmp_path: Path, mock_aiohttp: aioresponses, slept: list[float]
) -> None:
    """A JSON `true` is not a delay of 1 ms.

    bool is a subclass of int, so an isinstance check accepts it and float(True) gives
    1.0 ms -- below the clamp's floor, so it would slip past as effectively no backoff.
    """
    file = tmp_path / "x.png"
    file.write_bytes(b"x" * 50)
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", status=429, payload={"retry_after": True})
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", payload={"id": "ok"})
    async with aiohttp.ClientSession() as session:
        result = await upload_to_autumn(session, AUTUMN_URL, "attachments", file, TOKEN)
    assert result == "ok"
    assert slept[0] == 1.0


# ---------------------------------------------------------------------------
# Certificate failures (#137) — the one host v2.13.0's error work did not reach
# ---------------------------------------------------------------------------


def _cert_error(host: str = "cdn.stoatusercontent.com") -> aiohttp.ClientConnectorCertificateError:
    key = aiohttp.client_reqrep.ConnectionKey(host, 443, True, True, None, None, None)
    return aiohttp.ClientConnectorCertificateError(key, ssl.SSLCertVerificationError("bad"))


async def test_certificate_error_becomes_an_actionable_autumn_error(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """A certificate failure against Autumn names the host and the override.

    Kills two mutants. Without the `except aiohttp.ClientError` arm the raw
    ClientConnectorCertificateError propagates and `pytest.raises` fails on the
    type. Converting to AutumnUploadError but dropping `tls_hint` leaves a
    message with no host and no SSL_CERT_FILE, which the last two assertions
    reject — that is exactly the unactionable state #137 was filed about.
    """
    file = tmp_path / "icon.png"
    file.write_bytes(b"x" * 50)
    mock_aiohttp.post(f"{AUTUMN_URL}/icons", exception=_cert_error())

    async with aiohttp.ClientSession() as session:
        with pytest.raises(AutumnUploadError) as caught:
            await upload_to_autumn(session, AUTUMN_URL, "icons", file, TOKEN)

    message = str(caught.value)
    assert "cdn.stoatusercontent.com" in message
    assert "SSL_CERT_FILE" in message
    # Pins the explicit `from exc`. tls_hint also walks __context__, which Python
    # sets implicitly, so structure.py's role-icon handler would still re-derive
    # the hint without it — this assertion guards the idiom, not that behaviour.
    assert isinstance(caught.value.__cause__, aiohttp.ClientConnectorCertificateError)


async def test_certificate_error_is_not_retried(
    tmp_path: Path, mock_aiohttp: aioresponses, slept: list[float]
) -> None:
    """A certificate failure raises on attempt 1 rather than sleeping twice.

    Guards the plausible wrong fix rather than the current code: handling
    ClientError inside the loop invites `continue`, which would pay two backoff
    sleeps before failing with an error no retry can clear. Only one response is
    mocked, so a retry would also change the exception the caller sees.
    """
    file = tmp_path / "icon.png"
    file.write_bytes(b"x" * 50)
    mock_aiohttp.post(f"{AUTUMN_URL}/icons", exception=_cert_error())

    async with aiohttp.ClientSession() as session:
        with pytest.raises(AutumnUploadError):
            await upload_to_autumn(session, AUTUMN_URL, "icons", file, TOKEN)

    assert slept == [], "a certificate error must not pay the retry backoff"


async def test_non_certificate_client_error_is_re_raised_unchanged(
    tmp_path: Path, mock_aiohttp: aioresponses
) -> None:
    """Only certificate failures are converted; every other ClientError is untouched.

    Kills the over-broad fix: wrapping every ClientError in AutumnUploadError
    would change what each of the five callers' except clauses match, so a
    connection reset that a caller deliberately lets escape would start being
    swallowed as a warning instead. `pytest.raises(aiohttp.ClientError)` passes
    under both implementations — the AutumnUploadError check is what discriminates.
    """
    file = tmp_path / "icon.png"
    file.write_bytes(b"x" * 50)
    mock_aiohttp.post(f"{AUTUMN_URL}/icons", exception=aiohttp.ClientOSError("connection reset"))

    async with aiohttp.ClientSession() as session:
        with pytest.raises(aiohttp.ClientError) as caught:
            await upload_to_autumn(session, AUTUMN_URL, "icons", file, TOKEN)

    assert not isinstance(caught.value, AutumnUploadError)


# ---------------------------------------------------------------------------
# #135 — a refusing proxy must name itself at autumn.py:232
# ---------------------------------------------------------------------------


async def test_a_refused_proxy_names_the_proxy(
    tmp_path: Path, fake_proxy, proxy_env, os_proxy
) -> None:
    """SC-135-28. Killing: proxy_hint defined and never called at autumn.py:232.

    Without the call the hint is None, the `if hint is None: raise` arm fires,
    and the raw ClientHttpProxyError escapes -- so pytest.raises fails on the
    TYPE before any assertion below runs. That is the mutant's signature here.
    """
    file = tmp_path / "icon.png"
    file.write_bytes(b"x" * 50)
    make, _ = fake_proxy
    server = await make(b"403 Forbidden")
    port = server.sockets[0].getsockname()[1]
    async with server:
        with os_proxy({}), proxy_env(HTTPS_PROXY=f"http://127.0.0.1:{port}"):
            async with new_session() as session:
                with pytest.raises(AutumnUploadError) as caught:
                    await upload_to_autumn(session, AUTUMN_URL, "icons", file, TOKEN)

    message = str(caught.value)
    assert "Upload to Autumn failed" in message
    assert f"The request to autumn.test went through the proxy at 127.0.0.1:{port}" in message
    assert "FERRY_DISABLE_PROXY" in message
