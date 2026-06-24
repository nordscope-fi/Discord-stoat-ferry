"""Reaction migration — Phase 9."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from discord_ferry.core.events import MigrationEvent
from discord_ferry.core.security import safe_sanitize
from discord_ferry.migrator.api import api_add_reaction, get_session
from discord_ferry.state import save_state

if TYPE_CHECKING:
    from discord_ferry.config import FerryConfig
    from discord_ferry.core.events import EventCallback
    from discord_ferry.parser.models import DCEExport
    from discord_ferry.state import MigrationState

logger = logging.getLogger(__name__)

# Stoat hard limit: 20 reactions per message.
_MAX_REACTIONS_PER_MESSAGE = 20


async def run_reactions(
    config: FerryConfig,
    state: MigrationState,
    exports: list[DCEExport],
    on_event: EventCallback,
) -> None:
    """Phase 9 — Add reactions collected during message migration back to Stoat messages.

    Reactions are logged in ``state.pending_reactions`` by the messages phase as dicts with
    keys ``channel_id``, ``message_id``, and ``emoji``. Each refers to Stoat IDs, not Discord
    IDs. No user attribution is possible — the Ferry account adds all reactions.

    Args:
        config: Ferry configuration (stoat_url, token).
        state: Migration state; ``reactions_applied`` counter will be incremented.
        exports: Parsed DCE exports (not used directly; reactions come from state).
        on_event: Event callback for progress reporting.
    """
    if not state.pending_reactions:
        on_event(
            MigrationEvent(phase="reactions", status="completed", message="No reactions to apply")
        )
        return

    total = len(state.pending_reactions)
    on_event(
        MigrationEvent(
            phase="reactions",
            status="started",
            message=f"Applying {total} reactions",
            current=0,
            total=total,
        )
    )

    if config.dry_run:
        state.reactions_applied = total
        state.reaction_message_counts = {}
        state.pending_reactions = []
        on_event(
            MigrationEvent(
                phase="reactions",
                status="completed",
                message=f"[DRY RUN] {state.reactions_applied} reactions counted",
            )
        )
        return

    # Resume-safe: seed per-message cap counts from persisted state.
    per_message_counts: dict[str, int] = dict(state.reaction_message_counts)
    queue = list(state.pending_reactions)  # snapshot — we reassign state.pending_reactions below
    remaining: list[dict[str, object]] = []
    checkpoint_interval = max(config.checkpoint_interval, 1)
    last_save = time.monotonic()

    async with get_session(config) as session:
        for pos, entry in enumerate(queue, start=1):
            channel_id = str(entry["channel_id"])
            message_id = str(entry["message_id"])
            emoji = str(entry["emoji"])

            current_count = per_message_counts.get(message_id, 0)
            if current_count >= _MAX_REACTIONS_PER_MESSAGE:
                on_event(
                    MigrationEvent(
                        phase="reactions",
                        status="warning",
                        message=(
                            f"Skipping reaction on message {message_id} "
                            f"— already at {_MAX_REACTIONS_PER_MESSAGE} reactions"
                        ),
                        current=pos,
                        total=total,
                    )
                )
                continue  # cap-skipped: consumed (not retried, not retained)

            try:
                await api_add_reaction(
                    session, config.stoat_url, config.token, channel_id, message_id, emoji
                )
                state.reactions_applied += 1
                per_message_counts[message_id] = current_count + 1
                on_event(
                    MigrationEvent(
                        phase="reactions",
                        status="progress",
                        message=f"Added reaction {emoji} to message {message_id}",
                        current=pos,
                        total=total,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                remaining.append(entry)  # retained for resume
                state.errors.append(
                    {
                        "phase": "reactions",
                        "type": "reaction_add_failed",
                        "message": safe_sanitize(
                            config.token_store,
                            f"Failed to add reaction {emoji} to message {message_id}: {exc}",
                        ),
                    }
                )
                on_event(
                    MigrationEvent(
                        phase="reactions",
                        status="error",
                        message=f"Failed to add reaction {emoji} to message {message_id}: {exc}",
                        current=pos,
                        total=total,
                    )
                )

            # Periodic checkpoint: persist counter + remaining (incl. unprocessed tail)
            # + per-message counts together in ONE save_state — never a subset.
            now = time.monotonic()
            if pos % checkpoint_interval == 0 and now - last_save >= 5.0:
                state.pending_reactions = remaining + queue[pos:]
                state.reaction_message_counts = per_message_counts
                save_state(state, config.output_dir)
                last_save = now

            await asyncio.sleep(0.5)

    # Final settle: only un-applied (failed) entries remain.
    state.pending_reactions = remaining
    state.reaction_message_counts = per_message_counts
    save_state(state, config.output_dir)

    on_event(
        MigrationEvent(
            phase="reactions",
            status="completed",
            message=f"Reactions phase complete — {state.reactions_applied} applied",
        )
    )
