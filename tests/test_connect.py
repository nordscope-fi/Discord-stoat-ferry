"""Tests for the CONNECT phase (Phase 2)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from aioresponses import aioresponses

from discord_ferry.config import FerryConfig
from discord_ferry.errors import StoatConnectionError
from discord_ferry.migrator.connect import run_connect
from discord_ferry.state import MigrationState

if TYPE_CHECKING:
    from pathlib import Path

    from discord_ferry.core.events import MigrationEvent
    from discord_ferry.parser.models import DCEExport

STOAT_URL = "https://api.test"
AUTUMN_URL = "https://autumn.test"
TOKEN = "test-token"

_API_ROOT_RESPONSE = {
    "stoat": "0.8.5",
    "features": {
        "autumn": {
            "enabled": True,
            "url": AUTUMN_URL,
        },
    },
}


def _make_config(tmp_path: Path) -> FerryConfig:
    return FerryConfig(
        export_dir=tmp_path,
        stoat_url=STOAT_URL,
        token=TOKEN,
        output_dir=tmp_path,
    )


async def test_run_connect_discovers_autumn_url(tmp_path: Path) -> None:
    """CONNECT phase discovers Autumn URL and stores it in state."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState()
    exports: list[DCEExport] = []

    with aioresponses() as m:
        m.get(f"{STOAT_URL}/", payload=_API_ROOT_RESPONSE)
        m.get(f"{STOAT_URL}/users/@me", payload={"_id": "user123", "username": "ferry"})

        await run_connect(config, state, exports, events.append)

    assert state.autumn_url == AUTUMN_URL


