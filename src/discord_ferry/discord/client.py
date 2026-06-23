"""Async HTTP client for the Discord REST API (guild metadata only)."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from discord_ferry.discord.models import DiscordChannel, DiscordRole, PermissionOverwrite
from discord_ferry.errors import DiscordAuthError, MigrationError

DISCORD_API = "https://discord.com/api/v10"
_MAX_RETRIES = 3


async def fetch_guild(session: aiohttp.ClientSession, token: str, guild_id: str) -> dict[str, Any]:
    """Fetch the guild object from the Discord API.

    Args:
        session: An active aiohttp ClientSession.
        token: Discord user token (no "Bot " prefix).
        guild_id: Discord guild/server ID.

    Returns:
        Raw guild data dict.

    Raises:
        DiscordAuthError: On 401 Unauthorized.
        MigrationError: On other non-retryable errors.
    """
    return await _discord_get_object(session, token, f"/guilds/{guild_id}")


async def fetch_guild_roles(
    session: aiohttp.ClientSession, token: str, guild_id: str
) -> list[DiscordRole]:
    """Fetch all roles for a guild from the Discord API.

    Args:
        session: An active aiohttp ClientSession.
        token: Discord user token (no "Bot " prefix).
        guild_id: Discord guild/server ID.

    Returns:
        List of DiscordRole dataclasses with permissions parsed from strings.

    Raises:
        DiscordAuthError: On 401 Unauthorized.
        MigrationError: On other non-retryable errors.
    """
    data = await _discord_get(session, token, f"/guilds/{guild_id}/roles")
    return [_parse_role(r) for r in data]


async def fetch_guild_channels(
    session: aiohttp.ClientSession, token: str, guild_id: str
) -> list[DiscordChannel]:
    """Fetch all channels for a guild from the Discord API.

    Args:
        session: An active aiohttp ClientSession.
        token: Discord user token.
        guild_id: Discord guild/server ID.

    Returns:
        List of DiscordChannel dataclasses with NSFW flags and permission overwrites.

    Raises:
        DiscordAuthError: On 401 Unauthorized.
        MigrationError: On other non-retryable errors.
    """
    data = await _discord_get(session, token, f"/guilds/{guild_id}/channels")
    return [_parse_channel(c) for c in data]


async def _discord_get(
    session: aiohttp.ClientSession, token: str, path: str
) -> list[dict[str, Any]]:
    """Make an authenticated GET request to the Discord API with retry on 429."""
    url = f"{DISCORD_API}{path}"
    headers = {"Authorization": token, "Content-Type": "application/json"}

    for attempt in range(_MAX_RETRIES):
        try:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    return await resp.json()  # type: ignore[no-any-return]
                if resp.status == 401:
                    raise DiscordAuthError("Discord token is invalid or expired")
                if resp.status == 403:
                    raise MigrationError(
                        "Insufficient permissions to read guild metadata. "
                        "The token must belong to a member of the guild."
                    )
                if resp.status == 429:
                    body = await resp.json()
                    retry_after = float(body.get("retry_after", 1))
                    await asyncio.sleep(retry_after)
                    continue
                text = await resp.text()
                raise MigrationError(f"Discord API error {resp.status}: {text}")
        except (DiscordAuthError, MigrationError):
            raise
        except aiohttp.ClientError as exc:
            if attempt == _MAX_RETRIES - 1:
                raise MigrationError(f"Discord API network error: {exc}") from exc
            await asyncio.sleep(1)

    raise MigrationError(f"Discord API request failed after {_MAX_RETRIES} retries")


async def _discord_get_object(
    session: aiohttp.ClientSession, token: str, path: str
) -> dict[str, Any]:
    """Make an authenticated GET request returning a single JSON object."""
    url = f"{DISCORD_API}{path}"
    headers = {"Authorization": token, "Content-Type": "application/json"}

    for attempt in range(_MAX_RETRIES):
        try:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    return await resp.json()  # type: ignore[no-any-return]
                if resp.status == 401:
                    raise DiscordAuthError("Discord token is invalid or expired")
                if resp.status == 403:
                    raise MigrationError(
                        "Insufficient permissions to read guild metadata. "
                        "The token must belong to a member of the guild."
                    )
                if resp.status == 429:
                    body = await resp.json()
                    retry_after = float(body.get("retry_after", 1))
                    await asyncio.sleep(retry_after)
                    continue
                text = await resp.text()
                raise MigrationError(f"Discord API error {resp.status}: {text}")
        except (DiscordAuthError, MigrationError):
            raise
        except aiohttp.ClientError as exc:
            if attempt == _MAX_RETRIES - 1:
                raise MigrationError(f"Discord API network error: {exc}") from exc
            await asyncio.sleep(1)

    raise MigrationError(f"Discord API request failed after {_MAX_RETRIES} retries")


def _parse_role(data: dict[str, Any]) -> DiscordRole:
    return DiscordRole(
        id=str(data["id"]),
        name=data["name"],
        permissions=int(data["permissions"]),  # Discord sends as string
        position=data.get("position", 0),
        color=data.get("color", 0),
        hoist=data.get("hoist", False),
        managed=data.get("managed", False),
    )


def _parse_channel(data: dict[str, Any]) -> DiscordChannel:
    overwrites = [
        PermissionOverwrite(
            id=str(ow["id"]),
            type=ow["type"],
            allow=int(ow["allow"]),  # Discord sends as string
            deny=int(ow["deny"]),  # Discord sends as string
        )
        for ow in data.get("permission_overwrites", [])
    ]
    return DiscordChannel(
        id=str(data["id"]),
        name=data["name"],
        type=data["type"],
        nsfw=data.get("nsfw", False),
        position=data.get("position", 0),
        permission_overwrites=overwrites,
    )
