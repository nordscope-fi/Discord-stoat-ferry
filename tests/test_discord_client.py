"""Tests for Discord REST API client."""

from __future__ import annotations

import logging
import ssl

import aiohttp
import pytest
from aioresponses import aioresponses

from discord_ferry.discord import fetch_and_translate_guild_metadata
from discord_ferry.discord.client import download_role_icon, fetch_guild_channels, fetch_guild_roles
from discord_ferry.errors import DiscordAuthError

DISCORD_API = "https://discord.com/api/v10"
TOKEN = "test-discord-token"
GUILD_ID = "111222333"


@pytest.fixture
def mock_discord() -> aioresponses:
    with aioresponses() as m:
        yield m


async def test_fetch_guild_roles_parses_response(mock_discord: aioresponses) -> None:
    mock_discord.get(
        f"{DISCORD_API}/guilds/{GUILD_ID}/roles",
        payload=[
            {
                "id": "role1",
                "name": "Admin",
                "permissions": "2147483647",  # String, not int!
                "position": 5,
                "color": 16711680,
                "hoist": True,
                "managed": False,
            },
            {
                "id": "role2",
                "name": "BotRole",
                "permissions": "8",
                "position": 3,
                "color": 0,
                "hoist": False,
                "managed": True,
            },
        ],
    )
    async with aiohttp.ClientSession() as session:
        roles = await fetch_guild_roles(session, TOKEN, GUILD_ID)
    assert len(roles) == 2
    assert roles[0].id == "role1"
    assert roles[0].permissions == 2147483647  # Parsed from string
    assert roles[1].managed is True


async def test_fetch_guild_channels_parses_nsfw_and_overwrites(
    mock_discord: aioresponses,
) -> None:
    mock_discord.get(
        f"{DISCORD_API}/guilds/{GUILD_ID}/channels",
        payload=[
            {
                "id": "ch1",
                "name": "general",
                "type": 0,
                "nsfw": False,
                "permission_overwrites": [],
            },
            {
                "id": "ch2",
                "name": "nsfw-channel",
                "type": 0,
                "nsfw": True,
                "permission_overwrites": [
                    {"id": "role1", "type": 0, "allow": "4194304", "deny": "0"},
                ],
            },
        ],
    )
    async with aiohttp.ClientSession() as session:
        channels = await fetch_guild_channels(session, TOKEN, GUILD_ID)
    assert len(channels) == 2
    assert channels[0].nsfw is False
    assert channels[1].nsfw is True
    assert len(channels[1].permission_overwrites) == 1
    assert channels[1].permission_overwrites[0].allow == 4194304  # Parsed from string


async def test_fetch_guild_roles_401_raises_discord_auth_error(
    mock_discord: aioresponses,
) -> None:
    mock_discord.get(
        f"{DISCORD_API}/guilds/{GUILD_ID}/roles",
        status=401,
        body="401: Unauthorized",
    )
    async with aiohttp.ClientSession() as session:
        with pytest.raises(DiscordAuthError):
            await fetch_guild_roles(session, TOKEN, GUILD_ID)


async def test_fetch_guild_roles_429_retries(mock_discord: aioresponses) -> None:
    url = f"{DISCORD_API}/guilds/{GUILD_ID}/roles"
    mock_discord.get(url, status=429, payload={"retry_after": 0.01})
    mock_discord.get(
        url,
        payload=[
            {
                "id": "r1",
                "name": "R",
                "permissions": "0",
                "position": 0,
                "color": 0,
                "hoist": False,
                "managed": False,
            }
        ],
    )
    async with aiohttp.ClientSession() as session:
        roles = await fetch_guild_roles(session, TOKEN, GUILD_ID)
    assert len(roles) == 1