async def test_run_connect_emits_events(tmp_path: Path) -> None:
    """CONNECT phase emits progress events."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState()

    with aioresponses() as m:
        m.get(f"{STOAT_URL}/", payload=_API_ROOT_RESPONSE)
        m.get(f"{STOAT_URL}/users/@me", payload={"_id": "user123", "username": "ferry"})

        await run_connect(config, state, [], events.append)

    messages = [e.message for e in events]
    assert any("Connecting" in msg for msg in messages)
    assert any("Autumn URL" in msg for msg in messages)
    assert any("Authentication verified" in msg for msg in messages)


async def test_run_connect_invalid_token(tmp_path: Path) -> None:
    """CONNECT phase raises ConnectionError on 401."""
    config = _make_config(tmp_path)
    state = MigrationState()

    with aioresponses() as m:
        m.get(f"{STOAT_URL}/", payload=_API_ROOT_RESPONSE)
        m.get(f"{STOAT_URL}/users/@me", status=401)

        with pytest.raises(StoatConnectionError, match="invalid or expired token"):
            await run_connect(config, state, [], lambda e: None)


async def test_run_connect_unreachable(tmp_path: Path) -> None:
    """CONNECT phase raises ConnectionError when API is unreachable."""
    import aiohttp

    config = _make_config(tmp_path)
    state = MigrationState()

    with aioresponses() as m:
        m.get(f"{STOAT_URL}/", exception=aiohttp.ClientConnectionError("Connection refused"))

        with pytest.raises(StoatConnectionError, match="Cannot reach"):
            await run_connect(config, state, [], lambda e: None)


async def test_run_connect_missing_autumn_feature(tmp_path: Path) -> None:
    """CONNECT phase raises ConnectionError when response lacks Autumn URL."""
    config = _make_config(tmp_path)
    state = MigrationState()

    with aioresponses() as m:
        m.get(f"{STOAT_URL}/", payload={"stoat": "0.8.5", "features": {}})

        with pytest.raises(StoatConnectionError, match="missing Autumn URL"):
            await run_connect(config, state, [], lambda e: None)


async def test_run_connect_permission_precheck_warns_on_failure(tmp_path: Path) -> None:
    """CONNECT phase emits warning when server pre-check fails."""
    events: list[MigrationEvent] = []
    config = FerryConfig(
        export_dir=tmp_path,
        stoat_url=STOAT_URL,
        token=TOKEN,
        server_id="existing-srv",
        output_dir=tmp_path,
    )
    state = MigrationState()

    with aioresponses() as m:
        m.get(f"{STOAT_URL}/", payload=_API_ROOT_RESPONSE)
        m.get(f"{STOAT_URL}/users/@me", payload={"_id": "user123", "username": "ferry"})
        # Server fetch fails.
        m.get(f"{STOAT_URL}/servers/existing-srv", status=403)

        await run_connect(config, state, [], events.append)

    # Should emit a warning, not raise.
    warning_events = [e for e in events if e.status == "warning"]
    assert len(warning_events) > 0
    assert any("existing-srv" in e.message for e in warning_events)


async def test_run_connect_permission_precheck_success(tmp_path: Path) -> None:
    """CONNECT phase emits progress when server pre-check succeeds."""
    events: list[MigrationEvent] = []
    config = FerryConfig(
        export_dir=tmp_path,
        stoat_url=STOAT_URL,
        token=TOKEN,
        server_id="existing-srv",
        output_dir=tmp_path,
    )
    state = MigrationState()

    with aioresponses() as m:
        m.get(f"{STOAT_URL}/", payload=_API_ROOT_RESPONSE)
        m.get(f"{STOAT_URL}/users/@me", payload={"_id": "user123", "username": "ferry"})
        m.get(
            f"{STOAT_URL}/servers/existing-srv",
            payload={"_id": "existing-srv", "name": "Test"},
        )

        await run_connect(config, state, [], events.append)

    messages = [e.message for e in events]
    assert any("verified accessible" in msg for msg in messages)


async def test_run_connect_api_error_status(tmp_path: Path) -> None:
    """CONNECT phase raises StoatConnectionError on non-200 API root response."""
    config = _make_config(tmp_path)
    state = MigrationState()

    with aioresponses() as m:
        m.get(f"{STOAT_URL}/", status=500)

        with pytest.raises(StoatConnectionError, match="status 500"):
            await run_connect(config, state, [], lambda e: None)


# ---------------------------------------------------------------------------
# #135 — a refusing proxy must name itself at both handlers in this module
# ---------------------------------------------------------------------------
#
# Real sockets, not aioresponses: aioresponses patches ClientSession._request,
# so the request object is never built and the proxy is invisible. The two
# handlers are reached separately because a blanket refusing proxy fails the
# first request, and `_discover_autumn_url` runs before `_verify_token`.


async def test_a_refused_proxy_names_the_proxy_discovering_autumn(
    tmp_path: Path, fake_proxy, proxy_env, os_proxy
) -> None:
    """SC-135-28. Killing: proxy_hint defined and never called at connect.py:97."""
    make, _ = fake_proxy
    server = await make(b"403 Forbidden")
    port = server.sockets[0].getsockname()[1]
    async with server:
        with os_proxy({}), proxy_env(HTTPS_PROXY=f"http://127.0.0.1:{port}"):
            with pytest.raises(StoatConnectionError) as caught:
                await run_connect(_make_config(tmp_path), MigrationState(), [], lambda e: None)

    message = str(caught.value)
    # The site marker first: it passes under the wiring mutant, so the proxy
    # assertions below are guaranteed to be reached and graded.
    assert "Cannot reach Stoat API" in message
    # ONE assertion, naming the target AND the proxy.
    #
    # Not a bare `127.0.0.1:{port}`: ClientHttpProxyError renders
    # `url='http://127.0.0.1:PORT'` (its real_url, the proxy) in __str__, so a
    # bare host substring is already in the unwired message and cannot fail.
    #
    # Not split in two either. A separate `assert "api.test" in message` above
    # this line would take the failure under the wiring mutant and this line
    # would never run. Merged, one assertion grades wiring AND pins `target=`,
    # which a copy-paste `target=path` would otherwise pass.
    assert f"The request to api.test went through the proxy at 127.0.0.1:{port}" in message
    assert "FERRY_DISABLE_PROXY" in message


async def test_a_refused_proxy_names_the_proxy_verifying_the_token(
    tmp_path: Path, fake_proxy, proxy_env, os_proxy
) -> None:
    """SC-135-28 at connect.py:154, the second handler in this module.

    The API root is mocked so discovery succeeds; only `/users/@me` is passed
    through to the real connector, which is what puts this test at :154 rather
    than at :97. The "Token verification" assertion is what pins that, and it
    holds under the wiring mutant, so the proxy assertions below it always run.
    """
    make, _ = fake_proxy
    server = await make(b"403 Forbidden")
    port = server.sockets[0].getsockname()[1]
    async with server:
        with os_proxy({}), proxy_env(HTTPS_PROXY=f"http://127.0.0.1:{port}"):
            with aioresponses(passthrough=[f"{STOAT_URL}/users/@me"]) as m:
                m.get(f"{STOAT_URL}/", payload=_API_ROOT_RESPONSE)
                with pytest.raises(StoatConnectionError) as caught:
                    await run_connect(_make_config(tmp_path), MigrationState(), [], lambda e: None)

    message = str(caught.value)
    assert "Token verification" in message
    assert f"The request to api.test went through the proxy at 127.0.0.1:{port}" in message
    assert "FERRY_DISABLE_PROXY" in message
