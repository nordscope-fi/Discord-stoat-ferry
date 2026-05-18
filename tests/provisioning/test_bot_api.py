"""Tests for tests/provisioning/_bot_api.py."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import aiohttp
import pytest
from aioresponses import aioresponses

from tests.provisioning._bot_api import (
    BotApi,
    ProvisioningAuthError,
    ProvisioningError,
    ProvisioningPermissionError,
    ProvisioningRateLimitError,
    TokenRedactingFilter,
    _request_with_retry,
)

if TYPE_CHECKING:
    from collections.abc import Generator

DISCORD_API = "https://discord.com/api/v10"
TOKEN = "test-bot-token"
GUILD_ID = "987654321"


def test_exception_hierarchy_is_self_contained() -> None:
    """ProvisioningError hierarchy must NOT inherit from FerryError.

    The import firewall is structural (tests/ not in wheels) and the type
    hierarchy reinforces it: code in src/discord_ferry/ that catches
    FerryError will not accidentally catch ProvisioningError.
    """
    assert issubclass(ProvisioningAuthError, ProvisioningError)
    assert issubclass(ProvisioningPermissionError, ProvisioningError)
    assert issubclass(ProvisioningRateLimitError, ProvisioningError)
    assert issubclass(ProvisioningError, Exception)
    # The firewall in types:
    from discord_ferry.errors import FerryError

    assert not issubclass(ProvisioningError, FerryError)


def test_token_redacting_filter_scrubs_token_from_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Filter must replace literal token with <TOKEN> in record.getMessage()."""
    token = "secret-token-MTM0NTY3.ABCdef"
    fltr = TokenRedactingFilter(token)
    logger = logging.getLogger("test_token_filter")
    logger.addFilter(fltr)
    logger.setLevel(logging.DEBUG)

    with caplog.at_level(logging.DEBUG, logger="test_token_filter"):
        logger.info("about to call: token=%s", token)

    assert token not in caplog.text
    assert "<TOKEN>" in caplog.text


def test_token_redacting_filter_handles_no_token_in_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Filter must not modify records that don't contain the token."""
    token = "secret-token-xyz"
    fltr = TokenRedactingFilter(token)
    logger = logging.getLogger("test_token_filter_clean")
    logger.addFilter(fltr)
    logger.setLevel(logging.DEBUG)

    with caplog.at_level(logging.DEBUG, logger="test_token_filter_clean"):
        logger.info("normal log line with no secrets")

    assert "normal log line with no secrets" in caplog.text


@pytest.fixture
def mock_discord() -> Generator[aioresponses, None, None]:
    with aioresponses() as m:
        yield m


async def test_request_with_retry_succeeds_first_try(mock_discord: aioresponses) -> None:
    mock_discord.get(f"{DISCORD_API}/test", payload={"ok": True}, status=200)
    async with aiohttp.ClientSession() as session:
        result = await _request_with_retry(
            session, "GET", f"{DISCORD_API}/test", headers={}, json_body=None
        )
    assert result == {"ok": True}


async def test_request_with_retry_429_then_success(mock_discord: aioresponses) -> None:
    mock_discord.get(
        f"{DISCORD_API}/test",
        status=429,
        payload={"retry_after": 0.01},
    )
    mock_discord.get(f"{DISCORD_API}/test", payload={"ok": True}, status=200)
    async with aiohttp.ClientSession() as session:
        result = await _request_with_retry(
            session, "GET", f"{DISCORD_API}/test", headers={}, json_body=None
        )
    assert result == {"ok": True}


async def test_request_with_retry_429_exhausted_raises(mock_discord: aioresponses) -> None:
    mock_discord.get(
        f"{DISCORD_API}/test",
        status=429,
        payload={"retry_after": 0.01},
        repeat=True,
    )
    async with aiohttp.ClientSession() as session:
        with pytest.raises(ProvisioningRateLimitError):
            await _request_with_retry(
                session, "GET", f"{DISCORD_API}/test", headers={}, json_body=None
            )


