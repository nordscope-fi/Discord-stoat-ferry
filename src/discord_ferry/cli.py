"""CLI entry point for Discord Ferry (power users / Linux)."""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import urlparse

import aiohttp  # noqa: TCH002
import click
from dotenv import load_dotenv
from rich.console import Console
from rich.errors import MarkupError
from rich.live import Live
from rich.markup import escape
from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn
from rich.table import Table

from discord_ferry import __version__
from discord_ferry.config import FerryConfig
from discord_ferry.core.engine import PHASE_ORDER, run_migration, run_rollback
from discord_ferry.core.http import format_proxy_notices, new_session
from discord_ferry.core.logging_setup import configure_logging
from discord_ferry.core.security import register_secret
from discord_ferry.errors import CheckError, MigrationError, StateError
from discord_ferry.migrator.api import (
    api_create_channel,
    api_create_role,
    api_create_server,
    api_edit_role,
    api_set_role_permissions,
    api_upsert_categories,
    init_request_semaphore,
)
from discord_ferry.parser.dce_parser import (
    acknowledgement_required,
    parse_export_directory,
    validate_export,
)
from discord_ferry.state import MigrationState, load_state
from discord_ferry.stats import summarize_state

if TYPE_CHECKING:
    from discord_ferry.core.events import MigrationEvent
    from discord_ferry.migrator.verify import CheckReport
    from discord_ferry.parser.models import DCEExport
    from discord_ferry.review import RollbackSummary
    from discord_ferry.stats import StateSummary

console = Console()

# Phase status icons for the progress display.
_STATUS_ICONS: dict[str, str] = {
    "pending": "  ",
    "started": ">>",
    "progress": ">>",
    "completed": "OK",
    "skipped": "--",
    "error": "!!",
    "warning": ">>",
    "confirm": "??",
}


def _print_proxy_notices(*, to_stderr: bool = False) -> None:
    """Render format_proxy_notices() for the three entry points that do real
    network work outside run_migration (build, rollback, probe). run_migration's
    own preflight already emits these through the event stream (core/engine.py);
    this covers the paths that never construct a MigrationEvent at all.

    `probe --json` prints machine-readable JSON to stdout, so its call passes
    to_stderr=True to keep a notice off the channel a script parses.
    """
    sink = Console(stderr=True) if to_stderr else console
    for line in format_proxy_notices():
        sink.print(f"[cyan][i][/] {line}")


def _format_eta(total_messages: int, rate_limit: float) -> str:
    """Format an ETA string from message count and rate limit."""
    seconds = int(total_messages * rate_limit)
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours > 0:
        return f"~{hours}h {minutes}m"
    return f"~{minutes}m"


def _build_validate_table(exports: list[DCEExport]) -> Table:
    """Build a Rich table summarising parsed exports."""
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

    table = Table(title="Export Summary", show_header=True, header_style="bold")
    table.add_column("Item", style="cyan")
    table.add_column("Count", justify="right")

    table.add_row("Channels", str(len(exports)))
    table.add_row("Categories", str(len(categories)))
    table.add_row("Roles", str(len(roles)))
    table.add_row("Messages", f"{total_messages:,}")
    table.add_row("Attachments", f"{total_attachments:,}")
    table.add_row("Custom Emoji", str(len(emoji_ids)))
    table.add_row("Threads/Forums", str(threads))

    return table


def _build_stats_table(summary: StateSummary) -> Table:
    """Build the main migration-stats summary table.

    Renders entity counts, message counters, fidelity score with sub-scores,
    error/warning summary with truncated last-message preview, and elapsed
    duration with trinary state handling.
    """
    dry_tag = " [DRY-RUN]" if summary.is_dry_run else ""
    title = f"Migration Stats — Stoat ID: {summary.stoat_server_id}{dry_tag}"

    table = Table(title=title, show_header=True, header_style="bold")
    table.add_column("Section / Item", style="cyan")
    table.add_column("Value", justify="right")

    # Entities
    table.add_row("[bold]Entities[/]", "")
    table.add_row("  Channels", str(summary.channels))
    table.add_row("  Roles", str(summary.roles))
    table.add_row("  Categories", str(summary.categories))
    table.add_row("  Emojis", str(summary.emojis))
    table.add_row("  Messages migrated", f"{summary.messages:,}")

    # Counters
    table.add_row("[bold]Counters[/]", "")
    table.add_row("  Attachments uploaded", f"{summary.attachments_uploaded:,}")
    table.add_row("  Attachments skipped", f"{summary.attachments_skipped:,}")
    table.add_row("  Pins applied", str(summary.pins_applied))
    table.add_row("  Reactions applied", f"{summary.reactions_applied:,}")
    table.add_row(
        "  Replies linked / total",
        f"{summary.replies_linked:,} / {summary.replies_total:,}",
    )
    table.add_row(
        "  Embeds total / dropped",
        f"{summary.embeds_total:,} / {summary.embeds_dropped:,}",
    )
    table.add_row("  Failed messages", str(summary.failed_messages))
    table.add_row("  Prior messages total", f"{summary.prior_messages_total:,}")

    # Fidelity
    fb = summary.fidelity
    table.add_row("[bold]Fidelity[/]", "")
    table.add_row("  Overall", f"{fb.overall:.1f}%")
    table.add_row("  Messages", f"{fb.messages:.1f}%")
    table.add_row("  Attachments", f"{fb.attachments:.1f}%")
    table.add_row("  Embeds", f"{fb.embeds:.1f}%" if fb.embeds is not None else "n/a")
    table.add_row("  Replies", f"{fb.replies:.1f}%" if fb.replies is not None else "n/a")
    table.add_row("  Reactions", f"{fb.reactions:.1f}%" if fb.reactions is not None else "n/a")

    # Errors / warnings
    table.add_row("[bold]Errors / Warnings[/]", "")
    if summary.error_count == 0:
        table.add_row("  Errors", "0 (clean)")
    else:
        preview = textwrap.shorten(summary.last_error or "", width=80, placeholder="…")
        table.add_row("  Errors", f"{summary.error_count} — last: {_safe(preview)}")
    if summary.warning_count == 0:
        table.add_row("  Warnings", "0 (clean)")
    else:
        preview = textwrap.shorten(summary.last_warning or "", width=80, placeholder="…")
        table.add_row("  Warnings", f"{summary.warning_count} — last: {_safe(preview)}")

    # Elapsed
    table.add_row("[bold]Timing[/]", "")
    if summary.duration_state == "complete":
        secs = int(summary.duration_seconds or 0)
        hh = secs // 3600
        mm = (secs % 3600) // 60
        ss = secs % 60
        table.add_row("  Elapsed", f"{hh:02d}:{mm:02d}:{ss:02d}")
    elif summary.duration_state == "in_progress":
        table.add_row("  Elapsed", "in progress")
    else:
        table.add_row("  Elapsed", "unknown")

    return table


