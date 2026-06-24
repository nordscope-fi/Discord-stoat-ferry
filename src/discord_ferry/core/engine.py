"""Migration orchestrator — shared by CLI and GUI."""

from __future__ import annotations

import asyncio
import contextlib
import re
import socket
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

import aiohttp

from discord_ferry.config import FerryConfig
from discord_ferry.core.events import EventCallback, MigrationEvent
from discord_ferry.core.security import safe_sanitize
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
    api_create_invite,
    api_delete_channel,
    api_delete_emoji,
    api_delete_role,
    api_edit_message,
    api_edit_server,
    api_fetch_server,
    api_pin_message,
    api_send_message,
    api_upsert_categories,
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
from discord_ferry.review import (
    UntrackedSuspectChannel,
    _channel_names_from_server,
    _decode_ulid_timestamp,
    build_review_summary,
    build_rollback_summary,
)
from discord_ferry.state import (
    FailedMessage,
    MigrationState,
    RollbackFailure,
    RollbackProgress,
    load_state,
    save_state,
)

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

    # Create token store for sanitizing error messages at output boundaries.
    from discord_ferry.core.security import SecureTokenStore

    tokens: dict[str, str] = {"stoat": config.token}
    if config.discord_token:
        tokens["discord"] = config.discord_token
    config.token_store = SecureTokenStore(tokens)

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
            state.started_at = datetime.now(UTC).isoformat()
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
            state.invite_code = prior.invite_code
            state.invite_url = prior.invite_url
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
            # S3 + I2: carry forum index + per-channel counts so REPORT's
            # _rebuild_forum_indexes PATCHes (not re-posts) with cumulative counts,
            # and channel_categories so the CHANNELS upsert re-attaches carried
            # channels to the correct category on a partial re-export.
            state.forum_channel_members = {
                k: list(v) for k, v in prior.forum_channel_members.items()
            }
            state.forum_category_names = dict(prior.forum_category_names)
            state.forum_index_message_ids = dict(prior.forum_index_message_ids)
            state.channel_message_counts = dict(prior.channel_message_counts)
            state.category_names = dict(prior.category_names)
            state.channel_categories = dict(prior.channel_categories)
            state.native_fidelity_counts = dict(prior.native_fidelity_counts)
            # Carry-over audit (every MigrationState field classified):
            #   CARRY: role_map / channel_map / category_map / emoji_map,
            #     category_names, channel_categories, message_map, avatar_cache,
            #     upload_cache, author_names, stoat_server_id, autumn_url,
            #     invite_code / invite_url, channel_message_offsets,
            #     channel_message_counts, forum_channel_members /
            #     forum_category_names / forum_index_message_ids,
            #     native_fidelity_counts (cumulative fidelity counter), and the
            #     cumulative counters (attachments_uploaded/skipped,
            #     reactions_applied, pins_applied). prior_messages_total is DERIVED
            #     here from len(prior.message_map).
            #   RESET each run: completed_channel_ids (re-enter every channel for
            #     new msgs); pending_pins / pending_reactions (consumed by REPORT —
            #     carrying a stale list would re-pin/re-react); failed_messages,
            #     warnings / errors, validation_results, embeds_* / replies_*
            #     (per-run fidelity counters); current_phase, started_at,
            #     completed_at, export_completed, rollback_progress, is_dry_run.
            #   DECISION: autumn_uploads / referenced_autumn_ids stay reset —
            #     orphan-sweep is per-run; carrying matters only if a cross-run
            #     sweep is added (none today).
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
            state.started_at = datetime.now(UTC).isoformat()
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
        state.started_at = datetime.now(UTC).isoformat()
        state.is_dry_run = config.dry_run

    # Phase 0: EXPORT — run DCE subprocess inline (orchestrated mode)
    if not config.skip_export:
        on_event(MigrationEvent(phase="export", status="started", message="Starting export..."))
        await validate_discord_token(config.discord_token or "")
        on_event(
            MigrationEvent(
                phase="export",
                status="progress",
                message="Verifying .NET 8 runtime...",
            )
        )
        if not detect_dotnet():
            raise DotNetMissingError(
                "DCE requires .NET 8 runtime. "
                "Install from https://dotnet.microsoft.com/download/dotnet/8.0"
            )
        dce_path = get_dce_path()
        if dce_path is None:
            dce_path = await download_dce(on_event, skip_verify=config.skip_dce_verify)
        on_event(
            MigrationEvent(
                phase="export",
                status="progress",
                message="Launching DiscordChatExporter...",
            )
        )
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
    state.completed_at = datetime.now(UTC).isoformat()

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

    # S4: Post-migration invite (folds into REPORT; non-idempotent → guard on invite_code).
    if (
        config.create_invite
        and not config.dry_run
        and state.stoat_server_id
        and not state.invite_code
    ):
        await _generate_invite(config, state, exports, on_event)

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
            # current_phase may hold a terminal/post-pipeline value (e.g.
            # "validate_migration") that is not in PHASE_ORDER; treat any such
            # value as "all runnable phases complete" instead of crashing with
            # ValueError from .index().
            current_idx = (
                PHASE_ORDER.index(state.current_phase)
                if state.current_phase in PHASE_ORDER
                else len(PHASE_ORDER)
            )
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
            safe_exc = safe_sanitize(config.token_store, str(e))
            state.errors.append({"phase": phase_name, "type": "phase_failed", "error": safe_exc})
            save_state(state, config.output_dir)
            on_event(
                MigrationEvent(
                    phase=phase_name,
                    status="error",
                    message=f"Error in {phase_name}: {safe_exc}",
                    detail={"error": safe_exc},
                )
            )
            raise MigrationError(f"Phase {phase_name} failed: {safe_exc}") from e

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
            age = datetime.now(UTC).timestamp() - lock_ts
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
    ts = int(datetime.now(UTC).timestamp())
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
                existing_msg_id = state.forum_index_message_ids.get(forum_key)
                if existing_msg_id:
                    # Re-run: edit the existing index message instead of creating a duplicate.
                    await api_edit_message(
                        session,
                        config.stoat_url,
                        config.token,
                        index_channel_id,
                        existing_msg_id,
                        content=content,
                    )
                    index_msg_id: str = existing_msg_id
                else:
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
                    index_msg_id = msg_result["_id"]
                    state.forum_index_message_ids[forum_key] = index_msg_id
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