async def test_request_with_retry_401_raises_auth_error(mock_discord: aioresponses) -> None:
    mock_discord.get(f"{DISCORD_API}/test", status=401, payload={})
    async with aiohttp.ClientSession() as session:
        with pytest.raises(ProvisioningAuthError) as exc:
            await _request_with_retry(
                session, "GET", f"{DISCORD_API}/test", headers={}, json_body=None
            )
    # Token never in message:
    assert TOKEN not in str(exc.value)


async def test_request_with_retry_403_raises_permission_error(
    mock_discord: aioresponses,
) -> None:
    mock_discord.get(f"{DISCORD_API}/test", status=403, payload={})
    async with aiohttp.ClientSession() as session:
        with pytest.raises(ProvisioningPermissionError):
            await _request_with_retry(
                session, "GET", f"{DISCORD_API}/test", headers={}, json_body=None
            )


async def test_request_with_retry_400_includes_code_and_message_not_errors(
    mock_discord: aioresponses,
) -> None:
    """400 error must surface code+message but never the errors object."""
    mock_discord.post(
        f"{DISCORD_API}/test",
        status=400,
        payload={
            "code": 50035,
            "message": "Invalid Form Body",
            "errors": {"name": {"_errors": [{"message": "leak me"}]}},
        },
    )
    async with aiohttp.ClientSession() as session:
        with pytest.raises(ProvisioningError) as exc:
            await _request_with_retry(
                session, "POST", f"{DISCORD_API}/test", headers={}, json_body={"x": 1}
            )
    msg = str(exc.value)
    assert "50035" in msg
    assert "Invalid Form Body" in msg
    assert "leak me" not in msg


async def test_botapi_repr_redacts_token(mock_discord: aioresponses) -> None:
    real_looking_token = "MTM0NTY3ODkwMTIz.ABCdef.real-looking-token-xyz"
    async with aiohttp.ClientSession() as session:
        api = BotApi(session, real_looking_token)
        rendered = repr(api)
    assert "MTM0NTY3" not in rendered
    assert "<redacted>" in rendered


async def test_botapi_sends_bot_prefixed_authorization(
    mock_discord: aioresponses,
) -> None:
    """Every request must carry Authorization: Bot <token> and a User-Agent."""
    mock_discord.get(
        f"{DISCORD_API}/users/@me/guilds",
        payload=[],
        status=200,
    )
    async with aiohttp.ClientSession() as session:
        api = BotApi(session, TOKEN)
        await api.list_my_guilds()

    # aioresponses stores requests in .requests; verify headers
    requests = mock_discord.requests
    key = list(requests.keys())[0]
    call = requests[key][0]
    assert call.kwargs["headers"]["Authorization"] == f"Bot {TOKEN}"
    assert "DiscordFerry-TestProvisioner" in call.kwargs["headers"]["User-Agent"]


def test_botapi_headers_includes_audit_reason_when_provided() -> None:
    """X-Audit-Log-Reason is set only when audit_reason is provided."""
    api = BotApi(session=None, token=TOKEN)  # type: ignore[arg-type]
    with_reason = api._headers(audit_reason="cleanup test")
    without_reason = api._headers()
    assert with_reason["X-Audit-Log-Reason"] == "cleanup test"
    assert "X-Audit-Log-Reason" not in without_reason


def test_botapi_token_only_stored_in_underscore_token() -> None:
    """Regression guard: token must never be cached on any other attribute."""
    api = BotApi(session=None, token="abc123")  # type: ignore[arg-type]
    assert set(api.__dict__.keys()) == {"_session", "_token"}