def _build_channels_table(summary: StateSummary) -> Table | None:
    """Per-channel message breakdown. Returns None when no channels recorded."""
    if not summary.channel_breakdown:
        return None

    sorted_items = sorted(
        summary.channel_breakdown.items(),
        key=lambda kv: kv[1],
        reverse=True,
    )
    top = sorted_items[:20]
    remainder = len(sorted_items) - len(top)

    table = Table(title="Per-Channel Messages", show_header=True, header_style="bold")
    table.add_column("Channel ID", style="cyan")
    table.add_column("Messages", justify="right")
    for ch_id, count in top:
        table.add_row(ch_id, f"{count:,}")
    if remainder > 0:
        table.add_row(f"+{remainder} more", "")
    return table


def _build_rollback_table(summary: StateSummary) -> Table | None:
    """Rollback counters table. Returns None when no rollback recorded."""
    rb = summary.rollback
    if rb is None:
        return None

    table = Table(title="Rollback", show_header=True, header_style="bold")
    table.add_column("Item", style="cyan")
    table.add_column("Value", justify="right")
    table.add_row("Channels deleted", str(rb.channels_deleted))
    table.add_row("Roles deleted", str(rb.roles_deleted))
    table.add_row("Emoji deleted", str(rb.emoji_deleted))
    table.add_row("Categories cleaned", "yes" if rb.categories_cleaned else "no")
    table.add_row("Untracked channels deleted", str(rb.untracked_channels_deleted))
    table.add_row("Failures", str(rb.failure_count))
    table.add_row("Started at", rb.started_at or "—")
    table.add_row("Completed at", rb.completed_at or "—")
    return table


def _safe(value: object) -> str:
    """Escape a user-controlled value for safe Rich-markup interpolation.

    Discord/blueprint names can contain Rich markup metacharacters (``[``, ``]``,
    ``[/]``). Interpolating them raw into a markup string either corrupts the
    rendered output or raises ``rich.errors.MarkupError`` — and since CLI event
    handlers run synchronously inside the engine's ``emit``, that exception would
    abort the whole migration. Escaping neutralises the metacharacters.
    """
    return escape(str(value))


async def _build_blueprint_channel(
    session: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    server_id: str,
    ch: Any,
) -> str:
    """Create a blueprint channel, retrying a failed Voice channel as Text (Stoat Bug #194).

    Mirrors the migration path's voice fallback (``structure.py``). Only a ``"Voice"``-type
    create failure is retried as ``"Text"``; any other ``MigrationError`` propagates. Returns
    the created channel's Stoat ``_id``.
    """
    try:
        result = await api_create_channel(
            session, stoat_url, token, server_id, name=ch.name, channel_type=ch.type, nsfw=ch.nsfw
        )
    except MigrationError:
        if ch.type == "Voice":
            console.print(f"  [yellow]Voice channel '{_safe(ch.name)}' failed, retrying as text[/]")
            result = await api_create_channel(
                session,
                stoat_url,
                token,
                server_id,
                name=ch.name,
                channel_type="Text",
                nsfw=ch.nsfw,
            )
        else:
            raise
    return str(result["_id"])