# ---------------------------------------------------------------------------
# Rollback engine (issue #10)
# ---------------------------------------------------------------------------


def _select_invite_channel(
    config: FerryConfig,
    state: MigrationState,
    exports: list[DCEExport],
) -> str | None:
    """Pick a Stoat Text channel to invite to.

    Excludes voice (Discord type 2), threads, and synthetic forum-index channels.
    Forums (15/16 → Stoat Text) are valid. Prefers ``config.invite_channel_id``
    when it maps to an eligible channel. Returns a Stoat channel id, or None.
    """

    def _eligible(discord_id: str, ch_type: int, is_thread: bool) -> str | None:
        if ch_type == 2 or is_thread or discord_id.startswith("forum-index-"):
            return None
        return state.channel_map.get(discord_id)

    # Preferred override.
    if config.invite_channel_id:
        for export in exports:
            if export.channel.id == config.invite_channel_id:
                chosen = _eligible(export.channel.id, export.channel.type, export.is_thread)
                if chosen:
                    return chosen

    # First eligible by deterministic channel.id order.
    for export in sorted(exports, key=lambda e: e.channel.id):
        chosen = _eligible(export.channel.id, export.channel.type, export.is_thread)
        if chosen:
            return chosen
    return None


async def _generate_invite(
    config: FerryConfig,
    state: MigrationState,
    exports: list[DCEExport],
    on_event: EventCallback,
) -> None:
    """Mint a post-migration invite and store it on state (non-fatal on error).

    The engine caller also guards on ``not state.invite_code``; this internal
    guard makes the helper idempotent on its own (non-idempotent API).
    """
    if state.invite_code:
        return
    channel_id = _select_invite_channel(config, state, exports)
    if channel_id is None:
        state.warnings.append(
            {
                "phase": "report",
                "type": "invite_no_channel",
                "message": "No eligible text channel for invite",
            }
        )
        return
    try:
        async with get_session(config) as session:
            result = await api_create_invite(session, config.stoat_url, config.token, channel_id)
            code = result.get("_id") or result.get("code") or ""
            state.invite_code = code
            # Best-effort URL: our own root GET (connect's discover parses only autumn).
            if code:
                with contextlib.suppress(Exception):
                    async with session.get(f"{config.stoat_url.rstrip('/')}/") as resp:
                        root = await resp.json() if resp.status == 200 else {}
                    app = root.get("app")
                    if isinstance(app, str) and app:
                        state.invite_url = f"{app.rstrip('/')}/invite/{code}"
    except Exception as exc:  # noqa: BLE001 — invite is non-fatal
        # Sanitize through the token store before persisting to state.json (security.md).
        message = safe_sanitize(config.token_store, f"Invite generation failed: {exc}")
        state.warnings.append(
            {
                "phase": "report",
                "type": "invite_failed",
                "message": message,
            }
        )


