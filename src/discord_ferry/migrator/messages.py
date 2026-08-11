"""Message import with masquerade — Phase 8 of the migration pipeline."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from discord_ferry.core.events import MigrationEvent
from discord_ferry.core.security import safe_sanitize
from discord_ferry.errors import DuplicateSendError
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


logger = logging.getLogger(__name__)

_VALID_REACTION_MODES = frozenset({"text", "native", "skip"})

# Edited-message marker appended by _build_content; shared with the empty-message
# guard so the two stay byte-identical. Changing this changes migrated content.
_EDITED_MARKER = " *(edited)*"
# Stoat has no native forward, so a recovered forward is marked inline. Kept on its own
# line so it survives the 2000-char split and reads correctly when a forward also
# carries a comment of its own.
_FORWARD_MARKER = "[forwarded]"

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
    reactions_dropped: int = 0  # Batch 4 (S1): unmapped-emoji reactions silently dropped


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
    state.reactions_dropped += result.reactions_dropped


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

    # Clamp ≥1: Semaphore(0) never admits a worker (silent deadlock) and a
    # negative value raises ValueError. Shell inputs are validated, but a stale
    # GUI storage value or direct FerryConfig construction can bypass them.
    channel_sem = asyncio.Semaphore(max(config.max_concurrent_channels, 1))
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

        # Batch 3 (S2/S3): re-classify the gather outcome. Workers self-checkpoint
        # (completed_channel_ids + merge under save_lock) before returning, so successes are
        # already persisted; this loop merges defensively (idempotent — the worker resets its
        # result to empty after its own merge) then re-raises so cancel/crash PROPAGATE
        # instead of being swallowed as warnings.
        cancelled = False
        first_exc: BaseException | None = None
        for export, result in zip(eligible_exports, results, strict=True):
            # CancelledError is a BaseException (NOT Exception) — check it FIRST so a user
            # cancel never lands in the generic-exception branch (cancel wins — see below).
            if isinstance(result, asyncio.CancelledError):
                cancelled = True
                continue
            if isinstance(result, BaseException):
                # Channel worker raised an unhandled exception — record (token-safe: sanitise
                # once, use for BOTH the persisted error and the emitted event).
                safe_res = safe_sanitize(config.token_store, str(result))
                state.errors.append(
                    {
                        "phase": "messages",
                        "type": "channel_worker_failed",
                        "message": f"Channel {export.channel.name!r} worker failed: {safe_res}",
                    }
                )
                on_event(
                    MigrationEvent(
                        phase="messages",
                        status="warning",
                        message=f"Channel {export.channel.name!r} failed: {safe_res}",
                        channel_name=export.channel.name,
                    )
                )
                if first_exc is None:
                    first_exc = result
                continue
            # Success — merge + mark complete (idempotent with the worker's own self-merge +
            # result-reset; kept defensively, NOT load-bearing).
            _merge_channel_result(state, result)
            state.completed_channel_ids.add(export.channel.id)

        # Cancel takes precedence over a concurrent crash: a user cancel must reach the
        # engine's clean-cancel handler, not phase_failed.
        if cancelled or (config.cancel_event and config.cancel_event.is_set()):
            raise asyncio.CancelledError("Migration cancelled by user")
        # B1 abort-and-resume (S3): a pre-loop worker crash fails the phase so current_phase
        # stays "messages" and --resume re-runs it (completed channels skip). first_exc may be
        # a non-Exception BaseException (e.g. SystemExit) — re-raise as-is rather than filter
        # to Exception, which would silently drop it.
        if first_exc is not None:
            raise first_exc

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

            # These two markers are the ONLY thing preventing a re-run from
            # duplicating this thread into the parent channel. The Idempotency-Key
            # header does not help: Stoat's store is a 1000-entry in-memory LRU with
            # no TTL, cleared on restart, and a hit returns 409 rather than the
            # original message. And unlike flatten, the merge path never writes
            # `message_map`, so there is no second line of defence here.
            #
            # Batch 6 (#107): mirrors the flatten read at :808-826.
            # --resume reads the TRANSIENT per-thread offset, which is where a crashed
            # run stopped inside this thread and is cleared once the thread completes.
            # --incremental reads the DURABLE high-water mark, taking the transient
            # offset too in case a crash left it ahead. Plain runs skip nothing.
            if config.resume:
                _thread_skip_below = state.channel_message_offsets.get(export.channel.id, "")
            elif config.incremental:
                _thread_skip_below = max(
                    (
                        v
                        for v in (
                            state.channel_high_water.get(export.channel.id, ""),
                            state.channel_message_offsets.get(export.channel.id, ""),
                        )
                        if v and v.isdigit()
                    ),
                    key=int,
                    default="",
                )
            else:
                _thread_skip_below = ""
            # #77 parity: a non-numeric marker (only reachable via a hand-edited
            # state.json) degrades to "no threshold" rather than crashing int().
            if _thread_skip_below and not _thread_skip_below.isdigit():
                _thread_skip_below = ""
            # Batch 7 S1 (#76 self-heal ported): ids that failed to POST on a prior run
            # sit below the high-water mark; on incremental, exclude them from the skip so
            # the merge re-POSTs them. Empty unless incremental, so --resume is unaffected.
            # Scoped to parent_stoat_id (mirrors the parallel path's stoat_channel_id filter).
            #
            # Batch 6 (#107) note: `_posted` advances on a FAILED message as well as a sent
            # one, so the checkpoint below can persist the id of a message that never
            # landed, and --resume now skips past it. That matches flatten exactly and is
            # the contract SC-7 pins (`test_resume_does_not_reattempt_failed_id`): resume
            # is a pure continuation. The message is not lost -- it stays in
            # `state.failed_messages`, is reported as a failure, and an --incremental run
            # self-heals it. See `test_resume_keeps_a_failed_merge_message_recoverable`.
            _failed_ids_here = (
                {
                    fm.discord_msg_id
                    for fm in state.failed_messages
                    if fm.stoat_channel_id == parent_stoat_id
                }
                if config.incremental
                else set()
            )
            _succeeded_ids: set[str] = set()
            _thread_max_id = 0
            _posted = 0
            _checkpoint_interval = max(config.checkpoint_interval, 1)
            _last_save_time = time.monotonic()

            # Send separator message. Skipped when a prior run already posted it:
            # on incremental that is signalled by the durable marker, and on resume
            # by the transient offset a crashed run left behind (batch 6, #107) --
            # otherwise every crash would add another separator to the parent.
            _already_started = export.channel.id in state.channel_high_water or (
                export.channel.id in state.channel_message_offsets
            )
            if not ((config.incremental or config.resume) and _already_started):
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
                except DuplicateSendError:
                    # Already on the server. The return value is discarded here, so a
                    # duplicate is indistinguishable from a success.
                    pass
                except Exception as exc:  # noqa: BLE001
                    safe_exc = safe_sanitize(config.token_store, str(exc))
                    state.warnings.append(
                        {
                            "phase": "messages",
                            "type": "merge_separator_failed",
                            "message": (
                                f"Thread separator for {export.channel.name!r} failed: {safe_exc}"
                            ),
                        }
                    )

            # Send all thread messages to the parent channel.
            if export.json_path is not None:
                message_source = stream_messages(export.json_path)
            else:
                message_source = iter(sorted(export.messages, key=lambda m: m.timestamp))

            for msg in message_source:
                # Track the thread's max id over ALL messages (incl. skip-types),
                # ABOVE the _SKIP_TYPES continue, so the marker == max(all ids)
                # across runs (mirrors flatten _channel_max_id). isdigit-guarded.
                _mid = int(msg.id) if msg.id.isdigit() else None
                if _mid is not None and _mid > _thread_max_id:
                    _thread_max_id = _mid
                if msg.type in _SKIP_TYPES:
                    continue
                # Incremental: skip messages already copied on a prior run, UNLESS this id
                # failed on a prior run (#76 self-heal) — then re-attempt it.
                _would_skip = (
                    bool(_thread_skip_below)
                    and _mid is not None
                    and _mid <= int(_thread_skip_below)
                )
                if _would_skip and msg.id not in _failed_ids_here:
                    continue

                # This path never enters _process_message, so the forwarded payload has
                # to be promoted here too -- otherwise a forward inside a merged thread
                # is sent as an empty message with no warning.
                content = _build_content(_merge_forwarded(msg), state)
                masquerade = await _build_masquerade(msg.author, session, state, config)
                parts = _split_message(content)

                _msg_failed = False
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
                    except DuplicateSendError:
                        # Already on the server. _msg_failed deliberately stays False so
                        # the message still reaches _succeeded_ids and drives
                        # reconciliation. Recording a FailedMessage here is the defect
                        # this batch fixes: --incremental re-attempts previously-failed
                        # ids, the idempotency LRU has evicted the key by then, and the
                        # re-send succeeds, leaving a real duplicate in the channel.
                        continue
                    except Exception as exc:  # noqa: BLE001
                        safe_exc = safe_sanitize(config.token_store, str(exc))
                        state.warnings.append(
                            {
                                "phase": "messages",
                                "type": "merge_message_failed",
                                "message": f"Merge message {msg.id} failed: {safe_exc}",
                            }
                        )
                        # Batch 7 S1: record the failure ONCE per message (a multi-part
                        # message strands on the first failing part) so it lands in
                        # failed_messages — recoverable via incremental re-attempt / retry.
                        if not _msg_failed:
                            _msg_failed = True
                            state.failed_messages.append(
                                FailedMessage(
                                    discord_msg_id=msg.id,
                                    stoat_channel_id=parent_stoat_id,
                                    error=safe_exc,
                                    content_preview=content[:50] if content else "",
                                )
                            )

                # A message that POSTed all parts cleanly drives reconciliation (drop on
                # success); a failed one is left in failed_messages for the next run.
                if not _msg_failed:
                    _succeeded_ids.add(msg.id)
                _posted += 1

                # Batch 6 (#107): checkpoint inside the thread. Without this a crash
                # loses the record of everything sent since the phase began, and the
                # re-run re-delivers it -- the merge path has no message_map to fall
                # back on. Interval AND a 5s floor, mirroring :885-895.
                #
                # No save_lock: _merge_threads is awaited at :446, strictly after the
                # parallel gather has finished and been reconciled, so nothing else
                # touches `state` here. Do NOT add one "for symmetry".
                if _posted % _checkpoint_interval == 0:
                    now = time.monotonic()
                    if now - _last_save_time >= 5.0:
                        # #77 parity: only persist numeric ids, so a non-numeric id can
                        # never poison the offset the resume path reads back.
                        if msg.id.isdigit():
                            state.channel_message_offsets[export.channel.id] = msg.id
                        save_state(state, config.output_dir)
                        _last_save_time = now

                await _rate_limit_with_pause(config)

            # Durable high-water mark for this thread (written every run so a later
            # --incremental run skips already-copied messages). Overwrite, like flatten.
            if _thread_max_id:
                state.channel_high_water[export.channel.id] = str(_thread_max_id)

            # Batch 7 S1: reconcile this parent's previously-failed ids (mirrors the
            # parallel path :849-867). Drop any that succeeded this run (in _succeeded_ids
            # — the merge path never writes message_map), and collapse the carried +
            # fresh-re-fail duplicate to one entry. Scoped to parent_stoat_id + the ids
            # carried into this thread, so sibling threads' entries are untouched.
            #
            # Batch 6 (#107): this gate MUST match the one building _failed_ids_here
            # above. Re-attempting under a mode that does not reconcile would leave a
            # succeeded message still recorded as failed — an inaccurate durable
            # record, and a duplicate send the next time it is re-attempted.
            if config.incremental and _failed_ids_here:
                _seen: set[str] = set()
                _reconciled: list[FailedMessage] = []
                for fm in state.failed_messages:
                    if (
                        fm.stoat_channel_id == parent_stoat_id
                        and fm.discord_msg_id in _failed_ids_here
                    ):
                        if fm.discord_msg_id in _succeeded_ids:
                            continue  # succeeded this run -> drop
                        if fm.discord_msg_id in _seen:
                            continue  # carried + re-fail dup -> collapse
                        _seen.add(fm.discord_msg_id)
                    _reconciled.append(fm)
                state.failed_messages = _reconciled

            # Thread complete — the durable marker above now covers everything sent,
            # so the transient offset has nothing left to say. Clearing it is what
            # makes the separator gate distinguish "mid-flight" from "finished".
            state.channel_message_offsets.pop(export.channel.id, None)
            save_state(state, config.output_dir)

            on_event(
                MigrationEvent(
                    phase="messages",
                    status="progress",
                    message=(
                        f"Merged thread {export.channel.name!r} "
                        f"({_posted} messages) into parent channel."
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
            # A forward carries its text in the forwarded block, not in `content`, so
            # archiving it raw would write an empty entry.
            lines.append(_merge_forwarded(msg).content)
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
        # Gate on a missing durable marker so an unchanged (previously-completed)
        # thread/forum channel makes zero POSTs on an incremental run.
        if (
            export.is_thread
            and export.parent_channel_name
            and export.channel.id not in state.channel_high_water
        ):
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
            except DuplicateSendError:
                # Already on the server. The return value is discarded here.
                pass
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

        # Per-channel skip threshold: messages with id <= threshold are already copied.
        # Resume -> transient within-run offset. Incremental -> durable high-water mark
        # (or a carried transient offset from a crashed prior run, whichever is higher).
        # Plain runs skip nothing.
        if config.resume:
            _skip_below = state.channel_message_offsets.get(export.channel.id, "")
        elif config.incremental:
            _skip_below = max(
                (
                    v
                    for v in (
                        state.channel_high_water.get(export.channel.id, ""),
                        state.channel_message_offsets.get(export.channel.id, ""),
                    )
                    if v and v.isdigit()
                ),
                key=int,
                default="",
            )
        else:
            _skip_below = ""
        # #77: a checkpoint can persist a non-numeric raw msg.id as the transient
        # offset. Normalize so the single loop comparison int(_skip_below) is always
        # safe for BOTH resume and incremental — degrade to "no threshold", never crash.
        if _skip_below and not _skip_below.isdigit():
            _skip_below = ""
        # #76: ids that failed to POST on a prior run sit below the high-water mark;
        # on incremental, exclude them from the skip so the phase re-POSTs them
        # (self-heal). Empty unless incremental, so --resume is unaffected.
        #
        # Deliberate, and pinned by SC-7 (`test_resume_does_not_reattempt_failed_id`):
        # --resume is a pure continuation, so it does not re-send anything it has a
        # marker for -- including a failure sitting below that marker. The message is
        # not lost: it stays in `state.failed_messages` and is reported as a failure,
        # and an --incremental run self-heals it. Re-sending under resume would risk
        # duplicating a message that actually landed before the response was lost,
        # which is a trade-off only the opt-in delta mode makes.
        if config.incremental:
            _failed_ids_here = {
                fm.discord_msg_id
                for fm in state.failed_messages
                if fm.stoat_channel_id == stoat_channel_id
            }
        else:
            _failed_ids_here = set()
        _channel_max_id = 0
        _skipped = 0
        _copied = 0
        _retried = 0
        for idx, msg in enumerate(message_source):
            # Cancel check inside the message loop. Batch 3 (S2): raise (not break) so a
            # cancelled-mid-channel worker propagates like the _rate_limit_with_pause path
            # and never falls through to the self-mark-complete at the loop end (which would
            # lose the un-sent tail on --resume).
            if config.cancel_event and config.cancel_event.is_set():
                raise asyncio.CancelledError("Migration cancelled by user")

            # Track the highest numeric message id seen (durable high-water mark).
            # isdigit() guard: real Discord ids are numeric snowflakes, but tests and
            # some system messages may use non-numeric ids — never crash the loop.
            _mid = int(msg.id) if msg.id.isdigit() else None
            if _mid is not None and _mid > _channel_max_id:
                _channel_max_id = _mid
            # Skip messages already copied (resume offset or incremental high-water),
            # UNLESS this id failed on a prior run (#76 self-heal) — then re-attempt it.
            _would_skip = bool(_skip_below) and _mid is not None and _mid <= int(_skip_below)
            _is_retry = msg.id in _failed_ids_here
            if _would_skip and not _is_retry:
                _skipped += 1
                continue
            if _would_skip and _is_retry:
                _retried += 1
            else:
                _copied += 1

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
                        # #77: only persist numeric ids so a system-message id never
                        # poisons the offset (the read-side guard above is the real fix).
                        if msg.id.isdigit():
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
            # Durable high-water mark — survives completion (unlike the transient
            # offset below) so a later --incremental run skips already-copied messages.
            if _channel_max_id:
                state.channel_high_water[export.channel.id] = str(_channel_max_id)
            state.channel_message_offsets.pop(export.channel.id, None)
            # #76 reconcile this channel's previously-failed ids: drop any that
            # succeeded this run (now in message_map), and collapse the carried +
            # fresh-re-fail duplicate to a single entry. Scoped to this channel so
            # other channels' and brand-new failures are untouched.
            #
            # This gate MUST match the one building _failed_ids_here above.
            if config.incremental and _failed_ids_here:
                _seen: set[str] = set()
                _reconciled: list[FailedMessage] = []
                for fm in state.failed_messages:
                    if (
                        fm.stoat_channel_id == stoat_channel_id
                        and fm.discord_msg_id in _failed_ids_here
                    ):
                        if fm.discord_msg_id in state.message_map:
                            continue  # succeeded this run -> drop (S2-AC6)
                        if fm.discord_msg_id in _seen:
                            continue  # carried + re-fail dup -> collapse (S2-AC7)
                        _seen.add(fm.discord_msg_id)
                    _reconciled.append(fm)
                state.failed_messages = _reconciled
            save_state(state, config.output_dir)
            # Return empty result since we already merged.
            result = ChannelResult(channel_id=export.channel.id)

        if config.incremental:
            _parts = [f"{_copied} new", f"{_skipped} already present"]
            if _retried:
                _parts.append(f"{_retried} retried")
            _complete_msg = f"Completed {export.channel.name!r}: {', '.join(_parts)}."
        else:
            _complete_msg = f"Completed {export.channel.name!r} ({total} messages)."
        on_event(
            MigrationEvent(
                phase="messages",
                status="progress",
                message=_complete_msg,
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

    # DCE 2.47+ exports carry the forwarded payload; recover it rather than skip.
    if msg.forwarded_message is not None:
        msg = _merge_forwarded(msg)
    elif _is_unrecoverable_forward(msg):
        acc_warnings.append(
            {
                "phase": "messages",
                "type": "forwarded_message",
                "message": (
                    f"Forwarded message {msg.id} skipped: this export predates "
                    f"DiscordChatExporter 2.47 and does not carry the forwarded content. "
                    f"Re-exporting with a current DCE recovers it."
                ),
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
                verify_size=config.verify_uploads,
            )
            autumn_ids.append(sticker_id)
            state.autumn_uploads[sticker_id] = f"{msg.id}:sticker:{sticker_path.name}"
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
    embed_media_ids: list[str] = []
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
                        verify_size=config.verify_uploads,
                    )
                    flat["media"] = media_id
                    state.autumn_uploads[media_id] = f"{msg.id}:embed"
                    embed_media_ids.append(media_id)
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

    # Step 6: Empty message fallback — test the BUILT content, not raw msg.content.
    # Every PRE-guard append path (poll, sticker text, attachment placeholders,
    # failed-embed notes, cross-channel reply fallback) lands in `content` above, so a
    # body that came only from one of those is preserved. The empty baseline is rebuilt
    # from the same parts _build_content uses: prefix + " " join, plus the edited marker
    # (which carries its own leading space -> the empty-edited baseline has a DOUBLE space
    # by construction). Both sides are .strip()ed, so the internal double space matches.
    timestamp_prefix = format_original_timestamp(msg.timestamp)
    edited_suffix = _EDITED_MARKER if msg.timestamp_edited else ""
    empty_built = f"{timestamp_prefix} {edited_suffix}"
    if content.strip() == empty_built.strip() and not autumn_ids and not stoat_embeds:
        content = f"{timestamp_prefix} [empty message]{edited_suffix}"

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
    duplicate_unmapped = False
    try:
        for part_idx, part_content in enumerate(parts):
            is_first = part_idx == 0
            idem_key = f"ferry-{msg.id}" if len(parts) == 1 else f"ferry-{msg.id}_p{part_idx + 1}"
            try:
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
            except DuplicateSendError:
                # This part is already on the server. The catch is PER PART, not around
                # the loop: every part carries its own Idempotency-Key, so the parts
                # after a duplicate are NOT duplicates and must still be sent. Catching
                # at the loop truncates the message and loses their content with no
                # warning, which is worse than the bug this batch fixes.
                #
                # Only the first part's id reaches message_map, so only it is worth
                # noting. Stoat returns no id with the 409, so that entry is lost.
                if is_first:
                    duplicate_unmapped = True
                continue
            part_stoat_id: str = result["_id"]
            if is_first:
                stoat_msg_id = part_stoat_id

        # Only the statements that CONSUME the id are guarded. The counters and the
        # reference-set updates run either way, because the message IS on the server.
        #
        # Do NOT fold `stoat_msg_id` into the branch condition above. `else` means "not
        # the condition above", not "the retry path", so a parallel-path message with an
        # empty id would fall into the retry branch and write state.message_map directly,
        # bypassing ChannelResult and the save_lock discipline. Both branches leave the
        # same channel_message_counts, so no state-level test can see that mistake.
        # Pinned by test_duplicate_runs_the_parallel_branch_not_the_retry_branch.
        if channel_result is not None:
            if stoat_msg_id:
                channel_result.message_map_updates[msg.id] = stoat_msg_id
            channel_result.referenced_autumn_ids.update(autumn_ids, embed_media_ids)
            channel_result.messages_migrated += 1  # S15: track for forum index rebuild
        else:
            if stoat_msg_id:
                state.message_map[msg.id] = stoat_msg_id
            state.referenced_autumn_ids.update(autumn_ids, embed_media_ids)
            # S15: Track per-channel message count (direct-state path, e.g. retry).
            if export_channel_id:
                state.channel_message_counts[export_channel_id] = (
                    state.channel_message_counts.get(export_channel_id, 0) + 1
                )

        if duplicate_unmapped:
            # The message is on the server but Stoat returned no id with the 409, so
            # nothing can reference it: replies to it will not resolve, and its pin and
            # reactions are skipped above. Say so rather than reporting a clean run.
            acc_warnings.append(
                {
                    "phase": "messages",
                    "type": "duplicate_send_unmapped",
                    "message": (
                        f"Message {msg.id} was already on the server (duplicate send); "
                        "its Stoat id could not be recovered, so replies to it and its "
                        "pins and reactions were skipped"
                    ),
                }
            )

        if msg.is_pinned and stoat_msg_id:
            acc_pins.append((stoat_channel_id, stoat_msg_id))

        # Step 8b: Queue reactions (only in native mode). Guarded at the block, which
        # covers BOTH append sites, the custom-emoji one and the Unicode one.
        if _effective_mode == "native" and stoat_msg_id:
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
                    else:
                        # Batch 4 (S1): the emoji never entered emoji_map (no asset / beyond
                        # the cap) — the reaction is dropped. Count it (acc-aware) + warn so the
                        # fidelity report reflects the loss instead of silently claiming 100%.
                        if channel_result is not None:
                            channel_result.reactions_dropped += 1
                        else:
                            state.reactions_dropped += 1
                        acc_warnings.append(
                            {
                                "phase": "messages",
                                "type": "unmapped_emoji_reaction",
                                "message": safe_sanitize(
                                    config.token_store,
                                    f"Reaction emoji {reaction.emoji.id} not migrated "
                                    "(not in emoji_map) — dropped",
                                ),
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
        safe_exc = safe_sanitize(config.token_store, str(exc))
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
        # Batch 3 (S1): the retry path (engine.run_retry_failed) calls this with
        # channel_result=None and relies on an exception to mark a re-failure. Re-raise so
        # the retry loop accounts correctly and terminates. The parallel per-channel path
        # (channel_result set) keeps degrade-in-loop. Guard is provably retry-path-only:
        # only engine.py's retry loop passes channel_result=None.
        #
        # Batch 7: a DuplicateSendError never reaches here. It is caught per part inside
        # the send loop above, because a duplicate means the message landed and there is
        # no re-failure to mark. Re-raising it would leave the message in
        # failed_messages, which is exactly the defect batch 7 removes. Pinned by
        # test_retry_path_does_not_reraise_on_a_duplicate.
        if channel_result is None:
            raise
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


def _merge_forwarded(msg: DCEMessage) -> DCEMessage:
    """Promote a forwarded payload into the message's own fields.

    Everything downstream -- attachment upload, sticker handling, embed flattening, the
    content transforms -- then operates on it unmodified. Deliberately NOT a second
    sender: Stoat has no native forward, so a forward is just a message whose contents
    came from somewhere else.

    The marker is *prepended* rather than replacing the content, because a forward may
    carry a comment of its own, and because it keeps the message non-empty when the
    forwarded block holds only attachments -- which would otherwise trip the
    empty-message guard and be dropped a second way.

    ``author`` is deliberately untouched. The forwarded block carries none (upstream
    exports six fields and an author is not among them), so this necessarily posts under
    whoever forwarded it. See docs/guides/known-limitations.md.
    """
    fwd = msg.forwarded_message
    if fwd is None:
        return msg
    parts = [part for part in (msg.content, _FORWARD_MARKER, fwd.content) if part]
    return replace(
        msg,
        content="\n".join(parts),
        # Stoat caps attachments at 5; the existing overflow notice handles the excess.
        attachments=[*msg.attachments, *fwd.attachments],
        embeds=[*msg.embeds, *fwd.embeds],
        stickers=[*msg.stickers, *fwd.stickers],
        # A forward's reference points at its SOURCE; that is not a reply relationship,
        # and the reply step downstream treats any reference as one. Left set, it counts
        # the forward toward reply fidelity, makes Stoat render a reply-quote whenever
        # the source happens to be in message_map, and appends "[Replying to message in
        # #X]" beside the [forwarded] marker for a cross-channel source -- one message
        # claiming to be both. Clearing it here keeps the invariant in one place rather
        # than requiring every downstream consumer to re-check the kind.
        reference=None,
    )


def _is_unrecoverable_forward(msg: DCEMessage) -> bool:
    """A forward whose payload this export does not carry.

    **Only reached when ``msg.forwarded_message is None``** -- the caller merges first and
    consults this in the ``elif``. So a ``"Forward"`` reference arriving here has already
    been established to have no payload, which is exactly the unrecoverable case: either
    a pre-2.47 export, or one where upstream could not resolve the original (deleted, or
    not visible to the exporting account). Either way there is nothing to send, and a
    warning is the honest outcome.

    NOTE two different fields are both spelled "type" here: ``msg.type`` is the *message*
    kind ("Default"), while ``msg.reference.type`` is the *reference* kind
    (DCE's ``MessageReferenceKind``: "Default" or "Forward").

    Pre-2.47 exports never wrote the reference kind, so an empty one falls back to the
    old empty-content heuristic. Where the kind IS present we trust it instead, because
    that heuristic also matches an ordinary **reply** carrying only a sticker or only an
    embed -- which was being discarded as though it were a forward.
    """
    if msg.reference is None:
        return False
    if msg.reference.type == "Forward":
        return True
    if msg.reference.type:  # a known, non-Forward kind: an ordinary reply
        return False
    return msg.content == "" and not msg.attachments and msg.type == "Default"


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
        content += _EDITED_MARKER

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
                # Decimal MB, matching TAG_SIZE_LIMITS (see autumn.py).
                f"({att.file_size_bytes / 1_000_000:.1f} MB, "
                f"limit: {limit / 1_000_000:.1f} MB)"
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
