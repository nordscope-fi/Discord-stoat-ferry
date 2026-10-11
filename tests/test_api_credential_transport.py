"""Credential transport policy coverage for the shared Stoat API client."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aioresponses import aioresponses
from yarl import URL

from discord_ferry.errors import MigrationError
from discord_ferry.migrator import api as api_module
from discord_ferry.migrator.api import _api_request, api_execute_webhook, api_fetch_root


async def test_public_http_rejected_before_request_and_circuit_state_work() -> None:
    url = "http://public.example/servers/server-1"
    api_module._circuit_state.consecutive_failures = api_module._CIRCUIT_THRESHOLD

    with (
        aioresponses() as mocked,
        patch("asyncio.sleep", new_callable=AsyncMock) as slept,
        pytest.raises(
            MigrationError,
            match="Credential-bearing API requests require HTTPS",
        ),
    ):
        async with aiohttp.ClientSession() as session:
            await _api_request(session, "GET", url, "session-token")

    assert mocked.requests == {}
    assert api_module._circuit_state.consecutive_failures == api_module._CIRCUIT_THRESHOLD
    slept.assert_not_awaited()


async def test_private_http_destination_remains_available() -> None:
    url = "http://127.0.0.1:8765/servers/server-1"
    with aioresponses() as mocked:
        mocked.get(url, payload={"_id": "server-1"})
        async with aiohttp.ClientSession() as session:
            result = await _api_request(session, "GET", url, "session-token")

    assert result == {"_id": "server-1"}


async def test_redirect_is_returned_as_non_retryable_api_error() -> None:
    url = "https://api.test/servers/server-1"
    with aioresponses() as mocked:
        mocked.get(url, status=302, headers={"Location": "http://public.example/leak"})
        async with aiohttp.ClientSession() as session:
            with pytest.raises(MigrationError, match="API error 302"):
                await _api_request(session, "GET", url, "session-token")

    assert len(mocked.requests[("GET", URL(url))]) == 1


async def test_shared_request_explicitly_disables_redirects() -> None:
    url = "https://api.test/servers/server-1"
    with aioresponses() as mocked:
        mocked.get(url, payload={"_id": "server-1"})
        async with aiohttp.ClientSession() as session:
            await _api_request(session, "GET", url, "session-token")

    request = mocked.requests[("GET", URL(url))][0]
    assert request.kwargs["allow_redirects"] is False


async def test_webhook_url_credential_uses_the_same_transport_guard() -> None:
    with aioresponses() as mocked:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(
                MigrationError,
                match="Credential-bearing API requests require HTTPS",
            ):
                await api_execute_webhook(
                    session,
                    "http://public.example",
                    "webhook-1",
                    "webhook-token",
                    content="probe",
                )

    assert mocked.requests == {}


async def test_unauthenticated_root_probe_keeps_public_http_behavior() -> None:
    url = "http://public.example/"
    with aioresponses() as mocked:
        mocked.get(url, payload={"features": {}})
        async with aiohttp.ClientSession() as session:
            result = await api_fetch_root(session, "http://public.example")

    assert result == {"features": {}}