class _ProgressTracker:
    """Track migration progress and render Rich output with live progress bars."""

    def __init__(self, *, verbose: bool = False) -> None:
        self.verbose = verbose
        self.phase_status: dict[str, str] = {p: "pending" for p in PHASE_ORDER}
        self.messages_sent = 0
        self.error_count = 0
        self.warning_count = 0

        # Progress bars — created but only started inside the Live context.
        self._phase_progress = Progress(
            TextColumn("[bold]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total} phases"),
            transient=True,
        )
        self._msg_progress = Progress(
            TextColumn("  {task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeRemainingColumn(),
            transient=True,
        )
        self._phase_task_id = self._phase_progress.add_task(
            "Migration", total=len(PHASE_ORDER), completed=0
        )
        self._msg_task_id = self._msg_progress.add_task("Messages", total=0, completed=0)
        self._current_channel = ""
        self._live: Live | None = None

    def start_live(self) -> Live:
        """Create and return a Live context for the progress display."""
        self._live = Live(self._make_display(), console=console, refresh_per_second=4)
        return self._live

    def _make_display(self) -> Table:
        """Build a Rich Table combining phase progress, message progress, and stats."""
        grid = Table.grid(padding=(0, 1))
        grid.add_row(self._phase_progress)
        grid.add_row(self._msg_progress)
        stats = (
            f"Messages: {self.messages_sent:,}  "
            f"Errors: {self.error_count}  "
            f"Warnings: {self.warning_count}"
        )
        if self._current_channel:
            stats += f"  Channel: {_safe(self._current_channel)}"
        grid.add_row(stats)
        return grid

    def _log(self, text: str) -> None:
        """Print through the Live console if active, otherwise direct."""
        if self._live is not None:
            self._live.console.print(text)
        else:
            console.print(text)

    def on_event(self, event: MigrationEvent) -> None:
        """Handle a migration event — update state and progress bars."""
        self.phase_status[event.phase] = event.status

        # User-controlled values (event.message, channel_name, server_name, warnings) are escaped
        # before interpolation into Rich markup. The whole render is additionally guarded against
        # MarkupError so a missed escape site can never propagate into the engine's synchronous
        # emit() and abort an otherwise-healthy migration (defense-in-depth).
        try:
            match event.status:
                case "started":
                    self._phase_progress.update(
                        self._phase_task_id, description=f"Phase: {event.phase}"
                    )
                    self._log(f"[bold cyan][>>][/] {event.phase}: {_safe(event.message)}")
                case "completed":
                    completed = sum(1 for s in self.phase_status.values() if s == "completed")
                    self._phase_progress.update(self._phase_task_id, completed=completed)
                    self._log(f"[bold green][OK][/] {event.phase}: {_safe(event.message)}")
                case "skipped":
                    self._log(f"[dim][--][/] {event.phase}: {_safe(event.message)}")
                case "error":
                    self.error_count += 1
                    self._log(f"[bold red][!!][/] {event.phase}: {_safe(event.message)}")
                case "warning":
                    self.warning_count += 1
                    if self.verbose:
                        self._log(f"[yellow][!!][/] {event.phase}: {_safe(event.message)}")
                case "notice":
                    # Printed unconditionally. A configuration problem the user
                    # must see before the run, not a per-item warning. Does NOT
                    # increment warning_count, so the "N warning(s) suppressed"
                    # line stays accurate.
                    self._log(f"[cyan][i][/] {event.phase}: {_safe(event.message)}")
                case "confirm":
                    # Print review summary and ask for confirmation
                    if event.detail:
                        self._log("\n[bold]Pre-Migration Review[/]")
                        review_table = Table(show_header=True, header_style="bold")
                        review_table.add_column("Item", style="cyan")
                        review_table.add_column("Count", justify="right")
                        detail = event.detail
                        review_table.add_row("Server Name", _safe(detail.get("server_name", "")))
                        review_table.add_row("Roles", str(detail.get("roles", 0)))
                        review_table.add_row("Categories", str(detail.get("categories", 0)))
                        review_table.add_row("Channels", str(detail.get("channels", 0)))
                        review_table.add_row("Emoji", str(detail.get("emoji", 0)))
                        review_table.add_row("Messages", f"{detail.get('messages', 0):,}")
                        review_table.add_row("Threads", str(detail.get("threads", 0)))
                        review_table.add_row(
                            "Permissions", "Yes" if detail.get("has_permissions") else "No"
                        )
                        if detail.get("nsfw_channels"):
                            review_table.add_row(
                                "NSFW Channels", str(detail.get("nsfw_channels", 0))
                            )
                        self._log("")
                        console.print(review_table)
                        raw_warnings = detail.get("warnings")
                        warnings_list: list[object] = (
                            raw_warnings if isinstance(raw_warnings, list) else []
                        )
                        for w in warnings_list:
                            self._log(f"  [yellow]Warning: {_safe(w)}[/]")
                    self._log("")
                case "progress":
                    if event.total > 0:
                        self.messages_sent = event.current
                        self._msg_progress.update(
                            self._msg_task_id,
                            total=event.total,
                            completed=event.current,
                        )
                    if event.channel_name:
                        self._current_channel = event.channel_name
                        self._msg_progress.update(
                            self._msg_task_id,
                            description=f"  {_safe(event.channel_name)}",
                        )
                    if self.verbose:
                        self._log(f"[dim]    {_safe(event.message)}[/]")

            # Refresh live display if active.
            if self._live is not None:
                self._live.update(self._make_display())
        except MarkupError:
            # A dynamic value slipped through un-escaped; surface it unstyled rather than abort.
            sink = self._live.console if self._live is not None else console
            sink.print(f"{event.phase}: {event.message}", markup=False)

    def print_summary(self) -> None:
        """Print a final summary line."""
        console.print()
        console.print(
            f"[bold]Done.[/] Messages: {self.messages_sent:,}  "
            f"Errors: {self.error_count}  Warnings: {self.warning_count}"
        )


# ---------------------------------------------------------------------------
# Click commands
# ---------------------------------------------------------------------------

_common_options = [
    click.option(
        "--export-dir",
        type=click.Path(exists=True),
        default=None,
        help="Path to DCE exports (offline mode)",
    ),
    click.option(
        "--discord-token",
        envvar="DISCORD_TOKEN",
        default=None,
        help="Discord user token",
    ),
    click.option(
        "--discord-server", envvar="DISCORD_SERVER_ID", default=None, help="Discord server ID"
    ),
    click.option("--stoat-url", envvar="STOAT_URL", default=None, help="Stoat API base URL"),
    click.option(
        "--token",
        envvar="STOAT_TOKEN",
        default=None,
        help="Stoat user token (from browser Local Storage)",
    ),
    click.option("--server-id", default=None, help="Use existing Stoat server"),
    click.option("--server-name", default=None, help="Name for new server"),
    click.option(
        "--create-invite/--no-create-invite",
        default=True,
        help="Generate an invite to the migrated server (default on)",
    ),
    click.option("--invite-channel-id", default=None, help="Discord channel id to invite to"),
    click.option("--skip-messages", is_flag=True, help="Structure only"),
    click.option("--skip-emoji", is_flag=True, help="Skip emoji upload"),
    click.option("--skip-reactions", is_flag=True, help="Skip reactions"),
    click.option("--skip-threads", is_flag=True, help="Skip threads/forums"),
    click.option(
        "--thread-strategy",
        type=click.Choice(["flatten", "merge", "archive"]),
        default="flatten",
        help="Thread handling: flatten (channels), merge (into parent), archive (markdown export)",
    ),
    click.option("--rate-limit", default=1.0, type=float, help="Seconds between messages"),
    click.option("--upload-delay", default=0.5, type=float, help="Seconds between uploads"),
    click.option("--output-dir", default="./ferry-output", help="Report output directory"),
    click.option("--resume", is_flag=True, help="Resume from state file"),
    click.option(
        "--incremental",
        is_flag=True,
        default=False,
        help="Only migrate new messages since last completed run",
    ),
    click.option("--verbose", "-v", is_flag=True, help="Debug output"),
    click.option(
        "--dry-run",
        is_flag=True,
        default=False,
        help="Run all phases without API calls; test locally",
    ),
    click.option("--max-channels", default=200, type=int, help="Channel limit (self-hosted)"),
    click.option("--max-emoji", default=100, type=int, help="Emoji limit (self-hosted)"),
    click.option("--yes", "-y", is_flag=True, default=False, help="Skip ToS confirmation prompt"),
    click.option(
        "--force", is_flag=True, default=False, help="Override freshness and other soft errors"
    ),
    click.option(
        "--skip-dce-verify",
        is_flag=True,
        default=False,
        help="Skip DCE binary hash verification",
    ),
    click.option(
        "--verify-uploads",
        is_flag=True,
        default=False,
        help="Verify uploaded file size against Autumn response (best-effort)",
    ),
    click.option(
        "--cleanup-orphans",
        is_flag=True,
        default=False,
        help="Detect and log unreferenced Autumn uploads after migration (S16)",
    ),
    click.option(
        "--force-unlock",
        is_flag=True,
        default=False,
        help="Override a stale migration lock on the target server (S17)",
    ),
    click.option(
        "--reaction-mode",
        type=click.Choice(["text", "native", "skip"]),
        default="text",
        help=(
            "How reactions migrate: text = summary appended to the message (fast, default); "
            "native = per-emoji API calls (slow, Stoat cap 20/message); skip = none"
        ),
    ),
    click.option(
        "--min-thread-messages",
        type=click.IntRange(min=0),
        default=0,
        help=(
            "Exclude threads with fewer messages (0 = include all); "
            "applies to every thread strategy"
        ),
    ),
    click.option(
        "--checkpoint-interval",
        type=click.IntRange(min=1),
        default=50,
        help="Save state every N messages (lower = safer, more disk I/O)",
    ),
    click.option(
        "--max-concurrent-channels",
        type=click.IntRange(min=1),
        default=3,
        help="Channels migrated in parallel — raise only on self-hosted instances",
    ),
    click.option(
        "--max-concurrent-requests",
        type=click.IntRange(min=1),
        default=5,
        help="Concurrent API calls across all workers — raise only on self-hosted instances",
    ),
    click.option(
        "--skip-avatars",
        is_flag=True,
        default=False,
        help="Skip the avatar pre-flight phase; avatars still upload on demand during messages",
    ),
    click.option(
        "--validate-after",
        is_flag=True,
        default=False,
        help=(
            "After migration, fetch the server and compare channel/role counts "
            "(results in state.json)"
        ),
    ),
]


F = TypeVar("F", bound=Callable[..., Any])


def _add_options(options: list[Any]) -> Callable[[F], F]:
    """Apply a list of Click decorators to a command."""

    def decorator(func: F) -> F:
        for option in reversed(options):
            func = option(func)
        return func

    return decorator


def _build_config(kwargs: dict[str, Any]) -> FerryConfig:
    """Build a FerryConfig from Click kwargs."""
    export_dir_str = kwargs.get("export_dir")
    discord_token = kwargs.get("discord_token")
    discord_server = kwargs.get("discord_server")

    # Mode detection: orchestrated vs offline
    if export_dir_str and discord_token:
        raise click.UsageError("Cannot use both --export-dir and --discord-token")

    if export_dir_str:
        # Offline mode
        export_dir = Path(export_dir_str)
        skip_export = True
    elif discord_token and discord_server:
        # Orchestrated mode — export_dir will be set to default cache dir
        export_dir = Path(kwargs.get("output_dir", "./ferry-output")) / "dce_cache" / discord_server
        skip_export = False
    else:
        raise click.UsageError(
            "Provide either --export-dir (offline mode) or both "
            "--discord-token and --discord-server (orchestrated mode)"
        )

    # Register before the config exists, so anything logged between here and the
    # engine's _ensure_token_store is already redacted.
    register_secret("stoat", kwargs.get("token") or "")
    if discord_token:
        register_secret("discord", discord_token)

    return FerryConfig(
        export_dir=export_dir,
        stoat_url=kwargs["stoat_url"],
        token=kwargs["token"],
        server_id=kwargs.get("server_id"),
        server_name=kwargs.get("server_name"),
        dry_run=kwargs.get("dry_run", False),
        skip_messages=kwargs.get("skip_messages", False),
        skip_emoji=kwargs.get("skip_emoji", False),
        skip_reactions=kwargs.get("skip_reactions", False),
        skip_threads=kwargs.get("skip_threads", False),
        message_rate_limit=kwargs.get("rate_limit", 1.0),
        upload_delay=kwargs.get("upload_delay", 0.5),
        output_dir=Path(kwargs.get("output_dir", "./ferry-output")),
        resume=kwargs.get("resume", False),
        incremental=kwargs.get("incremental", False),
        verbose=kwargs.get("verbose", False),
        max_channels=kwargs.get("max_channels", 200),
        max_emoji=kwargs.get("max_emoji", 100),
        discord_token=discord_token,
        discord_server_id=discord_server,
        skip_export=skip_export,
        force=kwargs.get("force", False),
        skip_dce_verify=kwargs.get("skip_dce_verify", False),
        verify_uploads=kwargs.get("verify_uploads", False),
        thread_strategy=kwargs.get("thread_strategy", "flatten"),
        cleanup_orphans=kwargs.get("cleanup_orphans", False),
        force_unlock=kwargs.get("force_unlock", False),
        create_invite=kwargs.get("create_invite", True),
        invite_channel_id=kwargs.get("invite_channel_id"),
        reaction_mode=kwargs.get("reaction_mode", "text"),
        min_thread_messages=kwargs.get("min_thread_messages", 0),
        checkpoint_interval=kwargs.get("checkpoint_interval", 50),
        max_concurrent_channels=kwargs.get("max_concurrent_channels", 3),
        max_concurrent_requests=kwargs.get("max_concurrent_requests", 5),
        skip_avatars=kwargs.get("skip_avatars", False),
        validate_after=kwargs.get("validate_after", False),
    )


@click.group(invoke_without_command=True)
@click.version_option(__version__, "--version", prog_name="ferry")
@click.pass_context
def main(ctx: click.Context) -> None:
    """Migrate a Discord server export to Stoat."""
    load_dotenv()
    configure_logging()
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@main.command()
@_add_options(_common_options)
def migrate(**kwargs: Any) -> None:
    """Run the full migration."""
    stoat_url = kwargs.get("stoat_url")
    token = kwargs.get("token")

    if not stoat_url:
        console.print("[bold red]Error:[/] --stoat-url is required (or set STOAT_URL)")
        sys.exit(1)
    if not token:
        console.print("[bold red]Error:[/] --token is required (or set STOAT_TOKEN)")
        sys.exit(1)

    try:
        config = _build_config(kwargs)
    except click.UsageError as exc:
        console.print(f"[bold red]Error:[/] {_safe(exc)}")
        sys.exit(1)

    host = urlparse(config.stoat_url).hostname if config.stoat_url else None
    if host == "api.stoat.chat" and (
        config.max_concurrent_channels > 3 or config.max_concurrent_requests > 5
    ):
        console.print(
            "[yellow]Warning:[/] raising concurrency on the official Stoat service usually "
            "makes runs slower — its rate limits trigger 429 backoff. These flags are "
            "intended for self-hosted instances."
        )

    if not config.skip_export and not kwargs.get("yes"):
        try:
            click.confirm(
                "Using a user token may violate Discord's Terms of Service. Continue?",
                abort=True,
            )
        except click.exceptions.Abort:
            sys.exit(1)

    tracker = _ProgressTracker(verbose=config.verbose)

    console.print("[bold]Discord Ferry[/] — starting migration\n")

    final_state: MigrationState | None = None
    try:
        with tracker.start_live():
            final_state = asyncio.run(run_migration(config, on_event=tracker.on_event))
    except MigrationError as exc:
        console.print(f"\n[bold red]Migration failed:[/] {_safe(exc)}")
        sys.exit(1)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/] State saved — use --resume to continue.")
        sys.exit(130)

    tracker.print_summary()
    if not config.verbose and tracker.warning_count > 0:
        console.print(
            f"[dim]{tracker.warning_count} warning(s) suppressed — run with -v to see details[/]"
        )
    if final_state and (final_state.invite_url or final_state.invite_code):
        console.print(
            f"\n[bold green]Invite:[/] {final_state.invite_url or final_state.invite_code}"
        )
    if final_state and final_state.native_fidelity_counts:
        nf = final_state.native_fidelity_counts
        console.print(
            f"\n[green]Native fidelity:[/] "
            f"slowmode={nf.get('slowmode', 0)}, "
            f"user_limit={nf.get('user_limit', 0)}, "
            f"role_icons={nf.get('role_icons', 0)}"
        )


