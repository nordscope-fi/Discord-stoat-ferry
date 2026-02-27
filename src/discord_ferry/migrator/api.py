"""Thin async wrapper around the Stoat REST API."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import aiohttp  # noqa: TCH002

from discord_ferry.errors import MigrationError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from discord_ferry.config import FerryConfig

MAX_API_RETRIES = 3
_RETRYABLE_STATUSES = {429, 502, 503, 504}


@asynccontextmanager
async def get_session(config: FerryConfig) -> AsyncIterator[aiohttp.ClientSession]:
    """Yield the shared session from config, or create a temporary one."""
    if config.session is not None:
        yield config.session
    else:
        async with aiohttp.ClientSession() as session:
            yield session


def _headers(token: str) -> dict[str, str]:
    return {"x-session-token": token, "Content-Type": "application/json"}


async def _api_request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    token: str,
    json_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Make an authenticated API request with retry on 429/5xx.

    Args:
        session: An active aiohttp ClientSession.
        method: HTTP method string (GET, POST, PATCH, etc.).
        url: Full URL for the request.
        token: Stoat session token for the x-session-token header.
        json_data: Optional JSON body. Not sent for GET requests.

    Returns:
        Parsed JSON response as a dict.

    Raises:
        MigrationError: On non-retryable errors or when all retries are exhausted.
    """
    headers = _headers(token)
    # Don't send a JSON body for GET requests even if one is accidentally provided.
    body = json_data if method.upper() != "GET" else None

    for attempt in range(MAX_API_RETRIES):
        try:
            async with session.request(method, url, json=body, headers=headers) as resp:
                if resp.status in (200, 201):
                    return await resp.json()  # type: ignore[no-any-return]
                if resp.status == 204:
                    return {}

                if resp.status in _RETRYABLE_STATUSES:
                    if attempt == MAX_API_RETRIES - 1:
                        text = await resp.text()
                        raise MigrationError(
                            f"API request failed after {MAX_API_RETRIES} retries: "
                            f"{resp.status} {text}"
                        )
                    if resp.status == 429:
                        body_data: dict[str, Any] = await resp.json()
                        retry_ms = body_data.get("retry_after", 1000)
                        await asyncio.sleep(retry_ms / 1000)
                    else:
                        await asyncio.sleep(2)
                    continue

                text = await resp.text()
                raise MigrationError(f"API error {resp.status}: {text}")
        except aiohttp.ClientError as exc:
            if attempt == MAX_API_RETRIES - 1:
                raise MigrationError(
                    f"Network error after {MAX_API_RETRIES} retries: {exc}"
                ) from exc
            await asyncio.sleep(2)

    # Unreachable, but satisfies mypy.
    raise MigrationError(f"API request failed after {MAX_API_RETRIES} retries")


async def api_create_server(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    name: str,
) -> dict[str, Any]:
    """Create a new server on the Stoat instance.

    Args:
        session: An active aiohttp ClientSession.
        stoat_url: Stoat API base URL (e.g. "https://api.stoat.chat").
        token: Stoat session token.
        name: Display name for the new server.

    Returns:
        Server object dict from the API (includes ``_id``).
    """
    url = f"{stoat_url.rstrip('/')}/servers/create"
    return await _api_request(session, "POST", url, token, {"name": name})


async def api_fetch_server(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    server_id: str,
) -> dict[str, Any]:
    """Fetch server info by ID.

    Args:
        session: An active aiohttp ClientSession.
        stoat_url: Stoat API base URL.
        token: Stoat session token.
        server_id: Target server ID.

    Returns:
        Server object dict from the API.
    """
    url = f"{stoat_url.rstrip('/')}/servers/{server_id}"
    return await _api_request(session, "GET", url, token)


