"""GUI entry point for Discord Ferry (primary interface)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import multiprocessing
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from nicegui import app, background_tasks, ui

from discord_ferry.config import FerryConfig
from discord_ferry.core.engine import PHASE_ORDER, run_migration, run_rollback
from discord_ferry.errors import MigrationError
from discord_ferry.parser.dce_parser import parse_export_directory, validate_export
from discord_ferry.state import load_state

_HAS_WEBVIEW = False
try:
    import webview  # type: ignore[import-not-found,unused-ignore]

    _HAS_WEBVIEW = True
except ImportError:
    pass

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, MutableMapping, Sequence

    from discord_ferry.core.events import MigrationEvent
    from discord_ferry.parser.models import DCEExport

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PHASE_LABELS: dict[str, str] = {
    "export": "Export",
    "validate": "Validate",
    "connect": "Connect",
    "server": "Server",
    "roles": "Roles",
    "categories": "Categories",
    "channels": "Channels",
    "emoji": "Emoji",
    "avatars": "Avatars",
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
    "confirm": "amber",
}

_OFFICIAL_STOAT_URL = "https://api.stoat.chat"

_HEAD_HTML = (
    '<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:'
    'wght@400;500;600;700&display=swap" rel="stylesheet">'
    "<style>"
    "body { font-family: 'IBM Plex Sans', sans-serif; }"
    "@keyframes fadeIn { from { opacity: 0; transform: translateY(8px); }"
    " to { opacity: 1; transform: translateY(0); } }"
    ".fade-in { animation: fadeIn 0.4s ease-out; }"
    "</style>"
)

_STEP_LABELS: list[str] = ["Configure", "Export", "Validate", "Migrate", "Done"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_path(path: Path) -> None:
    """Open a file in the OS default handler.

    Path-with-spaces invariant: all three branches handle paths containing
    spaces correctly because none invoke a shell. Do NOT "simplify" any branch
    into a string command (e.g. shell=True, or `cmd /c start ...` as a single
    string) — that re-introduces shell-parsing bugs for paths with spaces.
    """
    if sys.platform == "win32":
        os.startfile(path)  # Python 3.8+ accepts Path objects
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def _format_bytes(n: int | float) -> str:
    """Format a byte count as a human-readable string."""
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(value) < 1024:
            return f"{value:,.1f} {unit}"
        value /= 1024
    return f"{value:,.1f} TB"


# Session credentials that must never persist to app.storage.user on disk.
# Per security.md (GUI Storage): clear all token-like values when migration
# completes or errors. `stoat_url` is also stored at setup but is intentionally
# EXCLUDED here — it is an instance URL, not a credential.
_SESSION_TOKEN_KEYS: tuple[str, ...] = ("token", "discord_token")


def _clear_tokens(storage: MutableMapping[str, Any], keys: Iterable[str]) -> None:
    """Best-effort removal of session secret keys from GUI storage.

    Total and non-throwing: a missing key is a no-op (``storage.pop(k, None)``),
    so this is idempotent and safe on any path (dry-run, partially-populated
    storage, repeat calls). Cleanup ownership: the export phase clears only
    ``discord_token``; the terminal migration screen clears
    ``_SESSION_TOKEN_KEYS`` (both tokens).
    """
    for key in keys:
        storage.pop(key, None)


_EXPOSED_REACTION_MODES = ("text", "native", "skip")


def _coerce_advanced_settings(storage: Mapping[str, Any]) -> dict[str, Any]:
    """Read the seven exposed settings from storage, defaulting and clamping.

    ``app.storage.user`` is disk-backed: a stale or hand-edited file can hold
    None / out-of-range / wrong-type values that the UI controls never produced,
    and ``ui.number`` yields float-or-None. Every value is normalised here so
    ``FerryConfig`` only ever sees engine-safe input (issue #99).
    """

    def _as_int(key: str, default: int, floor: int) -> int:
        raw = storage.get(key, default)
        try:
            value = int(raw) if raw is not None else default
        except (TypeError, ValueError):
            value = default
        return max(value, floor)

    mode = str(storage.get("reaction_mode") or "text")
    if mode not in _EXPOSED_REACTION_MODES:
        mode = "text"
    return {
        "reaction_mode": mode,
        "min_thread_messages": _as_int("min_thread_messages", 0, 0),
        "checkpoint_interval": _as_int("checkpoint_interval", 50, 1),
        "max_concurrent_channels": _as_int("max_concurrent_channels", 3, 1),
        "max_concurrent_requests": _as_int("max_concurrent_requests", 5, 1),
        "skip_avatars": bool(storage.get("skip_avatars", False)),
        "validate_after": bool(storage.get("validate_after", False)),
    }


def _store_session_tokens(store: MutableMapping[str, Any], *, stoat: str, discord: str) -> None:
    """Write the two session tokens into ``store``.

    Production passes ``app.storage.tab`` (memory-only, never written to disk —
    see Batch 8 / F8b). The parameter is named ``store`` (not ``storage``) so the
    security source-inspection guard, which forbids token assignment via the
    disk-backed ``storage`` alias, cannot false-match this body.
    """
    store["token"] = stoat
    store["discord_token"] = discord


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


def _phase_progress(phase: str) -> float | None:
    """Progress-bar fraction in (0, 1] for a pipeline phase, or None if it has no slot.

    Post-pipeline phases such as "validate_migration" are not in PHASE_ORDER and must
    not be passed to PHASE_ORDER.index() (which raises ValueError); callers skip the
    progress update when this returns None.
    """
    if phase not in PHASE_ORDER:
        return None
    return (PHASE_ORDER.index(phase) + 1) / len(PHASE_ORDER)


def _compute_summary(exports: list[DCEExport]) -> dict[str, int | str]:
    """Compute summary counts from a list of exports."""
    total_messages = sum(e.message_count for e in exports)
    total_attachments = sum(sum(len(m.attachments) for m in e.messages) for e in exports)
    total_attachment_bytes = sum(
        att.file_size_bytes
        for e in exports
        for m in e.messages
        for att in m.attachments
        if att.file_size_bytes
    )

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
        "attachment_bytes": total_attachment_bytes,
        "custom_emoji": len(emoji_ids),
        "threads": threads,
    }


def _resolve_stoat_url(toggle_value: str, custom_url_value: str) -> str:
    """Return the Stoat API URL based on toggle state."""
    if (toggle_value or "official") == "official":
        return _OFFICIAL_STOAT_URL
    return custom_url_value.strip()


def _detect_cached_exports(export_dir: Path) -> dict[str, int] | None:
    """Check for existing DCE JSON exports in a directory.

    Returns:
        Dict with 'file_count' and 'total_size' (bytes), or None if no exports found.
    """
    json_files = list(export_dir.glob("*.json"))
    if not json_files:
        return None
    total_size = sum(f.stat().st_size for f in json_files)
    return {"file_count": len(json_files), "total_size": total_size}


def _should_auto_export(cached: dict[str, int] | None) -> bool:
    """True when the /export page should auto-launch DCE on load.

    Only auto-launch when there is no cached export. When a cache exists the
    cached-export card's buttons drive the decision (Use Cached / Re-export),
    so the auto-launch must NOT fire (it would race the click and overwrite the
    cached JSON). See Batch 8 / F8a.
    """
    return cached is None


def _render_step_indicator(active_step: int) -> None:
    """Render a 4-step visual indicator (1-indexed). Call inside a ui.column."""
    with ui.row().classes("w-full justify-center items-center gap-0 mb-6"):
        for i, label in enumerate(_STEP_LABELS, start=1):
            is_active = i == active_step
            is_done = i < active_step
            # Circle
            colour = "amber-400" if is_active else ("green-500" if is_done else "gray-300")
            text_col = "white" if (is_active or is_done) else "gray-400"
            with ui.element("div").classes(
                f"w-8 h-8 rounded-full flex items-center justify-center bg-{colour}"
            ):
                if is_done:
                    ui.icon("check", size="16px").classes(f"text-{text_col}")
                else:
                    ui.label(str(i)).classes(f"text-{text_col} text-sm font-semibold")
            ui.label(label).classes(
                f"text-xs ml-1 {'font-semibold text-gray-800' if is_active else 'text-gray-400'}"
            )
            # Connector line (except after last step)
            if i < len(_STEP_LABELS):
                line_col = "green-500" if is_done else "gray-200"
                ui.element("div").classes(f"flex-grow h-0.5 bg-{line_col} mx-2")


# ---------------------------------------------------------------------------
# Screen 1: Setup
# ---------------------------------------------------------------------------


@ui.page("/")
def setup_page() -> None:
    """Setup screen — collect connection details and migration options."""
    ui.add_head_html(_HEAD_HTML)
    storage = app.storage.user
    # Scrub any token a pre-fix version persisted to .nicegui/storage-user.json.
    # Tokens now live only in memory (app.storage.tab); see Batch 8 / F8b.
    _clear_tokens(storage, _SESSION_TOKEN_KEYS)

    def _rate_label(value: float) -> str:
        return f"{value:.1f}s/msg ({_msgs_per_hour(value):,} msg/hr)"

    async def _on_browse() -> None:
        # app.native.main_window is NiceGUI's proxy to the window process; it is None
        # in browser mode. Do not reach for webview.windows here -- the window lives
        # in another process, so that list is always empty on this side.
        window = getattr(app.native, "main_window", None)
        if window is None:
            ui.notify("Folder picker requires native mode (pywebview window)")
            return
        folder = await _pick_folder(window)
        if folder:
            export_dir_input.set_value(folder)

    with ui.column().classes("w-full items-center min-h-screen bg-gray-50 py-10"):
        # Step indicator
        with ui.element("div").classes("w-full max-w-xl fade-in"):
            _render_step_indicator(active_step=1)

        # Pre-flight checklist banner
        with ui.card().classes("w-full max-w-xl mb-4 fade-in").props("flat bordered"):
            with ui.row().classes("items-center gap-2 mb-2"):
                ui.icon("info", color="blue").classes("text-lg")
                ui.label("Before you start").classes("font-semibold text-gray-700")
            for icon, text, link, link_text in [
                (
                    "o_check_circle",
                    "Discord credentials (token + server ID) or a DCE export folder",
                    None,
                    None,
                ),
                (
                    "o_check_circle",
                    "A Stoat server where you have admin access",
                    None,
                    None,
                ),
                (
                    "o_check_circle",
                    "A Stoat user token from your browser ",
                    "https://developers.stoat.chat",
                    "(how?)",
                ),
            ]:
                with ui.row().classes("items-center gap-2 pl-2"):
                    ui.icon(icon, color="green").classes("text-base")
                    if link and link_text:
                        with ui.row().classes("items-baseline gap-0"):
                            ui.label(text).classes("text-sm text-gray-600")
                            ui.link(link_text, link, new_tab=True).classes("text-sm text-blue-600")
                    else:
                        ui.label(text).classes("text-sm text-gray-600")

        # Main form card
        with ui.card().classes("w-full max-w-xl shadow-md fade-in").tight():
            # Dark header
            with ui.element("div").classes("w-full bg-[#0f172a] px-6 py-5 rounded-t-lg"):
                ui.label("Discord Ferry").classes("text-2xl font-bold text-white")
                ui.label("Migrate a Discord export to your Stoat server").classes(
                    "text-slate-400 text-sm mt-1"
                )

            # Form body
            with ui.column().classes("w-full px-6 py-5 gap-4"):
                # Mode selection
                ui.label("Migration mode").classes("text-sm font-medium text-gray-700 -mb-2")
                mode_toggle = ui.toggle(
                    {
                        "orchestrated": "1-Click Migration",
                        "offline": "I already have exports",
                    },
                    value=storage.get("mode", "orchestrated"),
                ).classes("w-full")

                # Discord credentials (orchestrated mode)
                with (
                    ui.column()
                    .classes("w-full gap-4")
                    .bind_visibility_from(mode_toggle, "value", value="orchestrated")
                ):
                    discord_token_input = ui.input(
                        label="Discord token",
                        placeholder="Paste your Discord user token",
                        password=True,
                        password_toggle_button=True,
                        value="",  # never pre-fill a token from disk (Batch 8 / F8b)
                    ).classes("w-full")

                    discord_server_input = ui.input(
                        label="Discord server ID",
                        placeholder="Right-click server > Copy Server ID",
                        value=str(storage.get("discord_server_id", "")),
                    ).classes("w-full")

                    with ui.dialog() as help_dialog, ui.card().classes("max-w-lg"):
                        ui.label("How to find your Discord credentials").classes(
                            "text-lg font-bold mb-2"
                        )
                        ui.label("Discord Token").classes("text-sm font-bold mt-2")
                        ui.html(
                            "<ol class='text-sm text-gray-700 pl-4 list-decimal'>"
                            "<li>Open Discord in your browser (not the desktop app)</li>"
                            "<li>Press F12 to open Developer Tools</li>"
                            "<li>Go to Network tab, type /api in the filter</li>"
                            "<li>Click any channel, then click a request in the list</li>"
                            "<li>Headers tab \u2192 copy the Authorization value</li>"
                            "</ol>"
                        )
                        ui.label("Server ID").classes("text-sm font-bold mt-3")
                        ui.html(
                            "<ol class='text-sm text-gray-700 pl-4 list-decimal'>"
                            "<li>Discord Settings \u2192 Advanced \u2192 enable Developer Mode</li>"
                            "<li>Right-click your server name \u2192 Copy Server ID</li>"
                            "</ol>"
                        )
                        with ui.row().classes("w-full justify-end mt-4"):
                            ui.button("Got it", on_click=help_dialog.close).props("flat")

                    with ui.row().classes("items-center gap-1 -mt-2"):
                        ui.icon("help_outline", size="16px").classes("text-gray-400")
                        ui.label("How to find your Discord token and server ID").classes(
                            "text-xs text-blue-600 cursor-pointer"
                        ).on("click", lambda: help_dialog.open())

                    tos_checkbox = ui.checkbox(
                        "I acknowledge that using a user token may violate Discord's ToS"
                    ).classes("text-sm")

                # Export folder (offline mode)
                with (  # noqa: SIM117
                    ui.column()
                    .classes("w-full gap-2")
                    .bind_visibility_from(mode_toggle, "value", value="offline")
                ):
                    with ui.input(  # noqa: SIM117
                        label="Export folder path",
                        placeholder="/path/to/your/dce-export",
                        value=str(storage.get("export_dir", "")),
                    ).classes("w-full") as export_dir_input:
                        with export_dir_input.add_slot("append"):
                            browse_btn = ui.button(icon="folder_open", on_click=_on_browse).props(
                                "flat dense"
                            )
                            if not _HAS_WEBVIEW:
                                browse_btn.disable()
                                browse_btn.tooltip("Install pywebview for folder picker")

                # Hosted / self-hosted toggle
                ui.label("Stoat instance").classes("text-sm font-medium text-gray-700 -mb-2")
                server_toggle = ui.toggle(
                    {"official": "Official Stoat (stoat.chat)", "self-hosted": "Self-hosted"},
                    value=storage.get("server_toggle", "official"),
                ).classes("w-full")

                custom_url_input = (
                    ui.input(
                        label="Stoat API URL",
                        placeholder="https://your-instance.example.com",
                        value=str(storage.get("custom_stoat_url", "")),
                    )
                    .classes("w-full")
                    .bind_visibility_from(server_toggle, "value", value="self-hosted")
                )

                # Token
                token_input = ui.input(
                    label="Stoat user token",
                    placeholder="Paste your session token here",
                    password=True,
                    password_toggle_button=True,
                    value="",  # never pre-fill a token from disk (Batch 8 / F8b)
                ).classes("w-full")

                with ui.column().classes("gap-0 -mt-2"):
                    ui.label(
                        "F12 \u2192 Application \u2192 Local Storage \u2192 copy session_token"
                    ).classes("text-xs text-gray-500")
                    with ui.row().classes("items-center gap-1"):
                        ui.icon("help_outline", size="16px").classes("text-gray-400")
                        ui.link(
                            "How to find your user token",
                            "https://developers.stoat.chat",
                            new_tab=True,
                        ).classes("text-xs text-blue-600")

                # Advanced options
                with ui.expansion("Advanced Options", icon="settings").classes("w-full"):
                    server_id_input = ui.input(
                        label="Existing server ID (optional)",
                        placeholder="Leave blank to create a new server",
                        value=str(storage.get("server_id", "")),
                    ).classes("w-full")

                    server_name_input = ui.input(
                        label="Server name (optional)",
                        placeholder="Defaults to Discord server name",
                        value=str(storage.get("server_name", "")),
                    ).classes("w-full")

                    stored_rate = float(storage.get("rate_limit", 1.0))
                    rate_slider_label = ui.label(_rate_label(stored_rate)).classes(
                        "text-sm text-gray-600 mt-3"
                    )
                    rate_slider = ui.slider(min=0.5, max=3.0, value=stored_rate, step=0.1).classes(
                        "w-full"
                    )
                    rate_slider.on(
                        "update:model-value",
                        lambda e: rate_slider_label.set_text(_rate_label(e.args)),
                    )

                    skip_messages_cb = ui.checkbox(
                        "Skip messages (structure only)",
                        value=bool(storage.get("skip_messages", False)),
                    )
                    skip_emoji_cb = ui.checkbox(
                        "Skip emoji upload",
                        value=bool(storage.get("skip_emoji", False)),
                    )
                    skip_reactions_cb = ui.checkbox(
                        "Skip reactions",
                        value=bool(storage.get("skip_reactions", False)),
                    )
                    skip_threads_cb = ui.checkbox(
                        "Skip threads and forum posts",
                        value=bool(storage.get("skip_threads", False)),
                    )
                    thread_strategy_select = ui.select(
                        label="Thread strategy",
                        options=["flatten", "merge", "archive"],
                        value=storage.get("thread_strategy", "flatten"),
                    ).classes("w-48")
                    dry_run_check = ui.checkbox(
                        "Dry run (no API calls)",
                        value=bool(storage.get("dry_run", False)),
                    ).classes("mt-2")

                    adv_stored = _coerce_advanced_settings(storage)

                    ui.label("Speed").classes("text-xs font-bold text-gray-500 mt-4")
                    max_channels_num = ui.number(
                        label="Concurrent channels",
                        value=adv_stored["max_concurrent_channels"],
                        min=1,
                        step=1,
                        precision=0,
                    ).classes("w-48")
                    max_requests_num = ui.number(
                        label="Concurrent API requests",
                        value=adv_stored["max_concurrent_requests"],
                        min=1,
                        step=1,
                        precision=0,
                    ).classes("w-48")

                    ui.label("Content").classes("text-xs font-bold text-gray-500 mt-4")
                    reaction_mode_select = ui.select(
                        label="Reaction mode",
                        options=["text", "native", "skip"],
                        value=adv_stored["reaction_mode"],
                    ).classes("w-48")
                    min_thread_num = ui.number(
                        label="Min thread messages",
                        value=adv_stored["min_thread_messages"],
                        min=0,
                        step=1,
                        precision=0,
                    ).classes("w-48")
                    skip_avatars_cb = ui.checkbox(
                        "Skip avatar pre-flight (avatars upload on demand)",
                        value=adv_stored["skip_avatars"],
                    )

                    ui.label("Safety").classes("text-xs font-bold text-gray-500 mt-4")
                    checkpoint_num = ui.number(
                        label="Checkpoint interval (messages)",
                        value=adv_stored["checkpoint_interval"],
                        min=1,
                        step=1,
                        precision=0,
                    ).classes("w-48")
                    validate_after_cb = ui.checkbox(
                        "Validate after migration (compare server counts)",
                        value=adv_stored["validate_after"],
                    )

                error_label = ui.label("").classes("text-red-500 text-sm")

                async def _on_validate_click() -> None:
                    mode = mode_toggle.value or "orchestrated"
                    toggle_val = server_toggle.value or "official"
                    stoat_url = _resolve_stoat_url(toggle_val, custom_url_input.value)
                    token = token_input.value.strip()

                    missing: list[str] = []

                    if mode == "orchestrated":
                        discord_token = discord_token_input.value.strip()
                        discord_server = discord_server_input.value.strip()
                        if not discord_token:
                            missing.append("Discord token")
                        if not discord_server:
                            missing.append("Discord server ID")
                        if not tos_checkbox.value:
                            error_label.set_text("You must acknowledge the Discord ToS disclaimer")
                            return
                    else:
                        discord_token = ""
                        discord_server = ""
                        export_dir = export_dir_input.value.strip()
                        if not export_dir:
                            missing.append("Export folder")

                    if toggle_val == "self-hosted" and not stoat_url:
                        missing.append("Stoat API URL")
                    elif toggle_val == "self-hosted" and not stoat_url.startswith(
                        ("http://", "https://")
                    ):
                        error_label.set_text("Stoat API URL must start with http:// or https://")
                        return
                    if not token:
                        missing.append("Stoat user token")
                    if missing:
                        error_label.set_text(f"Required: {', '.join(missing)}")
                        return

                    # Store values. Tokens go to the memory-only tab store (never to
                    # disk); tab access needs a connected client — the await is a no-op
                    # during a live click but guarantees access. See Batch 8 / F8b.
                    await ui.context.client.connected()
                    _store_session_tokens(app.storage.tab, stoat=token, discord=discord_token)
                    storage["mode"] = mode
                    storage["discord_server_id"] = discord_server
                    if mode == "offline":
                        storage["export_dir"] = export_dir_input.value.strip()
                    else:
                        output_dir = str(storage.get("output_dir", "./ferry-output"))
                        storage["export_dir"] = str(Path(output_dir) / "dce_cache" / discord_server)
                    storage["stoat_url"] = stoat_url
                    storage["server_toggle"] = toggle_val
                    storage["custom_stoat_url"] = custom_url_input.value.strip()
                    storage["server_id"] = server_id_input.value.strip()
                    storage["server_name"] = server_name_input.value.strip()
                    storage["rate_limit"] = rate_slider.value
                    storage["skip_messages"] = skip_messages_cb.value
                    storage["skip_emoji"] = skip_emoji_cb.value
                    storage["skip_reactions"] = skip_reactions_cb.value
                    storage["skip_threads"] = skip_threads_cb.value
                    storage["thread_strategy"] = thread_strategy_select.value
                    storage["dry_run"] = dry_run_check.value
                    storage["reaction_mode"] = reaction_mode_select.value
                    storage["min_thread_messages"] = min_thread_num.value
                    storage["checkpoint_interval"] = checkpoint_num.value
                    storage["max_concurrent_channels"] = max_channels_num.value
                    storage["max_concurrent_requests"] = max_requests_num.value
                    storage["skip_avatars"] = skip_avatars_cb.value
                    storage["validate_after"] = validate_after_cb.value
                    storage["skip_export"] = mode == "offline"

                    adv_chosen = _coerce_advanced_settings(storage)
                    host = urlparse(stoat_url).hostname if stoat_url else None
                    if host == "api.stoat.chat" and (
                        adv_chosen["max_concurrent_channels"] > 3
                        or adv_chosen["max_concurrent_requests"] > 5
                    ):
                        ui.notify(
                            "Raising concurrency on the official Stoat service usually makes "
                            "runs slower (rate-limit backoff). Intended for self-hosted "
                            "instances.",
                            type="warning",
                        )

                    if mode == "orchestrated":
                        ui.navigate.to("/export")
                    else:
                        ui.navigate.to("/validate")

                ui.button("Continue", on_click=_on_validate_click).classes("w-full mt-2").props(
                    "color=amber-7 text-color=white unelevated"
                ).classes("font-semibold")


# ---------------------------------------------------------------------------
# Screen 2: Export (orchestrated mode only)
# ---------------------------------------------------------------------------


@ui.page("/export")
def export_page() -> None:
    """Export screen — run DCE subprocess, show per-channel progress."""
    from discord_ferry.core.events import MigrationEvent as _MigrationEvent

    ui.add_head_html(_HEAD_HTML)

    storage = app.storage.user
    if storage.get("mode") != "orchestrated":
        ui.navigate.to("/validate")
        return

    # Check for cached exports
    export_dir = Path(str(storage.get("export_dir", "")))
    cached = _detect_cached_exports(export_dir) if export_dir.exists() else None

    # --- Cached export card (shown only when cached exports found) ---
    if cached is not None:
        size_mb = cached["total_size"] / 1_000_000
        with ui.column().classes(
            "w-full items-center min-h-screen bg-gray-50 py-10"
        ) as cached_view:
            with ui.element("div").classes("w-full max-w-2xl fade-in"):
                _render_step_indicator(active_step=2)
            with ui.card().classes("w-full max-w-2xl shadow-md fade-in"):
                ui.label("Found cached exports").classes("text-xl font-bold text-center mt-2")
                ui.label(f"{cached['file_count']} files \u00b7 {size_mb:.1f} MB").classes(
                    "text-sm text-gray-500 text-center mb-4"
                )
                with ui.row().classes("w-full justify-center gap-4 mt-2"):
                    # Use Cached: skip export, clear the now-unneeded Discord token
                    # (mirrors the export finally's discord-only clear — no skip-path
                    # leak; the Stoat token survives for /validate). The client is
                    # connected during a click, so the tab-store access is safe.
                    def _use_cached() -> None:
                        _clear_tokens(app.storage.tab, ("discord_token",))
                        ui.navigate.to("/validate")

                    ui.button("Use Cached", on_click=_use_cached).props("color=green")

                    # export_view / _run_export are defined below; closures are only
                    # invoked on click.
                    def _re_export() -> None:
                        cached_view.set_visibility(False)
                        export_view.set_visibility(True)
                        background_tasks.create(_run_export())

                    ui.button("Re-export", on_click=_re_export).props("color=grey")

    # --- Normal export UI ---
    with ui.column().classes("w-full items-center min-h-screen bg-gray-50 py-10") as export_view:
        if cached is not None:
            export_view.set_visibility(False)

        with ui.element("div").classes("w-full max-w-2xl fade-in"):
            _render_step_indicator(active_step=2)

        with ui.card().classes("w-full max-w-2xl shadow-md fade-in"):
            ui.label("Exporting from Discord").classes("text-2xl font-bold text-center mt-2")
            ui.label("Running DiscordChatExporter...").classes(
                "text-sm text-gray-500 text-center mb-4"
            )

            progress_bar = ui.linear_progress(value=0).classes("w-full")
            progress_bar.props("stripe animated color=blue")

            channel_label = ui.label("Preparing...").classes("text-sm text-gray-600 mt-2")
            log_display = ui.log(max_lines=200).classes("w-full h-48 font-mono text-xs mt-4")

            cancel_event = asyncio.Event()

            with ui.row().classes("w-full justify-end gap-2 mt-4"):
                ui.button(
                    "Cancel",
                    on_click=lambda: cancel_event.set(),
                ).classes("bg-red-600 text-white")

    def on_export_event(event: _MigrationEvent) -> None:
        with contextlib.suppress(Exception):
            if event.phase != "export":
                return
            log_display.push(f"[{event.status}] {event.message}")
            if event.status == "progress":
                if event.total > 0:
                    progress_bar.set_value(event.current / event.total)
                # The parser controls the label format. event.message is set
                # for PerChannel ("<ch> (<pct>%)" or "Finished <ch>") and Phase
                # ("Fetching channels...", etc.) events. Fall back to channel_name
                # only if message is somehow empty.
                if event.message:
                    channel_label.set_text(event.message)
                elif event.channel_name:
                    channel_label.set_text(event.channel_name)
            elif event.status == "completed":
                channel_label.set_text("Export complete!")
                progress_bar.set_value(1.0)
            elif event.status == "warning":
                # Warnings (e.g. "DCE reports N of M channels...") deserve label
                # surface time, not just the log panel.
                channel_label.set_text(event.message)
            elif event.status == "error":
                channel_label.set_text(f"Error: {event.message}")
            elif event.status == "heartbeat":
                # Heartbeat events surface in the log only; do not touch
                # progress_bar or channel_label (which represent real progress
                # and should not be perturbed by liveness pings).
                pass  # log_display.push() already happened above

    async def _run_export() -> None:
        from discord_ferry.errors import DiscordAuthError, DotNetMissingError
        from discord_ferry.exporter import (
            detect_dotnet,
            download_dce,
            get_dce_path,
            run_dce_export,
            validate_discord_token,
        )

        # Tokens live in the memory-only tab store; need a connected client to read.
        await ui.context.client.connected()
        if not app.storage.tab.get("discord_token"):
            # Restart edge: stale non-secret keys on disk but a fresh tab with no
            # token. Bounce cleanly rather than raising DiscordAuthError("").
            ui.notify("Session expired — re-enter your token", type="warning")
            ui.navigate.to("/")
            return

        discord_token = str(app.storage.tab.get("discord_token", ""))
        discord_server = str(storage.get("discord_server_id", ""))
        export_dir = Path(str(storage["export_dir"]))

        try:
            on_export_event(
                _MigrationEvent(
                    phase="export",
                    status="started",
                    message="Validating Discord token...",
                )
            )
            await validate_discord_token(discord_token)

            on_export_event(
                _MigrationEvent(
                    phase="export",
                    status="progress",
                    message="Checking for DCE binary...",
                )
            )
            dce_path = get_dce_path()
            if dce_path is None:
                on_export_event(
                    _MigrationEvent(
                        phase="export",
                        status="progress",
                        message="Downloading DCE...",
                    )
                )
                dce_path = await download_dce(on_export_event)

            on_export_event(
                _MigrationEvent(
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

            config = FerryConfig(
                export_dir=export_dir,
                stoat_url=str(storage.get("stoat_url", "")),
                token=str(app.storage.tab.get("token", "")),
                discord_token=discord_token,
                discord_server_id=discord_server,
                cancel_event=cancel_event,
            )

            on_export_event(
                _MigrationEvent(
                    phase="export",
                    status="progress",
                    message="Launching DiscordChatExporter...",
                )
            )
            await run_dce_export(config, dce_path, on_export_event)

            on_export_event(
                _MigrationEvent(
                    phase="export",
                    status="completed",
                    message="Export complete!",
                )
            )

            # Auto-navigate to validate
            await asyncio.sleep(1)
            ui.navigate.to("/validate")

        except DiscordAuthError as exc:
            on_export_event(_MigrationEvent(phase="export", status="error", message=str(exc)))
            ui.notify(f"Discord auth failed: {exc}", type="negative")
        except DotNetMissingError as exc:
            on_export_event(_MigrationEvent(phase="export", status="error", message=str(exc)))
            ui.notify(str(exc), type="negative")
        except Exception as exc:
            on_export_event(_MigrationEvent(phase="export", status="error", message=str(exc)))
            ui.notify(f"Export failed: {exc}", type="negative")
        finally:
            # Clear ONLY the Discord token here — export is its sole consumer.
            # The Stoat token MUST survive for /migrate (which reads it from the
            # tab store). The pre-migration page guards now check export_dir/
            # stoat_url, and migrate re-checks the tab token after connected(), so
            # clearing the Discord token here no longer bounces the user. Tokens
            # live in the memory-only tab store; suppress in case the task is
            # cancelled before connected() (the tab access would otherwise raise
            # and mask the original exception).
            with contextlib.suppress(Exception):
                _clear_tokens(app.storage.tab, ("discord_token",))

    if _should_auto_export(cached):
        background_tasks.create(_run_export())


# ---------------------------------------------------------------------------
# Screen 3: Validate
# ---------------------------------------------------------------------------


@ui.page("/validate")
def validate_page() -> None:
    """Validate screen — show parsed export summary, warnings, and ETA."""
    ui.add_head_html(_HEAD_HTML)

    storage = app.storage.user
    # Guard on the non-secret setup-complete signal (export_dir + stoat_url are
    # written in the same validate pass as the token). The token itself now lives
    # in the memory-only tab store and is re-checked at /migrate. See Batch 8 / F8b.
    if not storage.get("export_dir") or not storage.get("stoat_url"):
        ui.navigate.to("/")
        return

    export_dir = Path(storage["export_dir"])
    rate_limit: float = float(storage.get("rate_limit", 1.0))

    with ui.column().classes("w-full items-center min-h-screen bg-gray-50 py-10"):
        with ui.element("div").classes("w-full max-w-2xl fade-in"):
            _render_step_indicator(active_step=3)

        with ui.card().classes("w-full max-w-2xl shadow-md fade-in"):
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
                {
                    "item": "Total attachment size",
                    "count": _format_bytes(int(summary["attachment_bytes"])),
                },
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
                start_btn = (
                    ui.button("Start Migration", on_click=lambda: ui.navigate.to("/migrate"))
                    .props("color=amber-7 text-color=white unelevated")
                    .classes("font-semibold")
                )
                if has_rendered_markdown:
                    start_btn.disable()


# ---------------------------------------------------------------------------
# Screen 3: Migrate
# ---------------------------------------------------------------------------


@ui.page("/migrate")
async def migrate_page() -> None:
    """Migration screen — run the engine, show live phase/progress/log."""
    ui.add_head_html(_HEAD_HTML)

    storage = app.storage.user
    if not storage.get("export_dir") or not storage.get("stoat_url"):
        ui.navigate.to("/")
        return

    # Tokens live in the memory-only tab store; need a connected client to read it.
    await ui.context.client.connected()
    if not app.storage.tab.get("token"):
        # Restart edge: stale non-secret keys on disk but a fresh tab, no token.
        ui.notify("Session expired — re-enter your token", type="warning")
        ui.navigate.to("/")
        return

    # Check for a previous migration state — offer resume or fresh start.
    output_dir = Path("./ferry-output")
    state_path = output_dir / "state.json"
    previous_state = None
    if state_path.exists():
        with contextlib.suppress(Exception):
            previous_state = load_state(output_dir)

    # Flag to gate migration start behind the resume/fresh choice.
    resume_choice_made = asyncio.Event()
    needs_resume_choice = previous_state is not None and not previous_state.is_dry_run

    with ui.column().classes("w-full items-center"):  # noqa: SIM117
        with ui.element("div").classes("w-full max-w-3xl fade-in mt-10 mb-0"):
            _render_step_indicator(active_step=4)

    if needs_resume_choice:
        with ui.card().classes("w-full max-w-2xl mx-auto mb-4 bg-amber-50 border-amber-300"):
            msgs_done = len(previous_state.message_map)  # type: ignore[union-attr]
            phase = previous_state.current_phase or "unknown"  # type: ignore[union-attr]
            ui.label("Previous migration found").classes("text-lg font-bold text-amber-800")
            ui.label(
                f"Phase: {phase} | Messages: {msgs_done:,} | Errors: {len(previous_state.errors)}"  # type: ignore[union-attr]
            ).classes("text-amber-700")
            with ui.row():

                def _set_resume(val: bool) -> None:
                    storage["resume"] = val
                    resume_choice_made.set()

                ui.button("Resume", on_click=lambda: _set_resume(True)).classes(
                    "bg-amber-600 text-white"
                )
                ui.button("Start Fresh", on_click=lambda: _set_resume(False)).classes(
                    "bg-gray-400 text-white"
                )
    else:
        resume_choice_made.set()  # No previous state — start immediately.

    # Build FerryConfig from stored values
    pause_event = asyncio.Event()
    pause_event.set()  # start unpaused
    cancel_event = asyncio.Event()

    config = FerryConfig(
        export_dir=Path(storage["export_dir"]),
        stoat_url=storage["stoat_url"],
        token=app.storage.tab["token"],
        server_id=storage.get("server_id") or None,
        server_name=storage.get("server_name") or None,
        dry_run=bool(storage.get("dry_run", False)),
        message_rate_limit=float(storage.get("rate_limit", 1.0)),
        skip_messages=bool(storage.get("skip_messages", False)),
        skip_emoji=bool(storage.get("skip_emoji", False)),
        skip_reactions=bool(storage.get("skip_reactions", False)),
        skip_threads=bool(storage.get("skip_threads", False)),
        thread_strategy=str(storage.get("thread_strategy", "flatten")),
        output_dir=output_dir,
        resume=bool(storage.get("resume", False)),
        pause_event=pause_event,
        cancel_event=cancel_event,
        skip_export=True,  # Export already done by this point (via /export or offline mode)
        discord_token=app.storage.tab.get("discord_token") or None,
        discord_server_id=storage.get("discord_server_id") or None,
        **_coerce_advanced_settings(storage),
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
    rollback_btn: ui.button
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
                native_fidelity_label = ui.label("").classes("text-sm text-gray-500")
                native_fidelity_label.set_visibility(False)
                with ui.row().classes("gap-2 mt-2"):
                    open_report_btn = ui.button(
                        "Open Report", on_click=lambda: _open_report()
                    ).classes("bg-green-600 text-white")
                    rollback_btn = ui.button(
                        "Rollback this migration",
                        on_click=lambda: _start_rollback(),
                    ).classes("bg-red-600 text-white")

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
                _open_path(report_path[0])  # report_path[0] is already a Path
            except Exception as exc:
                ui.notify(f"Could not open report: {exc}", type="negative")

    def _show_review_dialog(detail: dict[str, object]) -> None:
        """Show a blocking review dialog with migration summary."""
        with ui.dialog() as review_dialog, ui.card().classes("w-96"):
            ui.label("Pre-Migration Review").classes("text-xl font-bold mb-4")

            with ui.column().classes("gap-1 w-full"):
                ui.label(f"Server: {detail.get('server_name', '')}").classes("font-medium")
                ui.separator()
                items = [
                    ("Roles", detail.get("roles", 0)),
                    ("Categories", detail.get("categories", 0)),
                    ("Channels", detail.get("channels", 0)),
                    ("Emoji", detail.get("emoji", 0)),
                    ("Messages", f"{detail.get('messages', 0):,}"),
                    ("Threads", detail.get("threads", 0)),
                ]
                for label, value in items:
                    with ui.row().classes("justify-between w-full"):
                        ui.label(label)
                        ui.label(str(value)).classes("font-medium")

                perm_text = "Yes" if detail.get("has_permissions") else "No"
                with ui.row().classes("justify-between w-full"):
                    ui.label("Permissions")
                    ui.label(perm_text).classes("font-medium")

                nsfw = detail.get("nsfw_channels", 0)
                if nsfw:
                    with ui.row().classes("justify-between w-full"):
                        ui.label("NSFW Channels")
                        ui.label(str(nsfw)).classes("font-medium")

                warnings_list = detail.get("warnings", [])
                if isinstance(warnings_list, list) and warnings_list:
                    ui.separator()
                    for w in warnings_list:
                        ui.label(f"⚠ {w}").classes("text-amber-600 text-sm")

            ui.separator()
            with ui.row().classes("justify-end gap-2 mt-2"):

                def _cancel() -> None:
                    cancel_event.set()
                    pause_event.set()  # Unblock engine so it can check cancel
                    review_dialog.close()

                def _proceed() -> None:
                    pause_event.set()  # Unblock engine
                    review_dialog.close()

                ui.button("Cancel", on_click=_cancel).classes("bg-gray-400 text-white")
                ui.button("Proceed", on_click=_proceed).classes("bg-blue-600 text-white")

        review_dialog.open()

    def _show_rollback_dialog(summary: object) -> None:
        """Show a blocking rollback confirmation dialog (issue #10).

        ``summary`` is a ``RollbackSummary`` dataclass (passed through
        ``event.detail["summary"]``). Per-suspect checkboxes set
        ``UntrackedSuspectChannel.opted_in``; engine reads that field
        when building the channel-delete task list. Confirm releases
        ``pause_event``; Cancel sets ``cancel_event`` and releases the
        gate so the engine's cancel-check fires.
        """
        # Pull fields off the dataclass with getattr so this works even if
        # the import fails (defensive — the engine should always send a
        # well-formed RollbackSummary).
        suspects = list(getattr(summary, "untracked_ferry_suspect", []) or [])

        with ui.dialog() as rollback_dialog, ui.card().classes("w-[36rem]"):
            ui.label("Pre-Rollback Review").classes("text-xl font-bold mb-2")
            server_name = getattr(summary, "stoat_server_name", "")
            server_id = getattr(summary, "stoat_server_id", "")
            ui.label(f"Server: {server_name} ({server_id})").classes("font-medium")
            ui.separator()

            with ui.column().classes("gap-1 w-full"):
                items = [
                    ("Channels to delete", len(getattr(summary, "channels_to_delete", []))),
                    ("Roles to delete", len(getattr(summary, "roles_to_delete", []))),
                    ("Emoji to delete", len(getattr(summary, "emoji_to_delete", []))),
                    ("Categories to clean", getattr(summary, "categories_to_clean", 0)),
                ]
                for label, value in items:
                    with ui.row().classes("justify-between w-full"):
                        ui.label(label)
                        ui.label(str(value)).classes("font-medium")

                orphans = getattr(summary, "autumn_orphan_count", 0)
                if orphans:
                    with ui.row().classes("justify-between w-full"):
                        ui.label("Autumn orphan uploads (NOT deleted)")
                        ui.label(str(orphans)).classes("font-medium text-amber-600")

                if getattr(summary, "has_failures_from_prior_run", False):
                    ui.label("⚠ Prior-run failures present in state.json").classes(
                        "text-amber-600 text-sm"
                    )

            # Per-item opt-in for untracked-Ferry-suspect channels.
            checkbox_widgets: list[tuple[Any, Any]] = []
            if suspects:
                ui.separator()
                ui.label("Untracked-Ferry-suspect channels").classes("font-bold mt-2")
                ui.label(
                    "Present on Stoat, absent from state.json. "
                    "Opt in per channel to delete; leave unchecked to keep."
                ).classes("text-sm text-gray-600 mb-2")

                # Header row.
                with ui.row().classes("w-full text-xs font-bold text-gray-700"):
                    ui.label("").classes("w-8")
                    ui.label("Name").classes("flex-1")
                    ui.label("Created (UTC)").classes("w-48")
                    ui.label("Stoat ID").classes("w-64")

                for suspect in suspects:
                    name = getattr(suspect, "name", "") or "(no name)"
                    # Display-layer translation: None → "unknown" so users don't see literal "None".
                    created = getattr(suspect, "created_at_iso", None) or "unknown"
                    stoat_id = getattr(suspect, "stoat_id", "")
                    with ui.row().classes("w-full items-center"):
                        cb = ui.checkbox(value=False)
                        ui.label(name).classes("flex-1")
                        ui.label(created).classes("w-48 text-xs")
                        ui.label(stoat_id).classes("w-64 text-xs font-mono")
                        checkbox_widgets.append((cb, suspect))

            ui.separator()
            with ui.row().classes("justify-end gap-2 mt-2"):

                def _cancel() -> None:
                    cancel_event.set()
                    pause_event.set()
                    rollback_dialog.close()

                def _proceed() -> None:
                    # Sync checkbox state back into the suspect dataclasses
                    # so the engine reads opted_in=True when building tasks.
                    for cb_widget, suspect_obj in checkbox_widgets:
                        with contextlib.suppress(Exception):
                            suspect_obj.opted_in = bool(cb_widget.value)
                    pause_event.set()
                    rollback_dialog.close()

                ui.button("Cancel", on_click=_cancel).classes("bg-gray-400 text-white")
                ui.button("Roll back", on_click=_proceed).classes("bg-red-600 text-white")

        rollback_dialog.open()

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

        # Update phase chip colour and text indicator (WCAG 1.4.1)
        chip = phase_chips.get(event.phase)
        if chip is not None:
            colour = _STATUS_COLOUR.get(event.status, "grey")
            chip.props(f"color={colour}")
            base_label = _PHASE_LABELS.get(event.phase, event.phase)
            indicator = {
                "completed": " ✓",
                "error": " ✗",
                "skipped": " —",
                "started": " ●",
                "progress": " ●",
                "warning": " ⚠",
                "confirm": " ?",
            }.get(event.status, "")
            chip.set_text(f"{base_label}{indicator}")

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
                    # Post-pipeline phases (e.g. "validate_migration") have no
                    # progress slot; _phase_progress returns None for them rather
                    # than crashing on PHASE_ORDER.index().
                    frac = _phase_progress(event.phase)
                    if frac is not None:
                        progress_bar.set_value(frac)
            case "confirm":
                if event.detail:
                    _show_review_dialog(event.detail)
            case "confirm_rollback":
                # Without this arm the GUI silently ignores the confirmation event
                # and the rollback hangs forever on pause_event. Guarded by SC-31.
                if event.detail and "summary" in event.detail:
                    _show_rollback_dialog(event.detail["summary"])

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
            try:
                report = json.loads(report_file.read_text(encoding="utf-8"))
                nf = report.get("native_fidelity") or {}
                if nf:
                    native_fidelity_label.set_visibility(True)
                    native_fidelity_label.set_text(
                        f"Native fidelity: slowmode={nf.get('slowmode', 0)}, "
                        f"user_limit={nf.get('user_limit', 0)}, "
                        f"role_icons={nf.get('role_icons', 0)}"
                    )
            except (OSError, ValueError):
                pass
        else:
            open_report_btn.disable()
        progress_bar.set_value(1.0)
        ui.notify("Migration complete!", type="positive")

    def _start_rollback() -> None:
        """Launch a rollback in a background task.

        Reuses the page's config + on_event handler. Creates fresh pause/cancel
        events so the rollback's lifecycle is independent of the prior
        migration's. The engine emits ``confirm_rollback`` before any DELETE;
        the existing on_event match arm routes that to ``_show_rollback_dialog``.
        """
        # Fresh events for the rollback's lifecycle.
        new_pause = asyncio.Event()
        new_cancel = asyncio.Event()
        config.pause_event = new_pause
        config.cancel_event = new_cancel
        # Update the closure-captured locals so the dialog's cancel button
        # writes to the rollback's cancel_event, not the migration's.
        nonlocal pause_event, cancel_event
        pause_event = new_pause
        cancel_event = new_cancel

        rollback_btn.disable()

        async def _run() -> None:
            try:
                state = load_state(config.output_dir)
                await run_rollback(config, state, exports=[], on_event=on_event)
                ui.notify("Rollback complete!", type="positive")
            except MigrationError as exc:
                log_display.push(f"[ERROR] Rollback failed: {exc}")
                ui.notify(f"Rollback failed: {exc}", type="negative")
            except Exception as exc:
                log_display.push(f"[ERROR] Unexpected error: {exc}")
                ui.notify(f"Unexpected error: {exc}", type="negative")
            finally:
                rollback_btn.enable()

        background_tasks.create(_run())

    # ---------------------------------------------------------------------------
    # Start migration in background
    # ---------------------------------------------------------------------------

    async def _run() -> None:
        # Wait for the user to choose Resume or Start Fresh before starting.
        await resume_choice_made.wait()

        # Rebuild config with the final resume choice.
        config.resume = bool(storage.get("resume", False))

        try:
            await run_migration(config, on_event=on_event)
        except MigrationError as exc:
            log_display.push(f"[ERROR] Migration failed: {exc}")
            errors_label.set_text(f"Errors: {error_count} (FAILED)")
            ui.notify(f"Migration failed: {exc}", type="negative")
        except Exception as exc:
            log_display.push(f"[ERROR] Unexpected error: {exc}")
            ui.notify(f"Unexpected error: {exc}", type="negative")
        finally:
            # Terminal owner of session-token cleanup, for BOTH offline and
            # orchestrated modes, on success AND error. (Offline mode never
            # visits /export, so its finally is the only cleanup point.) Tokens
            # live in the memory-only tab store; clearing here is good hygiene
            # (immediate, rather than waiting for the tab to close). Safe because
            # this runs AFTER run_migration returns: config already holds its own
            # token copy (a plain str), the engine never reads app.storage, and
            # the only in-_run storage read (config.resume, above) has already
            # completed. Do NOT move token reads below this. Suppress for parity
            # with the export finally: if the tab closed during a long migration,
            # this background task's tab access would otherwise raise on the now-
            # disconnected client (the token is memory-only — never on disk).
            with contextlib.suppress(Exception):
                _clear_tokens(app.storage.tab, _SESSION_TOKEN_KEYS)

    background_tasks.create(_run())


# ---------------------------------------------------------------------------
# Native window lifecycle
# ---------------------------------------------------------------------------

# Each rung of the teardown ladder waits at most this long. A healthy teardown was
# measured at ~16ms; this budget only bites when a child is wedged, and it must stay
# small because macOS escalates a force-quit SIGTERM to SIGKILL within seconds.
_TEARDOWN_JOIN_TIMEOUT = 0.5


def _terminate_children(
    children: Sequence[Any],
    window: Any | None,
    join_timeout: float = _TEARDOWN_JOIN_TIMEOUT,
) -> None:
    """Tear down the native window child: destroy -> terminate -> kill, all bounded.

    Idempotent: returns immediately when no child is alive, *without touching the
    window proxy*. That precondition matters because NiceGUI's ``check_shutdown``
    thread closes the proxy's method queues as soon as the server stops, so an
    unconditional ``destroy()`` would raise on every healthy shutdown.

    ``children`` and ``window`` are injected so the ladder is testable without a real
    window; :func:`_teardown_native_window` supplies the production values.
    """
    alive = [child for child in children if child.is_alive()]
    if not alive:
        return

    # Rung 1: the supported path. NiceGUI's own app.shutdown() closes the window this
    # way, and the child's method-executor thread keeps polling every 16ms even while
    # its Cocoa main thread is parked.
    if window is not None:
        try:
            window.destroy()
        except Exception:  # noqa: BLE001 - a closed queue must not abort the ladder
            logger.debug("native window destroy() failed", exc_info=True)
        for child in alive:
            child.join(timeout=join_timeout)
        alive = [child for child in alive if child.is_alive()]
        if not alive:
            return

    # Rung 2: SIGTERM, which is sufficient in practice (measured: child gone in 16ms).
    for child in alive:
        try:
            child.terminate()
        except Exception:  # noqa: BLE001 - best effort; rung 3 is the backstop
            logger.debug("terminate() failed for pid %s", child.pid, exc_info=True)
    for child in alive:
        child.join(timeout=join_timeout)
    alive = [child for child in alive if child.is_alive()]
    if not alive:
        return

    # Rung 3: SIGKILL. A belt rather than a measured necessity -- but a child that
    # outlives this ladder would hang the interpreter in multiprocessing's atexit
    # join, which has no timeout at all.
    for child in alive:
        try:
            child.kill()
        except Exception:  # noqa: BLE001 - nothing left to escalate to
            logger.debug("kill() failed for pid %s", child.pid, exc_info=True)
    for child in alive:
        child.join(timeout=join_timeout)

    survivors = [child.pid for child in alive if child.is_alive()]
    if survivors:
        logger.warning("native window child(ren) survived teardown: %s", survivors)


def _teardown_native_window() -> None:
    """Synchronous teardown against the live process table. Safe to call anywhere.

    Never raises. Both call sites are shutdown paths -- ``app.on_shutdown`` (where an
    exception would propagate into uvicorn's lifespan shutdown and skip NiceGUI's
    remaining handlers) and the ``finally:`` in :func:`_run_gui` (where it would mask
    whatever exception was already unwinding). The inner ladder guards the calls it
    makes on the window and the children; this guards everything else, including
    ``active_children()``, ``is_alive()`` and ``join()``.
    """
    try:
        _terminate_children(
            multiprocessing.active_children(),
            getattr(app.native, "main_window", None),
        )
    except Exception:  # noqa: BLE001 - a shutdown path must not raise
        logger.warning("native window teardown failed", exc_info=True)


async def _teardown_native_window_async() -> None:
    """Async wrapper for ``app.on_shutdown``, so the joins never block the event loop."""
    await asyncio.to_thread(_teardown_native_window)


def _folder_dialog_type() -> int:
    """Resolve pywebview's folder-dialog constant.

    Resolved lazily, never at import time: ``webview`` is an unbound name when the
    optional dependency is missing. Prefers ``FileDialog.FOLDER``; falls back to the
    deprecated ``FOLDER_DIALOG`` because pyproject floors pywebview at >=5.0.
    """
    file_dialog = getattr(webview, "FileDialog", None)
    if file_dialog is not None:
        return int(file_dialog.FOLDER)
    return int(webview.FOLDER_DIALOG)


async def _pick_folder(window: Any | None) -> str | None:
    """Ask the native window for a folder; None when unavailable or cancelled.

    Must go through NiceGUI's ``WindowProxy`` (``app.native.main_window``), which
    marshals the call to the child over the method queue. ``webview.windows`` is always
    empty in this process -- the window is created in another one -- so the previous
    ``webview.windows[0]`` could only ever raise IndexError.
    """
    if window is None:
        return None
    try:
        result = await window.create_file_dialog(_folder_dialog_type())
    except Exception:  # noqa: BLE001 - a dead window must not break the page
        logger.debug("native folder dialog failed", exc_info=True)
        return None
    if not result:
        return None
    return str(result[0])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _run_gui() -> None:
    """Start the UI and guarantee the native window child dies with the server."""
    native = _HAS_WEBVIEW

    if native:
        # Fires on every path that reaches the ASGI lifespan shutdown: window close,
        # Cmd-Q, Dock quit, force quit (SIGTERM) and a single Ctrl-C.
        app.on_shutdown(_teardown_native_window_async)

    try:
        ui.run(
            title="Discord Ferry",
            host="127.0.0.1",
            port=8765,
            native=native,
            reload=False,
            storage_secret=os.environ.get("FERRY_STORAGE_SECRET", secrets.token_hex(32)),
            # NiceGUI derives ping_interval = max(rt * 0.8, 4) and
            # ping_timeout = max(rt * 0.4, 2) from this, so the 3.0 default leaves the
            # browser only ~6s of tolerance before it shows "Connection lost".
            # 10.0 -> 8s/4s = 12s, still prompt about a genuinely dead server.
            reconnect_timeout=10.0,
            # uvicorn's graceful drain is unbounded by default and runs BEFORE the
            # shutdown hook. The only connection is the local websocket, which closes
            # immediately, so keep this short: a force-quit SIGKILL landing mid-drain
            # would orphan the window, which is the whole failure this prevents.
            timeout_graceful_shutdown=1,
        )
    finally:
        # Covers what the lifespan hook cannot: uvicorn's force_exit path (double
        # Ctrl-C) and a failure during startup. Without it a surviving daemon child
        # would hang the interpreter in multiprocessing's atexit join, which has no
        # timeout -- a hung Ferry still holding port 8765, worse than an orphan.
        if native:
            _teardown_native_window()


def main() -> None:
    """Launch the NiceGUI local web UI."""
    # MUST be the first statement. PyInstaller's rthook only rebinds
    # multiprocessing.freeze_support(); the diversion to spawn_main happens when it is
    # called, and NiceGUI only calls it deep inside ui.run(). Without this, the frozen
    # window child re-runs everything above ui.run() before diverting.
    multiprocessing.freeze_support()

    try:
        _run_gui()
    except KeyboardInterrupt:
        # The interrupt can arrive after uvicorn restores the default SIGINT handler,
        # or from NiceGUI's check_shutdown thread calling _thread.interrupt_main().
        # Exit with the conventional SIGINT status rather than a silent 0.
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
