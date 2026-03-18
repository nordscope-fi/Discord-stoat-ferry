"""Message import with masquerade — Phase 8 of the migration pipeline."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from discord_ferry.core.events import MigrationEvent
from discord_ferry.migrator.api import api_send_message, get_session
from discord_ferry.migrator.sanitize import truncate_name
from discord_ferry.parser.dce_parser import check_cdn_url_expiry, stream_messages
from discord_ferry.parser.transforms import (
    convert_spoilers,
    flatten_embed,
    flatten_poll,
    format_original_timestamp,
    handle_stickers,
    remap_emoji,
    remap_mentions,
    strip_underline,
)
from discord_ferry.state import FailedMessage, save_state
from discord_ferry.uploader.autumn import TAG_SIZE_LIMITS, upload_with_cache

if TYPE_CHECKING:
    from pathlib import Path

    import aiohttp

    from discord_ferry.config import FerryConfig
    from discord_ferry.core.events import EventCallback
    from discord_ferry.parser.models import DCEAuthor, DCEExport, DCEMessage, DCEReaction
    from discord_ferry.state import MigrationState

logger = logging.getLogger(__name__)

_VALID_REACTION_MODES = frozenset({"text", "native", "skip"})

# Message types that should be silently skipped without even a warning.
_SKIP_TYPES = frozenset(
    {
        "RecipientAdd",
        "RecipientRemove",
        "ChannelNameChange",
        "UserPremiumGuildSubscription",
        "GuildMemberJoin",
        "ThreadCreated",
        "Call",
        "ChannelIconChange",
    }
)


def _skip_attachment(
    state: MigrationState,
    filename: str,
    reason: str,
    phase: str = "messages",
) -> str:
    """Record a skipped attachment and return placeholder text."""
    state.attachments_skipped += 1
    state.warnings.append({"phase": phase, "type": "attachment_skipped", "message": reason})
    return f"[{reason}]"


def _build_reaction_text(reactions: list[DCEReaction], max_chars: int) -> str:
    """Build a text summary of reactions within a character budget.

    Args:
        reactions: Parsed reactions with emoji name and count.
        max_chars: Maximum characters available.

    Returns:
        Formatted string like ``\\n[Reactions: thumbsup 12 · tada 5]``
        or empty string if no reactions or no budget.
    """
    if not reactions or max_chars <= 0:
        return ""
    valid = [(r.emoji.name, r.count) for r in reactions if r.count > 0]
    if not valid:
        return ""
    parts = [f"{name} {count}" for name, count in valid]
    full = "\n[Reactions: " + " · ".join(parts) + "]"
    if len(full) <= max_chars:
        return full
    # Truncate: include as many reactions as fit
    prefix = "\n[Reactions: "
    suffix = "...]"
    budget = max_chars - len(prefix) - len(suffix)
    if budget <= 0:
        return ""
    truncated: list[str] = []
    used = 0
    for part in parts:
        addition = (" · " + part) if truncated else part
        if used + len(addition) > budget:
            break
        truncated.append(part)
        used += len(addition)
    if not truncated:
        return ""
    return prefix + " · ".join(truncated) + suffix


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
    # Validate reaction_mode — fall back to "text" on unrecognised values.
    if config.reaction_mode not in _VALID_REACTION_MODES:
        state.warnings.append(
            {
                "phase": "messages",
                "type": "invalid_reaction_mode",
                "message": (
                    f"Unknown reaction_mode {config.reaction_mode!r}, falling back to 'text'"
                ),
            }
        )
        logger.warning("Unknown reaction_mode %r — falling back to 'text'", config.reaction_mode)

    # Sort deterministically by Discord channel ID.
    sorted_exports = sorted(exports, key=lambda e: e.channel.id)

    on_event(
        MigrationEvent(
            phase="messages",
            status="started",
            message=f"Starting message import for {len(sorted_exports)} channel(s).",
        )
    )

    if config.dry_run:
        for export in sorted_exports:
            stoat_ch = state.channel_map.get(export.channel.id, f"dry-ch-{export.channel.id}")
            if export.json_path is not None:
                dry_source = stream_messages(export.json_path)
            else:
                dry_source = iter(export.messages)
            for msg_obj in dry_source:
                if msg_obj.type in _SKIP_TYPES:
                    continue
                if msg_obj.type == "ChannelPinnedMessage":
                    if msg_obj.reference and msg_obj.reference.message_id:
                        ref_id = state.message_map.get(msg_obj.reference.message_id)
                        if ref_id:
                            state.pending_pins.append((stoat_ch, ref_id))
                    continue
                state.message_map[msg_obj.id] = f"dry-msg-{msg_obj.id}"
                if msg_obj.is_pinned:
                    state.pending_pins.append((stoat_ch, f"dry-msg-{msg_obj.id}"))
        total_msgs = len(state.message_map)
        on_event(
            MigrationEvent(
                phase="messages",
                status="completed",
                message=f"[DRY RUN] Mapped {total_msgs} messages",
            )
        )
        return

    _checkpoint_interval = max(config.checkpoint_interval, 1)
    _last_save_time = time.monotonic()

    async with get_session(config) as session:
        for export in sorted_exports:
            # Skip thread/forum exports when skip_threads is enabled.
            if config.skip_threads and export.is_thread:
                continue

            stoat_channel_id = state.channel_map.get(export.channel.id)
            if stoat_channel_id is None:
                state.warnings.append(
                    {
                        "phase": "messages",
                        "type": "channel_not_mapped",
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

            # Inject a system header for flattened threads/forum posts.
            if export.is_thread and export.parent_channel_name:
                if export.channel.type in (15, 16):
                    header = f"[Forum post migrated from #{export.parent_channel_name}]"
                else:
                    header = f"[Thread migrated from #{export.parent_channel_name}]"
                try:
                    await api_send_message(
                        session,
                        config.stoat_url,
                        config.token,
                        stoat_channel_id,
                        content=header,
                        masquerade={"name": "Discord Ferry"},
                        idempotency_key=f"ferry-header-{export.channel.id}",
                    )
                except Exception as exc:  # noqa: BLE001
                    state.warnings.append(
                        {
                            "phase": "messages",
                            "type": "thread_header_failed",
                            "message": (f"Thread header for {export.channel.name!r} failed: {exc}"),
                        }
                    )
                    on_event(
                        MigrationEvent(
                            phase="messages",
                            status="warning",
                            message=f"Thread header for {export.channel.name!r} failed: {exc}",
                        )
                    )

            # Stream messages from JSON file if available (low memory), else fall back to in-memory.
            if export.json_path is not None:
                message_source = stream_messages(export.json_path)
            else:
                message_source = iter(sorted(export.messages, key=lambda m: m.timestamp))
            total = export.message_count

            for idx, msg in enumerate(message_source):
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

                # Periodic progress event and state save.
                if (idx + 1) % _checkpoint_interval == 0:
                    now = time.monotonic()
                    if now - _last_save_time >= 5.0:
                        save_state(state, config.output_dir)
                        _last_save_time = now
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

            # Channel complete — save state for crash recovery.
            state.last_completed_channel = export.channel.id
            state.last_completed_message = ""
            save_state(state, config.output_dir)

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

    # ChannelPinnedMessage: mark the referenced message for re-pinning, don't send.
    if msg.type == "ChannelPinnedMessage":
        if msg.reference and msg.reference.message_id:
            ref_stoat_id = state.message_map.get(msg.reference.message_id)
            if ref_stoat_id:
                state.pending_pins.append((stoat_channel_id, ref_stoat_id))
            else:
                state.warnings.append(
                    {
                        "phase": "messages",
                        "type": "pin_reference_missing",
                        "message": (
                            f"ChannelPinnedMessage {msg.id} references unknown message "
                            f"{msg.reference.message_id}"
                        ),
                    }
                )
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
                "type": "forwarded_message",
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
    autumn_ids, attachment_placeholders = await _upload_attachments(
        msg, config, state, session, on_event
    )

    # Step 1b: Upload sticker images as additional attachments.
    _, sticker_paths = handle_stickers(msg.stickers, config.export_dir)
    for sticker_path in sticker_paths:
        if len(autumn_ids) >= 5:
            break
        try:
            sticker_id = await upload_with_cache(
                session,
                state.autumn_url,
                "attachments",
                sticker_path,
                config.token,
                state.upload_cache,
                config.upload_delay,
            )
            autumn_ids.append(sticker_id)
            state.attachments_uploaded += 1
        except Exception as exc:  # noqa: BLE001
            state.warnings.append(
                {
                    "phase": "messages",
                    "type": "sticker_upload_failed",
                    "message": f"Sticker upload failed for msg {msg.id}: {exc}",
                }
            )

    # Step 2: Build and transform content.
    content = _build_content(msg, state)

    # Append placeholders for skipped attachments (oversized, expired CDN).
    if attachment_placeholders:
        content = content + "\n" + "\n".join(attachment_placeholders)

    # Step 3: Build masquerade dict.
    masquerade = await _build_masquerade(msg.author, session, state, config)

    # Step 4: Flatten embeds (max 5, only those with title or description).
    stoat_embeds: list[dict[str, Any]] = []
    for raw_embed in msg.embeds[:5]:
        flat, embed_media_path = flatten_embed(raw_embed, config.export_dir)
        if flat.get("description") or flat.get("title"):
            # Upload embed media (thumbnail/image) if a local file is available.
            if embed_media_path is not None:
                try:
                    media_id = await upload_with_cache(
                        session,
                        state.autumn_url,
                        "attachments",
                        embed_media_path,
                        config.token,
                        state.upload_cache,
                        config.upload_delay,
                    )
                    flat["media"] = media_id
                    state.attachments_uploaded += 1
                except Exception as exc:  # noqa: BLE001
                    state.warnings.append(
                        {
                            "phase": "messages",
                            "type": "embed_media_failed",
                            "message": f"Embed media upload failed for msg {msg.id}: {exc}",
                        }
                    )
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

    # Step 6b: Append reaction text if text mode.
    _effective_mode = (
        config.reaction_mode if config.reaction_mode in _VALID_REACTION_MODES else "text"
    )
    if msg.reactions and _effective_mode == "text":
        remaining = 2000 - len(content)
        reaction_text = _build_reaction_text(msg.reactions, remaining)
        content += reaction_text

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
            idempotency_key=f"ferry-{msg.id}",
        )
        stoat_msg_id: str = result["_id"]
        state.message_map[msg.id] = stoat_msg_id
        state.referenced_autumn_ids.update(autumn_ids)

        if msg.is_pinned:
            state.pending_pins.append((stoat_channel_id, stoat_msg_id))

        # Step 8b: Queue reactions (only in native mode).
        if _effective_mode == "native":
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
        state.errors.append(
            {
                "phase": "messages",
                "type": "message_send_failed",
                "message": f"Failed to send msg {msg.id}: {exc}",
            }
        )
        state.failed_messages.append(
            FailedMessage(
                discord_msg_id=msg.id,
                stoat_channel_id=stoat_channel_id,
                error=str(exc),
                content_preview=content[:50] if content else "",
            )
        )
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

    # Append sticker representations (text only — images uploaded separately).
    sticker_text, _ = handle_stickers(msg.stickers)
    content += sticker_text

    # Append poll text if present.
    if msg.poll is not None:
        content += "\n" + flatten_poll(msg.poll)

    return content


async def _upload_attachments(
    msg: DCEMessage,
    config: FerryConfig,
    state: MigrationState,
    session: aiohttp.ClientSession,
    on_event: EventCallback,
) -> tuple[list[str], list[str]]:
    """Upload up to 5 message attachments to Autumn.

    Args:
        msg: The parsed Discord message.
        config: Ferry run configuration.
        state: Current migration state (upload_cache mutated in-place).
        session: Active aiohttp session.
        on_event: Callback for warning events.

    Returns:
        Tuple of (autumn_file_ids, placeholder_texts). Placeholders are
        generated for skipped attachments (oversized, expired CDN URLs)
        and should be appended to the message content by the caller.
    """
    autumn_ids: list[str] = []
    placeholders: list[str] = []
    for att in msg.attachments[:5]:
        # Pre-check: skip oversized files before any network call.
        limit = TAG_SIZE_LIMITS.get("attachments", 0)
        if att.file_size_bytes > 0 and limit > 0 and att.file_size_bytes > limit:
            reason = (
                f"File too large: {att.file_name} "
                f"({att.file_size_bytes / 1_048_576:.1f} MB, "
                f"limit: {limit / 1_048_576:.1f} MB)"
            )
            placeholder = _skip_attachment(state, att.file_name, reason)
            placeholders.append(placeholder)
            on_event(
                MigrationEvent(
                    phase="messages",
                    status="warning",
                    message=f"Attachment {att.file_name!r} too large — skipped.",
                )
            )
            continue

        local_path = _resolve_attachment_path(config.export_dir, att.url)
        if local_path is None or not local_path.exists() or not local_path.is_file():
            if check_cdn_url_expiry(att.url) is True:
                reason = f"Attachment expired: {att.file_name}"
                placeholder = _skip_attachment(state, att.file_name, reason)
                placeholders.append(placeholder)
                on_event(
                    MigrationEvent(
                        phase="messages",
                        status="warning",
                        message=f"Attachment {att.file_name!r} expired — skipped.",
                    )
                )
            else:
                state.attachments_skipped += 1
                state.warnings.append(
                    {
                        "phase": "messages",
                        "type": "missing_media",
                        "message": (
                            f"Attachment {att.id!r} ({att.file_name!r}) "
                            "not found locally — skipped."
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
            state.attachments_uploaded += 1
            state.autumn_uploads[autumn_id] = att.id
        except Exception as exc:  # noqa: BLE001
            state.attachments_skipped += 1
            state.warnings.append(
                {
                    "phase": "messages",
                    "type": "attachment_upload_failed",
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

    return autumn_ids, placeholders


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
    name = truncate_name(author.nickname or author.name)
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
