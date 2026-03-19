"""Migration orchestrator — shared by CLI and GUI."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from typing import Any

import aiohttp

from discord_ferry.config import FerryConfig
from discord_ferry.core.events import EventCallback, MigrationEvent
from discord_ferry.discord import (
    fetch_and_translate_guild_metadata,
    load_discord_metadata,
    save_discord_metadata,
)
from discord_ferry.errors import DotNetMissingError, MigrationError
from discord_ferry.exporter import (
    detect_dotnet,
    download_dce,
    get_dce_path,
    run_dce_export,
    validate_discord_token,
)
from discord_ferry.migrator.api import (
    api_edit_server,
    api_fetch_server,
    api_pin_message,
    api_send_message,
    get_session,
    init_request_semaphore,
)
from discord_ferry.migrator.avatars import run_avatars
from discord_ferry.migrator.connect import run_connect
from discord_ferry.migrator.emoji import run_emoji
from discord_ferry.migrator.messages import _process_message, run_messages
from discord_ferry.migrator.pins import run_pins
from discord_ferry.migrator.reactions import run_reactions
from discord_ferry.migrator.structure import run_categories, run_channels, run_roles, run_server
from discord_ferry.parser.dce_parser import parse_export_directory, stream_messages, validate_export
from discord_ferry.parser.models import DCEExport, DCEMessage
from discord_ferry.reporter import generate_markdown_report, generate_report
from discord_ferry.review import build_review_summary
from discord_ferry.state import FailedMessage, MigrationState, load_state, save_state

PhaseFunction = Callable[
    [FerryConfig, MigrationState, list[DCEExport], EventCallback],
    Coroutine[Any, Any, None],
]

PHASE_ORDER: list[str] = [
    "export",  # Phase 0 — handled inline (DCE subprocess)
    "validate",  # Phase 1 — handled inline (parser)
    "connect",  # Phase 2
    "server",  # Phase 3
    "roles",  # Phase 4
    "categories",  # Phase 5
    "channels",  # Phase 6
    "emoji",  # Phase 7
    "avatars",  # Phase 7.5
    "messages",  # Phase 8
    "reactions",  # Phase 9
    "pins",  # Phase 10
    "report",  # Phase 11 — handled inline (reporter)
]

# Phases that can be skipped via config flags
_SKIPPABLE: dict[str, str] = {
    "export": "skip_export",
    "emoji": "skip_emoji",
    "avatars": "skip_avatars",
    "messages": "skip_messages",
    "reactions": "skip_reactions",
}

# Default phase implementations — grows as phases are implemented
_DEFAULT_PHASES: dict[str, PhaseFunction] = {
    "connect": run_connect,
    "server": run_server,
    "roles": run_roles,
    "categories": run_categories,
    "channels": run_channels,
    "emoji": run_emoji,
    "avatars": run_avatars,
    "messages": run_messages,
    "reactions": run_reactions,
    "pins": run_pins,
}


async def run_migration(
    config: FerryConfig,
    on_event: EventCallback,
    phase_overrides: dict[str, PhaseFunction] | None = None,
) -> MigrationState:
    """Run the full 12-phase migration.

    Args:
        config: Migration configuration.
        on_event: Callback for progress events. GUI subscribes to update UI,
                  CLI subscribes to print Rich output.
        phase_overrides: Optional dict mapping phase name to a phase function. Used by
                         tests to inject mock implementations. In production, the engine
                         will use real phase implementations once they are available.

    Returns:
        Final MigrationState after all phases complete.

    Raises:
        MigrationError: If any phase raises an exception.
    """
    config.output_dir.mkdir(parents=True, exist_ok=True)

    if config.resume and config.incremental:
        raise MigrationError("--resume and --incremental are mutually exclusive.")

    # Load or create state
    if config.resume:
        state = load_state(config.output_dir)
        if state.is_dry_run:
            raise MigrationError("Cannot resume from a dry-run state. Start a fresh migration.")
    elif config.incremental:
        state_path = config.output_dir / "state.json"
        if state_path.exists():
            prior = load_state(config.output_dir)
            state = MigrationState()
            state.started_at = datetime.now(timezone.utc).isoformat()
            state.is_dry_run = config.dry_run
            # Carry over ID maps so structure phases are skipped / reused
            state.channel_map = dict(prior.channel_map)
            state.role_map = dict(prior.role_map)
            state.category_map = dict(prior.category_map)
            state.emoji_map = dict(prior.emoji_map)
            state.avatar_cache = dict(prior.avatar_cache)
            state.author_names = dict(prior.author_names)
            state.upload_cache = dict(prior.upload_cache)
            state.message_map = dict(prior.message_map)
            state.stoat_server_id = prior.stoat_server_id
            state.autumn_url = prior.autumn_url
            # Carry over cumulative counters
            state.attachments_uploaded = prior.attachments_uploaded
            state.attachments_skipped = prior.attachments_skipped
            state.reactions_applied = prior.reactions_applied
            state.pins_applied = prior.pins_applied
            # Record prior message total for delta reporting
            state.prior_messages_total = len(prior.message_map)
            # Keep offsets so messages phase resumes from last offset per channel.
            # CLEAR completed_channel_ids so every channel is re-entered (new messages may exist).
            state.channel_message_offsets = dict(prior.channel_message_offsets)
            state.completed_channel_ids = set()
            on_event(
                MigrationEvent(
                    phase="validate",
                    status="progress",
                    message=(
                        f"Incremental mode: loaded prior state "
                        f"({state.prior_messages_total} messages already migrated)"
                    ),
                )
            )
        else:
            # No prior state — fall back to a fresh migration
            state = MigrationState()
            state.started_at = datetime.now(timezone.utc).isoformat()
            state.is_dry_run = config.dry_run
            on_event(
                MigrationEvent(
                    phase="validate",
                    status="warning",
                    message="Incremental mode: no prior state found — running full migration",
                )
            )
    else:
        state = MigrationState()
        state.started_at = datetime.now(timezone.utc).isoformat()
        state.is_dry_run = config.dry_run

    # Phase 0: EXPORT — run DCE subprocess inline (orchestrated mode)
    if not config.skip_export:
        on_event(MigrationEvent(phase="export", status="started", message="Starting export..."))
        await validate_discord_token(config.discord_token or "")
        if not detect_dotnet():
            raise DotNetMissingError(
                "DCE requires .NET 8 runtime. "
                "Install from https://dotnet.microsoft.com/download/dotnet/8.0"
            )
        dce_path = get_dce_path()
        if dce_path is None:
            dce_path = await download_dce(on_event, skip_verify=config.skip_dce_verify)
        await run_dce_export(config, dce_path, on_event)
        state.export_completed = True
        save_state(state, config.output_dir)
        on_event(MigrationEvent(phase="export", status="completed", message="Export complete."))
    else:
        on_event(MigrationEvent(phase="export", status="skipped", message="Using existing exports"))

    # Phase 0b: Fetch Discord guild metadata (permissions, NSFW flags)
    # Independent of skip_export — runs whenever discord_token is available.
    if config.discord_token and config.discord_server_id:
        existing_meta = load_discord_metadata(config.output_dir)
        if existing_meta and config.resume:
            on_event(
                MigrationEvent(
                    phase="export",
                    status="progress",
                    message="Using cached Discord metadata (resume)",
                )
            )
        else:
            on_event(
                MigrationEvent(
                    phase="export",
                    status="progress",
                    message="Fetching Discord guild metadata (permissions, NSFW)...",
                )
            )
            try:
                async with aiohttp.ClientSession() as discord_session:
                    meta = await fetch_and_translate_guild_metadata(
                        discord_session, config.discord_token, config.discord_server_id
                    )
                save_discord_metadata(meta, config.output_dir)
                role_count = len(meta.role_permissions)
                ch_count = len(meta.channel_metadata)
                on_event(
                    MigrationEvent(
                        phase="export",
                        status="progress",
                        message=f"Discord metadata: {role_count} roles, {ch_count} channels",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                state.warnings.append(
                    {
                        "phase": "export",
                        "type": "discord_metadata_fetch_failed",
                        "message": (
                            f"Could not fetch Discord metadata: {exc}. "
                            "Permissions will not be migrated."
                        ),
                    }
                )
                on_event(
                    MigrationEvent(
                        phase="export",
                        status="warning",
                        message=f"Discord metadata fetch failed: {exc}. Permissions skipped.",
                    )
                )
    else:
        if not config.discord_token:
            on_event(
                MigrationEvent(
                    phase="export",
                    status="warning",
                    message=(
                        "No Discord token — permission overrides will not be migrated. "
                        "Private channels may become publicly visible on Stoat."
                    ),
                )
            )

    # Phase 1: VALIDATE — parse exports inline
    on_event(MigrationEvent(phase="validate", status="started", message="Parsing exports..."))
    exports = parse_export_directory(config.export_dir, metadata_only=True)
    # Validate and collect author names in a single pass over all messages.
    warnings = validate_export(exports, config.export_dir, author_names=state.author_names)
    for w in warnings:
        state.warnings.append(w)
        on_event(MigrationEvent(phase="validate", status="warning", message=w["message"]))

    total_messages = sum(e.message_count for e in exports)
    on_event(
        MigrationEvent(
            phase="validate",
            status="completed",
            message=f"Parsed {len(exports)} exports, {total_messages} messages",
            total=total_messages,
        )
    )

    # S6: Filter threads by minimum message count
    threads_filtered = 0
    if config.min_thread_messages > 0:
        filtered_exports: list[DCEExport] = []
        for export in exports:
            if export.is_thread and export.message_count < config.min_thread_messages:
                threads_filtered += 1
                state.warnings.append(
                    {
                        "phase": "validate",
                        "type": "thread_filtered",
                        "message": (
                            f"Thread '{export.channel.name}' excluded "
                            f"({export.message_count} messages "
                            f"< {config.min_thread_messages} threshold)"
                        ),
                    }
                )
                on_event(
                    MigrationEvent(
                        phase="validate",
                        status="warning",
                        message=(
                            f"Thread '{export.channel.name}' filtered out "
                            f"({export.message_count} msgs)"
                        ),
                    )
                )
            else:
                filtered_exports.append(export)
        exports = filtered_exports

    # Pre-creation review: emit summary event and optionally wait for user confirmation
    if not config.dry_run and not config.resume:
        discord_meta = load_discord_metadata(config.output_dir)
        summary = build_review_summary(exports, discord_metadata=discord_meta)
        summary.threads_filtered = threads_filtered
        # Log warnings for user-specific permission overrides that Stoat cannot import
        if discord_meta and discord_meta.user_override_channels:
            for uo in discord_meta.user_override_channels:
                state.warnings.append(
                    {
                        "phase": "review",
                        "type": "user_override_skipped",
                        "message": (
                            f"Channel {uo['channel_name']} has {uo['override_count']} "
                            "user-specific permission overrides that cannot be migrated to Stoat"
                        ),
                    }
                )

        on_event(
            MigrationEvent(
                phase="review",
                status="confirm",
                message="Review migration before proceeding",
                detail={
                    "server_name": summary.server_name,
                    "roles": summary.role_count,
                    "categories": summary.category_count,
                    "channels": summary.channel_count,
                    "emoji": summary.emoji_count,
                    "messages": summary.message_count,
                    "threads": summary.thread_count,
                    "has_permissions": summary.has_permissions,
                    "nsfw_channels": summary.nsfw_channel_count,
                    "user_overrides": summary.user_override_count,
                    "threads_filtered": summary.threads_filtered,
                    "warnings": summary.warnings,
                    "reaction_mode": config.reaction_mode,
                },
            )
        )
        # Wait for user confirmation when a pause_event is provided (GUI mode)
        if config.pause_event is not None:
            config.pause_event.clear()
            while not config.pause_event.is_set():
                if config.cancel_event and config.cancel_event.is_set():
                    return state
                await asyncio.sleep(0.1)

    # Phases 2-10: run in order, skipping as appropriate
    runnable_phases = [p for p in PHASE_ORDER if p not in ("export", "validate", "report")]

    init_request_semaphore(config.max_concurrent_requests)

    async with aiohttp.ClientSession() as session:
        config.session = session

        # S17: Acquire advisory migration lock on the target server (existing server only).
        lock_acquired = False
        if config.server_id and not config.dry_run:
            lock_acquired = await _acquire_migration_lock(config, state, session, on_event)

        try:
            await _run_phases(config, state, exports, on_event, runnable_phases, phase_overrides)
        finally:
            if lock_acquired and config.server_id and not config.dry_run:
                await _release_migration_lock(config, state, session, on_event)

    config.session = None

    # Skip report if migration was cancelled
    if config.cancel_event and config.cancel_event.is_set():
        return state

    # Phase 11: REPORT — generate and save inline
    state.current_phase = "report"
    on_event(MigrationEvent(phase="report", status="started", message="Generating report..."))
    state.completed_at = datetime.now(timezone.utc).isoformat()

    # S15: Rebuild forum index messages with actual migration data.
    if state.forum_channel_members and not config.dry_run:
        await _rebuild_forum_indexes(config, state, on_event)

    # S16: Detect orphaned Autumn uploads when requested.
    if config.cleanup_orphans and not config.dry_run:
        orphans = set(state.autumn_uploads.keys()) - state.referenced_autumn_ids
        for orphan_id in orphans:
            state.warnings.append(
                {
                    "phase": "cleanup",
                    "type": "orphan_detected",
                    "message": f"Orphaned Autumn upload: {orphan_id}",
                }
            )
        on_event(
            MigrationEvent(
                phase="cleanup",
                status="completed",
                message=f"Found {len(orphans)} orphaned uploads",
            )
        )

    generate_report(config, state, exports)
    generate_markdown_report(config, state, exports)
    save_state(state, config.output_dir)
    on_event(MigrationEvent(phase="report", status="completed", message="Migration complete"))

    # Phase 12: VALIDATE_MIGRATION — optional post-migration verification
    if config.validate_after and state.stoat_server_id:
        state.current_phase = "validate_migration"
        on_event(
            MigrationEvent(
                phase="validate_migration",
                status="started",
                message="Validating migration results...",
            )
        )
        try:
            async with aiohttp.ClientSession() as validation_session:
                server = await api_fetch_server(
                    validation_session, config.stoat_url, config.token, state.stoat_server_id
                )
            actual_channels = len(server.get("channels", []))
            actual_roles = len(server.get("roles", {}))
            expected_channels = len(state.channel_map)
            expected_roles = len(state.role_map)
            failed_count = len(state.failed_messages)

            state.validation_results = {
                "channels_expected": expected_channels,
                "channels_found": actual_channels,
                "roles_expected": expected_roles,
                "roles_found": actual_roles,
                "failed_messages": failed_count,
                "passed": actual_channels == expected_channels and actual_roles == expected_roles,
            }

            if state.validation_results["passed"]:
                msg = f"Validation passed: {actual_channels} channels, {actual_roles} roles match."
            else:
                msg = (
                    f"Validation warning: expected {expected_channels} channels "
                    f"(found {actual_channels}), expected {expected_roles} roles "
                    f"(found {actual_roles})."
                )
            if failed_count:
                msg += f" {failed_count} messages failed (see failed_message_ids in report)."

            on_event(
                MigrationEvent(
                    phase="validate_migration",
                    status="completed" if state.validation_results["passed"] else "warning",
                    message=msg,
                )
            )
        except Exception as exc:  # noqa: BLE001
            on_event(
                MigrationEvent(
                    phase="validate_migration",
                    status="warning",
                    message=f"Validation skipped: {exc}",
                )
            )
        save_state(state, config.output_dir)

    return state


async def _run_phases(
    config: FerryConfig,
    state: MigrationState,
    exports: list[DCEExport],
    on_event: EventCallback,
    runnable_phases: list[str],
    phase_overrides: dict[str, PhaseFunction] | None,
) -> None:
    """Execute phases 2-10 in order."""
    for phase_name in runnable_phases:
        # Check cancel flag between phases
        if config.cancel_event and config.cancel_event.is_set():
            save_state(state, config.output_dir)
            on_event(
                MigrationEvent(
                    phase=phase_name, status="skipped", message="Migration cancelled by user"
                )
            )
            return

        # Check config skip flags
        skip_attr = _SKIPPABLE.get(phase_name)
        if skip_attr and getattr(config, skip_attr, False):
            on_event(
                MigrationEvent(phase=phase_name, status="skipped", message="Skipped by config")
            )
            continue

        # Check resume: skip phases that precede current_phase
        if config.resume and state.current_phase:
            phase_idx = PHASE_ORDER.index(phase_name)
            current_idx = PHASE_ORDER.index(state.current_phase)
            if phase_idx < current_idx:
                on_event(
                    MigrationEvent(
                        phase=phase_name,
                        status="skipped",
                        message="Already completed (resume)",
                    )
                )
                continue

        # Resolve phase function: overrides first, then defaults
        phase_fn: PhaseFunction | None = None
        if phase_overrides and phase_name in phase_overrides:
            phase_fn = phase_overrides[phase_name]
        elif phase_name in _DEFAULT_PHASES:
            phase_fn = _DEFAULT_PHASES[phase_name]

        if phase_fn is None:
            on_event(
                MigrationEvent(phase=phase_name, status="skipped", message="Not yet implemented")
            )
            continue

        state.current_phase = phase_name
        on_event(
            MigrationEvent(phase=phase_name, status="started", message=f"Starting {phase_name}")
        )

        try:
            await phase_fn(config, state, exports, on_event)
        except asyncio.CancelledError:
            save_state(state, config.output_dir)
            on_event(
                MigrationEvent(
                    phase=phase_name,
                    status="skipped",
                    message=f"Cancelled during {phase_name}",
                )
            )
            return
        except Exception as e:
            state.errors.append({"phase": phase_name, "type": "phase_failed", "error": str(e)})
            save_state(state, config.output_dir)
            on_event(
                MigrationEvent(
                    phase=phase_name,
                    status="error",
                    message=f"Error in {phase_name}: {e}",
                    detail={"error": str(e)},
                )
            )
            raise MigrationError(f"Phase {phase_name} failed: {e}") from e

        on_event(
            MigrationEvent(phase=phase_name, status="completed", message=f"Completed {phase_name}")
        )
        save_state(state, config.output_dir)


_FERRY_LOCK_MARKER = "[FERRY_LOCK:"
_LOCK_EXPIRY_SECONDS = 86400  # 24 hours


async def _acquire_migration_lock(
    config: FerryConfig,
    state: MigrationState,
    session: aiohttp.ClientSession,
    on_event: EventCallback,
) -> bool:
    """S17: Acquire advisory migration lock on the target server.

    Appends a ``[FERRY_LOCK:{timestamp}:{hostname}]`` marker to the server
    description. If a live marker is found, raises ``MigrationError``.

    Args:
        config: Ferry configuration (server_id, token, stoat_url, force_unlock).
        state: Migration state (warnings list for expired-lock warnings).
        session: Active aiohttp session.
        on_event: Event callback.

    Returns:
        True if lock was successfully acquired, False if skipped (no server_id).

    Raises:
        MigrationError: If a live lock is detected and force_unlock is False.
    """
    try:
        server = await api_fetch_server(
            session, config.stoat_url, config.token, config.server_id or ""
        )
    except Exception as exc:  # noqa: BLE001
        on_event(
            MigrationEvent(
                phase="connect",
                status="warning",
                message=f"Could not fetch server for lock check: {exc}",
            )
        )
        return False

    description: str = server.get("description", "") or ""
    lock_ts: float | None = None

    # Check for existing lock marker.
    lock_start = description.find(_FERRY_LOCK_MARKER)
    if lock_start != -1:
        lock_end = description.find("]", lock_start)
        if lock_end != -1:
            marker = description[lock_start : lock_end + 1]
            parts = marker[len(_FERRY_LOCK_MARKER) :].rstrip("]").split(":")
            if parts:
                try:
                    lock_ts = float(parts[0])
                except (ValueError, IndexError):
                    lock_ts = None

        if lock_ts is not None:
            age = datetime.now(timezone.utc).timestamp() - lock_ts
            if age < _LOCK_EXPIRY_SECONDS and not config.force_unlock:
                raise MigrationError(
                    f"Another migration is in progress (lock age: {int(age)}s). "
                    "Use --force-unlock to override a stale lock."
                )
            if age >= _LOCK_EXPIRY_SECONDS:
                warn_msg = f"Overriding expired migration lock (age: {int(age / 3600):.1f}h)"
                state.warnings.append(
                    {"phase": "connect", "type": "lock_expired", "message": warn_msg}
                )
                on_event(MigrationEvent(phase="connect", status="warning", message=warn_msg))
            # Remove old lock marker before appending new one.
            description = description[:lock_start] + description[lock_end + 1 :]
            description = description.strip()

    # Append new lock marker.
    ts = int(datetime.now(timezone.utc).timestamp())
    hostname = socket.gethostname()
    lock_marker = f"{_FERRY_LOCK_MARKER}{ts}:{hostname}]"
    new_description = f"{description} {lock_marker}".strip() if description else lock_marker

    try:
        await api_edit_server(
            session,
            config.stoat_url,
            config.token,
            config.server_id or "",
            description=new_description,
        )
        on_event(
            MigrationEvent(
                phase="connect",
                status="progress",
                message=f"Migration lock acquired on server {config.server_id}",
            )
        )
        return True
    except Exception as exc:  # noqa: BLE001
        on_event(
            MigrationEvent(
                phase="connect",
                status="warning",
                message=f"Could not acquire migration lock: {exc}",
            )
        )
        return False


async def _release_migration_lock(
    config: FerryConfig,
    state: MigrationState,
    session: aiohttp.ClientSession,
    on_event: EventCallback,
) -> None:
    """S17: Release the advisory migration lock by removing the marker from server description."""
    try:
        server = await api_fetch_server(
            session, config.stoat_url, config.token, config.server_id or ""
        )
        description: str = server.get("description", "") or ""
        lock_start = description.find(_FERRY_LOCK_MARKER)
        if lock_start != -1:
            lock_end = description.find("]", lock_start)
            if lock_end != -1:
                description = description[:lock_start] + description[lock_end + 1 :]
                description = description.strip()
                await api_edit_server(
                    session,
                    config.stoat_url,
                    config.token,
                    config.server_id or "",
                    description=description,
                )
        on_event(
            MigrationEvent(
                phase="connect",
                status="progress",
                message="Migration lock released",
            )
        )
    except Exception as exc:  # noqa: BLE001
        on_event(
            MigrationEvent(
                phase="connect",
                status="warning",
                message=f"Could not release migration lock: {exc}",
            )
        )


async def _rebuild_forum_indexes(
    config: FerryConfig,
    state: MigrationState,
    on_event: EventCallback,
) -> None:
    """S15: Rebuild forum index messages during REPORT phase with actual migration data.

    Sends a new pinned message to each forum index channel with accurate per-channel
    message counts from ``state.channel_message_counts`` (populated by the MESSAGES phase).

    Args:
        config: Ferry configuration.
        state: Migration state with channel and message maps populated.
        on_event: Event callback for progress reporting.
    """
    async with get_session(config) as session:
        for forum_key, discord_channel_ids in state.forum_channel_members.items():
            index_channel_id = state.channel_map.get(f"forum-index-{forum_key}")
            if not index_channel_id:
                continue

            forum_name = state.forum_category_names.get(forum_key, forum_key)

            # Build index lines using actual migrated message counts.
            lines = [f"**Forum: {forum_name}** *(updated after migration)*\n"]
            for discord_ch_id in discord_channel_ids:
                stoat_ch_id = state.channel_map.get(discord_ch_id)
                if not stoat_ch_id:
                    continue
                actual_count = state.channel_message_counts.get(discord_ch_id, 0)
                lines.append(f"- <#{stoat_ch_id}> — {actual_count} messages migrated")

            if len(lines) <= 1:
                content = f"**Forum: {forum_name}**\nNo posts migrated."
            else:
                content = "\n".join(lines)
                if len(content) > 2000:
                    while len(lines) > 1 and len("\n".join(lines)) > 1950:
                        lines.pop()
                    remaining = len(discord_channel_ids) - (len(lines) - 1)
                    lines.append(f"\n*...and {remaining} more posts*")
                    content = "\n".join(lines)

            try:
                msg_result = await api_send_message(
                    session,
                    config.stoat_url,
                    config.token,
                    index_channel_id,
                    content=content,
                    masquerade={"name": "Discord Ferry"},
                    idempotency_key=f"ferry-forum-index-rebuilt-{forum_key}",
                )
                await asyncio.sleep(config.upload_delay)
                index_msg_id: str = msg_result["_id"]
                await api_pin_message(
                    session,
                    config.stoat_url,
                    config.token,
                    index_channel_id,
                    index_msg_id,
                )
                await asyncio.sleep(config.upload_delay)
                on_event(
                    MigrationEvent(
                        phase="report",
                        status="progress",
                        message=f"Rebuilt forum index for '{forum_name}' with actual data",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                state.warnings.append(
                    {
                        "phase": "report",
                        "type": "forum_index_rebuild_failed",
                        "message": f"Failed to rebuild forum index for '{forum_name}': {exc}",
                    }
                )
                on_event(
                    MigrationEvent(
                        phase="report",
                        status="warning",
                        message=f"Forum index rebuild for '{forum_name}' failed: {exc}",
                    )
                )


async def run_retry_failed(
    config: FerryConfig,
    state: MigrationState,
    exports: list[DCEExport],
    on_event: EventCallback,
) -> None:
    """Re-process failed messages from state.failed_messages.

    Uses a single-scan strategy: collects all failed message IDs, then
    scans exports once to find matching DCEMessage objects.
    """
    if not state.failed_messages:
        on_event(
            MigrationEvent(
                phase="retry", status="completed", message="No failed messages to retry."
            )
        )
        return

    # Ensure request semaphore is initialized (may be called standalone, not from run_migration).
    init_request_semaphore(config.max_concurrent_requests)

    if not config.export_dir.exists():
        on_event(
            MigrationEvent(
                phase="retry",
                status="error",
                message=f"Cannot retry: export directory not found at {config.export_dir}",
            )
        )
        return

    # Collect failed IDs for single-scan lookup
    failed_ids = {fm.discord_msg_id for fm in state.failed_messages}

    on_event(
        MigrationEvent(
            phase="retry",
            status="started",
            message=f"Retrying {len(failed_ids)} failed messages",
        )
    )

    # Scan all exports once, collect matching messages
    found_messages: dict[str, DCEMessage] = {}
    for export in exports:
        msg_iter = (
            stream_messages(export.json_path)
            if export.json_path is not None
            else iter(export.messages)
        )
        for msg in msg_iter:
            if msg.id in failed_ids:
                found_messages[msg.id] = msg

    async with get_session(config) as session:
        config.session = session
        retried = 0
        still_failed: list[FailedMessage] = []
        for fm in state.failed_messages:
            found_msg = found_messages.get(fm.discord_msg_id)
            if found_msg is None:
                on_event(
                    MigrationEvent(
                        phase="retry",
                        status="warning",
                        message=f"Message {fm.discord_msg_id} not found in exports — skipping.",
                    )
                )
                still_failed.append(fm)
                continue

            stoat_channel_id = fm.stoat_channel_id
            try:
                await _process_message(
                    msg=found_msg,
                    stoat_channel_id=stoat_channel_id,
                    config=config,
                    state=state,
                    session=session,
                    on_event=on_event,
                )
                retried += 1
            except Exception:  # noqa: BLE001
                fm.retry_count += 1
                still_failed.append(fm)
        state.failed_messages = still_failed
    config.session = None

    save_state(state, config.output_dir)
    remaining = len(state.failed_messages)
    on_event(
        MigrationEvent(
            phase="retry",
            status="completed",
            message=f"Retry complete: {retried} succeeded, {remaining} still failed.",
        )
    )
