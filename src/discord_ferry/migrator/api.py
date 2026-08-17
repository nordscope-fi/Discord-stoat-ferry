"""Thin async wrapper around the Stoat REST API."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp  # noqa: TCH002

from discord_ferry.core.http import new_session, proxy_error_is_permanent, proxy_hint, tls_hint
from discord_ferry.errors import DuplicateSendError, MigrationError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from discord_ferry.config import FerryConfig

logger = logging.getLogger(__name__)

MAX_API_RETRIES = 3
_RETRYABLE_STATUSES = {429, 502, 503, 504}

_CIRCUIT_THRESHOLD = 5
_CIRCUIT_PAUSE_SECONDS = 30

_MAX_RETRY_DELAY_SECONDS = 60  # cap any server/body-advertised 429 retry delay


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
        async with new_session() as session:
            yield session


def _headers(token: str | None) -> dict[str, str]:
    h: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        h["x-session-token"] = token
    return h


async def _api_request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    token: str | None,
    json_data: dict[str, Any] | None = None,
    *,
    extra_headers: dict[str, str] | None = None,
    expected_404_ok: bool = False,
) -> dict[str, Any]:
    """Make an authenticated API request with retry on 429/5xx.

    Delegates to :func:`_api_request_inner` for the actual work, optionally
    wrapping the call with the concurrency semaphore when one has been
    initialised via :func:`init_request_semaphore`.

    Args:
        session: An active aiohttp ClientSession.
        method: HTTP method string (GET, POST, PATCH, etc.).
        url: Full URL for the request.
        token: Stoat session token for the x-session-token header, or None to
            omit it entirely (auth-less webhook-execute path).
        json_data: Optional JSON body. Not sent for GET requests.
        extra_headers: Additional HTTP headers to merge into the request.
        expected_404_ok: When True, a 404 response is treated as functional
            success — returns ``{}``, resets ``_circuit_state.consecutive_failures``
            and decays ``_rate_multiplier`` identically to a 204. Used by
            rollback DELETE wrappers so idempotent re-runs don't prime the
            circuit breaker.

    Returns:
        Parsed JSON response as a dict.

    Raises:
        MigrationError: On non-retryable errors or when all retries are exhausted.
    """
    if _request_semaphore is not None:
        async with _request_semaphore:
            return await _api_request_inner(
                session,
                method,
                url,
                token,
                json_data,
                extra_headers=extra_headers,
                expected_404_ok=expected_404_ok,
            )
    return await _api_request_inner(
        session,
        method,
        url,
        token,
        json_data,
        extra_headers=extra_headers,
        expected_404_ok=expected_404_ok,
    )


def _stoat_rate_delay_seconds(headers: Mapping[str, str]) -> float | None:
    """Resolve a 429 delay from Stoat's rate-limit headers, returned in SECONDS.

    UNIT HAZARD — do NOT merge this with ``discord.client._retry_after_seconds``.
    The two look like duplicates and are not. Stoat documents
    ``X-RateLimit-Reset-After`` as MILLISECONDS ("Milliseconds left until calls are
    replenished", developers.stoat.chat/developers/api/ratelimits), while Discord
    sends the identically-named header as delta-seconds. Unifying them silently
    breaks one side by a factor of 1000.
    ``tests/test_discord_client.py::test_discord_reset_after_header_is_seconds_not_milliseconds``
    pins the Discord side against exactly that refactor.

    ``Retry-After`` (RFC 9110 delta-seconds) is honoured first: Stoat itself does not
    send it, but a proxy or CDN in front of Stoat may. A non-numeric value (an
    HTTP-date form) is ignored so the caller falls back to the body.

    Returns None when no usable header is present.
    """
    raw_retry_after = headers.get("Retry-After")
    if raw_retry_after:
        try:
            return float(raw_retry_after)
        except ValueError:
            pass  # HTTP-date form — fall through to the Stoat header, then the body.

    raw_reset_after = headers.get("X-RateLimit-Reset-After")
    if raw_reset_after:
        try:
            return float(raw_reset_after) / 1000
        except ValueError:
            pass

    return None


async def _resolve_429_delay_seconds(resp: aiohttp.ClientResponse) -> float:
    """Resolve the 429 sleep in seconds: headers → body ``retry_after`` (ms) → 1s; capped.

    Every Stoat-side source is millisecond-based except ``Retry-After``; see
    :func:`_stoat_rate_delay_seconds` for the unit rules and why this must stay
    separate from the Discord client's same-named parser.

    The body parse is content-type-guarded so a non-JSON 429 (proxy HTML) can't raise
    ``ContentTypeError`` into the network-error path and prime the circuit breaker.
    """
    header_s = _stoat_rate_delay_seconds(resp.headers)
    if header_s is not None:
        # Floor at 0: a negative advertised delay parses fine as a float and would
        # otherwise disable backoff entirely (asyncio.sleep of a negative value
        # returns immediately, so we would retry in a hot loop).
        return max(0.0, min(header_s, _MAX_RETRY_DELAY_SECONDS))
    try:
        body = await resp.json(content_type=None)
    except (json.JSONDecodeError, ValueError):
        body = None
    raw = body.get("retry_after") if isinstance(body, dict) else None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw):
        retry_ms = float(raw)
    else:
        retry_ms = 1000.0
    return max(0.0, min(retry_ms / 1000, _MAX_RETRY_DELAY_SECONDS))


async def _api_request_inner(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    token: str | None,
    json_data: dict[str, Any] | None = None,
    *,
    extra_headers: dict[str, str] | None = None,
    expected_404_ok: bool = False,
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
                    try:
                        return await resp.json()  # type: ignore[no-any-return]
                    except (aiohttp.ContentTypeError, json.JSONDecodeError) as exc:
                        raise MigrationError(
                            f"Unexpected non-JSON {resp.status} response "
                            f"(content-type: {resp.content_type})"
                        ) from exc
                if resp.status == 204 or (resp.status == 404 and expected_404_ok):
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
                        # A 429 is rate-limiting, never a circuit-breaker failure — even on
                        # budget exhaustion. A 5xx still primes the breaker.
                        if resp.status != 429:
                            _circuit_state.consecutive_failures += 1
                        raise MigrationError(
                            f"API request failed after {MAX_API_RETRIES} retries: "
                            f"{resp.status} {text}"
                        )
                    if resp.status == 429:
                        # Rate-limited — NOT a circuit-breaker failure. The body parse is
                        # content-type-guarded so a non-JSON 429 can't escape into the network
                        # path and prime the breaker; honour Retry-After over the body delay.
                        await asyncio.sleep(await _resolve_429_delay_seconds(resp))
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

                if resp.status == 409:
                    # A still-cached Idempotency-Key. The message IS on the server;
                    # Stoat answers DuplicateNonce and does not return it, so the caller
                    # gets no message id. Only a request that CARRIED a key can reach
                    # this, so "the caller opted in" and "the caller sent a key" are the
                    # same condition and no opt-in flag is needed.
                    #
                    # isinstance-guarded exactly like the 429 body parse at :203: a
                    # syntactically valid 409 body that is a list, a number or a string
                    # would otherwise raise AttributeError out of this branch instead of
                    # falling through to the catch-all below.
                    try:
                        dup_body = await resp.json(content_type=None)
                    except (json.JSONDecodeError, ValueError):
                        dup_body = None
                    if isinstance(dup_body, dict) and dup_body.get("type") == "DuplicateNonce":
                        # A delivered message, so this must not prime the breaker. Same
                        # bookkeeping as the 204 branch above.
                        _circuit_state.consecutive_failures = 0
                        if _rate_multiplier > 1.0 and not any(
                            time.monotonic() - t < 30 for t in _rate_429_window
                        ):
                            _rate_multiplier = max(_rate_multiplier * 0.75, 1.0)
                        raise DuplicateSendError(
                            "Stoat rejected a duplicate Idempotency-Key; the message "
                            "is already on the server"
                        )

                text = await resp.text()
                raise MigrationError(f"API error {resp.status}: {text}")
        except aiohttp.ClientError as exc:
            # Compute each hint ONCE. Message and control flow are separate
            # decisions over the same two values, which is why this is two
            # lines rather than one. Proxy wins over the certificate hint and
            # they are NEVER concatenated: with an https:// proxy a certificate
            # error is built from the PROXY's connection key
            # (connector.py:1630-1632), so tls_hint would name a host the user
            # never configured.
            cert = tls_hint(exc)
            hint = proxy_hint(exc, target=url) or cert
            if hint is not None and (cert is not None or proxy_error_is_permanent(exc)):
                # Above both consecutive_failures increments on purpose. Five
                # short-circuited channels in the parallel message phase would
                # otherwise open the breaker and add a 30s sleep to an error
                # that cannot recover.
                #
                # Gated on permanence: a proxy 502, 503, 504 or connect timeout
                # CAN recover and must keep the retries it already had.
                raise MigrationError(f"Network error: {exc}{hint}") from exc
            if attempt == MAX_API_RETRIES - 1:
                _circuit_state.consecutive_failures += 1
                raise MigrationError(
                    f"Network error after {MAX_API_RETRIES} retries: {exc}{hint or ''}"
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
) -> str:
    """Create a new server on the Stoat instance and return its id.

    The route answers with ``CreateServerLegacyResponse`` (``create_server`` in upstream
    ``routes/servers/server_create.rs``), which nests the server beside the channels
    Stoat creates with it::

        {"server": {"_id": ..., ...}, "channels": [...]}

    There is no ``serde(flatten)``, so reading ``_id`` off the top level raised
    ``KeyError`` and killed the server phase (#265). The ``channels`` member is
    discarded: Ferry creates its own channels and does not adopt the default one.

    Args:
        session: An active aiohttp ClientSession.
        stoat_url: Stoat API base URL (e.g. "https://api.stoat.chat").
        token: Stoat session token.
        name: Display name for the new server.

    Returns:
        The new server's id.

    Raises:
        MigrationError: If the response carries no recognisable server id. The message
            names the route and the key names received, never a value: on the
            ``ferry build`` path nothing downstream redacts it.
    """
    url = f"{stoat_url.rstrip('/')}/servers/create"
    version_hint = "The Stoat instance may be running an incompatible API version."
    # ``object`` local: _api_request is declared -> dict[str, Any] but returns whatever
    # JSON arrived, so isinstance narrowing is required rather than optional.
    raw: object = await _api_request(session, "POST", url, token, {"name": name})
    if not isinstance(raw, dict):
        # Never sorted(raw) here: on a list that sorts the elements into the message.
        raise MigrationError(
            f"Stoat returned {type(raw).__name__} from {url}, expected an object. {version_hint}"
        )
    server = raw.get("server")
    if server is None:
        raise MigrationError(
            f"Stoat returned no 'server' member from {url}; keys were {sorted(raw)}. {version_hint}"
        )
    if not isinstance(server, dict):
        raise MigrationError(
            f"Stoat returned a {type(server).__name__} as 'server' from {url}, "
            f"expected an object. {version_hint}"
        )
    server_id = server.get("_id")
    if not isinstance(server_id, str) or not server_id:
        # Nested keys, not top-level: an upstream rename of _id leaves the top level
        # looking exactly as Ferry expects, so naming it would read like success.
        raise MigrationError(
            f"Stoat returned no server id from {url}; 'server' keys were "
            f"{sorted(server)}. {version_hint}"
        )
    return server_id


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


async def api_fetch_root(session: aiohttp.ClientSession, stoat_url: str) -> dict[str, Any]:
    """Best-effort GET / for instance config (limits, app URL).

    Returns the parsed config dict, or ``{}`` on any non-200 status, network
    error, or malformed JSON body. Intentionally NOT routed through
    ``_api_request`` -- this is a best-effort probe and must not carry the
    retry/circuit-breaker machinery.
    """
    url = f"{stoat_url.rstrip('/')}/"
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return {}
            return await resp.json()  # type: ignore[no-any-return]
    except (aiohttp.ClientError, ValueError):
        return {}


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


async def api_create_invite(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    channel_id: str,
) -> dict[str, Any]:
    """Create an invite to a channel.

    Uses ``POST /channels/{channel_id}/invites``. Backend requires a TextChannel,
    a non-bot caller, and the ``InviteOthers`` permission. The invite code is
    returned as the ``_id`` field (read ``result.get("_id") or result.get("code")``).
    Not idempotent — each call mints a new invite.
    """
    url = f"{stoat_url.rstrip('/')}/channels/{channel_id}/invites"
    return await _api_request(session, "POST", url, token, {})


# ---------------------------------------------------------------------------
# Webhook wrappers — PROBE-ONLY. Never called from the message-send path
# (feature C / webhook message-posting is OUT of scope). See migrator/probe.py.
# ---------------------------------------------------------------------------


async def api_create_webhook(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    channel_id: str,
    *,
    name: str,
    avatar: str | None = None,
) -> dict[str, Any]:
    """Create a channel webhook (probe-only).

    ``POST /channels/{channel_id}/webhooks``. Requires ``ManageWebhooks`` on a
    TextChannel/Group. Response ``id`` is the webhook id (Ulid, NOT ``_id``);
    ``token`` is the execute secret.
    """
    url = f"{stoat_url.rstrip('/')}/channels/{channel_id}/webhooks"
    data: dict[str, Any] = {"name": name}
    if avatar is not None:
        data["avatar"] = avatar
    return await _api_request(session, "POST", url, token, data)


async def api_fetch_channel(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    channel_id: str,
) -> dict[str, Any]:
    """Fetch a channel object by id (GET /channels/{id}).

    Used by the probe to read back the actual ``channel_type`` / ``voice`` field
    after a create (voice Bug #194 detection).
    """
    url = f"{stoat_url.rstrip('/')}/channels/{channel_id}"
    return await _api_request(session, "GET", url, token)


async def api_delete_webhook(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    webhook_id: str,
) -> None:
    """Delete a webhook (DELETE /webhooks/{id}); 404 treated as success."""
    url = f"{stoat_url.rstrip('/')}/webhooks/{webhook_id}"
    await _api_request(session, "DELETE", url, token, expected_404_ok=True)


async def api_execute_webhook(
    session: aiohttp.ClientSession,
    stoat_url: str,
    webhook_id: str,
    webhook_token: str,
    *,
    content: str | None = None,
    attachments: list[str] | None = None,
    embeds: list[dict[str, Any]] | None = None,
    masquerade: dict[str, str | None] | None = None,
    replies: list[dict[str, Any]] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Execute a webhook (probe-only): POST /webhooks/{id}/{token}.

    The URL token authenticates — the request MUST NOT carry ``x-session-token``
    (passed via ``token=None`` to ``_api_request``). Body mirrors
    ``api_send_message``'s ``DataMessageSend`` shape.
    """
    url = f"{stoat_url.rstrip('/')}/webhooks/{webhook_id}/{webhook_token}"
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
    extra = {"Idempotency-Key": idempotency_key} if idempotency_key else None
    return await _api_request(session, "POST", url, token=None, json_data=data, extra_headers=extra)


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


async def api_edit_role_ranks(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    server_id: str,
    ranks: list[str],
) -> dict[str, Any]:
    """Set the server's role hierarchy in one call.

    ``PATCH /servers/{id}/roles/{role_id}`` accepts a ``rank`` field and throws it
    away: the upstream ``roles_edit`` handler destructures ``DataEditRole`` with a
    rest pattern that does not bind ``rank``. This route is the only one that sets
    ordering.

    ``edit_role_ranks`` assigns each role a rank equal to its INDEX in *ranks*, so
    **index 0 is the top of the hierarchy**. It rejects any list that does not name
    every role on the server, answering ``InvalidOperation``.

    Args:
        session: An active aiohttp ClientSession.
        stoat_url: Stoat API base URL.
        token: Stoat session token.
        server_id: Target server ID.
        ranks: Every role id on the server, highest authority first.

    Returns:
        Updated server object dict.
    """
    url = f"{stoat_url.rstrip('/')}/servers/{server_id}/roles/ranks"
    return await _api_request(session, "PATCH", url, token, {"ranks": ranks})


async def api_edit_channel(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    channel_id: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Edit a channel's properties (slowmode, voice info, etc.).

    Args:
        session: An active aiohttp ClientSession.
        stoat_url: Stoat API base URL.
        token: Stoat session token.
        channel_id: Target channel ID.
        **kwargs: Fields to update (e.g. ``slowmode=30``, ``voice={"max_users": 5}``).

    Returns:
        Updated channel object dict.
    """
    url = f"{stoat_url.rstrip('/')}/channels/{channel_id}"
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


async def api_edit_message(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    channel_id: str,
    message_id: str,
    *,
    content: str | None = None,
    embeds: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Edit an existing message in a channel.

    Args:
        session: An active aiohttp ClientSession.
        stoat_url: Stoat API base URL.
        token: Stoat session token.
        channel_id: Channel containing the message.
        message_id: Target message ID to edit.
        content: New message text content. Optional.
        embeds: New list of embed dicts. Optional.

    Returns:
        Updated message object dict from the API.
    """
    url = f"{stoat_url.rstrip('/')}/channels/{channel_id}/messages/{message_id}"
    data: dict[str, Any] = {}
    if content is not None:
        data["content"] = content
    if embeds is not None:
        data["embeds"] = embeds
    return await _api_request(session, "PATCH", url, token, data)


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
    return await _api_request(session, "POST", url, token, None)


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


async def api_delete_channel(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    channel_id: str,
) -> None:
    """Delete a channel by ID.

    Uses DELETE /channels/{channel_id}. A 404 response is treated as success
    (idempotent re-rolls) via ``expected_404_ok=True``.
    """
    url = f"{stoat_url.rstrip('/')}/channels/{channel_id}"
    await _api_request(session, "DELETE", url, token, expected_404_ok=True)


async def api_delete_role(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    server_id: str,
    role_id: str,
) -> None:
    """Delete a role from a server.

    Uses DELETE /servers/{server_id}/roles/{role_id}. A 404 response is
    treated as success (idempotent re-rolls) via ``expected_404_ok=True``.
    """
    url = f"{stoat_url.rstrip('/')}/servers/{server_id}/roles/{role_id}"
    await _api_request(session, "DELETE", url, token, expected_404_ok=True)


async def api_delete_emoji(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    emoji_id: str,
) -> None:
    """Delete a custom emoji.

    Uses DELETE /custom/emoji/{emoji_id}. A 404 response is treated as success
    (idempotent re-rolls) via ``expected_404_ok=True``. Requires
    ``ManageCustomisation`` permission at the server level.
    """
    url = f"{stoat_url.rstrip('/')}/custom/emoji/{emoji_id}"
    await _api_request(session, "DELETE", url, token, expected_404_ok=True)


async def api_fetch_messages(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    channel_id: str,
    *,
    limit: int,
    sort: str,
) -> list[dict[str, Any]]:
    """Read a channel's messages (GET /channels/{id}/messages).

    ``limit`` and ``sort`` are keyword-only and required rather than defaulted,
    which is deliberate on both counts. Upstream validates ``limit`` as
    ``range(min = 1, max = 100)``, and its ``MessageSort`` default is
    ``Relevance``, which this route explicitly rejects. A caller that could omit
    ``sort`` would inherit a value the server refuses.

    ``include_users`` is never sent. The response is an untagged enum: without
    that parameter it is a bare array of messages, with it an object carrying
    ``messages``, ``users`` and ``members``. Omitting it pins the shape.

    A message id arrives as ``_id``, not ``id``. Ferry's webhook id field IS
    ``id``, so the two are inconsistent upstream and neither can be inferred
    from the other.

    Requires ``ReadMessageHistory``. Lands in the ``channels`` rate bucket, 15
    per 10 seconds keyed per channel id, because upstream routes to
    ``messaging`` only on POST.
    """
    # ValueError, not MigrationError, and deliberately so. These two guards
    # catch a CALLER passing arguments this route cannot accept, which is a
    # programming error, not a migration failure. Both values are supplied by
    # Ferry itself and no CLI flag feeds either, so neither is reachable from
    # user input. Raising a FerryError here would let the CLI's per-command
    # handler print a bad call as though the migration had failed, hiding the
    # bug; an uncaught ValueError points a developer straight at the call site.
    # A chunk review proposed converting these and the proposal was checked
    # rather than taken: the CLI genuinely does not catch ValueError, but the
    # path is unreachable by construction.
    if not 1 <= limit <= 100:
        raise ValueError(f"limit must be between 1 and 100, got {limit}")
    if sort not in ("Latest", "Oldest"):
        raise ValueError(f"sort must be 'Latest' or 'Oldest', got {sort!r}")
    url = f"{stoat_url.rstrip('/')}/channels/{channel_id}/messages?limit={limit}&sort={sort}"
    # Annotated `object` on purpose. _api_request is declared -> dict[str, Any]
    # but its success path returns whatever JSON arrived, and this route returns
    # an array. Widening that shared helper would cost 28 callers their typing,
    # and a type: ignore here would hide a real shape mismatch, so the narrowing
    # happens where the shape is actually known.
    raw: object = await _api_request(session, "GET", url, token)
    if not isinstance(raw, list):
        raise MigrationError(
            f"Expected a JSON array of messages from {url}, got {type(raw).__name__}"
        )
    return raw


async def api_fetch_server_with_channels(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    server_id: str,
) -> dict[str, Any]:
    """Fetch a server together with its channel objects.

    ``GET /servers/{id}?include_channels=true``. Deliberately a separate
    function from :func:`api_fetch_server` rather than a parameter on it.
    Upstream answers this route with ``FetchServerResponse::JustServer`` when
    the parameter is absent and ``ServerWithChannels`` when it is present, so
    adding it to the existing function would change the response shape under the
    ``validate_after`` block in ``run_migration`` and under rollback, neither of
    which this work touches.

    The response carries two lists, and the pair is the point. ``server`` is
    returned unmodified, so its own ``channels`` field still names every channel
    id on the server. The sibling ``channels`` array holds only the channel
    objects the caller may ``ViewChannel``. Comparing the two is the only way to
    tell a channel that was deleted from one this token simply cannot see.

    Lands in the ``servers`` rate bucket, 5 per 10 seconds keyed per server id.
    """
    url = f"{stoat_url.rstrip('/')}/servers/{server_id}?include_channels=true"
    return await _api_request(session, "GET", url, token)


async def api_fetch_emoji_list(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    server_id: str,
) -> list[dict[str, Any]]:
    """List a server's custom emoji (GET /servers/{id}/emojis).

    A separate request because the ``Server`` model carries no emoji field at
    all. Upstream returns ``Vec<Emoji>`` and requires only server membership,
    unlike the message route which requires ``ReadMessageHistory``.

    Narrowed the same way as :func:`api_fetch_messages`, and for the same
    reason: this route returns an array while the shared request helper is
    declared to return a mapping.

    Lands in the ``servers`` rate bucket, 5 per 10 seconds keyed per server id.
    """
    url = f"{stoat_url.rstrip('/')}/servers/{server_id}/emojis"
    raw: object = await _api_request(session, "GET", url, token)
    if not isinstance(raw, list):
        raise MigrationError(f"Expected a JSON array of emoji from {url}, got {type(raw).__name__}")
    return raw
