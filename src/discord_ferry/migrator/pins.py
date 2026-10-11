"""Pin preservation — Phase 10."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

from discord_ferry.core.events import MigrationEvent
from discord_ferry.core.security import safe_sanitize
from discord_ferry.errors import MigrationError
from discord_ferry.migrator.api import api_pin_message, get_session
from discord_ferry.state import save_state

if TYPE_CHECKING:
    from discord_ferry.config import FerryConfig
    from discord_ferry.core.events import EventCallback
    from discord_ferry.parser.models import DCEExport
    from discord_ferry.state import MigrationState

logger = logging.getLogger(__name__)


def _is_already_pinned_error(exc: Exception) -> bool:
    """Return whether Stoat's pin route reports its structured duplicate response."""
    if not isinstance(exc, MigrationError):
        return False

    prefix = "API error 400: "
    message = str(exc)
    if not message.startswith(prefix):
        return False

    try:
        body = json.loads(message.removeprefix(prefix))
    except json.JSONDecodeError:
        return False
    return isinstance(body, dict) and body.get("type") == "AlreadyPinned"


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
        state.pending_pins = []
        on_event(
            MigrationEvent(
                phase="pins",
                status="completed",
                message=f"[DRY RUN] {state.pins_applied} pins counted",
            )
        )
        return

    # #970: pins are replayed from state.json on a resume, so each must target a channel
    # this migration created. A pin aimed anywhere else is dropped with a warning rather
    # than sent. load_state has already checked the ids are well-formed.
    mapped_channels = set(state.channel_map.values())
    queue: list[tuple[str, str]] = []
    for channel_id, message_id in state.pending_pins:
        if channel_id in mapped_channels:
            queue.append((channel_id, message_id))
            continue
        notice = (
            f"Skipping pin of message {message_id}: channel {channel_id} is not one of the "
            "channels this migration created."
        )
        state.warnings.append({"phase": "pins", "type": "pin_unmapped_channel", "message": notice})
        on_event(MigrationEvent(phase="pins", status="warning", message=notice))
    remaining: list[tuple[str, str]] = []
    checkpoint_interval = max(config.checkpoint_interval, 1)
    last_save = time.monotonic()

    async with get_session(config) as session:
        for pos, (channel_id, message_id) in enumerate(queue, start=1):
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
                        current=pos,
                        total=total,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                if _is_already_pinned_error(exc):
                    state.pins_applied += 1
                    on_event(
                        MigrationEvent(
                            phase="pins",
                            status="progress",
                            message=(
                                f"Message {message_id} was already pinned in channel {channel_id}"
                            ),
                            current=pos,
                            total=total,
                        )
                    )
                else:
                    remaining.append((channel_id, message_id))
                    state.errors.append(
                        {
                            "phase": "pins",
                            "type": "pin_failed",
                            "message": safe_sanitize(
                                config.token_store,
                                (
                                    f"Failed to pin message {message_id} in channel "
                                    f"{channel_id}: {exc}"
                                ),
                            ),
                        }
                    )
                    on_event(
                        MigrationEvent(
                            phase="pins",
                            status="error",
                            message=f"Failed to pin message {message_id}: {exc}",
                            current=pos,
                            total=total,
                        )
                    )

            now = time.monotonic()
            if pos % checkpoint_interval == 0 and now - last_save >= 5.0:
                state.pending_pins = remaining + queue[pos:]
                save_state(state, config.output_dir)
                last_save = now

            await asyncio.sleep(0.5)

    state.pending_pins = remaining
    save_state(state, config.output_dir)

    on_event(
        MigrationEvent(
            phase="pins",
            status="completed",
            message=f"Pins phase complete: {state.pins_applied} messages pinned",
        )
    )
