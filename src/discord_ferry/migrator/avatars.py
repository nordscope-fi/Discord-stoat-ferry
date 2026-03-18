"""Phase 7.5: AVATARS — Pre-flight avatar download and Autumn upload."""

from __future__ import annotations

from typing import TYPE_CHECKING

from discord_ferry.core.events import MigrationEvent

if TYPE_CHECKING:
    from discord_ferry.config import FerryConfig
    from discord_ferry.core.events import EventCallback
    from discord_ferry.parser.models import DCEExport
    from discord_ferry.state import MigrationState


async def run_avatars(
    config: FerryConfig,
    state: MigrationState,
    exports: list[DCEExport],
    on_event: EventCallback,
) -> None:
    """Upload unique author avatars to Autumn before message migration."""
    on_event(
        MigrationEvent(phase="avatars", status="completed", message="No avatars to process (stub)")
    )
