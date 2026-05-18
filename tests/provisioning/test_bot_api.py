"""Tests for tests/provisioning/_bot_api.py."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import aiohttp
import pytest
from aioresponses import aioresponses

from tests.provisioning._bot_api import (
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
