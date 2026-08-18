"""Migration orchestrator — shared by CLI and GUI."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import re
import socket
from collections.abc import Callable, Coroutine, Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiohttp  # noqa: TCH002

from discord_ferry.config import FerryConfig
from discord_ferry.core.events import EventCallback, MigrationEvent
from discord_ferry.core.http import format_proxy_notices, new_session
from discord_ferry.core.security import SecureTokenStore, register_secret, safe_sanitize
from discord_ferry.discord import (
    fetch_and_translate_guild_metadata,
    load_discord_metadata,
    save_discord_metadata,
)
from discord_ferry.errors import DotNetMissingError, DuplicateSendError, MigrationError
from discord_ferry.exporter import (
    detect_dotnet,
    download_dce,
    get_dce_path,
    run_dce_export,
    validate_discord_token,
)
from discord_ferry.migrator.api import (
    api_create_channel,
    api_create_invite,
    api_create_role,
    api_delete_channel,
    api_delete_emoji,
    api_delete_role,
    api_edit_channel,
    api_edit_message,
    api_edit_server,
    api_fetch_server,
    api_fetch_server_with_channels,
    api_pin_message,
    api_send_message,
    api_upsert_categories,
    get_session,
    init_request_semaphore,
)
from discord_ferry.migrator.avatars import run_avatars
from discord_ferry.migrator.connect import run_connect
from discord_ferry.migrator.emoji import run_emoji
from discord_ferry.migrator.messages import _THREAD_STRATEGIES, _process_message, run_messages
from discord_ferry.migrator.pins import run_pins
from discord_ferry.migrator.reactions import run_reactions
from discord_ferry.migrator.structure import (
    _generate_category_id,
    _stoat_channel_type,
    apply_channel_permissions,
    apply_role_permissions,
    make_unique_channel_name,
    run_categories,
    run_channels,
    run_roles,
    run_server,
)
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

if TYPE_CHECKING:
    from discord_ferry.discord.metadata import ChannelMeta

    # Type-only: run_check itself is imported inside run_repair, matching how
    # check_cmd reaches it, so the verify module stays off the engine's import
    # path at runtime.
    from discord_ferry.migrator.verify import CheckResult

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


def _safe(config: FerryConfig, text: str) -> str:
    """Redact any known token values from *text* before it is emitted or persisted.

    Thin wrapper around :func:`safe_sanitize` (None-safe) so every engine event or
    warning that interpolates a raw exception ``repr`` honors safe_sanitize's
    persist-or-emit contract.
    """
    return safe_sanitize(config.token_store, text)


def _ensure_token_store(config: FerryConfig) -> None:
    """Populate ``config.token_store`` from the configured tokens if not already set.

    Both :func:`run_migration` and :func:`run_rollback` call this so that every error
    message emitted/persisted during either flow is token-redacted via :func:`_safe`.
    ``run_rollback`` is invoked directly by the CLI/GUI shells, which never set the
    store — without this the rollback-path ``_safe`` calls would be inert.
    """
    if config.token_store is not None:
        return
    tokens: dict[str, str] = {"stoat": config.token}
    if config.discord_token:
        tokens["discord"] = config.discord_token
    config.token_store = SecureTokenStore(tokens)
    # Also register process-wide so the log redaction filter can mask these.
    # The filter is installed in main(), long before any config exists, so it
    # has no way to reach this per-config store. See core/logging_setup.py.
    for name, value in tokens.items():
        register_secret(name, value)


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

    # Ensure error messages emitted/persisted are token-redacted at output boundaries.
    _ensure_token_store(config)

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
            if prior.is_dry_run:
                # A dry run fills message_map with `dry-msg-<id>` sentinels for EVERY
                # message, threads included, in the dry-run branch of run_messages, and
                # persists them. The carry-over below copies that map forward verbatim,
                # so reply targets resolve against ids that were never sent, and the
                # merge duplicate suppression in _merge_threads (batch 8, #110) reads the
                # same map and would skip every merged thread message as "already
                # delivered".
                #
                # --resume has refused a dry-run state since it was written, four lines
                # above. This is the sibling mode, and it never did.
                raise MigrationError(
                    f"Cannot run --incremental against a dry-run state ({state_path}). "
                    "A dry run records placeholder message ids, so continuing from it "
                    "would corrupt reply targets. Delete that file, or drop "
                    "--incremental to start a fresh migration."
                )
            state = MigrationState()
            state.started_at = datetime.now(UTC).isoformat()
            state.is_dry_run = config.dry_run
            # Carry over ID maps so structure phases are skipped / reused
            state.channel_map = dict(prior.channel_map)
            state.role_map = dict(prior.role_map)
            # Beside their id maps, and mandatory rather than tidy. A carried
            # channel skips creation entirely (run_channels snapshots
            # pre_existing_channel_ids before its create loop), and roles do the
            # same, so nothing re-records a name on an incremental run. Without
            # these two lines every incremental run ends with an empty name map
            # and ferry check reports ok for a renamed channel.
            state.created_channel_names = dict(prior.created_channel_names)
            state.created_role_names = dict(prior.created_role_names)
            # C1: carry finalized-roles so incremental skips re-editing (load_state seeds `prior`
            # for old-ferry states). Without this every incremental run re-PATCHes all roles.
            state.roles_finalized = set(prior.roles_finalized)
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
            state.channel_high_water = dict(prior.channel_high_water)
            # #76: carry failed messages forward (as independent copies) so an
            # incremental run self-heals prior failures instead of wiping the record.
            state.failed_messages = [dataclasses.replace(fm) for fm in prior.failed_messages]
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
            #     created_channel_names / created_role_names (a carried entity
            #     skips creation, so nothing re-records its name; omitting these
            #     makes ferry check report ok for a renamed channel),
            #     category_names, channel_categories, message_map, avatar_cache,
            #     upload_cache, author_names, stoat_server_id, autumn_url,
            #     invite_code / invite_url, channel_message_offsets,
            #     channel_high_water (durable per-channel high-water mark for
            #     --incremental delta skipping),
            #     failed_messages (so incremental self-heals prior failures — #76),
            #     channel_message_counts, forum_channel_members /
            #     forum_category_names / forum_index_message_ids,
            #     native_fidelity_counts (cumulative fidelity counter), and the
            #     cumulative counters (attachments_uploaded/skipped,
            #     reactions_applied, pins_applied). prior_messages_total is DERIVED
            #     here from len(prior.message_map).
            #   RESET each run: completed_channel_ids (re-enter every channel for
            #     new msgs); pending_pins / pending_reactions (consumed by the
            #     reactions/pins phases — carrying a stale list would re-pin/re-react);
            #     warnings / errors, validation_results, embeds_* / replies_*
            #     (per-run fidelity counters); current_phase, started_at,
            #     completed_at, export_completed, rollback_progress, is_dry_run;
            #     thread_strategy, which describes THIS run and is set from
            #     config after this block, at the point the four state paths
            #     converge.
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

    # Set AFTER the four state paths converge, deliberately, and not inside any
    # of them. --resume loads, --incremental either carries from a prior state or
    # constructs fresh, and neither flag constructs fresh. A fifth path must also
    # reach here to run the phases, so it inherits this without anyone having to
    # remember. Measured rather than assumed: moving this line beside
    # `state = MigrationState()` kills four of the five tests covering it and
    # leaves the fresh-run test passing, which is what makes that placement look
    # correct to whoever writes it.
    #
    # This records the strategy of the MOST RECENT run. A resume under a
    # different --thread-strategy than the original therefore describes the
    # resuming run while most of the content was migrated under the first, which
    # is why verify.py words its detail as the recorded strategy rather than as
    # the strategy every message in the channel was migrated under.
    #
    # The EFFECTIVE strategy, not the requested one. run_messages falls back to
    # "flatten" for any value outside _THREAD_STRATEGIES, so recording
    # config.thread_strategy raw would let state.json name a strategy the run
    # never used, and ferry check would then report that as the cause. --resume
    # and the CLI cannot reach it, because click.Choice validates there, but the
    # GUI reads its value back from a storage file it does not re-validate and a
    # programmatic FerryConfig is unconstrained. Found by a chunk review.
    state.thread_strategy = (
        config.thread_strategy if config.thread_strategy in _THREAD_STRATEGIES else "flatten"
    )

    # Preflight: surface any proxy configuration Ferry found but cannot use.
    # core/http.py returns data and never emits; the engine decides. Emitted at
    # "notice" rather than "warning" because cli.py gates warnings behind
    # --verbose, which defaults off.
    for line in format_proxy_notices():
        on_event(MigrationEvent(phase="preflight", status="notice", message=line))
        state.warnings.append({"phase": "preflight", "type": "proxy_notice", "message": line})

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
                async with new_session() as discord_session:
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
                        "message": _safe(
                            config,
                            f"Could not fetch Discord metadata: {exc}. "
                            "Permissions will not be migrated.",
                        ),
                    }
                )
                on_event(
                    MigrationEvent(
                        phase="export",
                        status="warning",
                        message=_safe(
                            config, f"Discord metadata fetch failed: {exc}. Permissions skipped."
                        ),
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

    # Persist the post-filter source message total so stats.summarize_state (state-only)
    # and reporter.generate_report share ONE denominator. Set unconditionally — the
    # messages phase is skipped on resume, so this must not be gated on it.
    state.source_messages_total = sum(e.message_count for e in exports)

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

    async with new_session() as session:
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
            async with new_session() as validation_session:
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
                    message=_safe(config, f"Validation skipped: {exc}"),
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
                message=_safe(config, f"Could not fetch server for lock check: {exc}"),
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
        # S1: stash the marker so the SERVER phase's description PATCH preserves it (transient).
        state.migration_lock_marker = lock_marker
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
                message=_safe(config, f"Could not acquire migration lock: {exc}"),
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
        state.migration_lock_marker = ""
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
                message=_safe(config, f"Could not release migration lock: {exc}"),
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
                        index_msg_id = msg_result["_id"]
                    except DuplicateSendError:
                        # Already on the server with no recoverable id. Write NOTHING to
                        # forum_index_message_ids: an entry there persists into
                        # state.json and drives an api_edit_message against an id that
                        # does not exist on the next run. Letting the broad handler take
                        # this instead would report a rebuild failure that did not
                        # happen, which is the same defect as the FailedMessage on the
                        # message path.
                        index_msg_id = ""
                    await asyncio.sleep(config.upload_delay)
                    if index_msg_id:
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
                        "message": _safe(
                            config, f"Failed to rebuild forum index for '{forum_name}': {exc}"
                        ),
                    }
                )
                on_event(
                    MigrationEvent(
                        phase="report",
                        status="warning",
                        message=_safe(
                            config, f"Forum index rebuild for '{forum_name}' failed: {exc}"
                        ),
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
    # Same reason run_rollback calls it: this coroutine is invoked directly by a
    # shell, which never sets the store, so without this every safe_sanitize on
    # the retry path is an identity no-op and a Stoat token in an exception
    # reaches state.failed_messages, and through it report.json, unredacted.
    # Before the early return, so the store exists on every path out.
    _ensure_token_store(config)

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
        # Batch 3 (S1): iterate a snapshot — _process_message (channel_result=None) appends
        # a FailedMessage to state.failed_messages on a re-fail, so a live-list loop would
        # never terminate (mutate-during-iteration). state.failed_messages is rebuilt from
        # still_failed below, discarding the in-loop duplicate.
        for fm in list(state.failed_messages):
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
# Repair engine (#107 batch 10)
# ---------------------------------------------------------------------------

#: The only structure kinds repair acts on.
#:
#: Membership against a literal set, NEVER a test on ``status``. A ``fail`` kind
#: added after this batch would be swept into a status test silently, and repair
#: has no way to handle a defect it has not seen.
_REPAIRABLE_STRUCTURE = frozenset({"channel_missing", "role_missing", "category_missing"})

#: The only tail kinds repair acts on. ``tail_not_recorded`` and
#: ``tail_window_exhausted`` are unverifiable: the check could not look, so
#: there is nothing to act on and acting anyway is guessing at a live server.
_REPAIRABLE_TAIL = frozenset({"tail_absent", "tail_and_after_absent"})

#: A synthetic channel key repair declines even when the kind matches.
#:
#: The forum index writer stores ``channel_map["forum-index-{key}"]`` whose
#: value is a real Stoat channel id, so a deleted index reports
#: ``channel_missing`` exactly like any other channel. Generic recreation cannot
#: restore it: there is no ``ChannelMeta`` for a synthetic id, a channel-scoped
#: export scan finds zero messages because the key names no Discord channel, and
#: nothing rebuilds the index message or ``forum_index_message_ids``.
#: ``_select_invite_channel`` already excludes the same prefix. Deferred as #311.
#:
#: This is the second level of the rule above: a test on ``status`` sweeps in a
#: future KIND, and a test on ``kind`` alone sweeps in a channel TYPE.
_UNREPAIRABLE_CHANNEL_PREFIX = "forum-index-"


async def _live_server_view(
    session: aiohttp.ClientSession,
    config: FerryConfig,
    state: MigrationState,
) -> tuple[set[str], list[dict[str, Any]]]:
    """One fetch, two things repair needs: the collision set and the categories.

    Returns ``(channel_names, categories)``. Both come from the same response
    because both are needed only when something is being recreated, and the
    /servers bucket allows 5 requests per 10 seconds.

    The categories half exists because ``api_upsert_categories`` is a FULL-ARRAY
    PATCH: it sets the server's entire categories list. Recreating one missing
    category by sending only that category would delete every other one. So the
    current array has to come back first, and a test drives two survivors.

    ``server.categories`` is Optional upstream and the key can be absent
    entirely, which is why it is read through ``or []`` rather than indexed.

    The channel-names half, for the collision set:

    Repair names a recreated channel with ``make_unique_channel_name``, and what
    differs from a migration is not the rule but its INPUT. ``run_channels``
    builds its set across the whole export, because on a migration that is every
    name about to exist. On a repair the correct set is what is on the server
    NOW: a channel originally created as ``general-1``, whose collision partner
    has since been deleted, should come back as ``general``.

    Costs ONE request, on the ``/servers`` bucket at 5 per 10 seconds. It cannot
    be taken from the ``CheckReport``, which is why repair pays for it
    separately: ``visible_names`` is local to ``_check_structure`` and reaches a
    ``CheckResult`` only through ``expected``/``found`` on a rename comparison,
    so the far more common unchanged-name case carries no name at all.

    Known limit, shared with ``ferry check`` itself and not introduced here: the
    sibling ``channels`` array holds only what this token may ViewChannel, so a
    channel it cannot see is absent from this set and a recreation could collide
    with one. That is the same blindness ``channel_not_visible`` documents.
    """
    payload = await api_fetch_server_with_channels(
        session, config.stoat_url, config.token, state.stoat_server_id
    )
    names: set[str] = set()
    for channel in payload.get("channels") or []:
        # isinstance rather than a bare .get: the payload is dict[str, Any], so
        # mypy cannot help, and an entry Ferry cannot read must be skipped rather
        # than contribute None to a set[str]. verify.py guards the same way.
        if isinstance(channel, dict):
            name = channel.get("name")
            if isinstance(name, str):
                names.add(name)
    server = payload.get("server") or {}
    categories = [c for c in (server.get("categories") or []) if isinstance(c, dict)]
    return names, categories


def _channel_message_ids(export: DCEExport) -> list[str]:
    """Every Discord message id this export holds, in export order.

    NEW work, not a reuse of the scan in ``run_retry_failed``. That one filters
    on ``msg.id in failed_ids``, a MESSAGE predicate seeded from failures Ferry
    already knows about. Repair needs a CHANNEL predicate, and nothing in
    ``MigrationState`` can answer "which ``message_map`` entries belonged to
    channel X": the map is keyed by message id with no channel anywhere in it.
    The export is the only source.

    Streams when a path is known, for the same reason ``run_retry_failed``
    streams: a channel export can be large and there is no need to hold it.
    """
    if export.json_path is not None:
        return [msg.id for msg in stream_messages(export.json_path)]
    return [msg.id for msg in export.messages]


def _clear_channel_state(
    state: MigrationState,
    discord_id: str,
    message_ids: list[str],
    old_stoat_id: str | None = None,
    new_stoat_id: str | None = None,
) -> None:
    """Drop everything that described the channel that is now gone.

    Every per-channel field is keyed by DISCORD id, so only one map VALUE moved
    (``channel_map``, written by the caller) and no key changes here.

    ``channel_message_counts`` is the one that is easy to think unnecessary and
    is not. ``_classify_tail``'s "nothing expected" shortcut requires the count
    to be zero AND no expected id. Leaving a stale non-zero count while the
    high-water mark is gone falls past that shortcut into ``channel_empty``,
    which is a ``fail``: a repair that did everything it could would report as a
    failure. It is observable ONLY when the resend puts nothing back, because a
    successful resend repopulates it, which is why the test for it drives an
    export holding nothing for this channel.

    ``channel_message_offsets`` is cleared for a different reason and no check
    can see it: nothing in ``_classify_tail`` or ``_expected_tail`` reads that
    map. It is transient within-run resume state for a channel that no longer
    exists, so it serves a later ``--resume`` rather than the next check. Not
    dead, just unreachable by that predicate.

    The ``message_map`` entries name messages deleted along with the channel.
    They come from a channel-scoped scan of the export because no reverse index
    from a channel to its messages exists anywhere in the state.
    """
    state.channel_high_water.pop(discord_id, None)
    state.channel_message_counts.pop(discord_id, None)
    state.channel_message_offsets.pop(discord_id, None)
    state.completed_channel_ids.discard(discord_id)
    for message_id in message_ids:
        state.message_map.pop(message_id, None)

    # A queued FAILURE, on the other hand, is still worth sending: it just has to
    # go to the channel that now exists. FailedMessage.stoat_channel_id was
    # recorded at migration time against the id that has since been deleted, and
    # run_retry_failed sends to exactly that field, so without this remap the
    # drain posts every backlogged message of a recreated channel to a dead id
    # and collects a 404. The repair would recreate the channel, restore its
    # export, and still never clear its backlog.
    if old_stoat_id and new_stoat_id:
        for failed in state.failed_messages:
            if failed.stoat_channel_id == old_stoat_id:
                failed.stoat_channel_id = new_stoat_id

    # Queued pins and reactions naming the deleted channel can never succeed.
    # Both lists survive a finished migration when their phase left failures
    # behind: run_pins ends with `state.pending_pins = remaining`, which keeps
    # exactly the ones that did not land, and run_reactions does the same. A
    # later --incremental would retry them against a channel that is gone and
    # collect another failure.
    #
    # Repair does not make that worse, the deletion did, so this is a tidy-up
    # rather than a fix. It is here because this function's job is dropping
    # everything that described the channel that is gone, and these do.
    if old_stoat_id:
        state.pending_pins = [p for p in state.pending_pins if p[0] != old_stoat_id]
        state.pending_reactions = [
            r for r in state.pending_reactions if r.get("channel_id") != old_stoat_id
        ]


def _ordered_messages(export: DCEExport) -> Iterator[DCEMessage]:
    """This export's messages in the order the migration sent them.

    Mirrors ``_process_single_channel`` exactly: stream from disk when a path is
    known, otherwise sort the in-memory list by timestamp. The sort is not
    cosmetic. Stoat orders by its own ULIDs, assigned on arrival, so sending out
    of order puts the channel out of order permanently, and the tail Ferry then
    records would not be the newest message.
    """
    if export.json_path is not None:
        return stream_messages(export.json_path)
    return iter(sorted(export.messages, key=lambda m: m.timestamp))


def _warn_unrestored_merge_threads(
    state: MigrationState,
    export: DCEExport,
    exports: list[DCEExport],
    on_event: EventCallback,
) -> None:
    """Name the merged thread content a recreated parent does not get back.

    Under ``--thread-strategy=merge`` a thread's messages were appended to the
    PARENT's Stoat channel, and ``_merge_threads`` resolves that target by parent
    channel NAME. A channel-scoped scan keyed on the parent's Discord id
    therefore cannot reach them, and the merge path never wrote ``message_map``
    either, so nothing in the state points at them.

    The gap is stated rather than silent, which is the whole difference between
    this and the defect a critique round found. Deferred as #310.
    """
    orphans = [
        e.channel.name
        for e in exports
        if e.is_thread and e.parent_channel_name == export.channel.name
    ]
    if not orphans:
        return
    names = ", ".join(sorted(orphans))
    message = (
        f"Channel '{export.channel.name}' was migrated with --thread-strategy=merge, so "
        f"{len(orphans)} thread(s) had their messages appended to it: {names}. "
        "Repair restored the channel's own messages only. The merged thread content is "
        "reachable by parent channel name rather than by id, so a channel-scoped resend "
        "cannot find it (see issue #310)."
    )
    state.warnings.append(
        {"phase": "repair", "type": "merge_thread_content_not_restored", "message": message}
    )
    on_event(MigrationEvent(phase="repair", status="warning", message=message))


async def _resend_channel(
    session: aiohttp.ClientSession,
    config: FerryConfig,
    state: MigrationState,
    export: DCEExport,
    on_event: EventCallback,
) -> int:
    """Re-send every message this export holds. Returns the count that landed.

    The origin header is sent here rather than left to ``_process_message``,
    because it does not live there: ``_process_single_channel`` sends it, gated
    on the channel being absent from ``channel_high_water``. Repair clears that
    mark, so a loop over ``_process_message`` alone would drop a header the
    original migration sent and the restored thread would lose the line saying
    where it came from.
    """
    new_channel_id = state.channel_map[export.channel.id]
    if export.is_thread and export.parent_channel_name:
        header = (
            f"[Forum post migrated from #{export.parent_channel_name}]"
            if export.channel.type in (15, 16)
            else f"[Thread migrated from #{export.parent_channel_name}]"
        )
        with contextlib.suppress(DuplicateSendError):
            await api_send_message(
                session,
                config.stoat_url,
                config.token,
                new_channel_id,
                content=header,
                masquerade={"name": "Discord Ferry"},
                # Salted with the NEW channel id for the same reason the message
                # keys are: the old key may still be in Stoat's LRU, and a 409
                # there would silently drop the header from a channel that is
                # genuinely new.
                idempotency_key=f"ferry-header-{export.channel.id}-{new_channel_id}",
            )

    # The mark is the max over EVERY id this export holds, not over the ones
    # that landed, and taking the easier reading gives a NICER answer that is
    # wrong. If the newest message fails to send, "max of successful" names the
    # second-newest, which did land, so the next check finds it and reports
    # ("ok", "tail_present") over a message that is genuinely missing. Taking
    # the max over everything reports ("unverifiable", "tail_not_recorded"),
    # which is honest: Ferry cannot confirm a tail it never sent.
    #
    # It matches the phase, where _channel_max_id advances before each send and
    # is not rolled back on failure. That is safe rather than a permanent hole
    # only because a failed send lands in state.failed_messages and the #76
    # self-heal re-attempts any id found there even below the mark.
    #
    # isdigit(): real Discord ids are numeric snowflakes, but a system message
    # can carry something else, and int() on it would abort the whole repair.
    sent = 0
    highest = 0
    succeeded: set[str] = set()
    for msg in _ordered_messages(export):
        # Tracked HERE rather than in a second pass over the export. The send
        # loop already visits every message, and for a streamed channel a
        # separate pass would double the file reads. Materialising the messages
        # to avoid that is not an option: streaming is what keeps a large export
        # from exhausting memory, which is the OutOfMemoryException DCE 2.47.2
        # fixed upstream.
        #
        # Advanced BEFORE the send and not rolled back on failure, so it covers a
        # message that did not land. That is the whole point of the formula.
        if msg.id.isdigit():
            highest = max(highest, int(msg.id))
        try:
            await _process_message(
                msg=msg,
                stoat_channel_id=new_channel_id,
                config=config,
                state=state,
                session=session,
                on_event=on_event,
                export_channel_id=export.channel.id,
                idempotency_salt=new_channel_id,
            )
            sent += 1
            succeeded.add(msg.id)
        except Exception:  # noqa: BLE001
            # _process_message appends a FailedMessage and re-raises when
            # channel_result is None, so the failure is already durable. Carrying
            # on is deliberate: one bad message must not cost the rest of the
            # channel, and `ferry retry` is what drains what is left.
            continue

    # _process_message does NOT write this; only the phase loops do. Without it
    # a recreated channel reports tail_not_recorded, which is `unverifiable`
    # rather than `ok`, and the repair looks unfinished when it is not.
    if highest:
        state.channel_high_water[export.channel.id] = str(highest)

    # Anything this resend delivered is no longer a failure, and leaving it
    # queued would make the drain send it a SECOND time. The two paths do not
    # protect each other: this one salts its idempotency key with the new
    # channel id and run_retry_failed does not, so Stoat sees two distinct
    # nonces and accepts both. _merge_threads reconciles the same way and for
    # the same reason, dropping from the queue whatever succeeded this run.
    if succeeded:
        state.failed_messages = [
            fm for fm in state.failed_messages if fm.discord_msg_id not in succeeded
        ]
    return sent


async def _repair_tail(
    session: aiohttp.ClientSession,
    config: FerryConfig,
    state: MigrationState,
    result: CheckResult,
    exports: list[DCEExport],
    on_event: EventCallback,
) -> bool:
    """Re-send the one message a present channel lost. True when it was sent.

    Both repairable tail kinds resolve to the same work. ``tail_absent`` means
    the recorded last message is gone while messages around it survive;
    ``tail_and_after_absent`` means everything from it onward is gone, and since
    the mark IS the newest thing Ferry recorded, there is nothing after it in
    Ferry's record either. One message, in both cases.

    This deliberately goes around the high-water gate. Content at or below the
    mark is unreachable by ``--resume``, ``--incremental`` and ``ferry retry``
    alike, which ``docs/guides/earlier-migrations.md`` documents, and reaching it
    is the point of this command rather than an oversight.
    """
    discord_id = result.discord_id or ""
    tail_message_id = state.channel_high_water.get(discord_id)
    if not tail_message_id:
        # The check cannot report an absent tail without one, so this is
        # unreachable today. Guarded anyway: a future kind arriving here with no
        # mark would otherwise send nothing and report success.
        return False

    export = next((e for e in exports if e.channel.id == discord_id), None)
    if export is None:
        message = (
            f"Cannot restore the last message of channel {discord_id}: it is not in the "
            "export directory given. Re-run with the export the migration used."
        )
        state.warnings.append({"phase": "repair", "type": "not_in_export", "message": message})
        on_event(MigrationEvent(phase="repair", status="warning", message=message))
        return False

    target = next((m for m in _ordered_messages(export) if m.id == tail_message_id), None)
    if target is None:
        message = (
            f"Cannot restore message {tail_message_id} in channel {discord_id}: the export "
            "given does not contain it. It may be narrower than the one the migration used."
        )
        state.warnings.append({"phase": "repair", "type": "not_in_export", "message": message})
        on_event(MigrationEvent(phase="repair", status="warning", message=message))
        return False

    stoat_channel_id = state.channel_map[discord_id]
    try:
        await _process_message(
            msg=target,
            stoat_channel_id=stoat_channel_id,
            config=config,
            state=state,
            session=session,
            on_event=on_event,
            export_channel_id=discord_id,
            # Salted for the same reason a recreation's sends are, and the need
            # is sharper here: this channel was NOT recreated, so its Stoat id is
            # unchanged and the original send's key is exactly `ferry-{msg.id}`.
            # Unsalted, Stoat's LRU would answer 409 and _process_message would
            # treat a message the server has lost as already delivered.
            idempotency_salt=stoat_channel_id,
        )
    except Exception:  # noqa: BLE001
        # Already durable in state.failed_messages, and re-raised by
        # _process_message when channel_result is None. `ferry retry` drains it.
        return False

    # The mark needs NO update, and the reason is worth stating because the plan
    # asked for a formula here. A tail repair re-sends the message the mark
    # already names, because `tail_message_id` is read out of
    # `channel_high_water` at the top of this function and nothing between here
    # and there writes it: `_process_message` never touches that map, only the
    # phase loops do. So any "greater of the two" comparison compares a value
    # with itself and can never fire.
    #
    # An earlier draft had exactly that comparison. A mutant replacing it with an
    # unconditional write SURVIVED, which is what showed it was inert rather than
    # protective. The recreation path in `_resend_channel` is where a real
    # formula is needed, because there the mark was cleared first.
    on_event(
        MigrationEvent(
            phase="repair",
            status="progress",
            message=f"Restored the last message of channel {discord_id}.",
        )
    )
    return True


def _no_recorded_name(state: MigrationState, kind: str, discord_id: str) -> str:
    """Record why an entity could not be recreated, and return the message.

    The name to recreate under is the one Ferry SENT, which lives in
    ``created_channel_names`` or ``created_role_names``. A migration run before
    2.17.0 recorded neither, and nothing in the state can reconstruct them: the
    Discord name is not there, and even with the export in hand the collision
    suffixes depended on the order that run happened to process channels in.

    So repair declines rather than inventing a name. A recreated entity under a
    different name is worse than an absent one: the operator sees something that
    looks restored and is not, and ``ferry check`` would then report it renamed
    forever after.
    """
    message = (
        f"Cannot recreate {kind} {discord_id}: Ferry has no record of the name it gave it. "
        "Migrations run before 2.17.0 did not record created names, and the name cannot be "
        "reconstructed from this state. Recreate it by hand, or re-run the migration with "
        "--incremental."
    )
    state.warnings.append({"phase": "repair", "type": "no_recorded_name", "message": message})
    return message


async def _recreate_role(
    session: aiohttp.ClientSession,
    config: FerryConfig,
    state: MigrationState,
    result: CheckResult,
    on_event: EventCallback,
) -> bool:
    """Recreate one missing role. Returns True when it was created."""
    discord_id = result.discord_id or ""
    recorded = state.created_role_names.get(discord_id)
    if not recorded:
        on_event(
            MigrationEvent(
                phase="repair",
                status="warning",
                message=_no_recorded_name(state, "role", discord_id),
            )
        )
        return False

    # api_create_role answers with `id`; api_create_channel answers with `_id`.
    # The five create routes disagree by design and a pass that "makes these
    # consistent" breaks two of the three.
    created = await api_create_role(
        session, config.stoat_url, config.token, state.stoat_server_id, recorded
    )
    new_id: str = created["id"]
    state.role_map[discord_id] = new_id
    # From the RESPONSE, which is what the server actually stored, not from the
    # local variable. 2.17.0's rule, and the reason it exists is that the two
    # differ exactly when it matters.
    state.created_role_names[discord_id] = created.get("name") or recorded
    on_event(
        MigrationEvent(
            phase="repair",
            status="progress",
            message=f"Recreated role '{state.created_role_names[discord_id]}' as {new_id}.",
        )
    )
    return True


async def _recreate_channel(
    session: aiohttp.ClientSession,
    config: FerryConfig,
    state: MigrationState,
    result: CheckResult,
    exports: list[DCEExport],
    existing_names: set[str],
    on_event: EventCallback,
) -> bool:
    """Recreate one missing channel. Returns True when it was created."""
    discord_id = result.discord_id or ""
    recorded = state.created_channel_names.get(discord_id)
    if not recorded:
        on_event(
            MigrationEvent(
                phase="repair",
                status="warning",
                message=_no_recorded_name(state, "channel", discord_id),
            )
        )
        return False

    # The channel TYPE lives only in the export. Nothing in MigrationState
    # records it, and a voice channel restored as text is a different channel:
    # nobody can join it. So a channel the given export does not describe is
    # declined rather than guessed at as Text.
    export = next((e for e in exports if e.channel.id == discord_id), None)
    if export is None:
        message = (
            f"Cannot recreate channel {discord_id}: it is not in the export directory given. "
            "The channel type is recorded only in the export, and restoring a voice channel "
            "as text would produce a channel nobody can join. Re-run with the export the "
            "migration used."
        )
        state.warnings.append({"phase": "repair", "type": "not_in_export", "message": message})
        on_event(MigrationEvent(phase="repair", status="warning", message=message))
        return False

    # The recorded name is what Ferry SENT, so it is already truncated to 32 and
    # already carries whatever suffix the migration needed. Passing it through
    # make_unique_channel_name against the LIVE set is what lets a suffix be
    # dropped when its collision partner is gone, and forces a new one when the
    # server has taken the name since.
    unique_name = make_unique_channel_name(recorded, existing_names)
    created = await api_create_channel(
        session,
        config.stoat_url,
        config.token,
        state.stoat_server_id,
        name=unique_name,
        channel_type=_stoat_channel_type(export.channel.type),
        description=export.channel.topic or None,
    )
    # `_id` here, `id` for roles. Upstream's spelling, not ours.
    new_id: str = created["_id"]
    state.channel_map[discord_id] = new_id
    state.created_channel_names[discord_id] = created.get("name") or unique_name
    # result.stoat_id is the id the check found missing, so it is the OLD one:
    # channel_map was overwritten a line above and no longer holds it.
    _clear_channel_state(state, discord_id, _channel_message_ids(export), result.stoat_id, new_id)
    on_event(
        MigrationEvent(
            phase="repair",
            status="progress",
            message=f"Recreated channel '{state.created_channel_names[discord_id]}' as {new_id}.",
        )
    )
    return True


async def _apply_channel_attributes(
    session: aiohttp.ClientSession,
    config: FerryConfig,
    state: MigrationState,
    stoat_channel_id: str,
    ch_meta: ChannelMeta | None,
    label: str,
) -> None:
    """Restore a recreated channel's slowmode and voice user limit.

    ``run_channels`` sets these with an ``api_edit_channel`` call after the
    permission pass. A recreation that skipped it would come back with slowmode
    off and a voice channel uncapped, which is a silent change to how the
    channel behaves rather than to how it looks.
    """
    if ch_meta is None or config.dry_run:
        return
    edits: dict[str, Any] = {}
    if ch_meta.slowmode > 0:
        edits["slowmode"] = min(ch_meta.slowmode, 21600)
    if ch_meta.user_limit > 0:
        edits["user_limit"] = ch_meta.user_limit
    if not edits:
        return
    try:
        await api_edit_channel(session, config.stoat_url, config.token, stoat_channel_id, **edits)
    except Exception as exc:  # noqa: BLE001
        state.warnings.append(
            {
                "phase": "repair",
                "type": "channel_attributes_failed",
                "message": f"Could not restore slowmode or user limit for '{label}': {exc}",
            }
        )


async def _reattach_to_category(
    session: aiohttp.ClientSession,
    config: FerryConfig,
    state: MigrationState,
    discord_id: str,
    new_channel_id: str,
    old_channel_id: str | None,
    live_categories: list[dict[str, Any]],
    on_event: EventCallback,
) -> None:
    """Put a recreated channel back into the category it belonged to.

    Category membership on Stoat lives ONLY in the server's categories array, so
    a channel created with ``api_create_channel`` sits outside every category
    until that array names it. ``run_channels`` handles this with an end-of-phase
    upsert; a recreation has no such phase behind it.

    Without this a repaired channel comes back bare, outside the category it was
    in, permanently and with nothing said about it. That is a visible structural
    change the operator did not ask for, and it is the primary path this release
    exists to serve.

    Mutates ``live_categories`` in place so a second recreation in the same run
    sees the first one's placement rather than overwriting it.
    """
    discord_category_id = state.channel_categories.get(discord_id)
    if not discord_category_id:
        # Redundant with the next guard on its own, and kept deliberately:
        # `category_map.get(None)` would also return None, so a mutant removing
        # this line SURVIVES. It stays because it names a distinct case, a
        # channel that belonged to no category, which the next line does not.
        return
    stoat_category_id = state.category_map.get(discord_category_id)
    if not stoat_category_id:
        return

    target = next((c for c in live_categories if c.get("id") == stoat_category_id), None)
    if target is None:
        # The category is gone too. _recreate_category rebuilds its channel list
        # from channel_categories and channel_map, and channel_map now holds the
        # new id, so it will pick this channel up. Nothing to do here.
        return
    channels = target.get("channels")
    if not isinstance(channels, list):
        return
    # Drop the dead id while adding the live one. Both can be present: if the
    # CATEGORY was recreated earlier in this same run, _recreate_category built
    # its channel list from channel_map, which still held the old id for a
    # channel not yet recreated. Appending without removing would leave the
    # category naming a channel that does not exist.
    stale = old_channel_id is not None and old_channel_id in channels
    if new_channel_id in channels and not stale:
        return
    if stale and old_channel_id is not None:
        channels.remove(old_channel_id)
    if new_channel_id not in channels:
        channels.append(new_channel_id)

    await api_upsert_categories(
        session, config.stoat_url, config.token, state.stoat_server_id, live_categories
    )
    on_event(
        MigrationEvent(
            phase="repair",
            status="progress",
            message=(
                f"Re-attached the channel to category '{target.get('title', stoat_category_id)}'."
            ),
        )
    )


async def _recreate_category(
    session: aiohttp.ClientSession,
    config: FerryConfig,
    state: MigrationState,
    result: CheckResult,
    live_categories: list[dict[str, Any]],
    on_event: EventCallback,
) -> bool:
    """Recreate one missing category by PATCHing the whole array back.

    Two things make this different from the channel and role cases.

    First, ``api_upsert_categories`` sets the server's ENTIRE categories list.
    Sending only the recreated category would delete every other one, so the
    live array is read first and the new entry appended to it. A test drives two
    existing categories and asserts both survive.

    Second, a Stoat category carries its channels. Recreating one with an empty
    channel list produces an empty category: a recreation that reports success
    and restores nothing, the same shape as the forum index case this module
    already declines. The channel list is rebuilt from ``channel_categories``,
    which records ``discord_channel_id -> discord_category_id`` for exactly
    this, mapped through ``channel_map`` to the ids the server knows.
    """
    discord_id = result.discord_id or ""
    title = state.category_names.get(discord_id)
    if not title:
        on_event(
            MigrationEvent(
                phase="repair",
                status="warning",
                message=_no_recorded_name(state, "category", discord_id),
            )
        )
        return False

    stoat_channel_ids = [
        stoat_id
        for discord_channel_id, category_id in state.channel_categories.items()
        if category_id == discord_id
        and (stoat_id := state.channel_map.get(discord_channel_id)) is not None
    ]
    new_id = state.category_map.get(discord_id) or _generate_category_id()
    rebuilt = [c for c in live_categories if c.get("id") != new_id]
    rebuilt.append({"id": new_id, "title": title, "channels": stoat_channel_ids})

    await api_upsert_categories(
        session, config.stoat_url, config.token, state.stoat_server_id, rebuilt
    )
    state.category_map[discord_id] = new_id
    on_event(
        MigrationEvent(
            phase="repair",
            status="progress",
            message=(
                f"Recreated category '{title}' with {len(stoat_channel_ids)} channel(s), "
                f"preserving {len(rebuilt) - 1} existing."
            ),
        )
    )
    return True


async def run_repair(
    config: FerryConfig,
    state: MigrationState,
    exports: list[DCEExport],
    on_event: EventCallback,
    *,
    session: aiohttp.ClientSession | None = None,
) -> None:
    """Act on a CheckReport: restore missing structure and lost messages.

    A sibling of :func:`run_retry_failed` and :func:`run_rollback`, not a phase.
    ``PHASE_ORDER``, the phase list and the resume-by-name comparison are
    untouched, and repair is not resumable by design: it re-derives its work
    from a fresh check on every run, so there is nothing to checkpoint.

    Raises:
        CheckError: the state is from a dry run, or records no server. Both come
            from ``run_check`` and are deliberately not caught here, because the
            shell turns them into an exit code and a sentence.
    """
    # Rollback preserves the id maps as an audit trail and NEVER clears them
    # (RollbackProgress' own docstring in state.py says so), which means a check
    # on a rolled-back state reports channel_missing for every channel it
    # mapped. A repair acting on that report would rebuild a server the user
    # deliberately destroyed, so this refusal comes before any request.
    #
    # rollback_progress is set once, at the start of run_rollback, checkpointed
    # on both success and failure, and never cleared, so a non-None value also
    # covers a rollback that failed at its first delete. What it cannot see: a
    # hand-edited state.json, or a fresh migration into the same output_dir.
    if state.rollback_progress is not None:
        on_event(
            MigrationEvent(
                phase="repair",
                status="error",
                message=(
                    "This state records a rollback, so every channel it maps was deleted "
                    "on purpose. Rollback keeps the id maps as an audit trail, so a check "
                    "reports them all missing and a repair would rebuild a server you "
                    "chose to remove. Refusing."
                ),
            )
        )
        return

    _ensure_token_store(config)
    init_request_semaphore(config.max_concurrent_requests)

    from discord_ferry.migrator.verify import run_check

    report = await run_check(config.stoat_url, config.token, state, on_event, session=session)
    counts = report.counts()
    on_event(
        MigrationEvent(
            phase="repair",
            status="progress",
            message=(
                f"Check complete: {counts['fail']} failing, {counts['warn']} warned, "
                f"{counts['unverifiable']} unverifiable."
            ),
        )
    )

    structure_work: list[CheckResult] = []
    tail_work: list[CheckResult] = []
    for result in report.results:
        is_structure = result.kind in _REPAIRABLE_STRUCTURE
        if not is_structure and result.kind not in _REPAIRABLE_TAIL:
            continue

        # The exclusion is tested ONCE, against both families, and that placement
        # is the fix for a real gap rather than tidiness. Guarding only the
        # structure branch let a forum index channel with `tail_absent` through
        # into the tail work, and the tail of a forum index channel IS the index
        # message: it lives in forum_index_message_ids and has no message in any
        # export to re-send. Measured during the chunk 3 review.
        if (result.discord_id or "").startswith(_UNREPAIRABLE_CHANNEL_PREFIX):
            notice = (
                f"Forum index channel {result.discord_id} needs repair ({result.kind}) and "
                "repair cannot do it. The index message is derived content with no message "
                "in the export to restore from, and rebuilding it needs the forum's posts. "
                "Re-run the migration with --incremental to rebuild it (see issue #311)."
            )
            on_event(MigrationEvent(phase="repair", status="warning", message=notice))
            # Not under --dry-run. A dry run reports and changes nothing, and
            # state.warnings is state: mutating it during a preview would leave
            # an in-memory record the run never persists and the operator never
            # asked for. The event above carries the same information either way.
            if not config.dry_run:
                state.warnings.append(
                    {
                        "phase": "repair",
                        "type": "forum_index_not_repairable",
                        "message": notice,
                    }
                )
            continue

        if is_structure:
            structure_work.append(result)
        else:
            tail_work.append(result)

    prefix = "[DRY RUN] " if config.dry_run else ""
    on_event(
        MigrationEvent(
            phase="repair",
            status="progress",
            message=(
                f"{prefix}{len(structure_work)} entities to recreate, "
                f"{len(tail_work)} channels with a lost tail."
            ),
        )
    )

    if config.dry_run:
        on_event(
            MigrationEvent(
                phase="repair",
                status="completed",
                message="[DRY RUN] Nothing was created, sent or written.",
            )
        )
        return

    if structure_work:
        # Only when something is actually being recreated. A repair with nothing
        # to create should not spend a request on the /servers bucket, and a
        # test asserts that.
        own_session = session is None
        sess = session or new_session()
        try:
            existing_names, live_categories = await _live_server_view(sess, config, state)
            on_event(
                MigrationEvent(
                    phase="repair",
                    status="progress",
                    message=f"{len(existing_names)} channel names already on the server.",
                )
            )
            # Loaded once, not per entity. None means the file is absent, which
            # is a real case: it is written during the migration and an operator
            # repairing from a copied output directory may not have brought it.
            metadata = load_discord_metadata(config.output_dir)
            if metadata is None and structure_work:
                message = (
                    "discord_metadata.json is not in the output directory, so a recreated "
                    "channel or role gets no permission overrides. It looks migrated and "
                    "grants nobody the right to use it. Copy that file across from the "
                    "original migration, or set the permissions by hand afterwards."
                )
                state.warnings.append(
                    {"phase": "repair", "type": "no_discord_metadata", "message": message}
                )
                on_event(MigrationEvent(phase="repair", status="warning", message=message))

            for result in structure_work:
                discord_id = result.discord_id or ""
                # Persist after EACH recreation, not once at the end. run_roles
                # already does this mid-loop and says why: a hard kill between a
                # create and the save leaves the id map naming the entity that is
                # gone, so the next run's check reports it missing again and
                # repair creates a SECOND one. One extra atomic write per
                # recreated entity is cheap against a duplicate nobody asked for.
                #
                # Unreachable under --dry-run: that path returns above.
                if result.kind == "role_missing":
                    role_created = await _recreate_role(sess, config, state, result, on_event)
                    if role_created:
                        # DECLINED, and said so rather than left silent. The
                        # migration sets a role's colour, hoist and icon through
                        # api_edit_role, and the icon path uploads a file to
                        # Autumn. Rank is NOT among them since #380: the per-role
                        # PATCH discards that field, and ordering now goes through
                        # _apply_role_ordering, which recomputes the whole server
                        # from a read-back. So an --incremental re-run does now
                        # restore this role's position, which is why that advice
                        # stays in the message below. Restoring the rest is a second
                        # content path in a batch already carrying two commands,
                        # and the reasoning that keeps emoji out of repair (#307)
                        # applies to the icon. Recorded whether or not metadata
                        # exists, because the attributes are lost either way.
                        # Deferred as #344.
                        role_label = state.created_role_names.get(discord_id, discord_id)
                        state.warnings.append(
                            {
                                "phase": "repair",
                                "type": "role_attributes_not_restored",
                                "message": (
                                    f"Role '{role_label}' was recreated with its name and "
                                    "permissions. Its colour, rank, hoist setting and icon are "
                                    "not restored: set them by hand, or re-run the migration "
                                    "with --incremental (see issue #344)."
                                ),
                            }
                        )
                    if role_created and metadata:
                        # ONE role. The server-default call that follows the loop
                        # this helper was extracted from is deliberately not here:
                        # it writes a mask onto the server's DEFAULT ROLE, which
                        # every member holds, and re-firing it during a one-role
                        # repair is the defect batch 5 of #107 existed to fix.
                        await apply_role_permissions(
                            sess,
                            config,
                            state,
                            state.role_map[discord_id],
                            metadata.role_permissions.get(discord_id),
                            state.created_role_names.get(discord_id, discord_id),
                            phase="repair",
                        )
                elif result.kind == "category_missing":
                    await _recreate_category(sess, config, state, result, live_categories, on_event)
                elif result.kind == "channel_missing":
                    created = await _recreate_channel(
                        sess, config, state, result, exports, existing_names, on_event
                    )
                    if created:
                        matching = next((e for e in exports if e.channel.id == discord_id), None)
                        label = state.created_channel_names.get(discord_id, discord_id)
                        if matching is not None:
                            if state.thread_strategy == "merge":
                                _warn_unrestored_merge_threads(state, matching, exports, on_event)
                            count = await _resend_channel(sess, config, state, matching, on_event)
                            on_event(
                                MigrationEvent(
                                    phase="repair",
                                    status="progress",
                                    message=f"Re-sent {count} message(s) into {label}.",
                                )
                            )
                    if created:
                        await _reattach_to_category(
                            sess,
                            config,
                            state,
                            discord_id,
                            state.channel_map[discord_id],
                            result.stoat_id,
                            live_categories,
                            on_event,
                        )
                    if created and metadata:
                        await apply_channel_permissions(
                            sess,
                            config,
                            state,
                            state.channel_map[discord_id],
                            metadata.channel_metadata.get(discord_id),
                            state.created_channel_names.get(discord_id, discord_id),
                            phase="repair",
                        )
                        await _apply_channel_attributes(
                            sess,
                            config,
                            state,
                            state.channel_map[discord_id],
                            metadata.channel_metadata.get(discord_id),
                            state.created_channel_names.get(discord_id, discord_id),
                        )
                save_state(state, config.output_dir)
        finally:
            if own_session:
                await sess.close()

    if tail_work:
        own_tail_session = session is None
        tail_sess = session or new_session()
        try:
            restored = 0
            for result in tail_work:
                if await _repair_tail(tail_sess, config, state, result, exports, on_event):
                    restored += 1
                    save_state(state, config.output_dir)
            on_event(
                MigrationEvent(
                    phase="repair",
                    status="progress",
                    message=f"Restored {restored} of {len(tail_work)} lost last message(s).",
                )
            )
        finally:
            if own_tail_session:
                await tail_sess.close()

    # Whatever is still in the dead-letter queue, including anything the two
    # passes above just failed on. run_retry_failed is reused rather than
    # reimplemented: it already resolves each FailedMessage back to a DCEMessage,
    # rebuilds the queue from what still fails, and saves. It returns early on an
    # empty queue, so this costs nothing when there is nothing to drain.
    if state.failed_messages:
        await run_retry_failed(config, state, exports, on_event)

    # One save, at the end. Never under --dry-run, and not merely a save that
    # happens to change nothing: run_check REFUSES a dry-run state outright,
    # because a dry run fills the id maps with `dry-` sentinels naming entities
    # nobody created. Writing those into a real state file would leave the user
    # with a migration that can never be checked again.
    save_state(state, config.output_dir)


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
                    error=_safe(config, str(exc)),
                    http_status=http_status,
                )
            )
            save_state(state, config.output_dir)
            severity = "error" if http_status == 401 else "warning"
            msg = _safe(config, f"Failed to delete channel {channel_id}: {exc}")
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
                error=_safe(config, str(exc)),
                http_status=http_status,
            )
        )
        save_state(state, config.output_dir)
        severity = "error" if http_status == 401 else "warning"
        msg = _safe(config, f"Failed to delete role {role_id}: {exc}")
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
                error=_safe(config, str(exc)),
                http_status=http_status,
            )
        )
        save_state(state, config.output_dir)
        severity = "error" if http_status == 401 else "warning"
        msg = _safe(config, f"Failed to delete emoji {emoji_id}: {exc}")
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
                error=_safe(config, f"Could not fetch server for category cleanup: {exc}"),
                http_status=_parse_http_status(str(exc)),
            )
        )
        save_state(state, config.output_dir)
        on_event(
            MigrationEvent(
                phase="rollback",
                status="warning",
                message=_safe(config, f"Category cleanup skipped: {exc}"),
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
                error=_safe(config, str(exc)),
                http_status=_parse_http_status(str(exc)),
            )
        )
        save_state(state, config.output_dir)
        on_event(
            MigrationEvent(
                phase="rollback",
                status="warning",
                message=_safe(config, f"Category cleanup PATCH failed: {exc}"),
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
    # Token-redact error messages emitted/persisted during rollback (the CLI/GUI
    # shells call run_rollback directly and never set the store).
    _ensure_token_store(config)

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
                        message=_safe(config, f"Could not fetch server {server_id}: {exc}"),
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