@main.command()
@click.argument("export_dir", type=click.Path(exists=True))
@click.option("--rate-limit", default=1.0, type=float, help="Rate for ETA calc (default 1.0s/msg)")
def validate(export_dir: str, rate_limit: float) -> None:
    """Parse and validate export only, no API calls."""
    export_path = Path(export_dir)
    exports = parse_export_directory(export_path)

    if not exports:
        console.print("[bold red]Error:[/] No valid DCE JSON files found.")
        sys.exit(1)

    guild_name = exports[0].guild.name
    console.print(f"[bold]Discord Ferry[/] — validating export for [cyan]{_safe(guild_name)}[/]\n")

    table = _build_validate_table(exports)
    console.print(table)
    console.print()

    warnings = validate_export(exports, export_path)
    if warnings:
        console.print(f"[yellow bold]Warnings ({len(warnings)}):[/]")
        for w in warnings:
            console.print(f"  [yellow]- {_safe(w['message'])}[/]")
        console.print()

    total_messages = sum(e.message_count for e in exports)
    eta = _format_eta(total_messages, rate_limit)
    console.print(f"[bold]{total_messages:,}[/] messages at {rate_limit:.1f}s/msg = {eta}")

    reason = acknowledgement_required(warnings)
    if reason is not None:
        console.print(f"\n[bold red]{_safe(reason)}[/]")
        sys.exit(1)
    else:
        console.print("[bold green]Export looks good.[/]")