async def test_fetch_and_translate_metadata(mock_discord: aioresponses) -> None:
    """Full pipeline: fetch -> translate -> DiscordMetadata."""
    guild_id = "111"
    mock_discord.get(
        f"{DISCORD_API}/guilds/{guild_id}",
        payload={"id": guild_id, "name": "Test", "banner": None},
    )
    mock_discord.get(
        f"{DISCORD_API}/guilds/{guild_id}/roles",
        payload=[
            # @everyone role (id == guild_id)
            {
                "id": guild_id,
                "name": "@everyone",
                "permissions": str(1 << 11),
                "position": 0,
                "color": 0,
                "hoist": False,
                "managed": False,
            },
            # Normal role with MANAGE_CHANNELS
            {
                "id": "role1",
                "name": "Mod",
                "permissions": str(1 << 4),
                "position": 2,
                "color": 0,
                "hoist": False,
                "managed": False,
            },
            # Bot-managed role — should be excluded from role_permissions
            {
                "id": "role2",
                "name": "BotRole",
                "permissions": str(1 << 4),
                "position": 1,
                "color": 0,
                "hoist": False,
                "managed": True,
            },
        ],
    )
    mock_discord.get(
        f"{DISCORD_API}/guilds/{guild_id}/channels",
        payload=[
            {
                "id": "ch1",
                "name": "general",
                "type": 0,
                "nsfw": False,
                "permission_overwrites": [
                    {"id": "role1", "type": 0, "allow": str(1 << 11), "deny": "0"},
                    {"id": "user1", "type": 1, "allow": str(1 << 11), "deny": "0"},  # user override
                    # @everyone channel override (id == guild_id)
                    {"id": guild_id, "type": 0, "allow": "0", "deny": str(1 << 10)},
                ],
            },
            {"id": "ch2", "name": "nsfw-ch", "type": 0, "nsfw": True, "permission_overwrites": []},
        ],
    )
    async with aiohttp.ClientSession() as session:
        meta = await fetch_and_translate_guild_metadata(session, TOKEN, guild_id)

    # @everyone -> server_default_permissions (SEND_MESSAGES -> SendMessage bit 22)
    assert meta.server_default_permissions == (1 << 22)

    # Mod role has MANAGE_CHANNELS -> ManageChannel (bit 0)
    assert "role1" in meta.role_permissions
    assert meta.role_permissions["role1"].allow == (1 << 0)

    # Bot role excluded
    assert "role2" not in meta.role_permissions

    # @everyone role excluded from role_permissions
    assert guild_id not in meta.role_permissions

    # Channel metadata
    assert meta.channel_metadata["ch1"].nsfw is False
    assert meta.channel_metadata["ch2"].nsfw is True

    # Only role overrides kept (user override type=1 filtered out, @everyone → default_override)
    assert len(meta.channel_metadata["ch1"].role_overrides) == 1
    assert meta.channel_metadata["ch1"].role_overrides[0].discord_role_id == "role1"

    # @everyone channel override extracted as default_override (VIEW_CHANNEL denied → bit 20)
    assert meta.channel_metadata["ch1"].default_override is not None
    assert meta.channel_metadata["ch1"].default_override.allow == 0
    assert meta.channel_metadata["ch1"].default_override.deny == (1 << 20)

    # Channel without @everyone override has no default_override
    assert meta.channel_metadata["ch2"].default_override is None


async def test_fetch_captures_hoist_position_desc_nsfw(mock_discord: aioresponses) -> None:
    """Capture role hoist/position, category position, and guild description/nsfw."""
    guild_id = "100"
    mock_discord.get(
        f"{DISCORD_API}/guilds/{guild_id}",
        payload={
            "id": guild_id,
            "name": "T",
            "description": "Hello",
            "nsfw_level": 1,
            "banner": None,
        },
    )
    mock_discord.get(
        f"{DISCORD_API}/guilds/{guild_id}/roles",
        payload=[
            {
                "id": guild_id,
                "name": "@everyone",
                "permissions": "0",
                "position": 0,
                "color": 0,
                "hoist": False,
                "managed": False,
            },
            {
                "id": "role-x",
                "name": "Mods",
                "permissions": "0",
                "position": 3,
                "color": 0,
                "hoist": True,
                "managed": False,
            },
        ],
    )
    mock_discord.get(
        f"{DISCORD_API}/guilds/{guild_id}/channels",
        payload=[
            {"id": "cat-1", "name": "INFO", "type": 4, "position": 2, "permission_overwrites": []},
            {
                "id": "txt-1",
                "name": "general",
                "type": 0,
                "position": 0,
                "permission_overwrites": [],
            },
        ],
    )
    async with aiohttp.ClientSession() as session:
        meta = await fetch_and_translate_guild_metadata(session, TOKEN, guild_id)

    assert meta.role_metadata["role-x"].hoist is True
    assert meta.role_metadata["role-x"].position == 3
    assert guild_id not in meta.role_metadata  # @everyone excluded
    assert meta.category_positions == {"cat-1": 2}  # only type-4 captured
    assert meta.guild_description == "Hello"
    assert meta.guild_nsfw is True  # nsfw_level 1 (EXPLICIT) → NSFW