_HTTP_STATUS_RE = re.compile(r"(?:API error|after \d+ retries:)\s*(\d{3})")


def _parse_http_status(error_text: str) -> int | None:
    """Extract HTTP status from MigrationError text raised by _api_request.

    Patterns covered:
      - "API error 404: ..." (non-retryable single attempt)
      - "API request failed after 3 retries: 503 ..." (retryable exhausted)
      - "Network error after 3 retries: ..." → None (no status)
    """
    m = _HTTP_STATUS_RE.search(error_text)
    return int(m.group(1)) if m else None


def _validate_rollback_inputs(state: MigrationState, config: FerryConfig) -> str:
    """Validate that rollback has a server to act against.

    Returns the resolved server ID (preferring ``state.stoat_server_id``,
    falling back to ``config.server_id``).

    Raises:
        MigrationError: If neither is populated.
    """
    server_id = state.stoat_server_id or (config.server_id or "")
    if not server_id:
        raise MigrationError(
            "Cannot run rollback: no Stoat server ID recorded in state.json "
            "and no --server-id provided. Nothing to roll back."
        )
    return server_id


def _build_rollback_targets(
    state: MigrationState,
    server_obj: dict[str, Any],
) -> list[UntrackedSuspectChannel]:
    """Identify untracked-Ferry-suspect channels by diffing server vs state.

    Channels present on the Stoat server but absent from ``state.channel_map``
    are surfaced for per-item opt-in in the confirmation gate. Names are
    best-effort from the server response (Stoat's GET /servers/{id} returns
    a list of IDs, not channel objects, so names are usually empty —
    display layer falls back to the stoat_id).

    Args:
        state: Current MigrationState — NOT mutated by this function.
        server_obj: Result of ``api_fetch_server`` (fresh fetch).

    Returns:
        Sorted list of suspect channels (each with opted_in=False).
    """
    channel_names = _channel_names_from_server(server_obj)
    server_channel_ids: set[str] = set()
    for c in server_obj.get("channels", []):
        if isinstance(c, str):
            server_channel_ids.add(c)
        elif isinstance(c, dict):
            cid = c.get("_id") or c.get("id")
            if cid:
                server_channel_ids.add(cid)

    mapped_ids = set(state.channel_map.values())
    untracked_ids = server_channel_ids - mapped_ids

    suspects = [
        UntrackedSuspectChannel(
            stoat_id=cid,
            name=channel_names.get(cid, ""),
            created_at_iso=_decode_ulid_timestamp(cid),
        )
        for cid in untracked_ids
    ]
    # Deterministic ordering for stable CLI/GUI presentation + reproducible tests.
    suspects.sort(key=lambda s: (s.name, s.stoat_id))
    return suspects


async def _delete_one_channel(
    channel_id: str,
    sem: asyncio.BoundedSemaphore,
    config: FerryConfig,
    state: MigrationState,
    session: aiohttp.ClientSession,
    on_event: EventCallback,
    *,
    is_suspect: bool = False,
) -> None:
    """Delete one channel with bounded concurrency + per-entity DLQ on failure."""
    if config.cancel_event is not None and config.cancel_event.is_set():
        return
    async with sem:
        if config.cancel_event is not None and config.cancel_event.is_set():
            return
        assert state.rollback_progress is not None  # set by run_rollback before tasks
        try:
            await api_delete_channel(session, config.stoat_url, config.token, channel_id)
        except Exception as exc:  # noqa: BLE001 — DLQ catches everything, gather would swallow otherwise
            http_status = _parse_http_status(str(exc)) if isinstance(exc, MigrationError) else None
            state.rollback_progress.failures.append(
                RollbackFailure(
                    entity_type="channel",
                    stoat_id=channel_id,
                    error=str(exc),
                    http_status=http_status,
                )
            )
            save_state(state, config.output_dir)
            severity = "error" if http_status == 401 else "warning"
            msg = f"Failed to delete channel {channel_id}: {exc}"
            if http_status == 401:
                msg += " (session token may be invalid)"
            on_event(MigrationEvent(phase="rollback", status=severity, message=msg))
            return

        # Success path — includes 404 (idempotent) via expected_404_ok=True.
        if is_suspect:
            state.rollback_progress.untracked_channels_deleted += 1
        else:
            state.rollback_progress.channels_deleted += 1
        state.rollback_progress.rolled_back_ids.add(channel_id)
        save_state(state, config.output_dir)
        on_event(
            MigrationEvent(
                phase="rollback",
                status="progress",
                message=f"Deleted channel {channel_id}",
            )
        )