@main.command()
@click.option(
    "--template",
    type=click.Choice(["gaming", "community", "education"]),
    default=None,
    help="Use a preset server template",
)
@click.option(
    "--blueprint",
    type=click.Path(exists=True),
    default=None,
    help="Path to a blueprint JSON file",
)
@click.option("--stoat-url", envvar="STOAT_URL", required=True, help="Stoat API base URL")
@click.option(
    "--token",
    envvar="STOAT_TOKEN",
    required=True,
    help="Stoat user token (from browser Local Storage)",
)
@click.option("--name", default=None, help="Override server name from blueprint")
def build(
    template: str | None,
    blueprint: str | None,
    stoat_url: str,
    token: str,
    name: str | None,
) -> None:
    """Build a Stoat server from a template or blueprint."""
    import importlib.resources

    from discord_ferry.blueprint import ServerBlueprint, import_blueprint

    if not template and not blueprint:
        console.print("[bold red]Error:[/] Provide --template or --blueprint")
        sys.exit(1)
    if template and blueprint:
        console.print("[bold red]Error:[/] Use either --template or --blueprint, not both")
        sys.exit(1)

    bp: ServerBlueprint
    if template:
        templates_dir = importlib.resources.files("discord_ferry.templates")
        bp = import_blueprint(Path(str(templates_dir / f"{template}.json")))
    else:
        bp = import_blueprint(Path(blueprint))  # type: ignore[arg-type]

    if name:
        bp.name = name

    _print_proxy_notices()
    console.print(f"[bold]Discord Ferry[/] — building server '{_safe(bp.name)}'\n")

    async def _build() -> None:
        async with new_session() as session:
            # Create server
            server_id = await api_create_server(session, stoat_url, token, bp.name)
            console.print(f"  Created server '{_safe(bp.name)}' ({server_id})")

            # Create roles
            for role in bp.roles:
                role_result = await api_create_role(session, stoat_url, token, server_id, role.name)
                role_id = role_result["id"]
                # Replay colour + rank in a single PATCH. Skip rank 0 (the default) so an
                # unranked blueprint role keeps Stoat's default ordering rather than being pinned.
                edit_kwargs: dict[str, Any] = {}
                if role.colour:
                    edit_kwargs["colour"] = role.colour
                if role.rank:
                    edit_kwargs["rank"] = role.rank
                if edit_kwargs:
                    await api_edit_role(
                        session, stoat_url, token, server_id, role_id, **edit_kwargs
                    )
                if role.permissions:
                    await api_set_role_permissions(
                        session,
                        stoat_url,
                        token,
                        server_id,
                        role_id,
                        allow=role.permissions,
                        deny=0,
                    )
                console.print(f"  Created role '{_safe(role.name)}'")

            # Create categories and channels
            import uuid

            all_categories: list[dict[str, Any]] = []
            for category in bp.categories:
                cat_id = uuid.uuid4().hex[:26]
                channel_ids: list[str] = []
                for ch in category.channels:
                    channel_ids.append(
                        await _build_blueprint_channel(session, stoat_url, token, server_id, ch)
                    )
                    console.print(
                        f"  Created channel '{_safe(ch.name)}' in '{_safe(category.name)}'"
                    )
                all_categories.append(
                    {
                        "id": cat_id,
                        "title": category.name[:32],
                        "channels": channel_ids,
                    }
                )
            if all_categories:
                await api_upsert_categories(session, stoat_url, token, server_id, all_categories)

            # Create uncategorized channels
            for ch in bp.uncategorized_channels:
                await _build_blueprint_channel(session, stoat_url, token, server_id, ch)
                console.print(f"  Created channel '{_safe(ch.name)}'")

            console.print(f"\n[bold green]Done![/] Server '{_safe(bp.name)}' created ({server_id})")

    try:
        asyncio.run(_build())
    except MigrationError as exc:
        console.print(f"\n[bold red]Build failed:[/] {_safe(exc)}")
        sys.exit(1)


@main.command(name="export-blueprint")
@click.option(
    "--from",
    "from_dir",
    type=click.Path(exists=True),
    required=True,
    help="Path to DCE export directory to convert",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    required=True,
    help="Output path for the blueprint JSON file",
)
@click.option("--name", default=None, help="Override server name in blueprint")
def export_blueprint_cmd(from_dir: str, output: str, name: str | None) -> None:
    """Export a DCE export directory as a reusable blueprint."""
    from discord_ferry.blueprint import (
        BlueprintCategory,
        BlueprintChannel,
        ServerBlueprint,
        export_blueprint,
    )

    exports = parse_export_directory(Path(from_dir))
    if not exports:
        console.print("[bold red]Error:[/] No valid DCE JSON files found.")
        sys.exit(1)

    guild_name = name or exports[0].guild.name

    # Collect categories and channels
    categories: dict[str, list[BlueprintChannel]] = {}
    uncategorized: list[BlueprintChannel] = []

    for export in exports:
        ch = export.channel
        if ch.type == 4:  # Skip category-type channels
            continue
        stoat_type = "Voice" if ch.type == 2 else "Text"
        bp_channel = BlueprintChannel(name=ch.name, type=stoat_type)

        if ch.category:
            categories.setdefault(ch.category, []).append(bp_channel)
        else:
            uncategorized.append(bp_channel)

    bp = ServerBlueprint(
        name=guild_name,
        description=f"Exported from Discord server '{guild_name}'",
        categories=[
            BlueprintCategory(name=cat_name, channels=channels)
            for cat_name, channels in categories.items()
        ],
        uncategorized_channels=uncategorized,
    )

    export_blueprint(bp, Path(output))
    console.print(
        f"[bold green]Blueprint exported[/] to {_safe(output)} "
        f"({len(bp.categories)} categories, "
        f"{sum(len(c.channels) for c in bp.categories) + len(uncategorized)} channels)"
    )


