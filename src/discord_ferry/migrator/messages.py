"""Message import with masquerade — Phase 8 of the migration pipeline."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from discord_ferry.core.events import MigrationEvent
from discord_ferry.migrator.api import api_send_message, get_rate_multiplier, get_session
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
    rewrite_discord_links,
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

_THREAD_STRATEGIES = frozenset({"flatten", "merge", "archive"})


def _safe_error(config: FerryConfig, text: str) -> str:
    """Sanitize an error/warning string, stripping any token values."""
    if config.token_store is not None:
        return config.token_store.sanitize(text)
    return text


logger = logging.getLogger(__name__)

_VALID_REACTION_MODES = frozenset({"text", "native", "skip"})

# ---------------------------------------------------------------------------
# Message splitting
# ---------------------------------------------------------------------------

_SPLIT_MARKER_RESERVE = 20  # chars reserved for "[continued K/N]" markers


def _split_message(content: str, max_len: int = 2000) -> list[str]:
    """Split content into chunks that fit within max_len.

    Splits at word boundaries when possible. Adds ``[continued K/N]`` markers.
    Returns a single-element list if content fits.

    Args:
        content: Message content to split (all transforms already applied).
        max_len: Maximum length per chunk (default: 2000).

    Returns:
        List of content chunks, each ≤ max_len characters.
    """
    if len(content) <= max_len:
        return [content]

    # Two-pass: first collect raw chunks, then apply markers.
    effective_max = max_len - _SPLIT_MARKER_RESERVE
    raw_chunks: list[str] = []
    remaining = content
    while remaining:
        if len(remaining) <= effective_max:
            raw_chunks.append(remaining)
            break
        # Try to split at last space within effective_max.
        cut = remaining[:effective_max]
        space_idx = cut.rfind(" ")
        if space_idx > 0:
            raw_chunks.append(remaining[:space_idx])
            remaining = remaining[space_idx + 1 :]
        else:
            # Hard split — no space found.
            raw_chunks.append(remaining[:effective_max])
            remaining = remaining[effective_max:]

    n = len(raw_chunks)
    if n == 1:
        # Shouldn't happen after the guard above, but be safe.
        return raw_chunks

    result: list[str] = []
    for k, chunk in enumerate(raw_chunks, start=1):
        if k == 1:
            result.append(chunk + f"\n[continued 1/{n}]")
        else:
            result.append(f"[continued {k}/{n}] " + chunk)
    return result


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


# ---------------------------------------------------------------------------
# ChannelResult accumulator for parallel message sends
# ---------------------------------------------------------------------------


@dataclass
class ChannelResult:
    """Per-channel accumulator — merged into main state after completion."""

    channel_id: str = ""
    warnings: list[dict[str, str]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    failed_messages: list[FailedMessage] = field(default_factory=list)
    message_map_updates: dict[str, str] = field(default_factory=dict)
    pending_pins: list[tuple[str, str]] = field(default_factory=list)
    pending_reactions: list[dict[str, object]] = field(default_factory=list)
    attachments_uploaded: int = 0
    attachments_skipped: int = 0
    referenced_autumn_ids: set[str] = field(default_factory=set)
    messages_migrated: int = 0  # S15: per-channel message count for forum index rebuild
    # S18: fidelity counters
    embeds_total: int = 0
    embeds_dropped: int = 0
    replies_linked: int = 0
    replies_total: int = 0


def _merge_channel_result(state: MigrationState, result: ChannelResult) -> None:
    """Merge a per-channel result into the shared migration state."""
    state.warnings.extend(result.warnings)
    state.errors.extend(result.errors)
    state.failed_messages.extend(result.failed_messages)
    state.message_map.update(result.message_map_updates)
    state.pending_pins.extend(result.pending_pins)
    state.pending_reactions.extend(result.pending_reactions)
    state.attachments_uploaded += result.attachments_uploaded
    state.attachments_skipped += result.attachments_skipped
    state.referenced_autumn_ids.update(result.referenced_autumn_ids)
    # S15: Accumulate per-channel message count for forum index rebuild.
    if result.channel_id:
        state.channel_message_counts[result.channel_id] = (
            state.channel_message_counts.get(result.channel_id, 0) + result.messages_migrated
        )
    # S18: Merge fidelity counters.
    state.embeds_total += result.embeds_total
    state.embeds_dropped += result.embeds_dropped
    state.replies_linked += result.replies_linked
    state.replies_total += result.replies_total


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


def _skip_attachment_to_result(
    result: ChannelResult,
    filename: str,
    reason: str,
    phase: str = "messages",
) -> str:
    """Record a skipped attachment to a ChannelResult and return placeholder text."""
    result.attachments_skipped += 1
    result.warnings.append({"phase": phase, "type": "attachment_skipped", "message": reason})
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

    Channels are processed in parallel (up to ``config.max_concurrent_channels``).
    Each channel worker accumulates results in a :class:`ChannelResult` that is
    merged into ``state`` after completion, preventing non-deterministic interleaving.

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

    # Separate thread exports from parent exports based on thread_strategy.
    thread_strategy = (
        config.thread_strategy if config.thread_strategy in _THREAD_STRATEGIES else "flatten"
    )
    thread_exports: list[DCEExport] = []

    # Pre-filter exports: skip unmapped channels, already-completed channels, etc.
    eligible_exports: list[DCEExport] = []
    for export in sorted_exports:
        if config.skip_threads and export.is_thread:
            continue

        # In merge/archive mode, separate thread exports for later processing.
        if export.is_thread and thread_strategy in ("merge", "archive"):
            thread_exports.append(export)
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

        if config.resume and export.channel.id in state.completed_channel_ids:
            continue

        eligible_exports.append(export)

    channel_sem = asyncio.Semaphore(config.max_concurrent_channels)
    save_lock = asyncio.Lock()

    async with get_session(config) as session:
        tasks: list[asyncio.Task[ChannelResult]] = []
        for export in eligible_exports:
            task = asyncio.create_task(
                _process_single_channel(
                    export=export,
                    config=config,
                    state=state,
                    session=session,
                    on_event=on_event,
                    channel_sem=channel_sem,
                    save_lock=save_lock,
                ),
                name=f"channel-{export.channel.id}",
            )
            tasks.append(task)

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for export, result in zip(eligible_exports, results, strict=True):
            if isinstance(result, BaseException):
                # Channel worker raised an unhandled exception — record as error.
                state.errors.append(
                    {
                        "phase": "messages",
                        "type": "channel_worker_failed",
                        "message": (f"Channel {export.channel.name!r} worker failed: {result}"),
                    }
                )
                on_event(
                    MigrationEvent(
                        phase="messages",
                        status="warning",
                        message=f"Channel {export.channel.name!r} failed: {result}",
                        channel_name=export.channel.name,
                    )
                )
            else:
                _merge_channel_result(state, result)
                state.completed_channel_ids.add(export.channel.id)

    # Process thread exports for merge/archive modes (after parent channels complete).
    if thread_strategy == "merge" and thread_exports:
        # Build name -> Stoat ID lookup from non-thread exports.
        parent_name_to_stoat: dict[str, str] = {}
        for exp in sorted_exports:
            if not exp.is_thread:
                stoat_id = state.channel_map.get(exp.channel.id)
                if stoat_id:
                    parent_name_to_stoat[exp.channel.name] = stoat_id
        await _merge_threads(thread_exports, config, state, on_event, parent_name_to_stoat)
    elif thread_strategy == "archive" and thread_exports:
        _archive_threads(thread_exports, config, on_event)

    on_event(
        MigrationEvent(
            phase="messages",
            status="completed",
            message="Message import complete.",
        )
    )


# ---------------------------------------------------------------------------
# Thread strategy helpers: merge & archive
# ---------------------------------------------------------------------------


async def _merge_threads(
    thread_exports: list[DCEExport],
    config: FerryConfig,
    state: MigrationState,
    on_event: EventCallback,
    parent_name_to_stoat: dict[str, str],
) -> None:
    """Merge thread messages into their parent channels with separators.

    Each thread's messages are appended to the parent channel after a
    separator line. Uses the original author masquerade for each message.

    Args:
        thread_exports: Thread exports to merge.
        config: Ferry configuration.
        state: Migration state.
        on_event: Event callback.
        parent_name_to_stoat: Mapping of parent channel name to Stoat channel ID.
    """
    async with get_session(config) as session:
        for export in thread_exports:
            parent_name = export.parent_channel_name or ""
            parent_stoat_id = parent_name_to_stoat.get(parent_name)

            if parent_stoat_id is None:
                state.warnings.append(
                    {
                        "phase": "messages",
                        "type": "merge_parent_not_found",
                        "message": (
                            f"Thread {export.channel.name!r} parent channel "
                            f"{parent_name!r} not found — skipping merge."
                        ),
                    }
                )
                continue

            # Send separator message.
            separator = (
                f"\u2500\u2500 Thread: {export.channel.name} "
                f"({export.message_count} messages) \u2500\u2500"
            )
            try:
                await api_send_message(
                    session,
                    config.stoat_url,
                    config.token,
                    parent_stoat_id,
                    content=separator,
                    masquerade={"name": "Discord Ferry"},
                    idempotency_key=f"ferry-thread-sep-{export.channel.id}",
                )
            except Exception as exc:  # noqa: BLE001
                state.warnings.append(
                    {
                        "phase": "messages",
                        "type": "merge_separator_failed",
                        "message": f"Thread separator for {export.channel.name!r} failed: {exc}",
                    }
                )

            # Send all thread messages to the parent channel.
            if export.json_path is not None:
                message_source = stream_messages(export.json_path)
            else:
                message_source = iter(sorted(export.messages, key=lambda m: m.timestamp))

            msg_count = 0
            for msg in message_source:
                if msg.type in _SKIP_TYPES:
                    continue

                content = _build_content(msg, state)
                masquerade = await _build_masquerade(msg.author, session, state, config)
                parts = _split_message(content)

                for part_idx, part_content in enumerate(parts):
                    idem_key = (
                        f"ferry-merge-{msg.id}"
                        if len(parts) == 1
                        else f"ferry-merge-{msg.id}_p{part_idx + 1}"
                    )
                    try:
                        await api_send_message(
                            session,
                            config.stoat_url,
                            config.token,
                            parent_stoat_id,
                            content=part_content,
                            masquerade=masquerade,
                            idempotency_key=idem_key,
                        )
                    except Exception as exc:  # noqa: BLE001
                        state.warnings.append(
                            {
                                "phase": "messages",
                                "type": "merge_message_failed",
                                "message": f"Merge message {msg.id} failed: {exc}",
                            }
                        )

                msg_count += 1
                await _rate_limit_with_pause(config)

            on_event(
                MigrationEvent(
                    phase="messages",
                    status="progress",
                    message=(
                        f"Merged thread {export.channel.name!r} "
                        f"({msg_count} messages) into parent channel."
                    ),
                    channel_name=export.channel.name,
                )
            )


def _archive_threads(
    thread_exports: list[DCEExport],
    config: FerryConfig,
    on_event: EventCallback,
) -> None:
    """Export thread messages as markdown files. No API calls.

    Creates ``{output_dir}/threads/{parent_channel_name}/{thread_name}.md``
    with each message formatted as a markdown heading with author and timestamp.
    """
    for export in thread_exports:
        parent_name = export.parent_channel_name or "uncategorized"
        thread_dir = config.output_dir / "threads" / parent_name
        thread_dir.mkdir(parents=True, exist_ok=True)

        md_path = thread_dir / f"{export.channel.name}.md"

        if export.json_path is not None:
            message_source = stream_messages(export.json_path)
        else:
            message_source = iter(sorted(export.messages, key=lambda m: m.timestamp))

        lines: list[str] = []
        msg_count = 0
        for msg in message_source:
            if msg.type in _SKIP_TYPES:
                continue
            # Format timestamp: extract date and time from ISO format.
            ts = msg.timestamp
            # Simple ISO parse: "2024-01-15T12:00:00+00:00" -> "2024-01-15 12:00 UTC"
            ts_display = ts.replace("T", " ")[:16] + " UTC"
            author_name = msg.author.nickname or msg.author.name
            lines.append(f"## {author_name} \u2014 {ts_display}")
            lines.append(msg.content)
            lines.append("")  # blank line between messages
            msg_count += 1

        md_path.write_text("\n".join(lines), encoding="utf-8")

        on_event(
            MigrationEvent(
                phase="messages",
                status="progress",
                message=(
                    f"Archived thread {export.channel.name!r} ({msg_count} messages) to {md_path}"
                ),
                channel_name=export.channel.name,
            )
        )


# ---------------------------------------------------------------------------
# Per-channel worker
# ---------------------------------------------------------------------------


async def _process_single_channel(
    *,
    export: DCEExport,
    config: FerryConfig,
    state: MigrationState,
    session: aiohttp.ClientSession,
    on_event: EventCallback,
    channel_sem: asyncio.Semaphore,
    save_lock: asyncio.Lock,
) -> ChannelResult:
    """Process all messages in a single channel, returning a ChannelResult.

    Reads from ``state`` (channel_map, emoji_map, avatar_cache, etc.) but writes
    accumulators (warnings, errors, counters) to its own ChannelResult.
    """
    async with channel_sem:
        stoat_channel_id = state.channel_map[export.channel.id]
        result = ChannelResult(channel_id=export.channel.id)

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
                result.warnings.append(
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

        _checkpoint_interval = max(config.checkpoint_interval, 1)
        _last_save_time = time.monotonic()

        _channel_msg_offset = state.channel_message_offsets.get(export.channel.id, "")
        for idx, msg in enumerate(message_source):
            # Cancel check inside the message loop.
            if config.cancel_event and config.cancel_event.is_set():
                break

            # Resume: skip messages already processed within this channel.
            # Compare as integers — Snowflake IDs are numeric.
            if config.resume and _channel_msg_offset and int(msg.id) <= int(_channel_msg_offset):
                continue

            await _process_message(
                msg=msg,
                stoat_channel_id=stoat_channel_id,
                config=config,
                state=state,
                session=session,
                on_event=on_event,
                channel_result=result,
                export_channel_id=export.channel.id,
            )

            # Periodic progress event and state save.
            if (idx + 1) % _checkpoint_interval == 0:
                now = time.monotonic()
                if now - _last_save_time >= 5.0:
                    async with save_lock:
                        state.channel_message_offsets[export.channel.id] = msg.id
                        # Merge partial result before saving so checkpoint includes progress.
                        _merge_channel_result(state, result)
                        save_state(state, config.output_dir)
                        # Reset result to avoid double-counting on next merge.
                        result = ChannelResult(channel_id=export.channel.id)
                    _last_save_time = now
                on_event(
                    MigrationEvent(
                        phase="messages",
                        status="progress",
                        message=(f"{export.channel.name!r}: {idx + 1}/{total} messages imported."),
                        current=idx + 1,
                        total=total,
                        channel_name=export.channel.name,
                    )
                )

            # Rate-limit courtesy delay with pause/cancel support.
            await _rate_limit_with_pause(config)

        # Channel complete — save state for crash recovery.
        async with save_lock:
            _merge_channel_result(state, result)
            state.completed_channel_ids.add(export.channel.id)
            state.channel_message_offsets.pop(export.channel.id, None)
            save_state(state, config.output_dir)
            # Return empty result since we already merged.
            result = ChannelResult(channel_id=export.channel.id)

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

        return result


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
    channel_result: ChannelResult | None = None,
    export_channel_id: str = "",
) -> None:
    """Process and send a single message.

    When *channel_result* is provided, accumulators (warnings, errors, counters)
    are written there instead of directly to *state*. Read-only lookups (channel_map,
    emoji_map, message_map, etc.) still go through *state*.

    When *channel_result* is ``None``, the function writes directly to *state*
    for backward compatibility (e.g., retry path in engine.py).

    *export_channel_id* is the Discord channel ID of the channel being processed,
    used for cross-channel reply detection.
    """
    # Choose accumulator target.
    acc_warnings: list[dict[str, str]] = (
        channel_result.warnings if channel_result is not None else state.warnings
    )
    acc_errors: list[dict[str, str]] = (
        channel_result.errors if channel_result is not None else state.errors
    )
    acc_failed: list[FailedMessage] = (
        channel_result.failed_messages if channel_result is not None else state.failed_messages
    )
    acc_pins: list[tuple[str, str]] = (
        channel_result.pending_pins if channel_result is not None else state.pending_pins
    )
    acc_reactions: list[dict[str, object]] = (
        channel_result.pending_reactions if channel_result is not None else state.pending_reactions
    )

    # Step 0: Filter by type.
    if msg.type in _SKIP_TYPES:
        return

    # ChannelPinnedMessage: mark the referenced message for re-pinning, don't send.
    if msg.type == "ChannelPinnedMessage":
        if msg.reference and msg.reference.message_id:
            # Check both the main state map and the channel result's local map.
            ref_stoat_id = state.message_map.get(msg.reference.message_id)
            if ref_stoat_id is None and channel_result is not None:
                ref_stoat_id = channel_result.message_map_updates.get(msg.reference.message_id)
            if ref_stoat_id:
                acc_pins.append((stoat_channel_id, ref_stoat_id))
            else:
                acc_warnings.append(
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
        acc_warnings.append(
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

    # Step 0b: Detect attachment overflow (Stoat limit: 5 per message).
    overflow_text = ""
    if len(msg.attachments) > 5:
        overflow = msg.attachments[5:]
        overflow_names = ", ".join(att.file_name for att in overflow)
        overflow_text = (
            f"\n[+{len(overflow)} more attachment(s) not migrated "
            f"(Stoat limit: 5): {overflow_names}]"
        )
        if channel_result is not None:
            channel_result.attachments_skipped += len(overflow)
            channel_result.warnings.append(
                {
                    "phase": "messages",
                    "type": "attachment_overflow",
                    "message": (
                        f"Message {msg.id}: {len(overflow)} attachments exceed Stoat limit of 5"
                    ),
                }
            )
        else:
            state.attachments_skipped += len(overflow)
            state.warnings.append(
                {
                    "phase": "messages",
                    "type": "attachment_overflow",
                    "message": (
                        f"Message {msg.id}: {len(overflow)} attachments exceed Stoat limit of 5"
                    ),
                }
            )

    # Step 1: Upload attachments (max 5).
    autumn_ids, attachment_placeholders = await _upload_attachments(
        msg, config, state, session, on_event, channel_result=channel_result
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
            if channel_result is not None:
                channel_result.attachments_uploaded += 1
            else:
                state.attachments_uploaded += 1
        except Exception as exc:  # noqa: BLE001
            acc_warnings.append(
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
                    if channel_result is not None:
                        channel_result.attachments_uploaded += 1
                    else:
                        state.attachments_uploaded += 1
                except Exception as exc:  # noqa: BLE001
                    acc_warnings.append(
                        {
                            "phase": "messages",
                            "type": "embed_media_failed",
                            "message": f"Embed media upload failed for msg {msg.id}: {exc}",
                        }
                    )
            stoat_embeds.append(flat)

    # Report embeds that could not be migrated (beyond cap or without title/description).
    failed_embeds = len(msg.embeds) - len(stoat_embeds)
    if failed_embeds > 0:
        content += f"\n[{failed_embeds} embed(s) could not be migrated]"

    # S18: Track embed fidelity counters.
    if channel_result is not None:
        channel_result.embeds_total += len(msg.embeds)
        channel_result.embeds_dropped += failed_embeds
    else:
        state.embeds_total += len(msg.embeds)
        state.embeds_dropped += failed_embeds

    # Step 5: Reply references.
    replies: list[dict[str, Any]] = []
    if msg.reference and msg.reference.message_id:
        # S18: Track reply fidelity counters.
        if channel_result is not None:
            channel_result.replies_total += 1
        else:
            state.replies_total += 1
        ref_stoat_id = state.message_map.get(msg.reference.message_id)
        if ref_stoat_id is None and channel_result is not None:
            ref_stoat_id = channel_result.message_map_updates.get(msg.reference.message_id)
        if ref_stoat_id:
            replies.append({"id": ref_stoat_id, "mention": False})
            if channel_result is not None:
                channel_result.replies_linked += 1
            else:
                state.replies_linked += 1
        elif msg.reference.channel_id and msg.reference.channel_id != export_channel_id:
            # Cross-channel reply — message not in map (different channel), add text fallback.
            content += f"\n[Replying to message in #{msg.reference.channel_id}]"
            warn_target: list[dict[str, str]] = (
                channel_result.warnings if channel_result is not None else state.warnings
            )
            warn_target.append(
                {
                    "phase": "messages",
                    "type": "cross_channel_reply",
                    "message": f"Cross-channel reply in msg {msg.id}",
                }
            )

    # Step 6: Empty message fallback.
    if msg.content == "" and not autumn_ids and not stoat_embeds:
        content = f"{format_original_timestamp(msg.timestamp)} [empty message]"

    # Step 6b: Append reaction text if text mode.
    _effective_mode = (
        config.reaction_mode if config.reaction_mode in _VALID_REACTION_MODES else "text"
    )
    if msg.reactions and _effective_mode == "text":
        # Budget is best-effort — overflow text may be appended after this.
        # Step 7 truncation (2000 chars) is the true safety net.
        remaining = 2000 - len(content)
        reaction_text = _build_reaction_text(msg.reactions, remaining)
        content += reaction_text

    # Step 6b2: Append reaction count annotations in native mode (counts > 1 only).
    if _effective_mode == "native" and msg.reactions:
        count_annotations = [
            f"{r.emoji.name} \u00d7{r.count}" for r in msg.reactions if r.count > 1
        ]
        if count_annotations:
            annotation = f"\n[Original counts: {', '.join(count_annotations)}]"
            remaining = 2000 - len(content)
            if remaining >= len(annotation):
                content += annotation

    # Step 6c: Append overflow text for attachments beyond the 5-file limit.
    if overflow_text:
        content += overflow_text

    # Step 7: Split content into ≤2000-char chunks (replaces hard truncation).
    parts = _split_message(content)
    if len(parts) > 1:
        acc_warnings.append(
            {
                "phase": "messages",
                "type": "message_split",
                "message": (
                    f"Message {msg.id} split into {len(parts)} parts "
                    f"(original length: {len(content)})"
                ),
            }
        )

    # Step 8: Send the message (all parts).
    stoat_msg_id: str = ""
    try:
        for part_idx, part_content in enumerate(parts):
            is_first = part_idx == 0
            idem_key = f"ferry-{msg.id}" if len(parts) == 1 else f"ferry-{msg.id}_p{part_idx + 1}"
            result = await api_send_message(
                session,
                config.stoat_url,
                config.token,
                stoat_channel_id,
                content=part_content,
                # Attachments, embeds, and replies only on the first part.
                attachments=(autumn_ids if autumn_ids and is_first else None),
                embeds=(stoat_embeds if stoat_embeds and is_first else None),
                masquerade=masquerade,
                replies=(replies if replies and is_first else None),
                idempotency_key=idem_key,
            )
            part_stoat_id: str = result["_id"]
            if is_first:
                stoat_msg_id = part_stoat_id

        if channel_result is not None:
            channel_result.message_map_updates[msg.id] = stoat_msg_id
            channel_result.referenced_autumn_ids.update(autumn_ids)
            channel_result.messages_migrated += 1  # S15: track for forum index rebuild
        else:
            state.message_map[msg.id] = stoat_msg_id
            state.referenced_autumn_ids.update(autumn_ids)
            # S15: Track per-channel message count (direct-state path, e.g. retry).
            if export_channel_id:
                state.channel_message_counts[export_channel_id] = (
                    state.channel_message_counts.get(export_channel_id, 0) + 1
                )

        if msg.is_pinned:
            acc_pins.append((stoat_channel_id, stoat_msg_id))

        # Step 8b: Queue reactions (only in native mode).
        if _effective_mode == "native":
            for reaction in msg.reactions:
                if reaction.emoji.id:  # Custom emoji.
                    stoat_emoji = state.emoji_map.get(reaction.emoji.id)
                    if stoat_emoji:
                        acc_reactions.append(
                            {
                                "channel_id": stoat_channel_id,
                                "message_id": stoat_msg_id,
                                "emoji": stoat_emoji,
                            }
                        )
                else:  # Unicode emoji.
                    acc_reactions.append(
                        {
                            "channel_id": stoat_channel_id,
                            "message_id": stoat_msg_id,
                            "emoji": reaction.emoji.name,
                        }
                    )

    except Exception as exc:  # noqa: BLE001
        safe_exc = _safe_error(config, str(exc))
        acc_errors.append(
            {
                "phase": "messages",
                "type": "message_send_failed",
                "message": f"Failed to send msg {msg.id}: {safe_exc}",
            }
        )
        acc_failed.append(
            FailedMessage(
                discord_msg_id=msg.id,
                stoat_channel_id=stoat_channel_id,
                error=safe_exc,
                content_preview=content[:50] if content else "",
            )
        )
        on_event(
            MigrationEvent(
                phase="messages",
                status="warning",
                message=f"Message {msg.id} failed: {safe_exc}",
            )
        )
        return

    # Step 9: Resume checkpoint handled in the caller's periodic save loop.


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


async def _rate_limit_with_pause(config: FerryConfig) -> None:
    """Sleep for rate limit, respecting pause/cancel flags from the GUI.

    The base delay is scaled by the adaptive rate multiplier from :mod:`api`
    so that sustained 429 pressure automatically slows message sending.
    """
    if config.cancel_event and config.cancel_event.is_set():
        raise asyncio.CancelledError("Migration cancelled by user")
    if config.pause_event:
        await config.pause_event.wait()  # blocks while event is cleared (paused)
    delay = config.message_rate_limit * get_rate_multiplier()
    await asyncio.sleep(delay)


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
    content = rewrite_discord_links(content, state.channel_map)
    content = remap_emoji(content, state.emoji_map)

    # Prepend original timestamp.
    content = f"{format_original_timestamp(msg.timestamp)} {content}"

    if msg.timestamp_edited:
        content += " *(edited)*"

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
    *,
    channel_result: ChannelResult | None = None,
) -> tuple[list[str], list[str]]:
    """Upload up to 5 message attachments to Autumn.

    Args:
        msg: The parsed Discord message.
        config: Ferry run configuration.
        state: Current migration state (upload_cache mutated in-place).
        session: Active aiohttp session.
        on_event: Callback for warning events.
        channel_result: Optional accumulator for parallel mode.

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
            if channel_result is not None:
                placeholder = _skip_attachment_to_result(channel_result, att.file_name, reason)
            else:
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
                if channel_result is not None:
                    placeholder = _skip_attachment_to_result(channel_result, att.file_name, reason)
                else:
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
                if channel_result is not None:
                    channel_result.attachments_skipped += 1
                    channel_result.warnings.append(
                        {
                            "phase": "messages",
                            "type": "missing_media",
                            "message": (
                                f"Attachment {att.id!r} ({att.file_name!r}) "
                                "not found locally — skipped."
                            ),
                        }
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
                verify_size=config.verify_uploads,
            )
            autumn_ids.append(autumn_id)
            if channel_result is not None:
                channel_result.attachments_uploaded += 1
            else:
                state.attachments_uploaded += 1
            state.autumn_uploads[autumn_id] = att.id
        except Exception as exc:  # noqa: BLE001
            if channel_result is not None:
                channel_result.attachments_skipped += 1
                channel_result.warnings.append(
                    {
                        "phase": "messages",
                        "type": "attachment_upload_failed",
                        "message": f"Attachment {att.file_name!r} upload failed: {exc}",
                    }
                )
            else:
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
    name = truncate_name(author.nickname or author.name, author_id=author.id)
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
