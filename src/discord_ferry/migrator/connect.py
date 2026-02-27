"""Phase 2: CONNECT — Test Stoat API connectivity and discover Autumn URL."""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiohttp

from discord_ferry.core.events import MigrationEvent
from discord_ferry.errors import ConnectionError as FerryConnectionError
from discord_ferry.migrator.api import get_session

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
        ConnectionError: If the API is unreachable, the token is invalid,
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


async def _discover_autumn_url(session: aiohttp.ClientSession, stoat_url: str) -> str:
    """GET the Stoat API root to discover the Autumn file server URL."""
    url = f"{stoat_url.rstrip('/')}/"
    try:
        async with session.get(url) as response:
            if response.status != 200:
                raise FerryConnectionError(f"Stoat API returned status {response.status} at {url}")
            data = await response.json()
    except aiohttp.ClientError as e:
        raise FerryConnectionError(f"Cannot reach Stoat API at {url}: {e}") from e

    try:
        autumn_url: str = data["features"]["autumn"]["url"]
    except (KeyError, TypeError) as e:
        raise FerryConnectionError(
            f"Stoat API response missing Autumn URL (features.autumn.url): {e}"
        ) from e

    if not autumn_url:
        raise FerryConnectionError("Stoat API returned empty Autumn URL")

    return autumn_url


async def _verify_token(session: aiohttp.ClientSession, stoat_url: str, token: str) -> None:
    """Verify the auth token by fetching the authenticated user's info."""
    url = f"{stoat_url.rstrip('/')}/users/@me"
    headers = {"x-session-token": token}
    try:
        async with session.get(url, headers=headers) as response:
            if response.status == 401:
                raise FerryConnectionError("Authentication failed: invalid or expired token")
            if response.status != 200:
                raise FerryConnectionError(
                    f"Token verification failed with status {response.status}"
                )
    except aiohttp.ClientError as e:
        raise FerryConnectionError(f"Token verification request failed: {e}") from e
