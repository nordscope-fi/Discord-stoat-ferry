"""Pin preservation — Phase 10."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from discord_ferry.core.events import MigrationEvent
from discord_ferry.migrator.api import api_pin_message, get_session

if TYPE_CHECKING:
    from discord_ferry.config import FerryConfig
    from discord_ferry.core.events import EventCallback
    from discord_ferry.parser.models import DCEExport
    from discord_ferry.state import MigrationState

logger = logging.getLogger(__name__)


async def run_pins(
    config: FerryConfig,
    state: MigrationState,
    exports: list[DCEExport],
    on_event: EventCallback,
) -> None:
    """Phase 10 — Re-pin messages that were pinned in Discord.

    The messages phase populates ``state.pending_pins`` as a list of
    ``(stoat_channel_id, stoat_message_id)`` tuples. This phase iterates that list
    and calls the Stoat pin API for each entry.

    Args:
        config: Ferry configuration (stoat_url, token).
        state: Migration state; ``pins_applied`` counter will be incremented.
        exports: Parsed DCE exports (not used directly; pins come from state).
        on_event: Event callback for progress reporting.
    """
    if not state.pending_pins:
        on_event(MigrationEvent(phase="pins", status="completed", message="No pins to restore"))
        return

    total = len(state.pending_pins)
    on_event(
        MigrationEvent(
            phase="pins",
            status="started",
            message=f"Restoring {total} pinned messages",
            current=0,
            total=total,
        )
    )

    if config.dry_run:
        state.pins_applied = len(state.pending_pins)
        on_event(
            MigrationEvent(
                phase="pins",
                status="completed",
                message=f"[DRY RUN] {state.pins_applied} pins counted",
            )
        )
        return

    async with get_session(config) as session:
        for idx, (channel_id, message_id) in enumerate(state.pending_pins, start=1):
            try:
                await api_pin_message(
                    session, config.stoat_url, config.token, channel_id, message_id
                )
                state.pins_applied += 1

                on_event(
                    MigrationEvent(
                        phase="pins",
                        status="progress",
                        message=f"Pinned message {message_id} in channel {channel_id}",
                        current=idx,
                        total=total,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                state.errors.append(
                    {
                        "phase": "pins",
                        "message": (
                            f"Failed to pin message {message_id} in channel {channel_id}: {exc}"
                        ),
                    }
                )
                on_event(
                    MigrationEvent(
                        phase="pins",
                        status="error",
                        message=f"Failed to pin message {message_id}: {exc}",
                        current=idx,
                        total=total,
                    )
                )

            await asyncio.sleep(0.5)

    on_event(
        MigrationEvent(
            phase="pins",
            status="completed",
            message=f"Pins phase complete — {state.pins_applied} messages pinned",
        )
    )
