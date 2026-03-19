"""Thin async wrapper around the Stoat REST API."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp  # noqa: TCH002

from discord_ferry.errors import MigrationError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from discord_ferry.config import FerryConfig

logger = logging.getLogger(__name__)

MAX_API_RETRIES = 3
_RETRYABLE_STATUSES = {429, 502, 503, 504}

_CIRCUIT_THRESHOLD = 5
_CIRCUIT_PAUSE_SECONDS = 30


@dataclass
class _CircuitState:
    consecutive_failures: int = 0


# Module-level state — safe for single-migration-per-process model.
_circuit_state = _CircuitState()
_request_semaphore: asyncio.Semaphore | None = None

# Adaptive rate state — tracks 429 pressure and adjusts inter-request delay.
_rate_429_window: deque[float] = deque(maxlen=20)  # timestamps of recent 429s
_rate_multiplier: float = 1.0


def _reset_circuit_state() -> None:
    """Reset circuit breaker state. Called by test fixtures."""
    _circuit_state.consecutive_failures = 0


def _reset_rate_state() -> None:
    """Reset adaptive rate state. Called by test fixtures."""
    global _rate_multiplier  # noqa: PLW0603
    _rate_429_window.clear()
    _rate_multiplier = 1.0


def get_rate_multiplier() -> float:
    """Return current rate multiplier for external use."""
    return _rate_multiplier


def init_request_semaphore(max_concurrent: int = 5) -> None:
    """Initialize the request concurrency semaphore."""
    global _request_semaphore  # noqa: PLW0603
    _request_semaphore = asyncio.Semaphore(max(max_concurrent, 1))


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
    *,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Make an authenticated API request with retry on 429/5xx.

    Delegates to :func:`_api_request_inner` for the actual work, optionally
    wrapping the call with the concurrency semaphore when one has been
    initialised via :func:`init_request_semaphore`.

    Args:
        session: An active aiohttp ClientSession.
        method: HTTP method string (GET, POST, PATCH, etc.).
        url: Full URL for the request.
        token: Stoat session token for the x-session-token header.
        json_data: Optional JSON body. Not sent for GET requests.
        extra_headers: Additional HTTP headers to merge into the request.

    Returns:
        Parsed JSON response as a dict.

    Raises:
        MigrationError: On non-retryable errors or when all retries are exhausted.
    """
    if _request_semaphore is not None:
        async with _request_semaphore:
            return await _api_request_inner(
                session, method, url, token, json_data, extra_headers=extra_headers
            )
    return await _api_request_inner(
        session, method, url, token, json_data, extra_headers=extra_headers
    )