async def api_edit_server(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    server_id: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Edit server properties (icon, banner, name, etc.).

    Args:
        session: An active aiohttp ClientSession.
        stoat_url: Stoat API base URL.
        token: Stoat session token.
        server_id: Target server ID.
        **kwargs: Fields to update (e.g. ``name="New Name"``, ``icon="autumn_id"``).

    Returns:
        Updated server object dict.
    """
    url = f"{stoat_url.rstrip('/')}/servers/{server_id}"
    return await _api_request(session, "PATCH", url, token, kwargs)


async def api_create_role(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    server_id: str,
    name: str,
) -> dict[str, Any]:
    """Create a role on a server.

    Args:
        session: An active aiohttp ClientSession.
        stoat_url: Stoat API base URL.
        token: Stoat session token.
        server_id: Target server ID.
        name: Display name for the new role.

    Returns:
        Role object dict from the API (includes ``id``).
    """
    url = f"{stoat_url.rstrip('/')}/servers/{server_id}/roles"
    return await _api_request(session, "POST", url, token, {"name": name})


async def api_edit_role(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    server_id: str,
    role_id: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Edit a role's properties (colour, hoist, permissions, etc.).

    Args:
        session: An active aiohttp ClientSession.
        stoat_url: Stoat API base URL.
        token: Stoat session token.
        server_id: Target server ID.
        role_id: Target role ID.
        **kwargs: Fields to update (e.g. ``colour=16711680``, ``hoist=True``).

    Returns:
        Updated role object dict.
    """
    url = f"{stoat_url.rstrip('/')}/servers/{server_id}/roles/{role_id}"
    return await _api_request(session, "PATCH", url, token, kwargs)


async def api_create_category(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    server_id: str,
    title: str,
) -> dict[str, Any]:
    """Create a category on a server.

    Args:
        session: An active aiohttp ClientSession.
        stoat_url: Stoat API base URL.
        token: Stoat session token.
        server_id: Target server ID.
        title: Display title for the new category.

    Returns:
        Category object dict from the API (includes ``id``).
    """
    url = f"{stoat_url.rstrip('/')}/servers/{server_id}/categories"
    return await _api_request(session, "POST", url, token, {"title": title})


async def api_edit_category(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    server_id: str,
    category_id: str,
    channels: list[str],
) -> dict[str, Any]:
    """Assign channels to a category (two-step category creation pattern).

    Args:
        session: An active aiohttp ClientSession.
        stoat_url: Stoat API base URL.
        token: Stoat session token.
        server_id: Target server ID.
        category_id: Target category ID.
        channels: Full list of channel IDs that belong to this category.

    Returns:
        Updated category object dict.
    """
    url = f"{stoat_url.rstrip('/')}/servers/{server_id}/categories/{category_id}"
    return await _api_request(session, "PATCH", url, token, {"channels": channels})


async def api_create_channel(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    server_id: str,
    *,
    name: str,
    channel_type: str | None = None,
    description: str | None = None,
    nsfw: bool = False,
) -> dict[str, Any]:
    """Create a channel on a server.

    Args:
        session: An active aiohttp ClientSession.
        stoat_url: Stoat API base URL.
        token: Stoat session token.
        server_id: Target server ID.
        name: Display name for the new channel.
        channel_type: Stoat channel type string (e.g. "Text", "Voice"). Optional.
        description: Channel topic/description. Optional.
        nsfw: Whether the channel is age-restricted. Defaults to False.

    Returns:
        Channel object dict from the API (includes ``_id``).
    """
    url = f"{stoat_url.rstrip('/')}/servers/{server_id}/channels"
    data: dict[str, Any] = {"name": name, "nsfw": nsfw}
    if channel_type is not None:
        data["type"] = channel_type
    if description is not None:
        data["description"] = description
    return await _api_request(session, "POST", url, token, data)


async def api_create_emoji(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    server_id: str,
    name: str,
    parent: str,
) -> dict[str, Any]:
    """Create a custom emoji on a server.

    Args:
        session: An active aiohttp ClientSession.
        stoat_url: Stoat API base URL.
        token: Stoat session token.
        server_id: Target server ID.
        name: Display name for the emoji (without colons).
        parent: Autumn file ID of the uploaded emoji image.

    Returns:
        Emoji object dict from the API (includes ``_id``).
    """
    url = f"{stoat_url.rstrip('/')}/servers/{server_id}/emojis"
    return await _api_request(session, "POST", url, token, {"name": name, "parent": parent})


async def api_send_message(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    channel_id: str,
    *,
    content: str | None = None,
    attachments: list[str] | None = None,
    embeds: list[dict[str, Any]] | None = None,
    masquerade: dict[str, str | None] | None = None,
    replies: list[dict[str, Any]] | None = None,
    nonce: str | None = None,
) -> dict[str, Any]:
    """Send a message to a channel.

    Args:
        session: An active aiohttp ClientSession.
        stoat_url: Stoat API base URL.
        token: Stoat session token.
        channel_id: Target channel ID.
        content: Message text content. Optional.
        attachments: List of Autumn file IDs to attach. Optional.
        embeds: List of embed dicts. Optional.
        masquerade: Masquerade dict with name/avatar/colour fields (values may be None). Optional.
        replies: List of reply reference dicts. Optional.
        nonce: Deduplication nonce (use ``f"ferry-{discord_msg_id}"``). Optional.

    Returns:
        Message object dict from the API (includes ``_id``).
    """
    url = f"{stoat_url.rstrip('/')}/channels/{channel_id}/messages"
    data: dict[str, Any] = {}
    if content is not None:
        data["content"] = content
    if attachments is not None:
        data["attachments"] = attachments
    if embeds is not None:
        data["embeds"] = embeds
    if masquerade is not None:
        data["masquerade"] = masquerade
    if replies is not None:
        data["replies"] = replies
    if nonce is not None:
        data["nonce"] = nonce
    return await _api_request(session, "POST", url, token, data)


async def api_add_reaction(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    channel_id: str,
    message_id: str,
    emoji: str,
) -> dict[str, Any]:
    """Add a reaction to a message.

    Args:
        session: An active aiohttp ClientSession.
        stoat_url: Stoat API base URL.
        token: Stoat session token.
        channel_id: Channel containing the message.
        message_id: Target message ID.
        emoji: Emoji string — Unicode character or custom emoji ID. URL-encoded automatically.

    Returns:
        Empty dict (API returns 204).
    """
    from urllib.parse import quote

    encoded_emoji = quote(emoji, safe="")
    url = (
        f"{stoat_url.rstrip('/')}/channels/{channel_id}"
        f"/messages/{message_id}/reactions/{encoded_emoji}"
    )
    return await _api_request(session, "PUT", url, token, None)


async def api_pin_message(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    channel_id: str,
    message_id: str,
) -> dict[str, Any]:
    """Pin a message in a channel.

    Args:
        session: An active aiohttp ClientSession.
        stoat_url: Stoat API base URL.
        token: Stoat session token.
        channel_id: Channel containing the message.
        message_id: Target message ID.

    Returns:
        Empty dict (API returns 204).
    """
    url = f"{stoat_url.rstrip('/')}/channels/{channel_id}/messages/{message_id}/pin"
    return await _api_request(session, "PUT", url, token, None)