@pytest.mark.parametrize(
    ("nsfw_level", "expected"),
    [(0, False), (1, True), (2, False), (3, True)],  # SAFE (2) must NOT flag NSFW
)
async def test_fetch_guild_nsfw_level_mapping(
    mock_discord: aioresponses, nsfw_level: int, expected: bool
) -> None:
    """Only EXPLICIT (1) and AGE_RESTRICTED (3) map to guild_nsfw=True."""
    guild_id = "200"
    mock_discord.get(
        f"{DISCORD_API}/guilds/{guild_id}",
        payload={"id": guild_id, "name": "T", "nsfw_level": nsfw_level, "banner": None},
    )
    mock_discord.get(f"{DISCORD_API}/guilds/{guild_id}/roles", payload=[])
    mock_discord.get(f"{DISCORD_API}/guilds/{guild_id}/channels", payload=[])
    async with aiohttp.ClientSession() as session:
        meta = await fetch_and_translate_guild_metadata(session, TOKEN, guild_id)
    assert meta.guild_nsfw is expected


async def test_capture_batch2_fields(mock_discord: aioresponses) -> None:
    """Capture slowmode/user_limit/role-icon/unicode_emoji into metadata."""
    guild_id = "100"
    mock_discord.get(
        f"{DISCORD_API}/guilds/{guild_id}",
        payload={"description": "", "nsfw_level": 0, "banner": None},
    )
    mock_discord.get(
        f"{DISCORD_API}/guilds/{guild_id}/roles",
        payload=[
            {
                "id": "200",
                "name": "Mod",
                "permissions": "0",
                "position": 2,
                "color": 0,
                "hoist": True,
                "managed": False,
                "icon": "iconhash123",
                "unicode_emoji": None,
            }
        ],
    )
    mock_discord.get(
        f"{DISCORD_API}/guilds/{guild_id}/channels",
        payload=[
            {
                "id": "300",
                "name": "gen",
                "type": 0,
                "nsfw": False,
                "position": 0,
                "rate_limit_per_user": 30,
                "permission_overwrites": [],
            },
            {
                "id": "301",
                "name": "vc",
                "type": 2,
                "nsfw": False,
                "position": 1,
                "user_limit": 5,
                "permission_overwrites": [],
            },
        ],
    )
    async with aiohttp.ClientSession() as session:
        meta = await fetch_and_translate_guild_metadata(session, TOKEN, guild_id)
    assert meta.channel_metadata["300"].slowmode == 30
    assert meta.channel_metadata["301"].user_limit == 5
    assert meta.role_metadata["200"].icon_hash == "iconhash123"
    assert meta.role_metadata["200"].unicode_emoji == ""


@pytest.mark.asyncio
async def test_download_role_icon_returns_bytes():
    url = "https://cdn.discordapp.com/role-icons/r1/hash.png"
    with aioresponses() as m:
        m.get(url, body=b"\x89PNG-data", status=200)
        async with aiohttp.ClientSession() as s:
            from discord_ferry.discord.client import download_role_icon

            data = await download_role_icon(s, "r1", "hash")
    assert data == b"\x89PNG-data"


@pytest.mark.asyncio
async def test_download_role_icon_none_on_404():
    url = "https://cdn.discordapp.com/role-icons/r1/hash.png"
    with aioresponses() as m:
        m.get(url, status=404)
        async with aiohttp.ClientSession() as s:
            from discord_ferry.discord.client import download_role_icon

            assert await download_role_icon(s, "r1", "hash") is None