@main.command()
@click.argument("output_dir", type=click.Path(exists=True))
def stats(output_dir: str) -> None:
    """Print aggregate stats for a completed (or in-progress) migration.

    Reads state.json from OUTPUT_DIR and renders entity counts, message
    counters, fidelity score, error/warning summary, optional per-channel
    breakdown, optional rollback section, and elapsed duration.

    Exit codes: 0 on success; 2 when OUTPUT_DIR does not exist (Click
    validation); 1 when state.json is missing inside OUTPUT_DIR or contains
    invalid JSON.
    """
    try:
        state = load_state(Path(output_dir))
    except StateError as e:
        console.print(f"[bold red]Error:[/] {_safe(e)}")
        sys.exit(1)

    summary = summarize_state(state)
    console.print(_build_stats_table(summary))

    channels_table = _build_channels_table(summary)
    if channels_table is not None:
        console.print(channels_table)

    rollback_table = _build_rollback_table(summary)
    if rollback_table is not None:
        console.print(rollback_table)


class _RollbackProgressTracker:
    """Render rollback progress + confirmation gate via Rich/Click.

    Dedicated tracker (NOT a case arm on _ProgressTracker.on_event) per
    design decision: keeps the migration progress display separate from
    the rollback confirmation flow.
    """

    def __init__(
        self,
        *,
        pause_event: asyncio.Event,
        skip_confirmations: bool,
        verbose: bool = False,
    ) -> None:
        self.pause_event = pause_event
        self.skip_confirmations = skip_confirmations
        self.verbose = verbose
        self.error_count = 0
        self.warning_count = 0
        self.last_summary: RollbackSummary | None = None

    def on_event(self, event: MigrationEvent) -> None:
        # confirm_rollback renders a table AND gates control flow (pause_event / click.confirm).
        # It self-escapes user values and is kept OUTSIDE the MarkupError guard below so a
        # swallowed error could never skip pause_event.set() and hang the rollback — a clean crash
        # there beats a silent hang.
        if event.status == "confirm_rollback":
            self._render_summary_and_prompt(event)
            return

        # The remaining cases only print (or render the final summary, which has no control flow).
        # User-controlled values are escaped; the render is additionally guarded against a missed
        # escape site so a MarkupError can't propagate into the engine's synchronous emit().
        try:
            match event.status:
                case "started":
                    console.print(f"[bold cyan][>>][/] {_safe(event.message)}")
                case "progress":
                    if self.verbose:
                        console.print(f"[dim]    {_safe(event.message)}[/]")
                case "completed":
                    console.print(f"[bold green][OK][/] {_safe(event.message)}")
                    if event.detail is not None:
                        self._render_final(event.detail.get("summary"))
                case "completed_with_failures":
                    console.print(f"[bold yellow][!!][/] {_safe(event.message)}")
                    if event.detail is not None:
                        self._render_final(event.detail.get("summary"))
                case "cancelled":
                    console.print(f"[yellow][--][/] {_safe(event.message)}")
                case "warning":
                    self.warning_count += 1
                    if self.verbose:
                        console.print(f"[yellow]    {_safe(event.message)}[/]")
                case "notice":
                    # Printed unconditionally. A configuration problem the user
                    # must see before the run, not a per-item warning. Does NOT
                    # increment warning_count, so the "N warning(s) suppressed"
                    # line stays accurate.
                    console.print(f"[cyan][i][/] {_safe(event.message)}")
                case "error":
                    self.error_count += 1
                    console.print(f"[bold red][!!][/] {_safe(event.message)}")
        except MarkupError:
            console.print(f"{event.message}", markup=False)

    def _render_summary_and_prompt(self, event: MigrationEvent) -> None:
        """Render the RollbackSummary table and gate on user confirmation."""
        summary_obj = None
        if event.detail is not None:
            summary_obj = event.detail.get("summary")
        if summary_obj is None:
            # Defensive — engine should always send a summary; if not, just proceed.
            self.pause_event.set()
            return

        # mypy: we know this is a RollbackSummary from the engine event.
        summary: RollbackSummary = summary_obj  # type: ignore[assignment]
        self.last_summary = summary

        console.print("\n[bold]Pre-Rollback Review[/]")

        table = Table(show_header=True, header_style="bold")
        table.add_column("Item", style="cyan")
        table.add_column("Count", justify="right")
        table.add_row("Server", f"{_safe(summary.stoat_server_name)} ({summary.stoat_server_id})")
        table.add_row("Channels to delete", str(len(summary.channels_to_delete)))
        table.add_row("Roles to delete", str(len(summary.roles_to_delete)))
        table.add_row("Emoji to delete", str(len(summary.emoji_to_delete)))
        table.add_row("Categories to clean", str(summary.categories_to_clean))
        table.add_row("Untracked-Ferry-suspect channels", str(len(summary.untracked_ferry_suspect)))
        if summary.autumn_orphan_count > 0:
            table.add_row("Autumn orphan uploads (NOT deleted)", str(summary.autumn_orphan_count))
        if summary.has_failures_from_prior_run:
            table.add_row("Prior-run failures present", "yes")
        console.print(table)

        if summary.untracked_ferry_suspect:
            console.print(
                "\n[bold yellow]Untracked-Ferry-suspect channels[/] "
                "(present on Stoat, absent from state.json):"
            )
            suspect_table = Table(show_header=True, header_style="bold")
            suspect_table.add_column("Name", style="cyan")
            suspect_table.add_column("Created (UTC)")
            suspect_table.add_column("Stoat ID", style="dim")
            for s in summary.untracked_ferry_suspect:
                # Display-layer translation: None -> "unknown" so users don't see literal "None".
                created = s.created_at_iso if s.created_at_iso is not None else "unknown"
                name = s.name if s.name else "(no name available)"
                suspect_table.add_row(_safe(name), created, s.stoat_id)
            console.print(suspect_table)

        if self.skip_confirmations:
            # --yes: don't delete untracked suspects (safe default), proceed.
            console.print(
                "[dim]--yes: skipping per-item opt-in; proceeding with mapped entities only[/]"
            )
            self.pause_event.set()
            return

        try:
            click.confirm("\nProceed with rollback?", abort=True)
        except click.exceptions.Abort:
            # Aborting click.confirm raises Abort which exits the program.
            # We won't reach here, but for clarity we'd need cancel handling.
            raise

        # Per-item opt-in for untracked suspects.
        for suspect in summary.untracked_ferry_suspect:
            created = suspect.created_at_iso if suspect.created_at_iso is not None else "unknown"
            name = suspect.name if suspect.name else "(no name available)"
            suspect.opted_in = click.confirm(
                f"Also delete untracked channel '{name}' "
                f"(created {created}, id {suspect.stoat_id})?",
                default=False,
            )

        self.pause_event.set()

    def _render_final(self, summary_obj: object) -> None:
        """Render the final progress dataclass (RollbackProgress) as a table."""
        # The engine emits the RollbackProgress dataclass directly in detail["summary"].
        if summary_obj is None:
            return
        table = Table(show_header=True, header_style="bold")
        table.add_column("Item", style="cyan")
        table.add_column("Count", justify="right")
        table.add_row("Channels deleted", str(getattr(summary_obj, "channels_deleted", 0)))
        table.add_row(
            "Untracked channels deleted",
            str(getattr(summary_obj, "untracked_channels_deleted", 0)),
        )
        table.add_row("Roles deleted", str(getattr(summary_obj, "roles_deleted", 0)))
        table.add_row("Emoji deleted", str(getattr(summary_obj, "emoji_deleted", 0)))
        cats_done = "yes" if getattr(summary_obj, "categories_cleaned", False) else "no"
        table.add_row("Categories cleaned", cats_done)
        failures = getattr(summary_obj, "failures", []) or []
        table.add_row("Failures", str(len(failures)))
        console.print("\n[bold]Rollback complete[/]")
        console.print(table)
        if failures:
            console.print("\n[bold red]Failures:[/]")
            for f in failures:
                status = f.http_status if f.http_status is not None else "n/a"
                console.print(
                    f"  [red]- {f.entity_type} {f.stoat_id} (HTTP {status})[/]: {_safe(f.error)}"
                )


