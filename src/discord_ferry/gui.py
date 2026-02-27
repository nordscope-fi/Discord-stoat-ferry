"""GUI entry point for Discord Ferry (primary interface)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from nicegui import app, background_tasks, ui

from discord_ferry.config import FerryConfig
from discord_ferry.core.engine import PHASE_ORDER, run_migration
from discord_ferry.errors import MigrationError
from discord_ferry.parser.dce_parser import parse_export_directory, validate_export

if TYPE_CHECKING:
    from discord_ferry.core.events import MigrationEvent
    from discord_ferry.parser.models import DCEExport

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PHASE_LABELS: dict[str, str] = {
    "validate": "Validate",
    "connect": "Connect",
    "server": "Server",
    "roles": "Roles",
    "categories": "Categories",
    "channels": "Channels",
    "emoji": "Emoji",
    "messages": "Messages",
    "reactions": "Reactions",
    "pins": "Pins",
    "report": "Report",
}

_STATUS_COLOUR: dict[str, str] = {
    "pending": "grey",
    "started": "blue",
    "progress": "blue",
    "completed": "green",
    "skipped": "grey",
    "error": "red",
    "warning": "orange",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_eta(total_messages: int, rate_limit: float) -> str:
    """Format an ETA string from message count and rate limit."""
    seconds = int(total_messages * rate_limit)
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours > 0:
        return f"~{hours}h {minutes}m"
    return f"~{minutes}m"


def _msgs_per_hour(rate_limit: float) -> int:
    """Convert per-message delay in seconds to messages per hour."""
    if rate_limit <= 0:
        return 0
    return int(3600 / rate_limit)


def _compute_summary(exports: list[DCEExport]) -> dict[str, int | str]:
    """Compute summary counts from a list of exports."""
    total_messages = sum(e.message_count for e in exports)
    total_attachments = sum(sum(len(m.attachments) for m in e.messages) for e in exports)

    categories: set[str] = set()
    roles: set[str] = set()
    emoji_ids: set[str] = set()
    threads = 0

    for export in exports:
        if export.channel.category:
            categories.add(export.channel.category)
        if export.is_thread:
            threads += 1
        for msg in export.messages:
            for role in msg.author.roles:
                roles.add(role.id)
            for reaction in msg.reactions:
                if reaction.emoji.id:
                    emoji_ids.add(reaction.emoji.id)

    return {
        "channels": len(exports),
        "categories": len(categories),
        "roles": len(roles),
        "messages": total_messages,
        "attachments": total_attachments,
        "custom_emoji": len(emoji_ids),
        "threads": threads,
    }


# ---------------------------------------------------------------------------
# Screen 1: Setup
# ---------------------------------------------------------------------------


@ui.page("/")
def setup_page() -> None:
    """Setup screen — collect connection details and migration options."""

    def _rate_label(value: float) -> str:
        return f"{value:.1f}s/msg ({_msgs_per_hour(value):,} msg/hr)"

    with ui.column().classes("w-full items-center min-h-screen bg-gray-50 py-10"):  # noqa: SIM117
        with ui.card().classes("w-full max-w-xl shadow-md"):
            ui.label("Discord Ferry").classes("text-2xl font-bold text-center mt-2")
            ui.label("Migrate a Discord export to your Stoat server").classes(
                "text-gray-500 text-sm text-center mb-4"
            )

            export_dir_input = ui.input(
                label="Export folder path",
                placeholder="/path/to/your/dce-export",
            ).classes("w-full")

            stoat_url_input = ui.input(
                label="Stoat API URL",
                placeholder="https://api.revolt.chat",
            ).classes("w-full")

            token_input = ui.input(
                label="Stoat token",
                placeholder="Your user or bot token",
                password=True,
                password_toggle_button=True,
            ).classes("w-full")

            with ui.expansion("Advanced Options", icon="settings").classes("w-full mt-2"):
                server_id_input = ui.input(
                    label="Existing server ID (optional)",
                    placeholder="Leave blank to create a new server",
                ).classes("w-full")

                rate_slider_label = ui.label(_rate_label(1.0)).classes("text-sm text-gray-600 mt-3")
                rate_slider = ui.slider(min=0.5, max=3.0, value=1.0, step=0.1).classes("w-full")
                rate_slider.on(
                    "update:model-value",
                    lambda e: rate_slider_label.set_text(_rate_label(e.args)),
                )

                skip_messages_cb = ui.checkbox("Skip messages (structure only)")
                skip_emoji_cb = ui.checkbox("Skip emoji upload")
                skip_reactions_cb = ui.checkbox("Skip reactions")

            error_label = ui.label("").classes("text-red-500 text-sm")

            def _on_validate_click() -> None:
                export_dir = export_dir_input.value.strip()
                stoat_url = stoat_url_input.value.strip()
                token = token_input.value.strip()

                if not export_dir or not stoat_url or not token:
                    error_label.set_text("Export folder, Stoat URL, and token are all required.")
                    return

                app.storage.user["export_dir"] = export_dir
                app.storage.user["stoat_url"] = stoat_url
                app.storage.user["token"] = token
                app.storage.user["server_id"] = server_id_input.value.strip()
                app.storage.user["rate_limit"] = rate_slider.value
                app.storage.user["skip_messages"] = skip_messages_cb.value
                app.storage.user["skip_emoji"] = skip_emoji_cb.value
                app.storage.user["skip_reactions"] = skip_reactions_cb.value

                ui.navigate.to("/validate")

            ui.button("Validate Export", on_click=_on_validate_click).classes(
                "w-full mt-4 bg-blue-600 text-white"
            )


# ---------------------------------------------------------------------------
# Screen 2: Validate
# ---------------------------------------------------------------------------


@ui.page("/validate")
def validate_page() -> None:
    """Validate screen — show parsed export summary, warnings, and ETA."""

    storage = app.storage.user
    if not storage.get("export_dir") or not storage.get("stoat_url") or not storage.get("token"):
        ui.navigate.to("/")
        return

    export_dir = Path(storage["export_dir"])
    rate_limit: float = float(storage.get("rate_limit", 1.0))

    with ui.column().classes("w-full items-center min-h-screen bg-gray-50 py-10"):  # noqa: SIM117
        with ui.card().classes("w-full max-w-2xl shadow-md"):
            # Parse export — these are fast synchronous operations
            try:
                exports = parse_export_directory(export_dir)
            except Exception as exc:
                ui.label(f"Failed to parse export: {exc}").classes("text-red-500 font-bold")
                ui.button("Back", on_click=lambda: ui.navigate.to("/")).classes("mt-4")
                return

            if not exports:
                ui.label("No valid DCE JSON files found in the selected folder.").classes(
                    "text-red-500 font-bold"
                )
                ui.button("Back", on_click=lambda: ui.navigate.to("/")).classes("mt-4")
                return

            warnings = validate_export(exports, export_dir)
            has_rendered_markdown = any(w["type"] == "rendered_markdown" for w in warnings)

            guild_name = exports[0].guild.name
            ui.label(f"Export: {guild_name}").classes("text-2xl font-bold text-center mt-2")

            # Status indicator
            if has_rendered_markdown:
                status_colour = "red"
                status_text = "Critical warnings — fix before migrating"
            elif warnings:
                status_colour = "amber"
                status_text = "Warnings present — review before migrating"
            else:
                status_colour = "green"
                status_text = "Export looks good"

            ui.chip(status_text, color=status_colour).classes("mx-auto my-2")

            # Summary table
            summary = _compute_summary(exports)
            columns = [
                {"name": "item", "label": "Item", "field": "item", "align": "left"},
                {"name": "count", "label": "Count", "field": "count", "align": "right"},
            ]
            rows = [
                {"item": "Channels", "count": f"{summary['channels']:,}"},
                {"item": "Categories", "count": f"{summary['categories']:,}"},
                {"item": "Roles", "count": f"{summary['roles']:,}"},
                {"item": "Messages", "count": f"{summary['messages']:,}"},
                {"item": "Attachments", "count": f"{summary['attachments']:,}"},
                {"item": "Custom Emoji", "count": f"{summary['custom_emoji']:,}"},
                {"item": "Threads / Forums", "count": f"{summary['threads']:,}"},
            ]
            ui.table(columns=columns, rows=rows).classes("w-full mt-2")

            # ETA estimate
            total_messages = int(summary["messages"])
            eta = _format_eta(total_messages, rate_limit)
            msg_hr = _msgs_per_hour(rate_limit)
            ui.label(f"ETA at {rate_limit:.1f}s/msg ({msg_hr:,} msg/hr): {eta}").classes(
                "text-sm text-gray-600 mt-3"
            )

            # Warnings section
            if warnings:
                with ui.expansion(f"Warnings ({len(warnings)})", icon="warning").classes(
                    "w-full mt-2"
                ):
                    for w in warnings:
                        colour = "red" if w["type"] == "rendered_markdown" else "orange"
                        ui.label(w["message"]).classes(f"text-{colour}-600 text-sm py-1")

            # Navigation buttons
            with ui.row().classes("w-full justify-between mt-6"):
                ui.button("Back", on_click=lambda: ui.navigate.to("/")).classes(
                    "bg-gray-200 text-gray-800"
                )
                start_btn = ui.button(
                    "Start Migration", on_click=lambda: ui.navigate.to("/migrate")
                ).classes("bg-blue-600 text-white")
                if has_rendered_markdown:
                    start_btn.disable()


# ---------------------------------------------------------------------------
# Screen 3: Migrate
# ---------------------------------------------------------------------------


@ui.page("/migrate")
def migrate_page() -> None:
    """Migration screen — run the engine, show live phase/progress/log."""

    storage = app.storage.user
    if not storage.get("export_dir") or not storage.get("stoat_url") or not storage.get("token"):
        ui.navigate.to("/")
        return

    # Build FerryConfig from stored values
    pause_event = asyncio.Event()
    pause_event.set()  # start unpaused
    cancel_event = asyncio.Event()

    config = FerryConfig(
        export_dir=Path(storage["export_dir"]),
        stoat_url=storage["stoat_url"],
        token=storage["token"],
        server_id=storage.get("server_id") or None,
        message_rate_limit=float(storage.get("rate_limit", 1.0)),
        skip_messages=bool(storage.get("skip_messages", False)),
        skip_emoji=bool(storage.get("skip_emoji", False)),
        skip_reactions=bool(storage.get("skip_reactions", False)),
        output_dir=Path("./ferry-output"),
        pause_event=pause_event,
        cancel_event=cancel_event,
    )

    # ---------------------------------------------------------------------------
    # Mutable state shared between the UI and the on_event callback
    # ---------------------------------------------------------------------------
    phase_status: dict[str, str] = {p: "pending" for p in PHASE_ORDER}
    phase_chips: dict[str, ui.chip] = {}
    progress_bar: ui.linear_progress
    log_display: ui.log
    messages_sent_label: ui.label
    errors_label: ui.label
    warnings_label: ui.label
    eta_label: ui.label
    pause_btn: ui.button
    cancel_btn: ui.button
    controls_row: ui.row
    completion_card: ui.card

    messages_sent = 0
    error_count = 0
    warning_count = 0
    total_messages = 0
    report_path: list[Path] = []  # mutable container so the closure can write to it

    with ui.column().classes("w-full items-center min-h-screen bg-gray-50 py-10"):  # noqa: SIM117
        with ui.card().classes("w-full max-w-3xl shadow-md"):
            ui.label("Migration Running").classes("text-2xl font-bold text-center mt-2 mb-4")

            # Phase indicator row
            with ui.row().classes("w-full flex-wrap gap-2 justify-center mb-4"):
                for phase in PHASE_ORDER:
                    chip = ui.chip(_PHASE_LABELS.get(phase, phase), color="grey").classes("text-xs")
                    phase_chips[phase] = chip

            # Progress bar
            progress_bar = ui.linear_progress(value=0).classes("w-full")
            progress_bar.props("stripe animated color=blue")

            # Stats row
            with ui.row().classes("w-full justify-between text-sm text-gray-600 mt-2"):
                messages_sent_label = ui.label("Messages: 0")
                errors_label = ui.label("Errors: 0")
                warnings_label = ui.label("Warnings: 0")
                eta_label = ui.label("ETA: —")

            # Log
            log_display = ui.log(max_lines=500).classes("w-full h-64 font-mono text-xs mt-4")

            # Controls
            with ui.row().classes("w-full justify-end gap-2 mt-4") as controls_row:
                pause_btn = ui.button("Pause", on_click=lambda: _toggle_pause())
                cancel_btn = ui.button("Cancel", on_click=lambda: _confirm_cancel()).classes(
                    "bg-red-600 text-white"
                )

            # Completion card (hidden initially)
            with ui.card().classes("w-full mt-4 hidden") as completion_card:
                ui.label("Migration Complete").classes("text-xl font-bold text-green-600")
                ui.label("").classes("text-sm text-gray-500").bind_text_from(errors_label, "text")
                open_report_btn = ui.button("Open Report", on_click=lambda: _open_report()).classes(
                    "mt-2 bg-green-600 text-white"
                )

    # ---------------------------------------------------------------------------
    # Cancel confirmation dialog
    # ---------------------------------------------------------------------------
    with ui.dialog() as cancel_dialog, ui.card():
        ui.label("Are you sure you want to cancel the migration?").classes("font-bold")
        ui.label("Progress has been saved and can be resumed later.").classes(
            "text-sm text-gray-500"
        )
        with ui.row().classes("justify-end gap-2 mt-4"):
            ui.button("Keep running", on_click=cancel_dialog.close)
            ui.button(
                "Cancel migration",
                on_click=lambda: _do_cancel(cancel_dialog),
            ).classes("bg-red-600 text-white")

    # ---------------------------------------------------------------------------
    # Control helpers
    # ---------------------------------------------------------------------------

    def _toggle_pause() -> None:
        if pause_event.is_set():
            # Currently running — pause it
            pause_event.clear()
            pause_btn.set_text("Resume")
            pause_btn.classes(add="bg-amber-500 text-white")
            log_display.push("-- Migration paused --")
        else:
            # Currently paused — resume
            pause_event.set()
            pause_btn.set_text("Pause")
            pause_btn.classes(remove="bg-amber-500 text-white")
            log_display.push("-- Migration resumed --")

    def _confirm_cancel() -> None:
        cancel_dialog.open()

    def _do_cancel(dialog: ui.dialog) -> None:
        dialog.close()
        cancel_event.set()
        cancel_btn.disable()
        log_display.push("-- Cancellation requested, finishing current operation... --")

    def _open_report() -> None:
        if report_path:
            try:
                opener = "open" if sys.platform == "darwin" else "xdg-open"
                subprocess.Popen([opener, str(report_path[0])])
            except Exception as exc:
                ui.notify(f"Could not open report: {exc}", type="negative")

    # ---------------------------------------------------------------------------
    # Event callback (called from the async engine, on the NiceGUI event loop)
    # ---------------------------------------------------------------------------

    def on_event(event: MigrationEvent) -> None:
        nonlocal messages_sent, error_count, warning_count, total_messages

        phase_status[event.phase] = event.status

        # Guard: if user navigated away, UI elements may be stale.
        with contextlib.suppress(Exception):
            _update_ui(event)

    def _update_ui(event: MigrationEvent) -> None:
        nonlocal messages_sent, error_count, warning_count, total_messages

        # Update phase chip colour
        chip = phase_chips.get(event.phase)
        if chip is not None:
            colour = _STATUS_COLOUR.get(event.status, "grey")
            chip.props(f"color={colour}")

        # Update stats
        match event.status:
            case "progress":
                if event.total > 0:
                    total_messages = event.total
                    messages_sent = event.current
                    progress = event.current / event.total
                    progress_bar.set_value(progress)
                    remaining = event.total - event.current
                    rate = config.message_rate_limit
                    eta_secs = int(remaining * rate)
                    h, rem = divmod(eta_secs, 3600)
                    m = rem // 60
                    eta_str = f"~{h}h {m}m" if h > 0 else f"~{m}m"
                    eta_label.set_text(f"ETA: {eta_str}")
                messages_sent_label.set_text(f"Messages: {messages_sent:,}")
            case "error":
                error_count += 1
                errors_label.set_text(f"Errors: {error_count}")
            case "warning":
                warning_count += 1
                warnings_label.set_text(f"Warnings: {warning_count}")
            case "completed":
                if event.phase == "report":
                    # Final completion — switch UI
                    _on_migration_complete()
                else:
                    progress_bar.set_value((PHASE_ORDER.index(event.phase) + 1) / len(PHASE_ORDER))

        log_display.push(f"[{event.phase}] {event.status}: {event.message}")

    def _on_migration_complete() -> None:
        controls_row.classes(add="hidden")
        completion_card.classes(remove="hidden")
        # Find the report file
        report_dir = config.output_dir
        report_file = report_dir / "migration_report.json"
        if report_file.exists():
            report_path.append(report_file)
            open_report_btn.enable()
        else:
            open_report_btn.disable()
        progress_bar.set_value(1.0)
        ui.notify("Migration complete!", type="positive")

    # ---------------------------------------------------------------------------
    # Start migration in background
    # ---------------------------------------------------------------------------

    async def _run() -> None:
        try:
            await run_migration(config, on_event=on_event)
        except MigrationError as exc:
            log_display.push(f"[ERROR] Migration failed: {exc}")
            errors_label.set_text(f"Errors: {error_count} (FAILED)")
            ui.notify(f"Migration failed: {exc}", type="negative")
        except Exception as exc:
            log_display.push(f"[ERROR] Unexpected error: {exc}")
            ui.notify(f"Unexpected error: {exc}", type="negative")

    background_tasks.create(_run())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Launch the NiceGUI local web UI."""
    native = False
    try:
        import webview  # noqa: F401

        native = True
    except ImportError:
        pass

    ui.run(
        title="Discord Ferry",
        port=8765,
        native=native,
        reload=False,
        storage_secret=os.environ.get("FERRY_STORAGE_SECRET", secrets.token_hex(32)),
    )


if __name__ == "__main__":
    main()