async def _delete_one_role(
    role_id: str,
    server_id: str,
    config: FerryConfig,
    state: MigrationState,
    session: aiohttp.ClientSession,
    on_event: EventCallback,
) -> None:
    """Delete one role serially with per-entity DLQ on failure."""
    if config.cancel_event is not None and config.cancel_event.is_set():
        return
    assert state.rollback_progress is not None
    try:
        await api_delete_role(session, config.stoat_url, config.token, server_id, role_id)
    except Exception as exc:  # noqa: BLE001 — DLQ catches everything to avoid aborting rollback
        http_status = _parse_http_status(str(exc)) if isinstance(exc, MigrationError) else None
        state.rollback_progress.failures.append(
            RollbackFailure(
                entity_type="role",
                stoat_id=role_id,
                error=str(exc),
                http_status=http_status,
            )
        )
        save_state(state, config.output_dir)
        severity = "error" if http_status == 401 else "warning"
        msg = f"Failed to delete role {role_id}: {exc}"
        if http_status == 401:
            msg += " (session token may be invalid)"
        on_event(MigrationEvent(phase="rollback", status=severity, message=msg))
        return

    state.rollback_progress.roles_deleted += 1
    state.rollback_progress.rolled_back_ids.add(role_id)
    save_state(state, config.output_dir)
    on_event(MigrationEvent(phase="rollback", status="progress", message=f"Deleted role {role_id}"))


async def _delete_one_emoji(
    emoji_id: str,
    config: FerryConfig,
    state: MigrationState,
    session: aiohttp.ClientSession,
    on_event: EventCallback,
) -> None:
    """Delete one custom emoji serially with per-entity DLQ on failure."""
    if config.cancel_event is not None and config.cancel_event.is_set():
        return
    assert state.rollback_progress is not None
    try:
        await api_delete_emoji(session, config.stoat_url, config.token, emoji_id)
    except Exception as exc:  # noqa: BLE001 — DLQ catches everything to avoid aborting rollback
        http_status = _parse_http_status(str(exc)) if isinstance(exc, MigrationError) else None
        state.rollback_progress.failures.append(
            RollbackFailure(
                entity_type="emoji",
                stoat_id=emoji_id,
                error=str(exc),
                http_status=http_status,
            )
        )
        save_state(state, config.output_dir)
        severity = "error" if http_status == 401 else "warning"
        msg = f"Failed to delete emoji {emoji_id}: {exc}"
        if http_status == 401:
            msg += " (session token may be invalid)"
        on_event(MigrationEvent(phase="rollback", status=severity, message=msg))
        return

    state.rollback_progress.emoji_deleted += 1
    state.rollback_progress.rolled_back_ids.add(emoji_id)
    save_state(state, config.output_dir)
    on_event(
        MigrationEvent(phase="rollback", status="progress", message=f"Deleted emoji {emoji_id}")
    )


