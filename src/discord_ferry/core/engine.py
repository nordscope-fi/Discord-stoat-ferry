"""Migration orchestrator — shared by CLI and GUI."""

from __future__ import annotations

import asyncio
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
from discord_ferry.migrator.connect import run_connect
from discord_ferry.migrator.emoji import run_emoji
from discord_ferry.migrator.messages import run_messages
from discord_ferry.migrator.pins import run_pins
from discord_ferry.migrator.reactions import run_reactions
from discord_ferry.migrator.structure import run_categories, run_channels, run_roles, run_server
from discord_ferry.parser.dce_parser import parse_export_directory, validate_export
from discord_ferry.parser.models import DCEExport
from discord_ferry.reporter import generate_report
from discord_ferry.state import MigrationState, load_state, save_state

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
    "messages",  # Phase 8
    "reactions",  # Phase 9
    "pins",  # Phase 10
    "report",  # Phase 11 — handled inline (reporter)
]

# Phases that can be skipped via config flags
_SKIPPABLE: dict[str, str] = {
    "export": "skip_export",
    "emoji": "skip_emoji",
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

    # Load or create state
    if config.resume:
        state = load_state(config.output_dir)
        if state.is_dry_run:
            raise MigrationError("Cannot resume from a dry-run state. Start a fresh migration.")
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
            dce_path = await download_dce(on_event)
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
                    status="progress",
                    message="No Discord token — permissions will not be migrated",
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

    # Phases 2-10: run in order, skipping as appropriate
    runnable_phases = [p for p in PHASE_ORDER if p not in ("export", "validate", "report")]

    async with aiohttp.ClientSession() as session:
        config.session = session
        await _run_phases(config, state, exports, on_event, runnable_phases, phase_overrides)
    config.session = None

    # Skip report if migration was cancelled
    if config.cancel_event and config.cancel_event.is_set():
        return state

    # Phase 11: REPORT — generate and save inline
    state.current_phase = "report"
    on_event(MigrationEvent(phase="report", status="started", message="Generating report..."))
    state.completed_at = datetime.now(timezone.utc).isoformat()
    generate_report(config, state, exports)
    save_state(state, config.output_dir)
    on_event(MigrationEvent(phase="report", status="completed", message="Migration complete"))

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