@main.command(name="rollback")
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False),
    required=True,
    help="Directory containing state.json from the migration to roll back",
)
@click.option("--stoat-url", envvar="STOAT_URL", default=None, help="Stoat API base URL")
@click.option(
    "--token",
    envvar="STOAT_TOKEN",
    default=None,
    help="Stoat user token (from browser Local Storage)",
)
@click.option(
    "--server-id",
    default=None,
    help="Override the Stoat server ID from state.json (rarely needed)",
)
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip confirmation prompt(s)")
@click.option(
    "--force-unlock",
    is_flag=True,
    default=False,
    help="Override a stale [FERRY_LOCK:...] marker on the target server",
)
@click.option(
    "--max-concurrent-requests",
    default=5,
    type=int,
    help="Max concurrent channel DELETEs (default 5)",
)
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
def rollback_cmd(
    output_dir: str,
    stoat_url: str | None,
    token: str | None,
    server_id: str | None,
    yes: bool,
    force_unlock: bool,
    max_concurrent_requests: int,
    verbose: bool,
) -> None:
    """Reverse a recorded migration by deleting Ferry-created entities.

    Reads ``state.json`` from --output-dir and deletes the Stoat entities
    listed there: channels, roles, custom emoji, and Ferry-owned categories.
    Idempotent: 404 responses are treated as "already deleted" and re-runs
    are clean no-ops. Autumn-hosted attachments are NOT removed (no public
    DELETE endpoint).
    """
    load_dotenv()

    if not stoat_url:
        console.print("[bold red]Error:[/] --stoat-url is required (or set STOAT_URL)")
        sys.exit(1)
    if not token:
        console.print("[bold red]Error:[/] --token is required (or set STOAT_TOKEN)")
        sys.exit(1)

    out_path = Path(output_dir)
    try:
        state = load_state(out_path)
    except StateError as exc:
        console.print(f"[bold red]Error:[/] state.json not found or unreadable: {_safe(exc)}")
        sys.exit(2)

    config = FerryConfig(
        export_dir=out_path,  # not used by rollback but required by dataclass
        stoat_url=stoat_url,
        token=token,
        output_dir=out_path,
        server_id=server_id or state.stoat_server_id or None,
        skip_export=True,
        force_unlock=force_unlock,
        max_concurrent_requests=max_concurrent_requests,
        verbose=verbose,
    )

    async def _runner() -> None:
        pause_event = asyncio.Event()
        cancel_event = asyncio.Event()
        config.pause_event = pause_event
        config.cancel_event = cancel_event
        tracker = _RollbackProgressTracker(
            pause_event=pause_event,
            skip_confirmations=yes,
            verbose=verbose,
        )
        await run_rollback(config, state, exports=[], on_event=tracker.on_event)

        # Exit code reflects rollback outcome.
        if state.rollback_progress is not None and state.rollback_progress.failures:
            sys.exit(1)

    _print_proxy_notices()
    console.print("[bold]Discord Ferry[/] — starting rollback\n")
    try:
        asyncio.run(_runner())
    except MigrationError as exc:
        console.print(f"\n[bold red]Rollback failed:[/] {_safe(exc)}")
        sys.exit(1)
    except click.exceptions.Abort:
        console.print("\n[yellow]Aborted.[/]")
        sys.exit(130)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/] State saved — re-run to resume.")
        sys.exit(130)


@main.command(name="probe")
@click.option("--stoat-url", envvar="STOAT_URL", default=None, help="Stoat API base URL")
@click.option("--token", envvar="STOAT_TOKEN", default=None, help="Stoat user token")
@click.option("--test-server-id", required=True, help="Throwaway server for probe entities")
@click.option("--deep", is_flag=True, default=False, help="Probe Autumn size boundaries by upload")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON")
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
def probe_cmd(
    stoat_url: str | None,
    token: str | None,
    test_server_id: str,
    deep: bool,
    as_json: bool,
    verbose: bool,
) -> None:
    """Probe a live Stoat instance for Autumn limits, rate window, voice Bug #194, webhooks."""
    load_dotenv()
    if not stoat_url:
        console.print("[bold red]Error:[/] --stoat-url is required (or set STOAT_URL)")
        sys.exit(1)
    if not token:
        console.print("[bold red]Error:[/] --token is required (or set STOAT_TOKEN)")
        sys.exit(1)

    # to_stderr=True: --json prints machine-readable JSON to stdout below, and a
    # notice landing on that channel would corrupt it for a script parsing it.
    _print_proxy_notices(to_stderr=True)

    # probe never builds a FerryConfig or a SecureTokenStore, so the engine's
    # _ensure_token_store hook never fires here. Without this line the Stoat
    # token has no redaction coverage at all for the whole command -- and the
    # regex backstop deliberately cannot match Stoat tokens (opaque base64url).
    register_secret("stoat", token)

    from discord_ferry.migrator.probe import run_probe

    report = asyncio.run(run_probe(stoat_url, token, test_server_id, lambda _e: None, deep=deep))

    if as_json:
        payload = {c.name: {"status": c.status, "detail": c.detail} for c in report.checks}
        # click.echo, not console.print: the module-level Console has soft_wrap=False
        # and falls back to 80 columns off a terminal, so it inserts a real newline
        # wherever the wrap lands -- including inside a string value, which makes the
        # output unparseable (issue #145). This branch is not for a human to read.
        click.echo(json.dumps(payload))
        return

    table = Table(title="Stoat Probe Results")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    for c in report.checks:
        colour = {"ok": "green", "warn": "yellow", "fail": "red"}.get(c.status, "white")
        table.add_row(c.name, f"[{colour}]{c.status}[/]", c.detail)
    console.print(table)


