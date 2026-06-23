"""Tests for Discord REST API client."""

from __future__ import annotations

import aiohttp
import pytest
from aioresponses import aioresponses

from discord_ferry.discord import fetch_and_translate_guild_metadata
from discord_ferry.discord.client import fetch_guild_channels, fetch_guild_roles
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
