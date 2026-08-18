"""Async HTTP client for the Discord REST API (guild metadata only)."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import TYPE_CHECKING, Any, cast

import aiohttp

from discord_ferry.core.http import proxy_error_is_permanent, proxy_hint, tls_hint
from discord_ferry.discord.models import DiscordChannel, DiscordRole, PermissionOverwrite
from discord_ferry.errors import DiscordAuthError, MigrationError

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"
_MAX_RETRIES = 3
_MAX_429_RETRIES = 5  # separate bounded budget for 429s (does not consume _MAX_RETRIES)
_MAX_RETRY_DELAY_SECONDS = 60  # cap any server/body-advertised 429 retry delay


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


def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """Parse a rate-limit delay (seconds) from standard headers; None if absent/non-numeric.

    ``Retry-After`` may be an HTTP-date rather than delta-seconds — a non-numeric value is
    ignored (returns None) so the caller falls back to the body / default delay.
    """
    for name in ("Retry-After", "X-RateLimit-Reset-After"):
        raw = headers.get(name)
        if raw:
            try:
                return float(raw)
            except ValueError:
                continue
    return None


async def _discord_429_delay_seconds(resp: aiohttp.ClientResponse) -> float:
    """Resolve the 429 sleep (seconds): header → body ``retry_after`` (Discord secs) → 1s; capped.

    The body parse is content-type-guarded so a non-JSON 429 (Cloudflare HTML) can't escape into
    the network-error path.
    """
    header_s = _retry_after_seconds(resp.headers)
    if header_s is not None:
        return min(header_s, _MAX_RETRY_DELAY_SECONDS)
    try:
        body = await resp.json(content_type=None)
    except (json.JSONDecodeError, ValueError):
        body = None
    raw = body.get("retry_after") if isinstance(body, dict) else None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw):
        retry_after = float(raw)
    else:
        retry_after = 1.0
    return min(retry_after, _MAX_RETRY_DELAY_SECONDS)


async def _discord_request(session: aiohttp.ClientSession, token: str, path: str) -> Any:
    """GET the Discord API, honouring Retry-After with separate bounded 429/network budgets.

    429s advance only ``rate_limit_retries`` (bounded by ``_MAX_429_RETRIES``); network errors
    advance only ``network_attempts`` (bounded by ``_MAX_RETRIES``) — a 429 burst can no longer
    exhaust the network budget and abort the metadata phase.
    """
    url = f"{DISCORD_API}{path}"
    headers = {"Authorization": token, "Content-Type": "application/json"}
    network_attempts = 0
    rate_limit_retries = 0

    while True:
        try:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    try:
                        return await resp.json()
                    except (aiohttp.ContentTypeError, json.JSONDecodeError) as exc:
                        raise MigrationError(
                            f"Unexpected non-JSON 200 response (content-type: {resp.content_type})"
                        ) from exc
                if resp.status == 401:
                    raise DiscordAuthError("Discord token is invalid or expired")
                if resp.status == 403:
                    raise MigrationError(
                        "Insufficient permissions to read guild metadata. "
                        "The token must belong to a member of the guild."
                    )
                if resp.status == 429:
                    rate_limit_retries += 1
                    if rate_limit_retries >= _MAX_429_RETRIES:
                        raise MigrationError(
                            f"Discord API rate-limited after {_MAX_429_RETRIES} retries"
                        )
                    await asyncio.sleep(await _discord_429_delay_seconds(resp))
                    continue
                text = await resp.text()
                raise MigrationError(f"Discord API error {resp.status}: {text}")
        except (DiscordAuthError, MigrationError):
            raise
        except aiohttp.ClientError as exc:
            # Two lines: message and control flow are separate decisions over
            # the same two values. Proxy wins over the certificate hint and they
            # are never concatenated, for the reason in api.py.
            cert = tls_hint(exc)
            hint = proxy_hint(exc, target=url) or cert
            if hint is not None and (cert is not None or proxy_error_is_permanent(exc)):
                # Gated on permanence because this jumps over the whole
                # _MAX_RETRIES loop. A proxy 502, 503, 504 or connect timeout can
                # recover, and turning one into a hard failure would abort the
                # metadata phase on a blip it used to survive.
                raise MigrationError(f"Discord API network error: {exc}{hint}") from exc
            network_attempts += 1
            if network_attempts >= _MAX_RETRIES:
                raise MigrationError(f"Discord API network error: {exc}{hint or ''}") from exc
            await asyncio.sleep(1)


async def _discord_get(
    session: aiohttp.ClientSession, token: str, path: str
) -> list[dict[str, Any]]:
    """Make an authenticated GET request to the Discord API with retry on 429."""
    return cast("list[dict[str, Any]]", await _discord_request(session, token, path))


async def _discord_get_object(
    session: aiohttp.ClientSession, token: str, path: str
) -> dict[str, Any]:
    """Make an authenticated GET request returning a single JSON object."""
    return cast("dict[str, Any]", await _discord_request(session, token, path))


_ROLE_ICON_MAX_BYTES = 2_500_000  # Autumn "icons" tag limit (stoatchat Revolt.toml)


async def download_role_icon(
    session: aiohttp.ClientSession, role_id: str, icon_hash: str
) -> bytes | None:
    """Download a Discord role icon PNG from the CDN.

    Returns the image bytes, or ``None`` on any non-200 status, network error,
    or when the image exceeds the Autumn icons limit. The CDN URL is public
    (no token) — safe to log.
    """
    url = f"https://cdn.discordapp.com/role-icons/{role_id}/{icon_hash}.png"
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            data = await resp.read()
            if len(data) > _ROLE_ICON_MAX_BYTES:
                return None
            return data
    except aiohttp.ClientError as exc:
        logger.warning("Role icon download failed for role %s: %s", role_id, exc)
        return None


def _parse_role(data: dict[str, Any]) -> DiscordRole:
    return DiscordRole(
        id=str(data["id"]),
        name=data["name"],
        permissions=int(data["permissions"]),  # Discord sends as string
        position=data.get("position", 0),
        color=data.get("color", 0),
        hoist=data.get("hoist", False),
        managed=data.get("managed", False),
        icon=str(data.get("icon") or ""),
        unicode_emoji=str(data.get("unicode_emoji") or ""),
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
        rate_limit_per_user=data.get("rate_limit_per_user", 0),
        user_limit=data.get("user_limit", 0),
        permission_overwrites=overwrites,
    )