@pytest.mark.asyncio
async def test_download_role_icon_none_when_oversize(monkeypatch):
    # Patch the limit down so a small body exercises the oversize branch without
    # streaming a multi-MB body through aioresponses (which trips an internal
    # parser assertion on aiohttp 3.14).
    from discord_ferry.discord import client as discord_client

    monkeypatch.setattr(discord_client, "_ROLE_ICON_MAX_BYTES", 4)
    url = "https://cdn.discordapp.com/role-icons/r1/hash.png"
    with aioresponses() as m:
        m.get(url, body=b"toobig", status=200)
        async with aiohttp.ClientSession() as s:
            assert await discord_client.download_role_icon(s, "r1", "hash") is None


@pytest.mark.asyncio
async def test_download_role_icon_none_on_client_error():
    url = "https://cdn.discordapp.com/role-icons/r1/hash.png"
    with aioresponses() as m:
        m.get(url, exception=aiohttp.ClientError())
        async with aiohttp.ClientSession() as s:
            from discord_ferry.discord.client import download_role_icon

            assert await download_role_icon(s, "r1", "hash") is None


async def test_a_role_icon_download_failure_logs_its_reason(caplog) -> None:
    """Killing: swallowing the reason. Today any ClientError returns None and
    structure.py reports 'Role icon download failed' with no cause."""
    with aioresponses() as m:
        m.get(
            "https://cdn.discordapp.com/role-icons/r1/abc.png",
            exception=aiohttp.ClientProxyConnectionError(
                aiohttp.client_reqrep.ConnectionKey("corp", 8080, False, True, None, None, None),
                OSError("refused"),
            ),
        )
        async with aiohttp.ClientSession() as s:
            with caplog.at_level(logging.WARNING):
                assert await download_role_icon(s, "r1", "abc") is None
    assert "corp" in caplog.text


@pytest.mark.asyncio
async def test_capture_role_name_and_hex_color() -> None:
    guild_id = "100"
    with aioresponses() as m:
        m.get(
            f"{DISCORD_API}/guilds/{guild_id}",
            payload={"description": "", "nsfw_level": 0, "banner": None},
        )
        m.get(
            f"{DISCORD_API}/guilds/{guild_id}/roles",
            payload=[
                {
                    "id": "200",
                    "name": "Mod",
                    "permissions": "0",
                    "position": 2,
                    "color": 16711680,
                    "hoist": False,
                    "managed": False,
                    "icon": None,
                    "unicode_emoji": None,
                },
                {
                    "id": "201",
                    "name": "NoColor",
                    "permissions": "0",
                    "position": 1,
                    "color": 0,
                    "hoist": False,
                    "managed": False,
                    "icon": None,
                    "unicode_emoji": None,
                },
            ],
        )
        m.get(f"{DISCORD_API}/guilds/{guild_id}/channels", payload=[])
        async with aiohttp.ClientSession() as s:
            meta = await fetch_and_translate_guild_metadata(s, "tok", guild_id)
    assert meta.role_metadata["200"].name == "Mod"
    assert meta.role_metadata["200"].color == "#ff0000"  # 16711680 -> hex
    assert meta.role_metadata["201"].color == ""  # color 0 -> ""


# ---------------------------------------------------------------------------
# Batch 6 — S3: Discord 429 honors Retry-After + separate bounded budget + DRY
# ---------------------------------------------------------------------------


def _sleep_capture():  # type: ignore[no-untyped-def]
    from unittest.mock import AsyncMock

    calls: list[float] = []
    return calls, AsyncMock(side_effect=lambda d: calls.append(d))


async def test_discord_get_429_html_honors_header(mock_discord: aioresponses) -> None:  # SC-15
    from unittest.mock import patch

    url = f"{DISCORD_API}/guilds/{GUILD_ID}/roles"
    mock_discord.get(
        url,
        status=429,
        body="<html>rl</html>",
        content_type="text/html",
        headers={"Retry-After": "2"},
    )
    mock_discord.get(url, payload=[])
    calls, sleep = _sleep_capture()
    with patch("discord_ferry.discord.client.asyncio.sleep", sleep):
        async with aiohttp.ClientSession() as session:
            roles = await fetch_guild_roles(session, TOKEN, GUILD_ID)
    assert roles == []
    assert calls[0] == pytest.approx(2.0, abs=0.05)  # header seconds, not fixed 1s


