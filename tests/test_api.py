"""Tests for the Stoat REST API wrapper."""

from __future__ import annotations

import logging
import ssl
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aioresponses import aioresponses

from discord_ferry.config import FerryConfig
from discord_ferry.core.http import new_session
from discord_ferry.errors import DuplicateSendError, MigrationError
from discord_ferry.migrator.api import (
    _api_request,
    _circuit_state,
    _headers,
    _reset_circuit_state,
    _reset_rate_state,
    api_add_reaction,
    api_create_channel,
    api_create_emoji,
    api_create_invite,
    api_create_role,
    api_create_server,
    api_create_webhook,
    api_delete_channel,
    api_delete_emoji,
    api_delete_role,
    api_delete_webhook,
    api_edit_role,
    api_edit_server,
    api_execute_webhook,
    api_fetch_channel,
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


# ---------------------------------------------------------------------------
# _headers — auth header construction (Task 1)
# ---------------------------------------------------------------------------


def test_headers_with_token_includes_session_token() -> None:
    """A real token yields the x-session-token header (regression guard)."""
    h = _headers(TOKEN)
    assert h["x-session-token"] == TOKEN
    assert h["Content-Type"] == "application/json"


def test_headers_none_omits_session_token() -> None:
    """token=None produces headers WITHOUT x-session-token (auth-less webhook path)."""
    h = _headers(None)
    assert "x-session-token" not in h
    assert h["Content-Type"] == "application/json"


# ---------------------------------------------------------------------------
# api_create_invite (Task 2)
# ---------------------------------------------------------------------------


async def test_api_create_invite_reads_id(mock_aiohttp: aioresponses) -> None:
    """POST /channels/{id}/invites returns the invite; code read from _id."""
    mock_aiohttp.post(
        f"{BASE_URL}/channels/ch1/invites", payload={"_id": "inv_AB", "type": "Server"}
    )
    async with aiohttp.ClientSession() as session:
        result = await api_create_invite(session, BASE_URL, TOKEN, "ch1")
    assert result["_id"] == "inv_AB"


async def test_api_create_invite_reads_code_fallback(mock_aiohttp: aioresponses) -> None:
    """Some responses carry `code` instead of `_id`; both must be accessible."""
    mock_aiohttp.post(f"{BASE_URL}/channels/ch1/invites", payload={"code": "inv_CD"})
    async with aiohttp.ClientSession() as session:
        result = await api_create_invite(session, BASE_URL, TOKEN, "ch1")
    assert result.get("_id", result.get("code")) == "inv_CD"


# ---------------------------------------------------------------------------
# Webhook + channel-fetch wrappers (Task 3, probe-support)
# ---------------------------------------------------------------------------


async def test_api_create_webhook_reads_id_and_token(mock_aiohttp: aioresponses) -> None:
    """Webhook id comes from `id` (NOT _id); token from `token`."""
    mock_aiohttp.post(
        f"{BASE_URL}/channels/ch1/webhooks",
        payload={"id": "wh1", "token": "tok_w", "name": "Discord Ferry"},
    )
    async with aiohttp.ClientSession() as session:
        result = await api_create_webhook(session, BASE_URL, TOKEN, "ch1", name="Discord Ferry")
    assert result["id"] == "wh1"
    assert result["token"] == "tok_w"


async def test_api_fetch_channel_returns_type(mock_aiohttp: aioresponses) -> None:
    """GET /channels/{id} returns the channel object (used for Bug #194 check)."""
    mock_aiohttp.get(
        f"{BASE_URL}/channels/ch_tmp", payload={"_id": "ch_tmp", "channel_type": "Text"}
    )
    async with aiohttp.ClientSession() as session:
        result = await api_fetch_channel(session, BASE_URL, TOKEN, "ch_tmp")
    assert result["channel_type"] == "Text"


async def test_api_delete_webhook_404_ok(mock_aiohttp: aioresponses) -> None:
    """DELETE /webhooks/{id} treats 404 as success (idempotent teardown)."""
    mock_aiohttp.delete(f"{BASE_URL}/webhooks/wh1", status=404)
    async with aiohttp.ClientSession() as session:
        await api_delete_webhook(session, BASE_URL, TOKEN, "wh1")  # must not raise


# ---------------------------------------------------------------------------
# api_execute_webhook — auth-leak-safe (Task 4)
# ---------------------------------------------------------------------------


async def test_api_execute_webhook_sends_no_session_token(mock_aiohttp: aioresponses) -> None:
    """Execute auths via the URL token only — NO x-session-token; Idempotency-Key passes through."""
    captured: dict[str, str] = {}

    def cap(url: object, **kwargs: object) -> None:
        captured.update(kwargs.get("headers") or {})  # type: ignore[arg-type]

    mock_aiohttp.post(f"{BASE_URL}/webhooks/wh1/tok_w", payload={"_id": "m1"}, callback=cap)
    async with aiohttp.ClientSession() as session:
        await api_execute_webhook(
            session, BASE_URL, "wh1", "tok_w", content="hi", idempotency_key="k1"
        )
    assert "x-session-token" not in captured
    assert captured.get("Idempotency-Key") == "k1"


async def test_api_send_message_still_authenticated(mock_aiohttp: aioresponses) -> None:
    """Auth regression: ordinary sends MUST still carry x-session-token."""
    captured: dict[str, str] = {}

    def cap(url: object, **kwargs: object) -> None:
        captured.update(kwargs.get("headers") or {})  # type: ignore[arg-type]

    mock_aiohttp.post(f"{BASE_URL}/channels/ch1/messages", payload={"_id": "m1"}, callback=cap)
    async with aiohttp.ClientSession() as session:
        await api_send_message(session, BASE_URL, TOKEN, "ch1", content="hi")
    assert captured.get("x-session-token") == TOKEN


async def test_api_execute_webhook_retries_429(mock_aiohttp: aioresponses) -> None:
    """A 429 (retry_after in ms) is retried; the second attempt succeeds."""
    mock_aiohttp.post(f"{BASE_URL}/webhooks/wh1/tok_w", status=429, payload={"retry_after": 50})
    mock_aiohttp.post(f"{BASE_URL}/webhooks/wh1/tok_w", payload={"_id": "m9"})
    async with aiohttp.ClientSession() as session:
        result = await api_execute_webhook(session, BASE_URL, "wh1", "tok_w", content="hi")
    assert result["_id"] == "m9"


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


async def test_certificate_error_does_not_prime_the_circuit_breaker(
    mock_aiohttp: aioresponses,
) -> None:
    """SC-134-26.

    Placing the short-circuit below either increment would let five
    short-circuited channels open the breaker and add a 30s sleep. Patching
    asyncio.sleep and asserting it is never awaited catches a guard moved
    below `await asyncio.sleep(delay)`: on this single-attempt call, that
    placement would still let the sleep run before the guard raises, and the
    assertion would catch it.
    """
    _reset_circuit_state()
    key = aiohttp.client_reqrep.ConnectionKey("api.stoat.chat", 443, True, True, None, None, None)
    cert_error = aiohttp.ClientConnectorCertificateError(key, ssl.SSLCertVerificationError("bad"))

    mock_aiohttp.get(f"{BASE_URL}/servers/x", exception=cert_error)
    with patch("asyncio.sleep", new_callable=AsyncMock) as slept:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(MigrationError) as caught:
                await _api_request(session, "GET", f"{BASE_URL}/servers/x", TOKEN)

    assert "SSL_CERT_FILE" in str(caught.value)
    assert _circuit_state.consecutive_failures == 0
    assert slept.await_count == 0, "a certificate error must not pay the backoff sleep"


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


# ---------------------------------------------------------------------------
# Rollback DELETE wrappers + expected_404_ok (SC-3, SC-6, SC-7)
# ---------------------------------------------------------------------------


async def test_api_delete_channel_204(mock_aiohttp: aioresponses) -> None:
    """SC-3: DELETE /channels/<id> returns 204 — wrapper returns cleanly."""
    mock_aiohttp.delete(f"{BASE_URL}/channels/ch01", status=204)
    async with aiohttp.ClientSession() as session:
        result = await api_delete_channel(session, BASE_URL, TOKEN, "ch01")
    assert result is None
    assert _circuit_state.consecutive_failures == 0


async def test_api_delete_role_204(mock_aiohttp: aioresponses) -> None:
    """SC-3: DELETE /servers/<id>/roles/<id> returns 204 — wrapper returns cleanly."""
    mock_aiohttp.delete(f"{BASE_URL}/servers/srv01/roles/role01", status=204)
    async with aiohttp.ClientSession() as session:
        result = await api_delete_role(session, BASE_URL, TOKEN, "srv01", "role01")
    assert result is None
    assert _circuit_state.consecutive_failures == 0


async def test_api_delete_emoji_204(mock_aiohttp: aioresponses) -> None:
    """SC-3: DELETE /custom/emoji/<id> returns 204 — wrapper returns cleanly."""
    mock_aiohttp.delete(f"{BASE_URL}/custom/emoji/em01", status=204)
    async with aiohttp.ClientSession() as session:
        result = await api_delete_emoji(session, BASE_URL, TOKEN, "em01")
    assert result is None
    assert _circuit_state.consecutive_failures == 0


async def test_expected_404_ok_resets_circuit_state(mock_aiohttp: aioresponses) -> None:
    """SC-6: 404+expected_404_ok resets consecutive_failures AND decays rate_multiplier."""
    import discord_ferry.migrator.api as _api_mod

    _circuit_state.consecutive_failures = 3
    _api_mod._rate_multiplier = 2.5

    mock_aiohttp.delete(f"{BASE_URL}/channels/ch01", status=404)
    async with aiohttp.ClientSession() as session:
        result = await api_delete_channel(session, BASE_URL, TOKEN, "ch01")

    assert result is None
    assert _circuit_state.consecutive_failures == 0
    # 2.5 * 0.75 = 1.875, above the 1.0 floor.
    assert get_rate_multiplier() == pytest.approx(1.875)


async def test_expected_404_ok_false_still_raises(mock_aiohttp: aioresponses) -> None:
    """SC-7: default (expected_404_ok=False) — 404 still raises MigrationError."""
    mock_aiohttp.get(
        f"{BASE_URL}/servers/missing",
        status=404,
        payload={"type": "NotFound"},
    )
    async with aiohttp.ClientSession() as session:
        with pytest.raises(MigrationError, match="API error 404"):
            await _api_request(session, "GET", f"{BASE_URL}/servers/missing", TOKEN)


async def test_expected_404_ok_with_real_404_body_discarded(
    mock_aiohttp: aioresponses,
) -> None:
    """Edge: 404 body present but discarded by expected_404_ok path."""
    mock_aiohttp.delete(
        f"{BASE_URL}/channels/ch01",
        status=404,
        payload={"type": "NotFound"},
    )
    async with aiohttp.ClientSession() as session:
        result = await api_delete_channel(session, BASE_URL, TOKEN, "ch01")
    assert result is None  # body discarded, just like 204.


async def test_expected_404_ok_with_204_unchanged_behaviour(
    mock_aiohttp: aioresponses,
) -> None:
    """Sanity: 204 still works with expected_404_ok=True — no regression."""
    mock_aiohttp.delete(f"{BASE_URL}/channels/ch01", status=204)
    async with aiohttp.ClientSession() as session:
        result = await _api_request(
            session,
            "DELETE",
            f"{BASE_URL}/channels/ch01",
            TOKEN,
            expected_404_ok=True,
        )
    assert result == {}
    assert _circuit_state.consecutive_failures == 0


@pytest.mark.asyncio
async def test_api_edit_channel_patches_body():
    with aioresponses() as m:
        m.patch("https://stoat.test/channels/ch1", payload={"_id": "ch1"})
        async with aiohttp.ClientSession() as s:
            from discord_ferry.migrator.api import api_edit_channel

            result = await api_edit_channel(
                s,
                "https://stoat.test",
                "tok",
                "ch1",
                slowmode=30,
                voice={"max_users": 5},
            )
    assert result["_id"] == "ch1"


@pytest.mark.asyncio
async def test_api_fetch_root_returns_config():
    from discord_ferry.migrator.api import api_fetch_root

    with aioresponses() as m:
        m.get(
            "https://stoat.test/",
            payload={"features": {"limits": {"global": {"server_roles": 200}}}},
        )
        async with aiohttp.ClientSession() as s:
            root = await api_fetch_root(s, "https://stoat.test")
    assert root["features"]["limits"]["global"]["server_roles"] == 200


@pytest.mark.asyncio
async def test_api_fetch_root_returns_empty_on_error():
    from discord_ferry.migrator.api import api_fetch_root

    with aioresponses() as m:
        m.get("https://stoat.test/", status=500)
        async with aiohttp.ClientSession() as s:
            assert await api_fetch_root(s, "https://stoat.test") == {}


@pytest.mark.asyncio
async def test_api_fetch_root_returns_empty_on_client_error():
    from discord_ferry.migrator.api import api_fetch_root

    with aioresponses() as m:
        m.get("https://stoat.test/", exception=aiohttp.ClientError())
        async with aiohttp.ClientSession() as s:
            assert await api_fetch_root(s, "https://stoat.test") == {}


@pytest.mark.asyncio
async def test_api_fetch_root_returns_empty_on_malformed_json():
    from discord_ferry.migrator.api import api_fetch_root

    with aioresponses() as m:
        m.get(
            "https://stoat.test/",
            body="not json",
            status=200,
            content_type="application/json",
        )
        async with aiohttp.ClientSession() as s:
            assert await api_fetch_root(s, "https://stoat.test") == {}


# ---------------------------------------------------------------------------
# Batch 6 — S1 (non-JSON 429 + Retry-After + breaker invariant) + S2 (non-JSON 2xx)
# ---------------------------------------------------------------------------


def _sleep_capture() -> tuple[list[float], AsyncMock]:
    calls: list[float] = []
    return calls, AsyncMock(side_effect=lambda d: calls.append(d))


async def test_429_non_json_body_honors_header_no_breaker(
    mock_aiohttp: aioresponses,
) -> None:  # SC-1
    mock_aiohttp.get(
        f"{BASE_URL}/servers/srv1",
        status=429,
        body="<html>rl</html>",
        content_type="text/html",
        headers={"Retry-After": "2"},
    )
    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", payload={"_id": "srv1", "name": "OK"})
    calls, sleep = _sleep_capture()
    with patch("discord_ferry.migrator.api.asyncio.sleep", sleep):
        async with aiohttp.ClientSession() as session:
            result = await api_fetch_server(session, BASE_URL, TOKEN, "srv1")
    assert result["_id"] == "srv1"
    assert _circuit_state.consecutive_failures == 0  # breaker NOT primed by a non-JSON 429
    assert calls[0] == pytest.approx(2.0, abs=0.05)  # header seconds honored


async def test_429_non_json_no_header_default_no_breaker(
    mock_aiohttp: aioresponses,
) -> None:  # SC-2
    mock_aiohttp.get(
        f"{BASE_URL}/servers/srv1", status=429, body="<html>rl</html>", content_type="text/html"
    )
    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", payload={"_id": "srv1"})
    calls, sleep = _sleep_capture()
    with patch("discord_ferry.migrator.api.asyncio.sleep", sleep):
        async with aiohttp.ClientSession() as session:
            await api_fetch_server(session, BASE_URL, TOKEN, "srv1")
    assert _circuit_state.consecutive_failures == 0
    assert calls[0] == pytest.approx(1.0, abs=0.05)  # default fallback


async def test_429_header_beats_body(mock_aiohttp: aioresponses) -> None:  # SC-3
    mock_aiohttp.get(
        f"{BASE_URL}/servers/srv1",
        status=429,
        payload={"retry_after": 5000},
        headers={"Retry-After": "2"},
    )
    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", payload={"_id": "srv1"})
    calls, sleep = _sleep_capture()
    with patch("discord_ferry.migrator.api.asyncio.sleep", sleep):
        async with aiohttp.ClientSession() as session:
            await api_fetch_server(session, BASE_URL, TOKEN, "srv1")
    assert calls[0] == pytest.approx(2.0, abs=0.05)  # header, not body 5.0s


async def test_429_x_ratelimit_reset_after(mock_aiohttp: aioresponses) -> None:  # SC-4
    """A non-JSON 429 still honours the header rather than the default delay.

    The advertised value is MILLISECONDS (1500 -> 1.5s). This test previously sent
    "1.5" and asserted 1.5s, which codified the unit bug fixed in v2.7.2 — the
    header was being read as seconds. Value corrected; the test's original intent
    (header honoured even when the body is unparseable HTML) is unchanged.
    """
    mock_aiohttp.get(
        f"{BASE_URL}/servers/srv1",
        status=429,
        body="<html>",
        content_type="text/html",
        headers={"X-RateLimit-Reset-After": "1500"},
    )
    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", payload={"_id": "srv1"})
    calls, sleep = _sleep_capture()
    with patch("discord_ferry.migrator.api.asyncio.sleep", sleep):
        async with aiohttp.ClientSession() as session:
            await api_fetch_server(session, BASE_URL, TOKEN, "srv1")
    assert calls[0] == pytest.approx(1.5, abs=0.05)


async def test_429_http_date_header_ignored(mock_aiohttp: aioresponses) -> None:  # SC-5
    mock_aiohttp.get(
        f"{BASE_URL}/servers/srv1",
        status=429,
        payload={"retry_after": 300},
        headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
    )
    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", payload={"_id": "srv1"})
    calls, sleep = _sleep_capture()
    with patch("discord_ferry.migrator.api.asyncio.sleep", sleep):
        async with aiohttp.ClientSession() as session:
            await api_fetch_server(session, BASE_URL, TOKEN, "srv1")
    assert calls[0] == pytest.approx(0.3, abs=0.05)  # HTTP-date ignored → body 300ms


async def test_429_delay_capped(mock_aiohttp: aioresponses) -> None:  # SC-6
    mock_aiohttp.get(
        f"{BASE_URL}/servers/srv1",
        status=429,
        body="<html>",
        content_type="text/html",
        headers={"Retry-After": "9999"},
    )
    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", payload={"_id": "srv1"})
    calls, sleep = _sleep_capture()
    with patch("discord_ferry.migrator.api.asyncio.sleep", sleep):
        async with aiohttp.ClientSession() as session:
            await api_fetch_server(session, BASE_URL, TOKEN, "srv1")
    assert calls[0] == 60  # _MAX_RETRY_DELAY_SECONDS


async def test_429_exhausted_does_not_prime_breaker(mock_aiohttp: aioresponses) -> None:  # SC-7
    for _ in range(3):
        mock_aiohttp.get(
            f"{BASE_URL}/servers/srv1", status=429, body="<html>", content_type="text/html"
        )
    with patch("discord_ferry.migrator.api.asyncio.sleep", new_callable=AsyncMock):
        async with aiohttp.ClientSession() as session:
            with pytest.raises(MigrationError, match="after 3 retries: 429"):
                await api_fetch_server(session, BASE_URL, TOKEN, "srv1")
    assert _circuit_state.consecutive_failures == 0  # 429 never primes, even exhausted


async def test_502_exhausted_primes_breaker(mock_aiohttp: aioresponses) -> None:  # SC-8 (contrast)
    for _ in range(3):
        mock_aiohttp.get(f"{BASE_URL}/servers/srv1", status=502, body="Bad Gateway")
    with patch("discord_ferry.migrator.api.asyncio.sleep", new_callable=AsyncMock):
        async with aiohttp.ClientSession() as session:
            with pytest.raises(MigrationError, match="after 3 retries: 502"):
                await api_fetch_server(session, BASE_URL, TOKEN, "srv1")
    assert _circuit_state.consecutive_failures == 3  # 5xx DOES prime (contrast SC-7)


async def test_429_empty_body_falls_back(mock_aiohttp: aioresponses) -> None:  # SC-9
    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", status=429, body="", content_type="text/html")
    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", payload={"_id": "srv1"})
    calls, sleep = _sleep_capture()
    with patch("discord_ferry.migrator.api.asyncio.sleep", sleep):
        async with aiohttp.ClientSession() as session:
            await api_fetch_server(session, BASE_URL, TOKEN, "srv1")
    assert _circuit_state.consecutive_failures == 0
    assert calls[0] == pytest.approx(1.0, abs=0.05)


async def test_non_json_200_clear_error(mock_aiohttp: aioresponses) -> None:  # SC-10
    mock_aiohttp.get(
        f"{BASE_URL}/servers/srv1", status=200, body="<html>nope</html>", content_type="text/html"
    )
    async with aiohttp.ClientSession() as session:
        with pytest.raises(MigrationError) as exc:
            await api_fetch_server(session, BASE_URL, TOKEN, "srv1")
    assert "Network error after 3 retries" not in str(exc.value)
    assert "text/html" in str(exc.value)


async def test_non_json_201_clear_error(mock_aiohttp: aioresponses) -> None:  # SC-11
    mock_aiohttp.post(
        f"{BASE_URL}/servers/create",
        status=201,
        body="<html>nope</html>",
        content_type="text/html",
    )
    async with aiohttp.ClientSession() as session:
        with pytest.raises(MigrationError) as exc:
            await api_create_server(session, BASE_URL, TOKEN, "My Server")
    assert "Network error after 3 retries" not in str(exc.value)
    assert "text/html" in str(exc.value)


async def test_json_200_unaffected(mock_aiohttp: aioresponses) -> None:  # SC-12
    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", payload={"_id": "srv1", "name": "OK"})
    async with aiohttp.ClientSession() as session:
        result = await api_fetch_server(session, BASE_URL, TOKEN, "srv1")
    assert result == {"_id": "srv1", "name": "OK"}


async def test_genuine_client_error_still_network_error(
    mock_aiohttp: aioresponses,
) -> None:  # SC-13
    for _ in range(3):
        mock_aiohttp.get(f"{BASE_URL}/servers/srv1", exception=aiohttp.ClientError("reset"))
    with patch("discord_ferry.migrator.api.asyncio.sleep", new_callable=AsyncMock):
        async with aiohttp.ClientSession() as session:
            with pytest.raises(MigrationError, match="Network error after 3 retries"):
                await api_fetch_server(session, BASE_URL, TOKEN, "srv1")
    assert _circuit_state.consecutive_failures >= 1


async def test_malformed_json_200_clear_error(mock_aiohttp: aioresponses) -> None:  # SC-14
    mock_aiohttp.get(
        f"{BASE_URL}/servers/srv1",
        status=200,
        body="{not valid json",
        content_type="application/json",
    )
    async with aiohttp.ClientSession() as session:
        with pytest.raises(MigrationError) as exc:
            await api_fetch_server(session, BASE_URL, TOKEN, "srv1")
    assert "Network error after 3 retries" not in str(exc.value)


async def test_429_null_retry_after_falls_back(
    mock_aiohttp: aioresponses,
) -> None:  # SC-9b (review M2)
    """A 429 body with retry_after=null must not crash; falls back to the default delay."""
    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", status=429, payload={"retry_after": None})
    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", payload={"_id": "srv1"})
    calls, sleep = _sleep_capture()
    with patch("discord_ferry.migrator.api.asyncio.sleep", sleep):
        async with aiohttp.ClientSession() as session:
            await api_fetch_server(session, BASE_URL, TOKEN, "srv1")
    assert _circuit_state.consecutive_failures == 0
    assert calls[0] == pytest.approx(1.0, abs=0.05)


# ---------------------------------------------------------------------------
# Rate-limit header units (batch 1)
#
# Stoat documents X-RateLimit-Reset-After as MILLISECONDS:
#   developers.stoat.chat/developers/api/ratelimits
#   "Milliseconds left until calls are replenished"
# Discord's identically-named header is delta-SECONDS. Before batch 1 both were
# parsed by same-named, same-bodied functions in migrator/api.py and
# discord/client.py — one correct, one not.
#
# The header branch was not merely untested: test_429_x_ratelimit_reset_after
# above ASSERTED the wrong unit ("1.5" -> 1.5s), so the suite actively locked the
# bug in. Anyone who suspected it would have seen a red test and backed off. That
# test's value is corrected to milliseconds; the cases below pin the semantics
# that actually distinguish the two services.
# ---------------------------------------------------------------------------


async def test_429_reset_after_header_is_milliseconds(mock_aiohttp: aioresponses) -> None:
    """SC-1: X-RateLimit-Reset-After is MILLISECONDS — 10000 means 10s, not 10000s.

    Read as seconds it becomes 10000, which the 60s cap clamps to a full minute:
    a 6x over-sleep on every rate-limit hit, worst in the message phase.
    """
    mock_aiohttp.get(
        f"{BASE_URL}/servers/srv1",
        status=429,
        headers={"X-RateLimit-Reset-After": "10000"},
    )
    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", payload={"_id": "srv1", "name": "OK"})

    calls, sleep = _sleep_capture()
    with patch("discord_ferry.migrator.api.asyncio.sleep", sleep):
        async with aiohttp.ClientSession() as session:
            result = await api_fetch_server(session, BASE_URL, TOKEN, "srv1")

    assert result["name"] == "OK"
    assert calls[0] == pytest.approx(10.0, abs=0.05)


async def test_429_retry_after_header_is_seconds(mock_aiohttp: aioresponses) -> None:
    """SC-2: Retry-After is RFC 9110 delta-seconds even in front of Stoat.

    Stoat itself never sends it, but a proxy or CDN in front of Stoat may.
    """
    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", status=429, headers={"Retry-After": "5"})
    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", payload={"_id": "srv1"})

    calls, sleep = _sleep_capture()
    with patch("discord_ferry.migrator.api.asyncio.sleep", sleep):
        async with aiohttp.ClientSession() as session:
            await api_fetch_server(session, BASE_URL, TOKEN, "srv1")

    assert calls[0] == pytest.approx(5.0, abs=0.05)


async def test_429_header_takes_precedence_over_body(mock_aiohttp: aioresponses) -> None:
    """SC-4: the header wins over the body, and is still converted from ms."""
    mock_aiohttp.get(
        f"{BASE_URL}/servers/srv1",
        status=429,
        headers={"X-RateLimit-Reset-After": "10000"},
        payload={"retry_after": 250},
    )
    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", payload={"_id": "srv1"})

    calls, sleep = _sleep_capture()
    with patch("discord_ferry.migrator.api.asyncio.sleep", sleep):
        async with aiohttp.ClientSession() as session:
            await api_fetch_server(session, BASE_URL, TOKEN, "srv1")

    assert calls[0] == pytest.approx(10.0, abs=0.05)


async def test_429_retry_after_beats_reset_after(mock_aiohttp: aioresponses) -> None:
    """SC-2b: with BOTH headers present, Retry-After wins — and keeps its own unit.

    Pins the precedence inside _stoat_rate_delay_seconds: a proxy's delta-seconds
    Retry-After is authoritative over Stoat's millisecond header, and must not be
    divided by 1000 on the way out.
    """
    mock_aiohttp.get(
        f"{BASE_URL}/servers/srv1",
        status=429,
        headers={"Retry-After": "3", "X-RateLimit-Reset-After": "10000"},
    )
    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", payload={"_id": "srv1"})

    calls, sleep = _sleep_capture()
    with patch("discord_ferry.migrator.api.asyncio.sleep", sleep):
        async with aiohttp.ClientSession() as session:
            await api_fetch_server(session, BASE_URL, TOKEN, "srv1")

    assert calls[0] == pytest.approx(3.0, abs=0.05)


async def test_429_http_date_retry_after_falls_through(mock_aiohttp: aioresponses) -> None:
    """SC-5: a non-numeric Retry-After (HTTP-date) is ignored, not crashed on."""
    mock_aiohttp.get(
        f"{BASE_URL}/servers/srv1",
        status=429,
        headers={"Retry-After": "Wed, 01 Aug 2026 12:00:00 GMT"},
        payload={"retry_after": 300},
    )
    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", payload={"_id": "srv1"})

    calls, sleep = _sleep_capture()
    with patch("discord_ferry.migrator.api.asyncio.sleep", sleep):
        async with aiohttp.ClientSession() as session:
            await api_fetch_server(session, BASE_URL, TOKEN, "srv1")

    assert calls[0] == pytest.approx(0.3, abs=0.05)


async def test_429_negative_advertised_delay_is_floored(mock_aiohttp: aioresponses) -> None:
    """SC-6b: a negative advertised delay is floored at 0, never passed through.

    A negative value parses fine as a float, and asyncio.sleep() of a negative
    number returns immediately — so without the floor a hostile or buggy header
    would disable backoff and turn the retry loop hot.
    """
    mock_aiohttp.get(
        f"{BASE_URL}/servers/srv1",
        status=429,
        headers={"X-RateLimit-Reset-After": "-5000"},
    )
    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", payload={"_id": "srv1"})

    calls, sleep = _sleep_capture()
    with patch("discord_ferry.migrator.api.asyncio.sleep", sleep):
        async with aiohttp.ClientSession() as session:
            await api_fetch_server(session, BASE_URL, TOKEN, "srv1")

    assert calls[0] == 0.0


async def test_429_reset_after_is_capped(mock_aiohttp: aioresponses) -> None:
    """SC-6: an absurd advertised delay is still clamped to the 60s cap."""
    mock_aiohttp.get(
        f"{BASE_URL}/servers/srv1",
        status=429,
        headers={"X-RateLimit-Reset-After": "999999999"},
    )
    mock_aiohttp.get(f"{BASE_URL}/servers/srv1", payload={"_id": "srv1"})

    calls, sleep = _sleep_capture()
    with patch("discord_ferry.migrator.api.asyncio.sleep", sleep):
        async with aiohttp.ClientSession() as session:
            await api_fetch_server(session, BASE_URL, TOKEN, "srv1")

    assert calls[0] == pytest.approx(60.0, abs=0.05)


async def test_get_session_prefers_the_config_session() -> None:
    """SC-134-5: the config branch must survive the factory swap."""
    from discord_ferry.core.http import new_session
    from discord_ferry.migrator.api import get_session

    async with new_session() as shared:
        config = FerryConfig(export_dir=Path("."), stoat_url="https://x.invalid", token="t")
        config.session = shared
        async with get_session(config) as yielded:
            assert yielded is shared
        assert not shared.closed


# ---------------------------------------------------------------------------
# #135 — a refusing proxy must name itself at api.py:300
# ---------------------------------------------------------------------------


async def test_a_refused_proxy_names_the_proxy(fake_proxy, proxy_env, os_proxy) -> None:
    """SC-135-28. Killing: proxy_hint defined and never called at api.py:300.

    A real socket, because aioresponses patches ClientSession._request and the
    request object -- the only thing that carries a proxy -- is never built.

    The circuit-breaker assertion is last on purpose: the proxy assertions above
    it are what grade the wiring, and they must be the ones that fail first.
    """
    _reset_circuit_state()
    make, _ = fake_proxy
    server = await make(b"403 Forbidden")
    port = server.sockets[0].getsockname()[1]
    async with server:
        with os_proxy({}), proxy_env(HTTPS_PROXY=f"http://127.0.0.1:{port}"):
            async with new_session() as session:
                with pytest.raises(MigrationError) as caught:
                    await api_fetch_server(session, BASE_URL, TOKEN, "srv1")

    message = str(caught.value)
    assert "Network error" in message
    assert f"The request to api.test went through the proxy at 127.0.0.1:{port}" in message
    assert "FERRY_DISABLE_PROXY" in message
    # A 403 from the proxy is permanent, so the short-circuit sits above both
    # consecutive_failures increments, exactly as the certificate case does.
    assert _circuit_state.consecutive_failures == 0


async def test_a_proxy_502_still_retries(fake_proxy, proxy_env, os_proxy) -> None:
    """SC-135-37 at api.py. Killing: wiring proxy_hint here without the
    permanence gate, i.e. `if hint is not None:` alone.

    A 502 from the proxy is NOT permanent, so it must keep the retries it had
    before proxy support existed. The captured-request count is the FIRST
    assertion, so it is the one that fails under the ungated mutant; the message
    assertion below it is reached only when the retries actually happened.
    """
    from discord_ferry.migrator.api import MAX_API_RETRIES

    _reset_circuit_state()
    make, captured = fake_proxy
    server = await make(b"502 Bad Gateway")
    port = server.sockets[0].getsockname()[1]
    async with server:
        with (
            os_proxy({}),
            proxy_env(HTTPS_PROXY=f"http://127.0.0.1:{port}"),
            patch("discord_ferry.migrator.api.asyncio.sleep", new_callable=AsyncMock),
        ):
            async with new_session() as session:
                with pytest.raises(MigrationError) as caught:
                    await api_fetch_server(session, BASE_URL, TOKEN, "srv1")

    assert len(captured) == MAX_API_RETRIES, "the 502 was treated as permanent and never retried"
    message = str(caught.value)
    # The exhaustion message still names the proxy: the hint is computed either
    # way, only the short-circuit is gated.
    assert f"Network error after {MAX_API_RETRIES} retries" in message
    assert f"The request to api.test went through the proxy at 127.0.0.1:{port}" in message


async def test_a_certificate_error_against_an_https_proxy_names_the_proxy_not_the_bundle(
    mock_aiohttp: aioresponses, proxy_env, os_proxy
) -> None:
    """SC-135-52, integration half. Killing: concatenating the two hints.

    The eight site tests all use an http:// proxy, where tls_hint is None, so a
    site written as `proxy_hint(...) + (tls_hint(...) or "")` passes every one
    of them. Only an https:// proxy makes both fire, and only then does the
    precedence rule have an observable consequence: SSL_CERT_FILE advice for a
    host the user never configured as a certificate authority.

    aioresponses injects the exception rather than a socket serving it, which is
    the same technique test_certificate_error_does_not_prime_the_circuit_breaker
    uses. The scan is warmed through resolve_proxy, not by assigning the global.
    """
    from discord_ferry.core import http

    _reset_circuit_state()
    key = aiohttp.client_reqrep.ConnectionKey("secure-proxy", 8443, True, True, None, None, None)
    cert_error = aiohttp.ClientConnectorCertificateError(key, ssl.SSLCertVerificationError("bad"))
    mock_aiohttp.get(f"{BASE_URL}/servers/x", exception=cert_error)

    with os_proxy({}), proxy_env(HTTPS_PROXY="https://secure-proxy:8443"):
        assert http.resolve_proxy(f"{BASE_URL}/servers/x") is not None
        with patch("asyncio.sleep", new_callable=AsyncMock):
            async with aiohttp.ClientSession() as session:
                with pytest.raises(MigrationError) as caught:
                    await _api_request(session, "GET", f"{BASE_URL}/servers/x", TOKEN)

    message = str(caught.value)
    assert "went through the proxy at secure-proxy:8443" in message
    assert "SSL_CERT_FILE" not in message, (
        "proxy wins and REPLACES the certificate hint; appending both sends the "
        "user to a CA bundle for a host they never configured"
    )


# ---------------------------------------------------------------------------
# DuplicateNonce 409 (#107 batch 7, chunk #195, task #201)
# ---------------------------------------------------------------------------


async def test_duplicate_nonce_raises_duplicate_send_error(mock_aiohttp: aioresponses) -> None:
    """SC-1.1: a DuplicateNonce body raises the distinct subclass.

    Body shape read from revoltchat/backend crates/core/result/src/lib.rs: ErrorType
    carries serde(tag = "type") and Error.error_type carries serde(flatten), so the
    variant tag is a top-level key.
    """
    mock_aiohttp.post(
        f"{BASE_URL}/channels/c1/messages",
        status=409,
        payload={"type": "DuplicateNonce", "location": "crates/x/src/lib.rs:12:3"},
    )
    async with aiohttp.ClientSession() as session:
        with pytest.raises(DuplicateSendError) as ei:
            await api_send_message(
                session, BASE_URL, TOKEN, "c1", content="x", idempotency_key="ferry-1"
            )
    assert isinstance(ei.value, MigrationError), (
        "must subclass MigrationError so an uncatching call site is unaffected"
    )


async def test_other_409_variant_raises_generic_error(mock_aiohttp: aioresponses) -> None:
    """SC-1.2: a different 409 variant is NOT a duplicate. Kills mutant M6."""
    mock_aiohttp.post(
        f"{BASE_URL}/channels/c1/messages",
        status=409,
        payload={"type": "SomethingElse", "location": "x.rs:1:1"},
    )
    async with aiohttp.ClientSession() as session:
        with pytest.raises(MigrationError) as ei:
            await api_send_message(
                session, BASE_URL, TOKEN, "c1", content="x", idempotency_key="ferry-1"
            )
    assert not isinstance(ei.value, DuplicateSendError)
    assert "409" in str(ei.value)


@pytest.mark.parametrize("payload", [["not", "an", "object"], 42, "bare string"])
async def test_non_object_409_body_does_not_crash(payload: object) -> None:
    """SC-1.3: a JSON 409 body that is not a dict must not raise AttributeError.

    Kills mutant M5, dropping the isinstance guard the 429 branch already applies.
    """
    with aioresponses() as m:
        m.post(f"{BASE_URL}/channels/c1/messages", status=409, payload=payload)
        async with aiohttp.ClientSession() as session:
            with pytest.raises(MigrationError) as ei:
                await api_send_message(
                    session, BASE_URL, TOKEN, "c1", content="x", idempotency_key="ferry-1"
                )
    assert not isinstance(ei.value, DuplicateSendError)


async def test_409_with_no_idempotency_key_is_unchanged(mock_aiohttp: aioresponses) -> None:
    """SC-1.4: an endpoint that sends no key still sees the generic error.

    A DuplicateNonce is only reachable when a key was sent, which is what makes the
    opt-in flag redundant. This pins that an unrelated 409 elsewhere is untouched.
    """
    mock_aiohttp.post(
        f"{BASE_URL}/servers/srv1/roles", status=409, payload={"type": "AlreadyInGroup"}
    )
    async with aiohttp.ClientSession() as session:
        with pytest.raises(MigrationError) as ei:
            await api_create_role(session, BASE_URL, TOKEN, "srv1", "role")
    assert not isinstance(ei.value, DuplicateSendError)


async def test_duplicate_path_resets_the_circuit_breaker(mock_aiohttp: aioresponses) -> None:
    """SC-1.5: a duplicate is a delivered message, so it must not prime the breaker.

    No functional test would notice if this bookkeeping were dropped, which is why it
    gets its own test.
    """
    _circuit_state.consecutive_failures = 3
    try:
        mock_aiohttp.post(
            f"{BASE_URL}/channels/c1/messages",
            status=409,
            payload={"type": "DuplicateNonce", "location": "x.rs:1:1"},
        )
        async with aiohttp.ClientSession() as session:
            with pytest.raises(DuplicateSendError):
                await api_send_message(
                    session, BASE_URL, TOKEN, "c1", content="x", idempotency_key="ferry-1"
                )
        assert _circuit_state.consecutive_failures == 0
    finally:
        _reset_circuit_state()


async def test_uncatching_call_site_still_sees_a_migration_error(
    mock_aiohttp: aioresponses,
) -> None:
    """SC-1.6: the property that removes the need for an opt-in flag.

    A caller written before this class existed catches MigrationError or Exception.
    Both must still catch a duplicate.
    """
    mock_aiohttp.post(
        f"{BASE_URL}/channels/c1/messages",
        status=409,
        payload={"type": "DuplicateNonce", "location": "x.rs:1:1"},
    )
    caught: str = ""
    async with aiohttp.ClientSession() as session:
        try:
            await api_send_message(
                session, BASE_URL, TOKEN, "c1", content="x", idempotency_key="ferry-1"
            )
        except MigrationError as exc:  # the pre-existing handler shape
            caught = type(exc).__name__
    assert caught == "DuplicateSendError"
