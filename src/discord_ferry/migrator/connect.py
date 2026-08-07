"""Phase 2: CONNECT — Test Stoat API connectivity and discover Autumn URL."""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiohttp

from discord_ferry.core.events import MigrationEvent
from discord_ferry.core.http import tls_hint
from discord_ferry.errors import StoatConnectionError
from discord_ferry.migrator.api import api_fetch_server, get_session

if TYPE_CHECKING:
    from discord_ferry.config import FerryConfig
    from discord_ferry.core.events import EventCallback
    from discord_ferry.parser.models import DCEExport
    from discord_ferry.state import MigrationState


async def run_connect(
    config: FerryConfig,
    state: MigrationState,
    exports: list[DCEExport],
    on_event: EventCallback,
) -> None:
    """Test Stoat API connectivity, discover Autumn URL, and verify auth token.

    Args:
        config: Ferry configuration with stoat_url and token.
        state: Migration state — autumn_url will be set on success.
        exports: Parsed DCE exports (unused by this phase).
        on_event: Event callback for progress reporting.

    Raises:
        StoatConnectionError: If the API is unreachable, the token is invalid,
            or the Autumn URL cannot be discovered.
    """
    on_event(
        MigrationEvent(
            phase="connect",
            status="progress",
            message=f"Connecting to {config.stoat_url}...",
        )
    )

    if config.dry_run:
        state.autumn_url = "https://dry-run.invalid"
        on_event(
            MigrationEvent(
                phase="connect",
                status="completed",
                message="[DRY RUN] Skipping API connection",
            )
        )
        return

    async with get_session(config) as session:
        # Step 1: Test connectivity and discover Autumn URL
        autumn_url = await _discover_autumn_url(session, config.stoat_url)
        state.autumn_url = autumn_url
        on_event(
            MigrationEvent(
                phase="connect",
                status="progress",
                message=f"Autumn URL: {autumn_url}",
            )
        )

        # Step 2: Verify auth token
        await _verify_token(session, config.stoat_url, config.token)
        on_event(
            MigrationEvent(
                phase="connect",
                status="progress",
                message="Authentication verified",
            )
        )

        # Step 3: Best-effort permission pre-check on existing server.
        if config.server_id:
            await _check_server_permissions(
                session, config.stoat_url, config.token, config.server_id, on_event
            )


async def _discover_autumn_url(session: aiohttp.ClientSession, stoat_url: str) -> str:
    """GET the Stoat API root to discover the Autumn file server URL."""
    url = f"{stoat_url.rstrip('/')}/"
    try:
        async with session.get(url) as response:
            if response.status != 200:
                raise StoatConnectionError(f"Stoat API returned status {response.status} at {url}")
            data = await response.json()
    except aiohttp.ClientError as e:
        raise StoatConnectionError(
            f"Cannot reach Stoat API at {url}: {e}{tls_hint(e) or ''}"
        ) from e

    try:
        autumn_url: str = data["features"]["autumn"]["url"]
    except (KeyError, TypeError) as e:
        raise StoatConnectionError(
            f"Stoat API response missing Autumn URL (features.autumn.url): {e}"
        ) from e

    if not autumn_url:
        raise StoatConnectionError("Stoat API returned empty Autumn URL")

    return autumn_url


async def _check_server_permissions(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    server_id: str,
    on_event: EventCallback,
) -> None:
    """Best-effort check that the server exists and is accessible."""
    try:
        await api_fetch_server(session, stoat_url, token, server_id)
        on_event(
            MigrationEvent(
                phase="connect",
                status="progress",
                message=f"Server {server_id} verified accessible",
            )
        )
    except Exception as exc:  # noqa: BLE001
        on_event(
            MigrationEvent(
                phase="connect",
                status="warning",
                message=f"Could not verify server {server_id}: {exc}",
            )
        )


async def _verify_token(session: aiohttp.ClientSession, stoat_url: str, token: str) -> None:
    """Verify the auth token by fetching the authenticated user's info."""
    url = f"{stoat_url.rstrip('/')}/users/@me"
    headers = {"x-session-token": token}
    try:
        async with session.get(url, headers=headers) as response:
            if response.status == 401:
                raise StoatConnectionError("Authentication failed: invalid or expired token")
            if response.status != 200:
                raise StoatConnectionError(
                    f"Token verification failed with status {response.status}"
                )
    except aiohttp.ClientError as e:
        raise StoatConnectionError(
            f"Token verification request failed: {e}{tls_hint(e) or ''}"
        ) from e