async def test_discord_object_429_html_honors_header(mock_discord: aioresponses) -> None:  # SC-16
    from unittest.mock import patch

    from discord_ferry.discord.client import _discord_get_object

    url = f"{DISCORD_API}/guilds/{GUILD_ID}"
    mock_discord.get(
        url,
        status=429,
        body="<html>rl</html>",
        content_type="text/html",
        headers={"Retry-After": "2"},
    )
    mock_discord.get(url, payload={"id": GUILD_ID, "name": "G"})
    calls, sleep = _sleep_capture()
    with patch("discord_ferry.discord.client.asyncio.sleep", sleep):
        async with aiohttp.ClientSession() as session:
            obj = await _discord_get_object(session, TOKEN, f"/guilds/{GUILD_ID}")
    assert obj["id"] == GUILD_ID
    assert calls[0] == pytest.approx(2.0, abs=0.05)  # DRY: same fix covers both getters


async def test_discord_429_json_body_seconds(mock_discord: aioresponses) -> None:  # SC-17
    from unittest.mock import patch

    url = f"{DISCORD_API}/guilds/{GUILD_ID}/roles"
    mock_discord.get(url, status=429, payload={"retry_after": 0.5})
    mock_discord.get(url, payload=[])
    calls, sleep = _sleep_capture()
    with patch("discord_ferry.discord.client.asyncio.sleep", sleep):
        async with aiohttp.ClientSession() as session:
            await fetch_guild_roles(session, TOKEN, GUILD_ID)
    assert calls[0] == pytest.approx(0.5, abs=0.05)  # Discord body = seconds (no ÷1000)


async def test_discord_429_separate_budget(mock_discord: aioresponses) -> None:  # SC-18
    from unittest.mock import AsyncMock, patch

    url = f"{DISCORD_API}/guilds/{GUILD_ID}/roles"
    for _ in range(4):  # 4 > _MAX_RETRIES(3) but < _MAX_429_RETRIES(5)
        mock_discord.get(url, status=429, payload={"retry_after": 0.0})
    mock_discord.get(url, payload=[])
    with patch("discord_ferry.discord.client.asyncio.sleep", new_callable=AsyncMock):
        async with aiohttp.ClientSession() as session:
            roles = await fetch_guild_roles(session, TOKEN, GUILD_ID)
    assert roles == []  # 429s did NOT exhaust the network budget


async def test_discord_429_budget_bounded(mock_discord: aioresponses) -> None:  # SC-19
    from unittest.mock import AsyncMock, patch

    from discord_ferry.errors import MigrationError

    url = f"{DISCORD_API}/guilds/{GUILD_ID}/roles"
    for _ in range(5):
        mock_discord.get(url, status=429, payload={"retry_after": 0.0})
    with patch("discord_ferry.discord.client.asyncio.sleep", new_callable=AsyncMock):
        async with aiohttp.ClientSession() as session:
            with pytest.raises(MigrationError, match="rate-limited after 5 retries"):
                await fetch_guild_roles(session, TOKEN, GUILD_ID)


async def test_discord_network_error_bounded(mock_discord: aioresponses) -> None:  # SC-20
    from unittest.mock import AsyncMock, patch

    from discord_ferry.errors import MigrationError

    url = f"{DISCORD_API}/guilds/{GUILD_ID}/roles"
    for _ in range(3):
        mock_discord.get(url, exception=aiohttp.ClientError("boom"))
    with patch("discord_ferry.discord.client.asyncio.sleep", new_callable=AsyncMock):
        async with aiohttp.ClientSession() as session:
            with pytest.raises(MigrationError, match="Discord API network error"):
                await fetch_guild_roles(session, TOKEN, GUILD_ID)