async def test_list_channels_returns_channel_objects(
    mock_discord: aioresponses,
) -> None:
    mock_discord.get(
        f"{DISCORD_API}/guilds/{GUILD_ID}/channels",
        payload=[
            {"id": "100", "name": "general", "type": 0, "topic": "[ferry-fixture] x"},
            {"id": "101", "name": "Feedback Forum", "type": 15, "topic": "[ferry-fixture] y"},
        ],
    )
    async with aiohttp.ClientSession() as session:
        api = BotApi(session, TOKEN)
        result = await api.list_channels(GUILD_ID)
    assert len(result) == 2
    assert result[0]["name"] == "general"


async def test_list_messages_uses_limit_query(mock_discord: aioresponses) -> None:
    mock_discord.get(
        f"{DISCORD_API}/channels/100/messages?limit=100",
        payload=[
            {"id": "m1", "content": "hello", "channel_id": "100"},
        ],
    )
    async with aiohttp.ClientSession() as session:
        api = BotApi(session, TOKEN)
        result = await api.list_messages("100")
    assert len(result) == 1
    assert result[0]["id"] == "m1"


async def test_list_active_threads_returns_threads(mock_discord: aioresponses) -> None:
    mock_discord.get(
        f"{DISCORD_API}/guilds/{GUILD_ID}/threads/active",
        payload={
            "threads": [
                {"id": "t1", "name": "Cool Thread", "type": 11, "parent_id": "100"},
            ],
            "members": [],
        },
    )
    async with aiohttp.ClientSession() as session:
        api = BotApi(session, TOKEN)
        result = await api.list_active_threads(GUILD_ID)
    assert len(result) == 1
    assert result[0]["name"] == "Cool Thread"


async def test_list_archived_public_threads_returns_threads(
    mock_discord: aioresponses,
) -> None:
    mock_discord.get(
        f"{DISCORD_API}/channels/100/threads/archived/public",
        payload={
            "threads": [
                {"id": "t2", "name": "Archived Thread", "type": 11, "parent_id": "100"},
            ],
            "members": [],
            "has_more": False,
        },
    )
    async with aiohttp.ClientSession() as session:
        api = BotApi(session, TOKEN)
        result = await api.list_archived_public_threads("100")
    assert len(result) == 1
    assert result[0]["id"] == "t2"


async def test_send_message_posts_content_and_embed(mock_discord: aioresponses) -> None:
    mock_discord.post(
        f"{DISCORD_API}/channels/100/messages",
        payload={"id": "m99", "channel_id": "100", "content": "hi"},
        status=200,
    )
    async with aiohttp.ClientSession() as session:
        api = BotApi(session, TOKEN)
        result = await api.send_message(
            "100",
            content="hi [ferry:msg-001]",
            embed=None,
            audit_reason="provision (issue #35)",
        )
    assert result["id"] == "m99"
    # Audit reason header was sent:
    call = list(mock_discord.requests.values())[0][0]
    assert call.kwargs["headers"]["X-Audit-Log-Reason"] == "provision (issue #35)"


async def test_create_channel_text_type_zero(mock_discord: aioresponses) -> None:
    mock_discord.post(
        f"{DISCORD_API}/guilds/{GUILD_ID}/channels",
        payload={"id": "100", "name": "general", "type": 0},
        status=201,
    )
    async with aiohttp.ClientSession() as session:
        api = BotApi(session, TOKEN)
        result = await api.create_channel(
            GUILD_ID,
            name="general",
            channel_type=0,
            topic="[ferry-fixture] primary test channel",
            audit_reason="provision (issue #35)",
        )
    assert result["id"] == "100"


async def test_create_channel_forum_type_fifteen(mock_discord: aioresponses) -> None:
    mock_discord.post(
        f"{DISCORD_API}/guilds/{GUILD_ID}/channels",
        payload={"id": "101", "name": "Feedback Forum", "type": 15},
        status=201,
    )
    async with aiohttp.ClientSession() as session:
        api = BotApi(session, TOKEN)
        result = await api.create_channel(
            GUILD_ID,
            name="Feedback Forum",
            channel_type=15,
            topic="[ferry-fixture] forum channel",
            audit_reason="provision (issue #35)",
        )
    assert result["type"] == 15
