"""CLI entry point for Discord Ferry (power users / Linux)."""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import aiohttp
import click
from dotenv import load_dotenv
from rich.console import Console
from rich.live import Live
from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn
from rich.table import Table

from discord_ferry.config import FerryConfig
from discord_ferry.core.engine import PHASE_ORDER, run_migration, run_rollback
from discord_ferry.errors import MigrationError, StateError
from discord_ferry.parser.dce_parser import parse_export_directory, validate_export
from discord_ferry.state import MigrationState, load_state
from discord_ferry.stats import summarize_state

if TYPE_CHECKING:
    from discord_ferry.core.events import MigrationEvent
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
        table.add_row("  Errors", f"{summary.error_count} — last: {preview}")
    if summary.warning_count == 0:
        table.add_row("  Warnings", "0 (clean)")
    else:
        preview = textwrap.shorten(summary.last_warning or "", width=80, placeholder="…")
        table.add_row("  Warnings", f"{summary.warning_count} — last: {preview}")

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
            stats += f"  Channel: {self._current_channel}"
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

        match event.status:
            case "started":
                self._phase_progress.update(
                    self._phase_task_id, description=f"Phase: {event.phase}"
                )
                self._log(f"[bold cyan][>>][/] {event.phase}: {event.message}")
            case "completed":
                completed = sum(1 for s in self.phase_status.values() if s == "completed")
                self._phase_progress.update(self._phase_task_id, completed=completed)
                self._log(f"[bold green][OK][/] {event.phase}: {event.message}")
            case "skipped":
                self._log(f"[dim][--][/] {event.phase}: {event.message}")
            case "error":
                self.error_count += 1
                self._log(f"[bold red][!!][/] {event.phase}: {event.message}")
            case "warning":
                self.warning_count += 1
                if self.verbose:
                    self._log(f"[yellow][!!][/] {event.phase}: {event.message}")
            case "confirm":
                # Print review summary and ask for confirmation
                if event.detail:
                    self._log("\n[bold]Pre-Migration Review[/]")
                    review_table = Table(show_header=True, header_style="bold")
                    review_table.add_column("Item", style="cyan")
                    review_table.add_column("Count", justify="right")
                    detail = event.detail
                    review_table.add_row("Server Name", str(detail.get("server_name", "")))
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
                        review_table.add_row("NSFW Channels", str(detail.get("nsfw_channels", 0)))
                    self._log("")
                    console.print(review_table)
                    raw_warnings = detail.get("warnings")
                    warnings_list: list[object] = (
                        raw_warnings if isinstance(raw_warnings, list) else []
                    )
                    for w in warnings_list:
                        self._log(f"  [yellow]Warning: {w}[/]")
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
                        description=f"  {event.channel_name}",
                    )
                if self.verbose:
                    self._log(f"[dim]    {event.message}[/]")

        # Refresh live display if active.
        if self._live is not None:
            self._live.update(self._make_display())

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
    )


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    """Migrate a Discord server export to Stoat."""
    load_dotenv()
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
        console.print(f"[bold red]Error:[/] {exc}")
        sys.exit(1)

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
        console.print(f"\n[bold red]Migration failed:[/] {exc}")
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
    console.print(f"[bold]Discord Ferry[/] — validating export for [cyan]{guild_name}[/]\n")

    table = _build_validate_table(exports)
    console.print(table)
    console.print()

    warnings = validate_export(exports, export_path)
    if warnings:
        console.print(f"[yellow bold]Warnings ({len(warnings)}):[/]")
        for w in warnings:
            console.print(f"  [yellow]- {w['message']}[/]")
        console.print()

    total_messages = sum(e.message_count for e in exports)
    eta = _format_eta(total_messages, rate_limit)
    console.print(f"[bold]{total_messages:,}[/] messages at {rate_limit:.1f}s/msg = {eta}")

    has_critical = any(w["type"] == "rendered_markdown" for w in warnings)
    if has_critical:
        console.print("\n[bold red]Critical warnings found.[/] Fix before migrating.")
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
    from discord_ferry.migrator.api import (
        api_create_channel,
        api_create_role,
        api_create_server,
        api_edit_role,
        api_set_role_permissions,
        api_upsert_categories,
    )

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

    console.print(f"[bold]Discord Ferry[/] — building server '{bp.name}'\n")

    async def _build() -> None:
        async with aiohttp.ClientSession() as session:
            # Create server
            result = await api_create_server(session, stoat_url, token, bp.name)
            server_id = result["_id"]
            console.print(f"  Created server '{bp.name}' ({server_id})")

            # Create roles
            for role in bp.roles:
                role_result = await api_create_role(session, stoat_url, token, server_id, role.name)
                role_id = role_result["id"]
                if role.colour:
                    await api_edit_role(
                        session, stoat_url, token, server_id, role_id, colour=role.colour
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
                console.print(f"  Created role '{role.name}'")

            # Create categories and channels
            import uuid

            all_categories: list[dict[str, Any]] = []
            for category in bp.categories:
                cat_id = uuid.uuid4().hex[:26]
                channel_ids: list[str] = []
                for ch in category.channels:
                    ch_result = await api_create_channel(
                        session,
                        stoat_url,
                        token,
                        server_id,
                        name=ch.name,
                        channel_type=ch.type,
                        nsfw=ch.nsfw,
                    )
                    channel_ids.append(ch_result["_id"])
                    console.print(f"  Created channel '{ch.name}' in '{category.name}'")
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
                await api_create_channel(
                    session,
                    stoat_url,
                    token,
                    server_id,
                    name=ch.name,
                    channel_type=ch.type,
                    nsfw=ch.nsfw,
                )
                console.print(f"  Created channel '{ch.name}'")

            console.print(f"\n[bold green]Done![/] Server '{bp.name}' created ({server_id})")

    try:
        asyncio.run(_build())
    except MigrationError as exc:
        console.print(f"\n[bold red]Build failed:[/] {exc}")
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
        f"[bold green]Blueprint exported[/] to {output} "
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
        console.print(f"[bold red]Error:[/] {e}")
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
        match event.status:
            case "started":
                console.print(f"[bold cyan][>>][/] {event.message}")
            case "confirm_rollback":
                self._render_summary_and_prompt(event)
            case "progress":
                if self.verbose:
                    console.print(f"[dim]    {event.message}[/]")
            case "completed":
                console.print(f"[bold green][OK][/] {event.message}")
                if event.detail is not None:
                    self._render_final(event.detail.get("summary"))
            case "completed_with_failures":
                console.print(f"[bold yellow][!!][/] {event.message}")
                if event.detail is not None:
                    self._render_final(event.detail.get("summary"))
            case "cancelled":
                console.print(f"[yellow][--][/] {event.message}")
            case "warning":
                self.warning_count += 1
                if self.verbose:
                    console.print(f"[yellow]    {event.message}[/]")
            case "error":
                self.error_count += 1
                console.print(f"[bold red][!!][/] {event.message}")

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
        table.add_row("Server", f"{summary.stoat_server_name} ({summary.stoat_server_id})")
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
                suspect_table.add_row(name, created, s.stoat_id)
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
                    f"  [red]- {f.entity_type} {f.stoat_id} (HTTP {status})[/]: {f.error}"
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
        console.print(f"[bold red]Error:[/] state.json not found or unreadable: {exc}")
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

    console.print("[bold]Discord Ferry[/] — starting rollback\n")
    try:
        asyncio.run(_runner())
    except MigrationError as exc:
        console.print(f"\n[bold red]Rollback failed:[/] {exc}")
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

    from discord_ferry.migrator.probe import run_probe

    report = asyncio.run(run_probe(stoat_url, token, test_server_id, lambda _e: None, deep=deep))

    if as_json:
        payload = {c.name: {"status": c.status, "detail": c.detail} for c in report.checks}
        console.print(json.dumps(payload))
        return

    table = Table(title="Stoat Probe Results")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    for c in report.checks:
        colour = {"ok": "green", "warn": "yellow", "fail": "red"}.get(c.status, "white")
        table.add_row(c.name, f"[{colour}]{c.status}[/]", c.detail)
    console.print(table)


if __name__ == "__main__":
    main()