async def test_certificate_error_skips_the_network_retry(mock_discord: aioresponses) -> None:
    """SC-134-27.

    Mirrors the api.py and manager.py short-circuit tests. A certificate
    failure cannot succeed on retry, so the 1s sleep must never be paid, and
    the await_count assertion also proves only one request was attempted
    instead of _MAX_RETRIES.
    """
    from unittest.mock import AsyncMock, patch

    from discord_ferry.errors import MigrationError

    url = f"{DISCORD_API}/guilds/{GUILD_ID}/roles"
    key = aiohttp.client_reqrep.ConnectionKey("discord.com", 443, True, True, None, None, None)
    cert_error = aiohttp.ClientConnectorCertificateError(key, ssl.SSLCertVerificationError("bad"))
    mock_discord.get(url, exception=cert_error)

    with patch("discord_ferry.discord.client.asyncio.sleep", new_callable=AsyncMock) as sleep:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(MigrationError) as caught:
                await fetch_guild_roles(session, TOKEN, GUILD_ID)

    assert "SSL_CERT_FILE" in str(caught.value)
    assert sleep.await_count == 0, "a certificate error must not pay the 1s retry sleep"


async def test_discord_403_raises_migration_error(mock_discord: aioresponses) -> None:  # SC-21
    from discord_ferry.errors import MigrationError

    url = f"{DISCORD_API}/guilds/{GUILD_ID}/roles"
    mock_discord.get(url, status=403, body="Forbidden")
    async with aiohttp.ClientSession() as session:
        with pytest.raises(MigrationError, match="Insufficient permissions"):
            await fetch_guild_roles(session, TOKEN, GUILD_ID)


async def test_discord_429_delay_capped(mock_discord: aioresponses) -> None:  # SC-22
    from unittest.mock import patch

    url = f"{DISCORD_API}/guilds/{GUILD_ID}/roles"
    mock_discord.get(
        url,
        status=429,
        body="<html>",
        content_type="text/html",
        headers={"Retry-After": "9999"},
    )
    mock_discord.get(url, payload=[])
    calls, sleep = _sleep_capture()
    with patch("discord_ferry.discord.client.asyncio.sleep", sleep):
        async with aiohttp.ClientSession() as session:
            await fetch_guild_roles(session, TOKEN, GUILD_ID)
    assert calls[0] == 60  # _MAX_RETRY_DELAY_SECONDS


async def test_discord_non_json_200_clear_error(
    mock_discord: aioresponses,
) -> None:  # SC-23 (review M1)
    """A non-JSON 200 raises a clear content-type error, not a misclassified network error."""
    from discord_ferry.errors import MigrationError

    url = f"{DISCORD_API}/guilds/{GUILD_ID}/roles"
    mock_discord.get(url, status=200, body="<html>nope</html>", content_type="text/html")
    async with aiohttp.ClientSession() as session:
        with pytest.raises(MigrationError) as exc:
            await fetch_guild_roles(session, TOKEN, GUILD_ID)
    assert "network error" not in str(exc.value).lower()
    assert "text/html" in str(exc.value)


async def test_discord_429_null_retry_after_falls_back(
    mock_discord: aioresponses,
) -> None:  # SC-24 (review M2)
    """A 429 body with retry_after=null must not crash; falls back to the default delay."""
    from unittest.mock import patch

    url = f"{DISCORD_API}/guilds/{GUILD_ID}/roles"
    mock_discord.get(url, status=429, payload={"retry_after": None})
    mock_discord.get(url, payload=[])
    calls, sleep = _sleep_capture()
    with patch("discord_ferry.discord.client.asyncio.sleep", sleep):
        async with aiohttp.ClientSession() as session:
            await fetch_guild_roles(session, TOKEN, GUILD_ID)
    assert calls[0] == pytest.approx(1.0, abs=0.05)


