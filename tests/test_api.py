"""Tests for the Stoat REST API wrapper."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aioresponses import aioresponses

from discord_ferry.errors import MigrationError
from discord_ferry.migrator.api import (
    _circuit_state,
    _reset_circuit_state,
    _reset_rate_state,
    api_add_reaction,
    api_create_channel,
    api_create_emoji,
    api_create_role,
    api_create_server,
    api_edit_role,
    api_edit_server,
    api_fetch_server,
    api_pin_message,
    api_send_message,
    api_set_channel_default_permissions,
    api_set_channel_role_permissions,
    api_set_role_permissions,
    api_set_server_default_permissions,
    api_upsert_categories,
    get_rate_multiplier,
    init_request_semaphore,
)

BASE_URL = "https://api.test"
TOKEN = "test-session-token"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_circuit() -> None:  # type: ignore[misc]
    """Reset circuit breaker, semaphore, and adaptive rate state between tests."""
    import discord_ferry.migrator.api as _api_mod

    _reset_circuit_state()
    _reset_rate_state()
    _api_mod._request_semaphore = None
    yield  # type: ignore[misc]
    _reset_circuit_state()
    _reset_rate_state()
    _api_mod._request_semaphore = None


@pytest.fixture
def mock_aiohttp() -> aioresponses:
    with aioresponses() as m:
        yield m


# ---------------------------------------------------------------------------
# api_create_server
# ---------------------------------------------------------------------------


async def test_api_create_server(mock_aiohttp: aioresponses) -> None:
    """POST /servers/create returns the new server dict including _id."""
    mock_aiohttp.post(f"{BASE_URL}/servers/create", payload={"_id": "srv123", "name": "Test"})
    async with aiohttp.ClientSession() as session:
        result = await api_create_server(session, BASE_URL, TOKEN, "Test")
    assert result["_id"] == "srv123"
    assert result["name"] == "Test"


# ---------------------------------------------------------------------------
# api_fetch_server
# ---------------------------------------------------------------------------


async def test_api_fetch_server(mock_aiohttp: aioresponses) -> None:
    """GET /servers/abc123 returns the server info dict."""
    mock_aiohttp.get(
        f"{BASE_URL}/servers/abc123",
        payload={"_id": "abc123", "name": "My Server"},
    )
    async with aiohttp.ClientSession() as session:
        result = await api_fetch_server(session, BASE_URL, TOKEN, "abc123")
    assert result["_id"] == "abc123"
    assert result["name"] == "My Server"


# ---------------------------------------------------------------------------
# api_create_role
# ---------------------------------------------------------------------------


async def test_api_create_role(mock_aiohttp: aioresponses) -> None:
    """POST /servers/srv1/roles returns the new role dict including id."""
    mock_aiohttp.post(
        f"{BASE_URL}/servers/srv1/roles",
        payload={"id": "role99", "name": "Moderator"},
        status=200,
    )
    async with aiohttp.ClientSession() as session:
        result = await api_create_role(session, BASE_URL, TOKEN, "srv1", "Moderator")
    assert result["id"] == "role99"
    assert result["name"] == "Moderator"


# ---------------------------------------------------------------------------
# api_edit_role
# ---------------------------------------------------------------------------


async def test_api_edit_role(mock_aiohttp: aioresponses) -> None:
    """PATCH /servers/srv1/roles/role1 sends colour in the JSON body."""
    mock_aiohttp.patch(
        f"{BASE_URL}/servers/srv1/roles/role1",
        payload={"id": "role1", "colour": "#FF0000"},
    )
    async with aiohttp.ClientSession() as session:
        result = await api_edit_role(
            session, BASE_URL, TOKEN, "srv1", "role1", colour="#FF0000", hoist=True
        )
    assert result["colour"] == "#FF0000"


# ---------------------------------------------------------------------------
# api_upsert_categories
# ---------------------------------------------------------------------------


async def test_api_upsert_categories(mock_aiohttp: aioresponses) -> None:
    """PATCH /servers/srv1 with categories array in the body."""
    categories = [
        {"id": "cat1", "title": "General", "channels": ["ch1", "ch2"]},
        {"id": "cat2", "title": "Off-Topic", "channels": []},
    ]
    mock_aiohttp.patch(
        f"{BASE_URL}/servers/srv1",
        payload={"_id": "srv1", "categories": categories},
    )
    async with aiohttp.ClientSession() as session:
        result = await api_upsert_categories(session, BASE_URL, TOKEN, "srv1", categories)
    assert result["categories"] == categories


# ---------------------------------------------------------------------------
# api_create_channel
# ---------------------------------------------------------------------------


async def test_api_create_channel(mock_aiohttp: aioresponses) -> None:
    """POST /servers/srv1/channels sends name and type and returns the channel dict."""
    mock_aiohttp.post(
        f"{BASE_URL}/servers/srv1/channels",
        payload={"_id": "ch99", "name": "general", "channel_type": "Text"},
        status=201,
    )
    async with aiohttp.ClientSession() as session:
        result = await api_create_channel(
            session, BASE_URL, TOKEN, "srv1", name="general", channel_type="Text"
        )
    assert result["_id"] == "ch99"
    assert result["name"] == "general"


# ---------------------------------------------------------------------------
# api_edit_server
# ---------------------------------------------------------------------------


async def test_api_edit_server(mock_aiohttp: aioresponses) -> None:
    """PATCH /servers/srv1 passes kwargs as the JSON body."""
    mock_aiohttp.patch(
        f"{BASE_URL}/servers/srv1",
        payload={"_id": "srv1", "name": "Renamed Server"},
    )
    async with aiohttp.ClientSession() as session:
        result = await api_edit_server(session, BASE_URL, TOKEN, "srv1", name="Renamed Server")
    assert result["name"] == "Renamed Server"


# ---------------------------------------------------------------------------
# Error and retry tests
# ---------------------------------------------------------------------------


async def test_api_error_403(mock_aiohttp: aioresponses) -> None:
    """A 403 response raises MigrationError immediately (not retried)."""
    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", status=403, body="Forbidden")
    async with aiohttp.ClientSession() as session:
        with pytest.raises(MigrationError, match="API error 403"):
            await api_fetch_server(session, BASE_URL, TOKEN, "srv1")


# ---------------------------------------------------------------------------
# api_create_emoji
# ---------------------------------------------------------------------------


async def test_api_create_emoji(mock_aiohttp: aioresponses) -> None:
    """PUT /custom/emoji/{autumn_id} sends name and parent object in the body."""
    captured_body: dict[str, object] = {}

    def capture_callback(url: object, **kwargs: object) -> None:
        body = kwargs.get("json") or {}
        captured_body.update(body)  # type: ignore[arg-type]

    mock_aiohttp.put(
        f"{BASE_URL}/custom/emoji/autumn123",
        payload={"_id": "autumn123", "name": "party"},
        callback=capture_callback,
    )
    async with aiohttp.ClientSession() as session:
        result = await api_create_emoji(session, BASE_URL, TOKEN, "autumn123", "party", "srv1")
    assert result["_id"] == "autumn123"
    assert captured_body["name"] == "party"
    assert captured_body["parent"] == {"type": "Server", "id": "srv1"}


# ---------------------------------------------------------------------------
# api_send_message
# ---------------------------------------------------------------------------


async def test_api_send_message(mock_aiohttp: aioresponses) -> None:
    """POST /channels/ch1/messages sends content with idempotency_key header."""
    mock_aiohttp.post(
        f"{BASE_URL}/channels/ch1/messages",
        payload={"_id": "msg99", "content": "Hello"},
        status=200,
    )
    async with aiohttp.ClientSession() as session:
        result = await api_send_message(
            session,
            BASE_URL,
            TOKEN,
            "ch1",
            content="Hello",
            idempotency_key="ferry-discord123",
        )
    assert result["_id"] == "msg99"
    assert result["content"] == "Hello"


async def test_api_send_message_idempotency_key_header(mock_aiohttp: aioresponses) -> None:
    """api_send_message sends idempotency_key as Idempotency-Key HTTP header, not in body."""
    captured_headers: dict[str, str] = {}
    captured_body: dict[str, object] = {}

    def capture_callback(url: object, **kwargs: object) -> None:
        hdrs = kwargs.get("headers") or {}
        captured_headers.update(hdrs)  # type: ignore[arg-type]
        body = kwargs.get("json") or {}
        captured_body.update(body)  # type: ignore[arg-type]

    mock_aiohttp.post(
        f"{BASE_URL}/channels/ch1/messages",
        payload={"_id": "msg99"},
        callback=capture_callback,
    )
    async with aiohttp.ClientSession() as session:
        await api_send_message(
            session,
            BASE_URL,
            TOKEN,
            "ch1",
            content="Hello",
            idempotency_key="ferry-discord123",
        )

    assert captured_headers.get("Idempotency-Key") == "ferry-discord123"
    assert "nonce" not in captured_body


async def test_api_send_message_excludes_none_fields(mock_aiohttp: aioresponses) -> None:
    """api_send_message does not include None-valued optional fields in the request body."""
    captured_body: dict[str, object] = {}

    def capture_callback(url: object, **kwargs: object) -> None:
        body = kwargs.get("json") or {}
        captured_body.update(body)  # type: ignore[arg-type]

    mock_aiohttp.post(
        f"{BASE_URL}/channels/ch1/messages",
        payload={"_id": "msg1"},
        callback=capture_callback,
    )
    async with aiohttp.ClientSession() as session:
        await api_send_message(session, BASE_URL, TOKEN, "ch1", content="Hi")

    assert "content" in captured_body
    assert "attachments" not in captured_body
    assert "embeds" not in captured_body
    assert "masquerade" not in captured_body
    assert "replies" not in captured_body


async def test_api_send_message_includes_silent_by_default(mock_aiohttp: aioresponses) -> None:
    """api_send_message includes silent=true in the payload by default."""
    captured_body: dict[str, object] = {}

    def capture_callback(url: object, **kwargs: object) -> None:
        body = kwargs.get("json") or {}
        captured_body.update(body)  # type: ignore[arg-type]

    mock_aiohttp.post(
        f"{BASE_URL}/channels/ch1/messages",
        payload={"_id": "msg1"},
        callback=capture_callback,
    )
    async with aiohttp.ClientSession() as session:
        await api_send_message(session, BASE_URL, TOKEN, "ch1", content="Hello")

    assert captured_body.get("silent") is True


async def test_api_send_message_silent_false_omits_field(mock_aiohttp: aioresponses) -> None:
    """api_send_message with silent=False omits the silent field from payload."""
    captured_body: dict[str, object] = {}

    def capture_callback(url: object, **kwargs: object) -> None:
        body = kwargs.get("json") or {}
        captured_body.update(body)  # type: ignore[arg-type]

    mock_aiohttp.post(
        f"{BASE_URL}/channels/ch1/messages",
        payload={"_id": "msg1"},
        callback=capture_callback,
    )
    async with aiohttp.ClientSession() as session:
        await api_send_message(session, BASE_URL, TOKEN, "ch1", content="Hello", silent=False)

    assert "silent" not in captured_body


# ---------------------------------------------------------------------------
# api_add_reaction
# ---------------------------------------------------------------------------


async def test_api_add_reaction(mock_aiohttp: aioresponses) -> None:
    """PUT /channels/ch1/messages/msg1/reactions/:emoji returns empty dict on 204."""
    mock_aiohttp.put(
        f"{BASE_URL}/channels/ch1/messages/msg1/reactions/%F0%9F%91%8D",
        status=204,
    )
    async with aiohttp.ClientSession() as session:
        result = await api_add_reaction(session, BASE_URL, TOKEN, "ch1", "msg1", "\U0001f44d")
    assert result == {}


async def test_api_add_reaction_custom_emoji(mock_aiohttp: aioresponses) -> None:
    """PUT with a custom emoji ID (no URL encoding needed for plain ASCII)."""
    mock_aiohttp.put(
        f"{BASE_URL}/channels/ch1/messages/msg1/reactions/customEmojiId",
        status=204,
    )
    async with aiohttp.ClientSession() as session:
        result = await api_add_reaction(session, BASE_URL, TOKEN, "ch1", "msg1", "customEmojiId")
    assert result == {}


# ---------------------------------------------------------------------------
# api_pin_message
# ---------------------------------------------------------------------------


async def test_api_pin_message(mock_aiohttp: aioresponses) -> None:
    """PUT /channels/ch1/messages/msg1/pin returns empty dict on 204."""
    mock_aiohttp.put(
        f"{BASE_URL}/channels/ch1/messages/msg1/pin",
        status=204,
    )
    async with aiohttp.ClientSession() as session:
        result = await api_pin_message(session, BASE_URL, TOKEN, "ch1", "msg1")
    assert result == {}


# ---------------------------------------------------------------------------
# Error and retry tests
# ---------------------------------------------------------------------------


async def test_api_429_retry(mock_aiohttp: aioresponses) -> None:
    """A 429 response triggers a retry; the subsequent 200 returns the result."""
    # First response: 429 with 100 ms retry_after
    mock_aiohttp.get(
        f"{BASE_URL}/servers/srv1",
        status=429,
        payload={"retry_after": 100},
    )
    # Second response: success
    mock_aiohttp.get(
        f"{BASE_URL}/servers/srv1",
        payload={"_id": "srv1", "name": "Recovered"},
    )
    async with aiohttp.ClientSession() as session:
        result = await api_fetch_server(session, BASE_URL, TOKEN, "srv1")
    assert result["_id"] == "srv1"


async def test_api_network_error_retry_recovers(mock_aiohttp: aioresponses) -> None:
    """A transient ClientError on attempt 1 is retried; success on attempt 2."""
    mock_aiohttp.get(
        f"{BASE_URL}/servers/srv1",
        exception=aiohttp.ClientError("Connection reset"),
    )
    mock_aiohttp.get(
        f"{BASE_URL}/servers/srv1",
        payload={"_id": "srv1", "name": "Recovered"},
    )
    async with aiohttp.ClientSession() as session:
        result = await api_fetch_server(session, BASE_URL, TOKEN, "srv1")
    assert result["_id"] == "srv1"


async def test_api_network_error_exhausted(mock_aiohttp: aioresponses) -> None:
    """Three consecutive ClientErrors exhaust retries and raise MigrationError."""
    for _ in range(3):
        mock_aiohttp.get(
            f"{BASE_URL}/servers/srv1",
            exception=aiohttp.ClientError("Connection refused"),
        )
    async with aiohttp.ClientSession() as session:
        with pytest.raises(MigrationError, match="Network error after 3 retries"):
            await api_fetch_server(session, BASE_URL, TOKEN, "srv1")


async def test_api_502_retry_exhaustion(mock_aiohttp: aioresponses) -> None:
    """Three consecutive 502 responses exhaust retries and raise MigrationError."""
    for _ in range(3):
        mock_aiohttp.get(
            f"{BASE_URL}/servers/srv1",
            status=502,
            body="Bad Gateway",
        )
    async with aiohttp.ClientSession() as session:
        with pytest.raises(MigrationError, match="API request failed after 3 retries"):
            await api_fetch_server(session, BASE_URL, TOKEN, "srv1")


async def test_api_503_retry_success(mock_aiohttp: aioresponses) -> None:
    """A 503 on attempt 1 is retried; success on attempt 2."""
    mock_aiohttp.get(
        f"{BASE_URL}/servers/srv1",
        status=503,
        body="Service Unavailable",
    )
    mock_aiohttp.get(
        f"{BASE_URL}/servers/srv1",
        payload={"_id": "srv1", "name": "Recovered"},
    )
    async with aiohttp.ClientSession() as session:
        result = await api_fetch_server(session, BASE_URL, TOKEN, "srv1")
    assert result["_id"] == "srv1"


# ---------------------------------------------------------------------------
# api_set_role_permissions
# ---------------------------------------------------------------------------


async def test_api_set_role_permissions(mock_aiohttp: aioresponses) -> None:
    """PUT /servers/srv1/permissions/role1 sends allow/deny permission pair."""
    mock_aiohttp.put(f"{BASE_URL}/servers/srv1/permissions/role1", payload={})
    async with aiohttp.ClientSession() as session:
        await api_set_role_permissions(
            session, BASE_URL, TOKEN, "srv1", "role1", allow=4194304, deny=0
        )


# ---------------------------------------------------------------------------
# api_set_server_default_permissions
# ---------------------------------------------------------------------------


async def test_api_set_server_default_permissions(mock_aiohttp: aioresponses) -> None:
    """PUT /servers/srv1/permissions/default sends a single permissions integer."""
    mock_aiohttp.put(f"{BASE_URL}/servers/srv1/permissions/default", payload={})
    async with aiohttp.ClientSession() as session:
        await api_set_server_default_permissions(
            session, BASE_URL, TOKEN, "srv1", permissions=1048576
        )


# ---------------------------------------------------------------------------
# api_set_channel_role_permissions
# ---------------------------------------------------------------------------


async def test_api_set_channel_role_permissions(mock_aiohttp: aioresponses) -> None:
    """PUT /channels/ch1/permissions/role1 sends allow/deny permission pair."""
    mock_aiohttp.put(f"{BASE_URL}/channels/ch1/permissions/role1", payload={})
    async with aiohttp.ClientSession() as session:
        await api_set_channel_role_permissions(
            session, BASE_URL, TOKEN, "ch1", "role1", allow=4194304, deny=8388608
        )


# ---------------------------------------------------------------------------
# api_set_channel_default_permissions
# ---------------------------------------------------------------------------


async def test_api_set_channel_default_permissions(mock_aiohttp: aioresponses) -> None:
    """PUT /channels/ch1/permissions/default sends allow/deny permission pair."""
    mock_aiohttp.put(f"{BASE_URL}/channels/ch1/permissions/default", payload={})
    async with aiohttp.ClientSession() as session:
        await api_set_channel_default_permissions(
            session, BASE_URL, TOKEN, "ch1", allow=4194304, deny=0
        )


# ---------------------------------------------------------------------------
# Exponential backoff tests
# ---------------------------------------------------------------------------


async def test_exponential_backoff_timing(mock_aiohttp: aioresponses) -> None:
    """5xx retries use exponential delays: ~1, ~2, then fail (3 attempts)."""
    for _ in range(3):
        mock_aiohttp.get(f"{BASE_URL}/servers/srv1", status=502, body="Bad Gateway")

    sleep_calls: list[float] = []
    original_sleep = AsyncMock(side_effect=lambda d: sleep_calls.append(d))

    with patch("discord_ferry.migrator.api.asyncio.sleep", original_sleep):
        async with aiohttp.ClientSession() as session:
            with pytest.raises(MigrationError, match="API request failed after 3 retries"):
                await api_fetch_server(session, BASE_URL, TOKEN, "srv1")

    # Two sleeps (attempts 0 and 1 — attempt 2 raises immediately).
    assert len(sleep_calls) == 2
    # Attempt 0: 2^0 + jitter = ~1.1–1.5
    assert 1.0 <= sleep_calls[0] <= 1.6
    # Attempt 1: 2^1 + jitter = ~2.1–2.5
    assert 2.0 <= sleep_calls[1] <= 2.6


async def test_429_uses_retry_after(mock_aiohttp: aioresponses) -> None:
    """429 uses retry_after from response body, not exponential backoff."""
    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", status=429, payload={"retry_after": 200})
    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", payload={"_id": "srv1", "name": "OK"})

    sleep_calls: list[float] = []
    original_sleep = AsyncMock(side_effect=lambda d: sleep_calls.append(d))

    with patch("discord_ferry.migrator.api.asyncio.sleep", original_sleep):
        async with aiohttp.ClientSession() as session:
            result = await api_fetch_server(session, BASE_URL, TOKEN, "srv1")

    assert result["_id"] == "srv1"
    # Should have slept for retry_after ms converted to seconds (0.2s).
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == pytest.approx(0.2, abs=0.01)


# ---------------------------------------------------------------------------
# Circuit breaker tests
# ---------------------------------------------------------------------------


async def test_circuit_opens_after_consecutive_failures(
    mock_aiohttp: aioresponses, caplog: pytest.LogCaptureFixture
) -> None:
    """Circuit breaker opens after _CIRCUIT_THRESHOLD consecutive failures."""
    # Pre-load the circuit state just below threshold.
    _circuit_state.consecutive_failures = 4

    # This request will fail (502 x3), pushing failures to 5+ before next call.
    for _ in range(3):
        mock_aiohttp.get(f"{BASE_URL}/servers/srv1", status=502, body="Bad Gateway")

    with patch("discord_ferry.migrator.api.asyncio.sleep", new_callable=AsyncMock):
        async with aiohttp.ClientSession() as session:
            with pytest.raises(MigrationError):
                await api_fetch_server(session, BASE_URL, TOKEN, "srv1")

    # Failures were incremented: 4 + 2 retries + 1 final = 7
    assert _circuit_state.consecutive_failures >= 5

    # Next call should trigger circuit breaker warning.
    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", payload={"_id": "srv1", "name": "OK"})
    with (
        patch("discord_ferry.migrator.api.asyncio.sleep", new_callable=AsyncMock),
        caplog.at_level(logging.WARNING, logger="discord_ferry.migrator.api"),
    ):
        async with aiohttp.ClientSession() as session:
            await api_fetch_server(session, BASE_URL, TOKEN, "srv1")

    assert "Circuit breaker open" in caplog.text


async def test_circuit_resets_on_success(mock_aiohttp: aioresponses) -> None:
    """A successful request resets the circuit failure counter to zero."""
    _circuit_state.consecutive_failures = 4

    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", payload={"_id": "srv1", "name": "OK"})
    async with aiohttp.ClientSession() as session:
        await api_fetch_server(session, BASE_URL, TOKEN, "srv1")

    assert _circuit_state.consecutive_failures == 0


async def test_429_not_counted_as_circuit_failure(mock_aiohttp: aioresponses) -> None:
    """429 rate-limited responses do not increment circuit failure counter."""
    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", status=429, payload={"retry_after": 10})
    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", payload={"_id": "srv1", "name": "OK"})

    with patch("discord_ferry.migrator.api.asyncio.sleep", new_callable=AsyncMock):
        async with aiohttp.ClientSession() as session:
            await api_fetch_server(session, BASE_URL, TOKEN, "srv1")

    # Counter should be 0: 429 doesn't increment, success resets.
    assert _circuit_state.consecutive_failures == 0


# ---------------------------------------------------------------------------
# Semaphore tests
# ---------------------------------------------------------------------------


async def test_semaphore_not_initialized(mock_aiohttp: aioresponses) -> None:
    """Requests work correctly without initializing the semaphore."""
    import discord_ferry.migrator.api as _api_mod

    assert _api_mod._request_semaphore is None

    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", payload={"_id": "srv1", "name": "OK"})
    async with aiohttp.ClientSession() as session:
        result = await api_fetch_server(session, BASE_URL, TOKEN, "srv1")
    assert result["_id"] == "srv1"


async def test_max_concurrent_zero_clamped(mock_aiohttp: aioresponses) -> None:
    """init_request_semaphore(0) clamps to 1 and still works."""
    init_request_semaphore(0)

    import discord_ferry.migrator.api as _api_mod

    assert _api_mod._request_semaphore is not None
    # Semaphore value should be 1 (clamped from 0)
    assert _api_mod._request_semaphore._value == 1  # type: ignore[attr-defined]

    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", payload={"_id": "srv1", "name": "OK"})
    async with aiohttp.ClientSession() as session:
        result = await api_fetch_server(session, BASE_URL, TOKEN, "srv1")
    assert result["_id"] == "srv1"


async def test_semaphore_initialized_limits_concurrency(mock_aiohttp: aioresponses) -> None:
    """When semaphore is initialized, requests flow through it."""
    init_request_semaphore(3)

    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", payload={"_id": "srv1", "name": "OK"})
    async with aiohttp.ClientSession() as session:
        result = await api_fetch_server(session, BASE_URL, TOKEN, "srv1")
    assert result["_id"] == "srv1"


async def test_network_error_exponential_backoff(mock_aiohttp: aioresponses) -> None:
    """Network errors also use exponential backoff with jitter."""
    for _ in range(3):
        mock_aiohttp.get(
            f"{BASE_URL}/servers/srv1",
            exception=aiohttp.ClientError("Connection refused"),
        )

    sleep_calls: list[float] = []
    original_sleep = AsyncMock(side_effect=lambda d: sleep_calls.append(d))

    with patch("discord_ferry.migrator.api.asyncio.sleep", original_sleep):
        async with aiohttp.ClientSession() as session:
            with pytest.raises(MigrationError, match="Network error after 3 retries"):
                await api_fetch_server(session, BASE_URL, TOKEN, "srv1")

    # Two sleeps (attempts 0 and 1 — attempt 2 raises immediately).
    assert len(sleep_calls) == 2
    # Attempt 0: 2^0 + jitter = ~1.1–1.5
    assert 1.0 <= sleep_calls[0] <= 1.6
    # Attempt 1: 2^1 + jitter = ~2.1–2.5
    assert 2.0 <= sleep_calls[1] <= 2.6


# ---------------------------------------------------------------------------
# Adaptive 429 rate multiplier tests
# ---------------------------------------------------------------------------


async def test_rate_multiplier_increases_after_429_burst(mock_aiohttp: aioresponses) -> None:
    """Multiplier increases above 1.0 after more than 3 recent 429 responses."""
    import time as _time

    import discord_ferry.migrator.api as _api_mod

    # Pre-seed window with 3 recent timestamps (within 60 s).
    now = _time.monotonic()
    for _ in range(3):
        _api_mod._rate_429_window.append(now)

    # One more 429 → total recent = 4 → should ramp up multiplier.
    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", status=429, payload={"retry_after": 10})
    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", payload={"_id": "srv1", "name": "OK"})

    with patch("discord_ferry.migrator.api.asyncio.sleep", new_callable=AsyncMock):
        async with aiohttp.ClientSession() as session:
            await api_fetch_server(session, BASE_URL, TOKEN, "srv1")

    assert get_rate_multiplier() > 1.0


async def test_rate_multiplier_does_not_increase_below_threshold(
    mock_aiohttp: aioresponses,
) -> None:
    """Multiplier stays at 1.0 with 3 or fewer recent 429s (threshold is >3)."""
    # 1 429, then success.
    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", status=429, payload={"retry_after": 10})
    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", payload={"_id": "srv1", "name": "OK"})

    with patch("discord_ferry.migrator.api.asyncio.sleep", new_callable=AsyncMock):
        async with aiohttp.ClientSession() as session:
            await api_fetch_server(session, BASE_URL, TOKEN, "srv1")

    assert get_rate_multiplier() == pytest.approx(1.0)


async def test_rate_multiplier_caps_at_5x(mock_aiohttp: aioresponses) -> None:
    """Multiplier never exceeds 5.0 regardless of 429 burst size."""
    import time as _time

    import discord_ferry.migrator.api as _api_mod

    # Pre-seed window far above threshold and set multiplier near ceiling.
    now = _time.monotonic()
    for _ in range(20):
        _api_mod._rate_429_window.append(now)
    _api_mod._rate_multiplier = 4.0

    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", status=429, payload={"retry_after": 10})
    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", payload={"_id": "srv1", "name": "OK"})

    with patch("discord_ferry.migrator.api.asyncio.sleep", new_callable=AsyncMock):
        async with aiohttp.ClientSession() as session:
            await api_fetch_server(session, BASE_URL, TOKEN, "srv1")

    assert get_rate_multiplier() <= 5.0


async def test_rate_multiplier_decreases_after_clear_period(mock_aiohttp: aioresponses) -> None:
    """Multiplier decays toward 1.0 on successful requests with no recent 429s."""
    import discord_ferry.migrator.api as _api_mod

    # Set multiplier high; window holds only a stale (>30 s old) timestamp.
    _api_mod._rate_multiplier = 3.0
    _api_mod._rate_429_window.append(0.0)  # epoch — far in the past

    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", payload={"_id": "srv1", "name": "OK"})

    async with aiohttp.ClientSession() as session:
        await api_fetch_server(session, BASE_URL, TOKEN, "srv1")

    # Should have decayed: 3.0 * 0.75 = 2.25
    assert get_rate_multiplier() == pytest.approx(2.25, rel=1e-3)


async def test_rate_multiplier_does_not_decay_with_recent_429(
    mock_aiohttp: aioresponses,
) -> None:
    """Multiplier stays high when there is a very recent 429 in the window."""
    import time as _time

    import discord_ferry.migrator.api as _api_mod

    _api_mod._rate_multiplier = 3.0
    # Recent timestamp — within 30 s.
    _api_mod._rate_429_window.append(_time.monotonic())

    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", payload={"_id": "srv1", "name": "OK"})

    async with aiohttp.ClientSession() as session:
        await api_fetch_server(session, BASE_URL, TOKEN, "srv1")

    # Multiplier should NOT have decayed.
    assert get_rate_multiplier() == pytest.approx(3.0)


async def test_reset_rate_state() -> None:
    """_reset_rate_state clears window and resets multiplier to 1.0."""
    import time as _time

    import discord_ferry.migrator.api as _api_mod

    _api_mod._rate_multiplier = 4.5
    _api_mod._rate_429_window.append(_time.monotonic())

    _reset_rate_state()

    assert get_rate_multiplier() == pytest.approx(1.0)
    assert len(_api_mod._rate_429_window) == 0