async def _clean_categories(
    state: MigrationState,
    config: FerryConfig,
    server_id: str,
    session: aiohttp.ClientSession,
    on_event: EventCallback,
) -> None:
    """Final PATCH to remove Ferry-owned categories from the server.

    Re-fetches the server immediately before PATCH to minimise the TOCTOU
    window — last-write-wins on user edits during rollback is documented
    behaviour. If no Ferry categories were created, this is a no-op.
    """
    assert state.rollback_progress is not None
    if not state.category_map:
        state.rollback_progress.categories_cleaned = True
        save_state(state, config.output_dir)
        return

    ferry_cat_ids = set(state.category_map.values())
    try:
        server = await api_fetch_server(session, config.stoat_url, config.token, server_id)
    except MigrationError as exc:
        state.rollback_progress.failures.append(
            RollbackFailure(
                entity_type="category",
                stoat_id="<all>",
                error=f"Could not fetch server for category cleanup: {exc}",
                http_status=_parse_http_status(str(exc)),
            )
        )
        save_state(state, config.output_dir)
        on_event(
            MigrationEvent(
                phase="rollback",
                status="warning",
                message=f"Category cleanup skipped: {exc}",
            )
        )
        return

    current_categories = server.get("categories") or []
    remaining = [c for c in current_categories if c.get("id") not in ferry_cat_ids]

    try:
        await api_upsert_categories(session, config.stoat_url, config.token, server_id, remaining)
    except MigrationError as exc:
        state.rollback_progress.failures.append(
            RollbackFailure(
                entity_type="category",
                stoat_id="<all>",
                error=str(exc),
                http_status=_parse_http_status(str(exc)),
            )
        )
        save_state(state, config.output_dir)
        on_event(
            MigrationEvent(
                phase="rollback",
                status="warning",
                message=f"Category cleanup PATCH failed: {exc}",
            )
        )
        return

    state.rollback_progress.categories_cleaned = True
    save_state(state, config.output_dir)
    on_event(
        MigrationEvent(
            phase="rollback",
            status="progress",
            message=f"Cleaned {len(ferry_cat_ids)} Ferry-owned categories",
        )
    )