async def test_discord_reset_after_header_is_seconds_not_milliseconds(
    mock_discord: aioresponses,
) -> None:
    """UNIT PIN: Discord's ``X-RateLimit-Reset-After`` is delta-SECONDS.

    Stoat's identically-named header is MILLISECONDS, and is parsed by a
    deliberately differently-named function (``migrator.api._stoat_rate_delay_seconds``).
    The two parsers look like duplicates and MUST NOT be merged: unifying them
    silently breaks one side by a factor of 1000. This test fails if the Discord
    side is ever "tidied up" to divide by 1000.

    The sibling test ``test_discord_get_429_html_honors_header`` covers
    ``Retry-After``, which is seconds on both services and so cannot detect the
    hazard; only this header distinguishes them.
    """
    from unittest.mock import patch

    url = f"{DISCORD_API}/guilds/{GUILD_ID}/roles"
    mock_discord.get(url, status=429, headers={"X-RateLimit-Reset-After": "5"})
    mock_discord.get(url, payload=[])
    calls, sleep = _sleep_capture()
    with patch("discord_ferry.discord.client.asyncio.sleep", sleep):
        async with aiohttp.ClientSession() as session:
            roles = await fetch_guild_roles(session, TOKEN, GUILD_ID)

    assert roles == []
    assert calls[0] == pytest.approx(5.0, abs=0.05), (
        "Discord's X-RateLimit-Reset-After is delta-seconds: 5 must mean 5s, not 0.005s"
    )


# ---------------------------------------------------------------------------
# #135 — a refusing proxy must name itself at discord/client.py:163
# ---------------------------------------------------------------------------


async def test_a_refused_proxy_names_the_proxy(fake_proxy, proxy_env, os_proxy) -> None:
    """SC-135-28. Killing: proxy_hint defined and never called at client.py:163.

    A real socket: aioresponses patches ClientSession._request, so the request
    object that carries a proxy is never built.

    `asyncio.sleep` is patched so an unwired run costs no wall time, and the
    await_count assertion pins the permanence half -- a refused proxy is
    permanent, so it must jump over the _MAX_RETRIES loop rather than pay it.
    That assertion is LAST, so the wiring assertions above it fail first.
    """
    from unittest.mock import AsyncMock, patch

    from discord_ferry.core.http import new_session
    from discord_ferry.errors import MigrationError

    make, _ = fake_proxy
    server = await make(b"403 Forbidden")
    port = server.sockets[0].getsockname()[1]
    async with server:
        with (
            os_proxy({}),
            proxy_env(HTTPS_PROXY=f"http://127.0.0.1:{port}"),
            patch("discord_ferry.discord.client.asyncio.sleep", new_callable=AsyncMock) as sleep,
        ):
            async with new_session() as session:
                with pytest.raises(MigrationError) as caught:
                    await fetch_guild_roles(session, TOKEN, GUILD_ID)

    message = str(caught.value)
    assert "Discord API network error" in message
    assert f"The request to discord.com went through the proxy at 127.0.0.1:{port}" in message
    assert "FERRY_DISABLE_PROXY" in message
    assert sleep.await_count == 0, "a refused proxy must not pay the 1s retry sleep"


async def test_a_proxy_502_still_retries(fake_proxy, proxy_env, os_proxy) -> None:
    """SC-135-37 at discord/client.py. Killing: wiring proxy_hint here without
    the permanence gate, i.e. `if hint is not None:` alone.

    This handler jumps over the whole _MAX_RETRIES loop, so an ungated hint
    would abort the metadata phase on a proxy blip it used to survive. The
    captured-request count is the FIRST assertion, so it is the one that fails
    under the mutant.
    """
    from unittest.mock import AsyncMock, patch

    from discord_ferry.core.http import new_session
    from discord_ferry.discord.client import _MAX_RETRIES
    from discord_ferry.errors import MigrationError

    make, captured = fake_proxy
    server = await make(b"502 Bad Gateway")
    port = server.sockets[0].getsockname()[1]
    async with server:
        with (
            os_proxy({}),
            proxy_env(HTTPS_PROXY=f"http://127.0.0.1:{port}"),
            patch("discord_ferry.discord.client.asyncio.sleep", new_callable=AsyncMock),
        ):
            async with new_session() as session:
                with pytest.raises(MigrationError) as caught:
                    await fetch_guild_roles(session, TOKEN, GUILD_ID)

    assert len(captured) == _MAX_RETRIES, "the 502 was treated as permanent and never retried"
    message = str(caught.value)
    assert "Discord API network error" in message
    assert f"The request to discord.com went through the proxy at 127.0.0.1:{port}" in message
