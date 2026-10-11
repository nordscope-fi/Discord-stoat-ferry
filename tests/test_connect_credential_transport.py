"""Credential transport policy coverage for connection token verification."""

from __future__ import annotations

import aiohttp
import pytest
from aioresponses import aioresponses
from yarl import URL

from discord_ferry.errors import StoatConnectionError
from discord_ferry.migrator.connect import _verify_token


async def test_public_http_token_verification_rejected_before_request() -> None:
    with aioresponses() as mocked:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(
                StoatConnectionError,
                match="Credential-bearing token verification requires HTTPS",
            ):
                await _verify_token(session, "http://public.example", "session-token")

    assert mocked.requests == {}


async def test_private_http_token_verification_remains_available() -> None:
    url = "http://127.0.0.1:8765/users/@me"
    with aioresponses() as mocked:
        mocked.get(url, payload={"_id": "user-1"})
        async with aiohttp.ClientSession() as session:
            await _verify_token(session, "http://127.0.0.1:8765", "session-token")

    assert len(mocked.requests[("GET", URL(url))]) == 1


async def test_token_verification_redirect_is_an_error() -> None:
    url = "https://api.test/users/@me"
    with aioresponses() as mocked:
        mocked.get(url, status=302, headers={"Location": "http://public.example/leak"})
        async with aiohttp.ClientSession() as session:
            with pytest.raises(StoatConnectionError, match="status 302"):
                await _verify_token(session, "https://api.test", "session-token")

    assert len(mocked.requests[("GET", URL(url))]) == 1


async def test_token_verification_explicitly_disables_redirects() -> None:
    url = "https://api.test/users/@me"
    with aioresponses() as mocked:
        mocked.get(url, payload={"_id": "user-1"})
        async with aiohttp.ClientSession() as session:
            await _verify_token(session, "https://api.test", "session-token")

    request = mocked.requests[("GET", URL(url))][0]
    assert request.kwargs["allow_redirects"] is False