async def run_rollback(
    config: FerryConfig,
    state: MigrationState,
    exports: list[DCEExport],  # noqa: ARG001 — signature parity with run_migration
    on_event: EventCallback,
) -> MigrationState:
    """Reverse a recorded migration by deleting Ferry-created entities.

    Order: channels (parallel, bounded by ``config.max_concurrent_requests``)
    → roles (serial, shared /servers bucket) → emoji (serial, shared bucket)
    → category cleanup (single PATCH). Per-entity DLQ on failure; rollback
    never aborts on a single delete error.

    Idempotent: 404 responses are treated as "already deleted" and counted
    via ``expected_404_ok=True`` on the DELETE wrappers. State.json's entity
    maps (``channel_map`` / ``role_map`` / ``emoji_map``) are NEVER mutated
    — forensic preservation. Deletions are tracked in
    ``state.rollback_progress.rolled_back_ids``.

    Args:
        config: Ferry configuration. ``config.pause_event``, if provided, is
            cleared and awaited for the confirmation gate. ``config.cancel_event``
            is checked at task boundaries for cooperative cancellation.
        state: Current MigrationState. Must have ``stoat_server_id`` set, or
            ``config.server_id`` must be set instead.
        exports: Unused (signature parity with ``run_migration``).
        on_event: Event callback. Receives ``confirm_rollback`` event before
            any DELETE, and ``progress`` / ``warning`` / ``completed`` events
            during the run.

    Returns:
        Mutated ``state`` with ``rollback_progress`` populated.

    Raises:
        MigrationError: If neither state.stoat_server_id nor config.server_id
            is set, or if the migration lock cannot be acquired (a concurrent
            operation is in progress).
    """
    server_id = _validate_rollback_inputs(state, config)
    # Ensure the lock helpers use the resolved server ID.
    config.server_id = server_id

    if state.rollback_progress is None:
        state.rollback_progress = RollbackProgress(started_at=datetime.now(UTC).isoformat())

    on_event(
        MigrationEvent(
            phase="rollback",
            status="started",
            message=f"Starting rollback on server {server_id}",
        )
    )

    async with get_session(config) as session:
        config.session = session
        init_request_semaphore(config.max_concurrent_requests)

        # Lock gate — MUST run before the try, since _acquire_migration_lock
        # returns False on PATCH failure rather than raising. Wrapping inside
        # the try would let `finally` invoke _release_migration_lock on a lock
        # that was never written. The acquire helper does raise on live-lock
        # conflict — that raise propagates here, before the try is entered.
        lock_acquired = await _acquire_migration_lock(config, state, session, on_event)
        if not lock_acquired:
            raise MigrationError(
                "Could not acquire rollback lock — aborting to avoid concurrent "
                "operation. Pass --force-unlock if the existing lock is stale."
            )

        try:
            # Fetch server + build summary for confirmation gate.
            try:
                server_obj = await api_fetch_server(
                    session, config.stoat_url, config.token, server_id
                )
            except MigrationError as exc:
                on_event(
                    MigrationEvent(
                        phase="rollback",
                        status="error",
                        message=f"Could not fetch server {server_id}: {exc}",
                    )
                )
                raise

            untracked = _build_rollback_targets(state, server_obj)
            summary = build_rollback_summary(state, server_obj, untracked)

            # Clear the gate BEFORE emitting the event, not after. CLI handlers
            # may call ``pause_event.set()`` synchronously inside ``on_event``
            # (because ``click.confirm`` blocks the event loop until the user
            # responds, then sets the event before returning). Clearing after
            # would erase the user's approval and the wait-loop would hang
            # forever. GUI dialogs are non-blocking so the ordering doesn't
            # matter for them — but the CLI ordering is load-bearing.
            if config.pause_event is not None:
                config.pause_event.clear()

            on_event(
                MigrationEvent(
                    phase="rollback",
                    status="confirm_rollback",
                    message="Review rollback before proceeding",
                    detail={"summary": summary},
                )
            )

            # Wait for the shell to release the gate (CLI/GUI both honour pause_event).
            if config.pause_event is not None:
                while not config.pause_event.is_set():
                    if config.cancel_event is not None and config.cancel_event.is_set():
                        on_event(
                            MigrationEvent(
                                phase="rollback",
                                status="cancelled",
                                message="Rollback cancelled at confirmation gate",
                            )
                        )
                        return state
                    await asyncio.sleep(0.1)

            # Early cancel-check after the gate is released.
            if config.cancel_event is not None and config.cancel_event.is_set():
                on_event(
                    MigrationEvent(
                        phase="rollback",
                        status="cancelled",
                        message="Rollback cancelled before channel deletes",
                    )
                )
                return state

            # Channels — parallel with shared semaphore. Construct ONCE here
            # and pass by reference into each task. Constructing inside the
            # task would give every task its own semaphore at count=N and
            # defeat the limit.
            sem = asyncio.BoundedSemaphore(config.max_concurrent_requests)
            channel_tasks: list[Coroutine[Any, Any, None]] = []
            seen: set[str] = set()
            for stoat_id in state.channel_map.values():
                if stoat_id in seen:
                    continue
                seen.add(stoat_id)
                if stoat_id in state.rollback_progress.rolled_back_ids:
                    continue
                channel_tasks.append(
                    _delete_one_channel(stoat_id, sem, config, state, session, on_event)
                )
            # Opted-in untracked-Ferry-suspect channels.
            for suspect in untracked:
                if suspect.opted_in and suspect.stoat_id not in seen:
                    seen.add(suspect.stoat_id)
                    if suspect.stoat_id in state.rollback_progress.rolled_back_ids:
                        continue
                    channel_tasks.append(
                        _delete_one_channel(
                            suspect.stoat_id,
                            sem,
                            config,
                            state,
                            session,
                            on_event,
                            is_suspect=True,
                        )
                    )

            if channel_tasks:
                await asyncio.gather(*channel_tasks, return_exceptions=True)

            # Roles — serial (shared /servers 5/10s bucket).
            for stoat_id in state.role_map.values():
                if config.cancel_event is not None and config.cancel_event.is_set():
                    break
                if stoat_id in state.rollback_progress.rolled_back_ids:
                    continue
                await _delete_one_role(stoat_id, server_id, config, state, session, on_event)

            # Emoji — serial (shared /servers bucket).
            for stoat_id in state.emoji_map.values():
                if config.cancel_event is not None and config.cancel_event.is_set():
                    break
                if stoat_id in state.rollback_progress.rolled_back_ids:
                    continue
                await _delete_one_emoji(stoat_id, config, state, session, on_event)

            # Category cleanup — single PATCH.
            if not (config.cancel_event is not None and config.cancel_event.is_set()):
                await _clean_categories(state, config, server_id, session, on_event)

            # Final summary.
            state.rollback_progress.completed_at = datetime.now(UTC).isoformat()
            save_state(state, config.output_dir)
            final_status = (
                "completed_with_failures" if state.rollback_progress.failures else "completed"
            )
            on_event(
                MigrationEvent(
                    phase="rollback",
                    status=final_status,
                    message=(
                        f"Rollback finished: {state.rollback_progress.channels_deleted} channels, "
                        f"{state.rollback_progress.untracked_channels_deleted} suspects, "
                        f"{state.rollback_progress.roles_deleted} roles, "
                        f"{state.rollback_progress.emoji_deleted} emoji, "
                        f"{len(state.rollback_progress.failures)} failures"
                    ),
                    detail={"summary": state.rollback_progress},
                )
            )

        finally:
            if lock_acquired:
                await _release_migration_lock(config, state, session, on_event)

        config.session = None

    return state