async def _api_request_inner(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    token: str,
    json_data: dict[str, Any] | None = None,
    *,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Core request logic with exponential backoff and circuit breaker."""
    global _rate_multiplier  # noqa: PLW0603
    headers = _headers(token)
    if extra_headers:
        headers.update(extra_headers)
    # Don't send a JSON body for GET requests even if one is accidentally provided.
    body = json_data if method.upper() != "GET" else None

    # Circuit breaker check: pause and reset if too many consecutive failures.
    if _circuit_state.consecutive_failures >= _CIRCUIT_THRESHOLD:
        logger.warning(
            "Circuit breaker open: %d consecutive failures. Pausing %ds.",
            _circuit_state.consecutive_failures,
            _CIRCUIT_PAUSE_SECONDS,
        )
        await asyncio.sleep(_CIRCUIT_PAUSE_SECONDS)
        _circuit_state.consecutive_failures = 0

    for attempt in range(MAX_API_RETRIES):
        try:
            async with session.request(method, url, json=body, headers=headers) as resp:
                if resp.status in (200, 201):
                    _circuit_state.consecutive_failures = 0
                    # Decay the rate multiplier gradually on successful requests.
                    if _rate_multiplier > 1.0 and not any(
                        time.monotonic() - t < 30 for t in _rate_429_window
                    ):
                        _rate_multiplier = max(_rate_multiplier * 0.75, 1.0)
                    return await resp.json()  # type: ignore[no-any-return]
                if resp.status == 204:
                    _circuit_state.consecutive_failures = 0
                    # Decay the rate multiplier gradually on successful requests.
                    if _rate_multiplier > 1.0 and not any(
                        time.monotonic() - t < 30 for t in _rate_429_window
                    ):
                        _rate_multiplier = max(_rate_multiplier * 0.75, 1.0)
                    return {}

                if resp.status in _RETRYABLE_STATUSES:
                    if attempt == MAX_API_RETRIES - 1:
                        text = await resp.text()
                        _circuit_state.consecutive_failures += 1
                        raise MigrationError(
                            f"API request failed after {MAX_API_RETRIES} retries: "
                            f"{resp.status} {text}"
                        )
                    if resp.status == 429:
                        # Rate-limited — NOT a circuit-breaker failure.
                        body_data: dict[str, Any] = await resp.json()
                        retry_ms = body_data.get("retry_after", 1000)
                        await asyncio.sleep(retry_ms / 1000)
                        # Track 429 frequency and ramp up the rate multiplier.
                        _rate_429_window.append(time.monotonic())
                        recent = sum(1 for t in _rate_429_window if time.monotonic() - t < 60)
                        if recent > 3:
                            _rate_multiplier = min(_rate_multiplier * 1.5, 5.0)
                            logger.info(
                                "Rate limit pressure — delay multiplier now %.1f×",
                                _rate_multiplier,
                            )
                    else:
                        # 5xx — exponential backoff with jitter.
                        delay = min(2**attempt, 60) + random.uniform(0.1, 0.5)
                        await asyncio.sleep(delay)
                        _circuit_state.consecutive_failures += 1
                    continue

                text = await resp.text()
                raise MigrationError(f"API error {resp.status}: {text}")
        except aiohttp.ClientError as exc:
            if attempt == MAX_API_RETRIES - 1:
                _circuit_state.consecutive_failures += 1
                raise MigrationError(
                    f"Network error after {MAX_API_RETRIES} retries: {exc}"
                ) from exc
            delay = min(2**attempt, 60) + random.uniform(0.1, 0.5)
            await asyncio.sleep(delay)
            _circuit_state.consecutive_failures += 1

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


async def api_upsert_categories(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    server_id: str,
    categories: list[dict[str, Any]],
) -> dict[str, Any]:
    """Set the full categories array on a server via PATCH.

    Each category dict must have ``id`` (str, 1-32 chars), ``title`` (str, max 32),
    and ``channels`` (list of channel ID strings).

    Args:
        session: An active aiohttp ClientSession.
        stoat_url: Stoat API base URL.
        token: Stoat session token.
        server_id: Target server ID.
        categories: Full list of category dicts to set on the server.

    Returns:
        Updated server object dict.
    """
    url = f"{stoat_url.rstrip('/')}/servers/{server_id}"
    return await _api_request(session, "PATCH", url, token, {"categories": categories})


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
    emoji_id: str,
    name: str,
    server_id: str,
) -> dict[str, Any]:
    """Create a custom emoji on a Stoat server.

    Uses ``PUT /custom/emoji/{emoji_id}`` where ``emoji_id`` is the Autumn
    file ID from a prior upload.  The Autumn ID becomes the emoji's permanent
    Stoat ID.

    Args:
        session: An active aiohttp ClientSession.
        stoat_url: Stoat API base URL.
        token: Stoat session token.
        emoji_id: Autumn file ID (becomes the emoji's permanent ID).
        name: Emoji display name (must match ``^[a-z0-9_]+$``, max 32 chars).
        server_id: Server that owns this emoji.

    Returns:
        Emoji object dict from the API.
    """
    url = f"{stoat_url.rstrip('/')}/custom/emoji/{emoji_id}"
    return await _api_request(
        session,
        "PUT",
        url,
        token,
        {"name": name, "parent": {"type": "Server", "id": server_id}},
    )


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
    idempotency_key: str | None = None,
    silent: bool = True,
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
        idempotency_key: Deduplication key sent as ``Idempotency-Key`` HTTP header
            (use ``f"ferry-{discord_msg_id}"``). Optional.
        silent: Suppress notifications. Defaults to True to avoid spam during migration.

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
    if silent:
        data["silent"] = True
    extra = {"Idempotency-Key": idempotency_key} if idempotency_key else None
    return await _api_request(session, "POST", url, token, data, extra_headers=extra)


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


async def api_set_role_permissions(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    server_id: str,
    role_id: str,
    *,
    allow: int,
    deny: int,
) -> dict[str, Any]:
    """Set permissions for a role on a server.

    Uses PUT /servers/{server}/permissions/{role_id}.
    """
    url = f"{stoat_url.rstrip('/')}/servers/{server_id}/permissions/{role_id}"
    return await _api_request(
        session, "PUT", url, token, {"permissions": {"allow": allow, "deny": deny}}
    )


async def api_set_server_default_permissions(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    server_id: str,
    *,
    permissions: int,
) -> dict[str, Any]:
    """Set server default (@everyone) permissions.

    Uses PUT /servers/{server}/permissions/default.
    """
    url = f"{stoat_url.rstrip('/')}/servers/{server_id}/permissions/default"
    return await _api_request(session, "PUT", url, token, {"permissions": permissions})


async def api_set_channel_role_permissions(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    channel_id: str,
    role_id: str,
    *,
    allow: int,
    deny: int,
) -> dict[str, Any]:
    """Set per-role permission override on a channel.

    Uses PUT /channels/{channel}/permissions/{role_id}.
    """
    url = f"{stoat_url.rstrip('/')}/channels/{channel_id}/permissions/{role_id}"
    return await _api_request(
        session, "PUT", url, token, {"permissions": {"allow": allow, "deny": deny}}
    )


async def api_set_channel_default_permissions(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    channel_id: str,
    *,
    allow: int,
    deny: int,
) -> dict[str, Any]:
    """Set default (everyone) permission override on a channel.

    Uses PUT /channels/{channel}/permissions/default.
    """
    url = f"{stoat_url.rstrip('/')}/channels/{channel_id}/permissions/default"
    return await _api_request(
        session, "PUT", url, token, {"permissions": {"allow": allow, "deny": deny}}
    )
