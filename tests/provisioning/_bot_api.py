"""Discord REST helpers for test-server provisioning (Bot auth).

This module deliberately does NOT inherit its exceptions from FerryError.
The location (tests/provisioning/) excludes it from built wheels via the
[tool.hatch.build.targets.wheel] stanza in pyproject.toml; the separate
exception hierarchy reinforces that isolation in the type system.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

DISCORD_API_BASE = "https://discord.com/api/v10"
USER_AGENT = (
    "DiscordFerry-TestProvisioner (https://github.com/nordscope-fi/Discord-stoat-ferry, v0.0.0)"
)


class ProvisioningError(Exception):
    """Base for all provisioning failures."""


class ProvisioningAuthError(ProvisioningError):
    """Bot token invalid, expired, or missing required scopes (401)."""


class ProvisioningPermissionError(ProvisioningError):
    """Bot lacks Discord permission for the operation (403)."""


class ProvisioningRateLimitError(ProvisioningError):
    """Rate limited after exhausting retries (429)."""


class TokenRedactingFilter(logging.Filter):
    """Scrubs the bot token from every log record's resolved message.

    Operates on record.getMessage() (which uniformly handles %-style, {}-style,
    and Mapping-args formatting) rather than scanning record.msg and record.args
    separately. If the token is found, replaces record.msg with the redacted
    version and clears record.args so re-formatting doesn't reintroduce the
    token.
    """

    REDACTED = "<TOKEN>"

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if self._token in message:
            record.msg = message.replace(self._token, self.REDACTED)
            record.args = None
        return True


_MAX_RETRIES = 3
_BACKOFF_SECONDS = (1.0, 2.0, 4.0)
_JITTER_SECONDS = 0.05


async def _request_with_retry(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    headers: dict[str, str],
    json_body: dict[str, Any] | None,
) -> dict[str, Any] | list[dict[str, Any]]:
    """Perform an HTTP request with 429/5xx/network retry.

    On 429: sleep retry_after + jitter, retry up to 3 times total.
    On 5xx or aiohttp.ClientError: exp backoff 1/2/4s, retry up to 3 times.
    On 401: ProvisioningAuthError (no retry).
    On 403: ProvisioningPermissionError (no retry).
    On 4xx (other): ProvisioningError with Discord's code + message (never errors).
    """
    last_exception: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            async with session.request(method, url, headers=headers, json=json_body) as resp:
                if resp.status in (200, 201, 204):
                    if resp.status == 204:
                        return {}
                    return await resp.json()  # type: ignore[no-any-return]
                if resp.status == 401:
                    raise ProvisioningAuthError("bot token rejected by Discord (401 Unauthorized)")
                if resp.status == 403:
                    raise ProvisioningPermissionError(
                        f"missing permission for {method} {url.split('/')[-2]} (403 Forbidden)"
                    )
                if resp.status == 429:
                    body = await resp.json()
                    retry_after = float(body.get("retry_after", 1))
                    await asyncio.sleep(retry_after + _JITTER_SECONDS)
                    continue
                if 500 <= resp.status < 600:
                    last_exception = ProvisioningError(f"Discord server error ({resp.status})")
                    await asyncio.sleep(_BACKOFF_SECONDS[attempt])
                    continue
                # 4xx (other): surface code + message ONLY
                body = await resp.json()
                code = body.get("code", "unknown")
                message = body.get("message", "no message")
                raise ProvisioningError(f"Discord {resp.status} (code {code}): {message}")
        except (ProvisioningAuthError, ProvisioningPermissionError, ProvisioningError):
            raise
        except aiohttp.ClientError as exc:
            last_exception = ProvisioningError(f"network error: {type(exc).__name__}")
            await asyncio.sleep(_BACKOFF_SECONDS[attempt])
            continue

    # Loop exhausted
    if isinstance(last_exception, ProvisioningError):
        raise last_exception
    raise ProvisioningRateLimitError("rate limited after 3 retries")


class BotApi:
    """Authenticated async client for Discord REST API v10 using Bot tokens.

    The Authorization HEADER DICT is built ad-hoc inside each method;
    the token VALUE is stored once as self._token and never exposed via
    introspection (overridden __repr__ redacts it).
    """

    def __init__(self, session: aiohttp.ClientSession, token: str) -> None:
        self._session = session
        self._token = token

    def __repr__(self) -> str:
        return "BotApi(token=<redacted>)"

    def _headers(self, audit_reason: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bot {self._token}",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        }
        if audit_reason is not None:
            headers["X-Audit-Log-Reason"] = audit_reason
        return headers

    async def list_my_guilds(self) -> list[dict[str, Any]]:
        """GET /users/@me/guilds — for --create-guild preflight."""
        result = await _request_with_retry(
            self._session,
            "GET",
            f"{DISCORD_API_BASE}/users/@me/guilds",
            self._headers(),
            None,
        )
        return result  # type: ignore[return-value]

    async def list_channels(self, guild_id: str) -> list[dict[str, Any]]:
        """GET /guilds/{id}/channels — returns text+forum channels, NOT threads."""
        result = await _request_with_retry(
            self._session,
            "GET",
            f"{DISCORD_API_BASE}/guilds/{guild_id}/channels",
            self._headers(),
            None,
        )
        return result  # type: ignore[return-value]

    async def list_messages(self, channel_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """GET /channels/{id}/messages?limit=N — paginated reads not modelled."""
        result = await _request_with_retry(
            self._session,
            "GET",
            f"{DISCORD_API_BASE}/channels/{channel_id}/messages?limit={limit}",
            self._headers(),
            None,
        )
        return result  # type: ignore[return-value]

    async def list_active_threads(self, guild_id: str) -> list[dict[str, Any]]:
        """GET /guilds/{id}/threads/active — guild-wide active threads."""
        result = await _request_with_retry(
            self._session,
            "GET",
            f"{DISCORD_API_BASE}/guilds/{guild_id}/threads/active",
            self._headers(),
            None,
        )
        # API returns {"threads": [...], "members": [...]} — extract threads.
        return result["threads"]  # type: ignore[call-overload,no-any-return]

    async def list_archived_public_threads(self, parent_channel_id: str) -> list[dict[str, Any]]:
        """GET /channels/{parent}/threads/archived/public — per-parent.

        Pagination NOT implemented; test server is bounded to <100 threads
        per parent by spec. If has_more becomes true at scale, paginate via
        the `before` cursor.
        """
        result = await _request_with_retry(
            self._session,
            "GET",
            f"{DISCORD_API_BASE}/channels/{parent_channel_id}/threads/archived/public",
            self._headers(),
            None,
        )
        return result["threads"]  # type: ignore[call-overload,no-any-return]

    async def send_message(
        self,
        channel_id: str,
        *,
        content: str,
        embed: dict[str, Any] | None,
        audit_reason: str,
    ) -> dict[str, Any]:
        """POST /channels/{id}/messages — supports text + optional embed."""
        body: dict[str, Any] = {"content": content}
        if embed is not None:
            body["embeds"] = [embed]
        result = await _request_with_retry(
            self._session,
            "POST",
            f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
            self._headers(audit_reason),
            body,
        )
        return result  # type: ignore[return-value]

    async def create_channel(
        self,
        guild_id: str,
        *,
        name: str,
        channel_type: int,
        topic: str,
        audit_reason: str,
    ) -> dict[str, Any]:
        """POST /guilds/{id}/channels — text (type=0) or forum (type=15)."""
        body = {"name": name, "type": channel_type, "topic": topic}
        result = await _request_with_retry(
            self._session,
            "POST",
            f"{DISCORD_API_BASE}/guilds/{guild_id}/channels",
            self._headers(audit_reason),
            body,
        )
        return result  # type: ignore[return-value]

    async def create_thread_from_message(
        self,
        channel_id: str,
        message_id: str,
        *,
        name: str,
        audit_reason: str,
    ) -> dict[str, Any]:
        """POST /channels/{ch}/messages/{msg}/threads — thread from text-channel message."""
        body = {"name": name, "auto_archive_duration": 1440}
        result = await _request_with_retry(
            self._session,
            "POST",
            f"{DISCORD_API_BASE}/channels/{channel_id}/messages/{message_id}/threads",
            self._headers(audit_reason),
            body,
        )
        return result  # type: ignore[return-value]

    async def create_forum_post(
        self,
        forum_channel_id: str,
        *,
        name: str,
        first_message_content: str,
        audit_reason: str,
    ) -> dict[str, Any]:
        """POST /channels/{forum}/threads — creates forum post (thread+nested first message).

        Forum channels REJECT POST /messages. Posts go through this endpoint
        which returns a channel object with the nested first message.
        """
        body = {
            "name": name,
            "auto_archive_duration": 1440,
            "message": {"content": first_message_content},
        }
        result = await _request_with_retry(
            self._session,
            "POST",
            f"{DISCORD_API_BASE}/channels/{forum_channel_id}/threads",
            self._headers(audit_reason),
            body,
        )
        return result  # type: ignore[return-value]