@main.command(name="check")
@click.argument("output_dir", type=click.Path(exists=True))
@click.option("--stoat-url", envvar="STOAT_URL", default=None, help="Stoat API base URL")
@click.option(
    "--token",
    envvar="STOAT_TOKEN",
    default=None,
    # Prefer the environment variable, and say so here rather than only in the
    # guide: a token passed as an argument lands in shell history and in `ps`.
    #
    # A review proposed hide_input=True. Click consumes that in exactly one
    # place, prompt_for_value, so on an option that never prompts it does
    # nothing at all: it would read as a fix in the diff and change no
    # behaviour. Making it real needs prompt=True, which breaks the
    # non-interactive use the exit-code contract exists to serve, and would
    # make this the only one of Ferry's four token options behaving that way.
    help="Stoat user token. Prefer the STOAT_TOKEN environment variable",
)
def check_cmd(output_dir: str, stoat_url: str | None, token: str | None) -> None:
    """Verify a finished migration against the live Stoat server.

    Reads the state file in OUTPUT_DIR and asks the server whether everything it
    records is still there. Read-only: it creates, edits and deletes nothing.
    """
    load_dotenv()
    if not stoat_url:
        console.print("[bold red]Error:[/] --stoat-url is required (or set STOAT_URL)")
        sys.exit(1)
    if not token:
        console.print("[bold red]Error:[/] --token is required (or set STOAT_TOKEN)")
        sys.exit(1)

    _print_proxy_notices(to_stderr=True)

    # Same hazard probe_cmd documents: this command builds no FerryConfig, so
    # the engine's _ensure_token_store hook never fires and the Stoat token has
    # NO masking coverage for the whole run without this line. The regex
    # backstop deliberately cannot match Stoat's opaque base64url values.
    register_secret("stoat", token)

    # Without this the module semaphore stays None, which _api_request treats as
    # "no limit" rather than as an error. Nothing would report the omission.
    init_request_semaphore(FerryConfig.max_concurrent_requests)

    from discord_ferry.migrator.verify import run_check

    try:
        state = load_state(Path(output_dir))
    except StateError as exc:
        console.print(f"[bold red]Error:[/] {_safe(exc)}")
        sys.exit(1)

    try:
        report = asyncio.run(run_check(stoat_url, token, state, lambda _e: None))
    except (CheckError, MigrationError) as exc:
        console.print(f"[bold red]Cannot check this migration:[/] {_safe(exc)}")
        sys.exit(1)

    _render_check_report(report, state.thread_strategy)
    sys.exit(1 if report.has_failures else 0)


def _render_check_report(report: CheckReport, thread_strategy: str = "") -> None:
    """Print the results table and the summary line.

    The summary leads with counts and states the exit code, so neither has to be
    inferred. When anything could not be verified it also says so in a sentence:
    on a merge migration most tail results are unverifiable, and a bare count
    beside three green ones reads like approval of something never examined.
    """
    # `detail` can embed a server-supplied error body, which this project treats
    # as attacker-influenced in the general case. escape() neutralises Rich
    # markup in it, and that is ALL it does: it leaves an ESC byte untouched.
    #
    # A raw ANSI sequence in that text is defanged anyway, but by the rendering
    # rather than by escape(). Rich interleaves its own style codes between the
    # ESC and the following '[', so "\x1b[2J" never reaches the terminal as one
    # contiguous sequence. Measured, not assumed.
    #
    # That protection is incidental, so it disappears the moment this text is
    # emitted any other way. If a --json mode is added (spec P1 S6), it must go
    # through click.echo per the recorded rule, and click.echo would print the
    # ESC verbatim. Strip control characters there rather than relying on this.
    colours = {"ok": "green", "warn": "yellow", "fail": "red", "unverifiable": "cyan"}
    table = Table(title="Migration Check")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    for result in report.results:
        colour = colours.get(result.status, "white")
        table.add_row(
            escape(result.name),
            f"[{colour}]{result.status}[/]",
            escape(result.detail),
        )
    console.print(table)

    counts = report.counts()
    exit_code = 1 if report.has_failures else 0
    console.print(
        f"{counts['ok']} ok · {counts['fail']} failed · "
        f"{counts['unverifiable']} unverifiable · {counts['warn']} warning"
        f"   (exit {exit_code})"
    )
    if counts["unverifiable"]:
        # The causes worth naming depend on what the migration actually did, and
        # since 2.17.0 state.json records that. Before it did, this sentence
        # offered merge to every user including the ones who never used it,
        # which is the wording issue #267 exists to correct.
        #
        # verify.py carries the matching per-result detail, which is what --json
        # serialises and what the repair tool reads in-process. This is what a
        # human reads, and fixing one without the other leaves half the audience
        # with the wrong explanation.
        if thread_strategy == "merge":
            cause = (
                "which is expected under --thread-strategy=merge, after a duplicate "
                "send, or for a channel this token cannot read"
            )
        elif thread_strategy:
            cause = (
                f"which for a --thread-strategy={thread_strategy} migration means a "
                "duplicate send, or a channel this token cannot read"
            )
        else:
            cause = (
                "which is expected when --thread-strategy=merge was used, after a "
                "duplicate send, or for a channel this token cannot read"
            )
        console.print(
            f"[cyan]{counts['unverifiable']} checks could not be verified.[/] Ferry did "
            f"not record what it would need to confirm them, {cause}."
        )


@main.command("tls-check")
def tls_check_cmd() -> None:
    """Report which certificate authorities Ferry trusts, and the proxy state.

    An earlier version of this docstring said "four fixed keys". describe_proxy
    (Task 11) added proxy-http, proxy-https, proxy-source and proxy-disabled;
    release.yml pins key names from both groups, so changing any breaks CI.
    """
    from discord_ferry.core.http import describe_proxy, describe_trust

    for key, value in describe_trust().items():
        click.echo(f"{key}: {value}")

    for key, value in describe_proxy().items():
        click.echo(f"{key}: {value}")


if __name__ == "__main__":
    main()
