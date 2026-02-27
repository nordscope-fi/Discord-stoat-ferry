"""Message import with masquerade — Phase 8 of the migration pipeline."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import aiohttp

from discord_ferry.core.events import MigrationEvent
from discord_ferry.migrator.api import api_send_message
from discord_ferry.parser.transforms import (
    convert_spoilers,
    flatten_embed,
    format_original_timestamp,
    handle_stickers,
    remap_emoji,
    remap_mentions,
    strip_underline,
)
from discord_ferry.uploader.autumn import upload_with_cache

if TYPE_CHECKING:
    from pathlib import Path

    from discord_ferry.config import FerryConfig
    from discord_ferry.core.events import EventCallback
    from discord_ferry.parser.models import DCEAuthor, DCEExport, DCEMessage
    from discord_ferry.state import MigrationState

# Message types that should be silently skipped without even a warning.
_SKIP_TYPES = frozenset(
    {"RecipientAdd", "RecipientRemove", "ChannelNameChange", "UserPremiumGuildSubscription"}
)

# Emit a progress event every this many messages.
_PROGRESS_EVERY = 50


# ---------------------------------------------------------------------------
# Public phase entry point
# ---------------------------------------------------------------------------


async def run_messages(
    config: FerryConfig,
    state: MigrationState,
    exports: list[DCEExport],
    on_event: EventCallback,
) -> None:
    """Import messages oldest-first, per channel, with masquerade and resume support.

    Args:
        config: Ferry run configuration.
        state: Current migration state (mutated in-place).
        exports: Parsed DCE export files (one per channel/thread).
        on_event: Callback for progress events.
    """
    # Sort deterministically by Discord channel ID.
    sorted_exports = sorted(exports, key=lambda e: e.channel.id)

    on_event(
        MigrationEvent(
            phase="messages",
            status="started",
            message=f"Starting message import for {len(sorted_exports)} channel(s).",
        )
    )

    async with aiohttp.ClientSession() as session:
        for export in sorted_exports:
            stoat_channel_id = state.channel_map.get(export.channel.id)
            if stoat_channel_id is None:
                state.warnings.append(
                    {
                        "phase": "messages",
                        "message": (
                            f"Channel {export.channel.id} ({export.channel.name!r}) "
                            "not found in channel_map — skipping."
                        ),
                    }
                )
                on_event(
                    MigrationEvent(
                        phase="messages",
                        status="skipped",
                        message=(f"Skipping channel {export.channel.name!r} (not in channel map)."),
                        channel_name=export.channel.name,
                    )
                )
                continue

            # Resume: skip channels that were fully completed in a previous run.
            # Compare as integers — Snowflake IDs are numeric and string comparison
            # doesn't preserve numeric order ("9" > "100" as strings).
            if (
                config.resume
                and state.last_completed_channel
                and int(export.channel.id) < int(state.last_completed_channel)
            ):
                continue
            # The channel whose ID equals last_completed_channel is the one
            # we partially processed; we will process it but skip already-done messages.

            on_event(
                MigrationEvent(
                    phase="messages",
                    status="progress",
                    message=f"Importing {export.channel.name!r}...",
                    channel_name=export.channel.name,
                )
            )

            # Sort messages oldest-first (ISO 8601 timestamps sort lexicographically).
            sorted_messages = sorted(export.messages, key=lambda m: m.timestamp)
            total = len(sorted_messages)

            for idx, msg in enumerate(sorted_messages):
                # Resume: skip messages already processed within the resume channel.
                # Compare as integers — Snowflake IDs are numeric.
                if (
                    config.resume
                    and state.last_completed_channel == export.channel.id
                    and state.last_completed_message
                    and int(msg.id) <= int(state.last_completed_message)
                ):
                    continue

                await _process_message(
                    msg=msg,
                    stoat_channel_id=stoat_channel_id,
                    config=config,
                    state=state,
                    session=session,
                    on_event=on_event,
                )

                # Periodic progress event.
                if (idx + 1) % _PROGRESS_EVERY == 0:
                    on_event(
                        MigrationEvent(
                            phase="messages",
                            status="progress",
                            message=(
                                f"{export.channel.name!r}: {idx + 1}/{total} messages imported."
                            ),
                            current=idx + 1,
                            total=total,
                            channel_name=export.channel.name,
                        )
                    )

                # Rate-limit courtesy delay with pause/cancel support.
                await _rate_limit_with_pause(config)

            # Channel complete.
            state.last_completed_channel = export.channel.id
            state.last_completed_message = ""

            on_event(
                MigrationEvent(
                    phase="messages",
                    status="progress",
                    message=f"Completed {export.channel.name!r} ({total} messages).",
                    current=total,
                    total=total,
                    channel_name=export.channel.name,
                )
            )

    on_event(
        MigrationEvent(
            phase="messages",
            status="completed",
            message="Message import complete.",
        )
    )


# ---------------------------------------------------------------------------
# Per-message processing
# ---------------------------------------------------------------------------


async def _process_message(
    *,
    msg: DCEMessage,
    stoat_channel_id: str,
    config: FerryConfig,
    state: MigrationState,
    session: aiohttp.ClientSession,
    on_event: EventCallback,
) -> None:
    """Process and send a single message.  Mutates *state* on success."""
    # Step 0: Filter by type.
    if msg.type in _SKIP_TYPES:
        return

    # Forwarded message detection:
    # empty content + no attachments + non-null reference + type "Default"
    if (
        msg.content == ""
        and len(msg.attachments) == 0
        and msg.reference is not None
        and msg.type == "Default"
    ):
        state.warnings.append(
            {
                "phase": "messages",
                "message": f"Forwarded message {msg.id} skipped (DCE limitation).",
            }
        )
        on_event(
            MigrationEvent(
                phase="messages",
                status="warning",
                message=f"Forwarded message {msg.id} skipped.",
            )
        )
        return

    # Step 1: Upload attachments (max 5).
    autumn_ids = await _upload_attachments(msg, config, state, session, on_event)

    # Step 2: Build and transform content.
    content = _build_content(msg, state)

    # Step 3: Build masquerade dict.
    masquerade = await _build_masquerade(msg.author, session, state, config)

    # Step 4: Flatten embeds (max 5, only those with title or description).
    stoat_embeds: list[dict[str, Any]] = []
    for raw_embed in msg.embeds[:5]:
        flat = flatten_embed(raw_embed)
        if flat.get("description") or flat.get("title"):
            stoat_embeds.append(flat)

    # Step 5: Reply references.
    replies: list[dict[str, Any]] = []
    if msg.reference and msg.reference.message_id:
        ref_stoat_id = state.message_map.get(msg.reference.message_id)
        if ref_stoat_id:
            replies.append({"id": ref_stoat_id, "mention": False})

    # Step 6: Empty message fallback.
    if msg.content == "" and not autumn_ids and not stoat_embeds:
        content = f"{format_original_timestamp(msg.timestamp)} [empty message]"

    # Step 7: Truncate to 2000 characters.
    if len(content) > 2000:
        content = content[:1997] + "..."

    # Step 8: Send the message.
    try:
        result = await api_send_message(
            session,
            config.stoat_url,
            config.token,
            stoat_channel_id,
            content=content,
            attachments=autumn_ids if autumn_ids else None,
            embeds=stoat_embeds if stoat_embeds else None,
            masquerade=masquerade,
            replies=replies if replies else None,
            nonce=f"ferry-{msg.id}",
        )
        stoat_msg_id: str = result["_id"]
        state.message_map[msg.id] = stoat_msg_id

        if msg.is_pinned:
            state.pending_pins.append((stoat_channel_id, stoat_msg_id))

        # Step 8b: Queue reactions.
        for reaction in msg.reactions:
            if reaction.emoji.id:  # Custom emoji.
                stoat_emoji = state.emoji_map.get(reaction.emoji.id)
                if stoat_emoji:
                    state.pending_reactions.append(
                        {
                            "channel_id": stoat_channel_id,
                            "message_id": stoat_msg_id,
                            "emoji": stoat_emoji,
                        }
                    )
            else:  # Unicode emoji.
                state.pending_reactions.append(
                    {
                        "channel_id": stoat_channel_id,
                        "message_id": stoat_msg_id,
                        "emoji": reaction.emoji.name,
                    }
                )

    except Exception as exc:  # noqa: BLE001
        state.errors.append({"phase": "messages", "message": f"Failed to send msg {msg.id}: {exc}"})
        on_event(
            MigrationEvent(
                phase="messages",
                status="warning",
                message=f"Message {msg.id} failed: {exc}",
            )
        )
        return

    # Step 9: Resume checkpoint (updated after successful send).
    state.last_completed_message = msg.id


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


async def _rate_limit_with_pause(config: FerryConfig) -> None:
    """Sleep for rate limit, respecting pause/cancel flags from the GUI."""
    if config.cancel_event and config.cancel_event.is_set():
        raise asyncio.CancelledError("Migration cancelled by user")
    if config.pause_event:
        await config.pause_event.wait()  # blocks while event is cleared (paused)
    await asyncio.sleep(config.message_rate_limit)


def _resolve_attachment_path(export_dir: Path, url: str) -> Path | None:
    """Resolve an attachment URL to a local path, returning None for remote URLs.

    Args:
        export_dir: Root directory of the DCE export.
        url: Attachment URL from the DCE export (may be a relative local path or an http URL).

    Returns:
        Absolute local Path if the URL is relative, or None for http/https URLs.
    """
    if url.startswith(("http://", "https://")):
        return None
    return export_dir / url


def _build_content(msg: DCEMessage, state: MigrationState) -> str:
    """Apply all content transforms in the required order.

    Args:
        msg: The parsed Discord message.
        state: Current migration state (for ID maps).

    Returns:
        Transformed content string (not yet truncated).
    """
    content = msg.content

    # Transforms applied in order.
    content = convert_spoilers(content)
    content = strip_underline(content)
    content = remap_mentions(content, state.channel_map, state.role_map, state.author_names)
    content = remap_emoji(content, state.emoji_map)

    # Prepend original timestamp.
    content = f"{format_original_timestamp(msg.timestamp)} {content}"

    # Append sticker representations.
    content += handle_stickers(msg.stickers)

    return content


async def _upload_attachments(
    msg: DCEMessage,
    config: FerryConfig,
    state: MigrationState,
    session: aiohttp.ClientSession,
    on_event: EventCallback,
) -> list[str]:
    """Upload up to 5 message attachments to Autumn.

    Args:
        msg: The parsed Discord message.
        config: Ferry run configuration.
        state: Current migration state (upload_cache mutated in-place).
        session: Active aiohttp session.
        on_event: Callback for warning events.

    Returns:
        List of Autumn file IDs for successfully uploaded attachments.
    """
    autumn_ids: list[str] = []
    for att in msg.attachments[:5]:
        local_path = _resolve_attachment_path(config.export_dir, att.url)
        if local_path is None or not local_path.exists():
            state.attachments_skipped += 1
            state.warnings.append(
                {
                    "phase": "messages",
                    "message": (
                        f"Attachment {att.id!r} ({att.file_name!r}) not found locally — skipped."
                    ),
                }
            )
            on_event(
                MigrationEvent(
                    phase="messages",
                    status="warning",
                    message=f"Attachment {att.file_name!r} not found — skipped.",
                )
            )
            continue

        try:
            autumn_id = await upload_with_cache(
                session,
                state.autumn_url,
                "attachments",
                local_path,
                config.token,
                state.upload_cache,
                config.upload_delay,
            )
            autumn_ids.append(autumn_id)
        except Exception as exc:  # noqa: BLE001
            state.attachments_skipped += 1
            state.warnings.append(
                {
                    "phase": "messages",
                    "message": f"Attachment {att.file_name!r} upload failed: {exc}",
                }
            )
            on_event(
                MigrationEvent(
                    phase="messages",
                    status="warning",
                    message=f"Attachment {att.file_name!r} upload failed: {exc}",
                )
            )

    return autumn_ids


async def _build_masquerade(
    author: DCEAuthor,
    session: aiohttp.ClientSession,
    state: MigrationState,
    config: FerryConfig,
) -> dict[str, str | None]:
    """Build a Stoat masquerade dict for a message author.

    Uploads the author's avatar to Autumn if not already cached.  Avatar upload
    failures are non-fatal.

    Args:
        author: Parsed Discord author.
        session: Active aiohttp session.
        state: Current migration state (avatar_cache and upload_cache mutated in-place).
        config: Ferry run configuration.

    Returns:
        Masquerade dict with ``name``, ``avatar`` (URL or None), and ``colour`` (or None).
    """
    name = author.nickname or author.name
    avatar_url: str | None = None

    if author.id in state.avatar_cache:
        avatar_url = f"{state.autumn_url}/avatars/{state.avatar_cache[author.id]}"
    elif author.avatar_url and not author.avatar_url.startswith(("http://", "https://")):
        local = config.export_dir / author.avatar_url
        if local.exists():
            try:
                file_id = await upload_with_cache(
                    session,
                    state.autumn_url,
                    "avatars",
                    local,
                    config.token,
                    state.upload_cache,
                    config.upload_delay,
                )
                state.avatar_cache[author.id] = file_id
                avatar_url = f"{state.autumn_url}/avatars/{file_id}"
            except Exception:  # noqa: BLE001
                pass  # Avatar upload failure is non-fatal.

    colour: str | None = author.color if author.color else None

    # Filter out None values — Stoat API may reject null fields in masquerade.
    result: dict[str, str | None] = {"name": name}
    if avatar_url is not None:
        result["avatar"] = avatar_url
    if colour is not None:
        result["colour"] = colour
    return result
