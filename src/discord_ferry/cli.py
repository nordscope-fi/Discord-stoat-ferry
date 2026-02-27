"""CLI entry point for Discord Ferry (power users / Linux)."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from discord_ferry.config import FerryConfig
from discord_ferry.core.engine import PHASE_ORDER, run_migration
from discord_ferry.errors import MigrationError
from discord_ferry.parser.dce_parser import parse_export_directory, validate_export

if TYPE_CHECKING:
    from discord_ferry.core.events import MigrationEvent
    from discord_ferry.parser.models import DCEExport

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


class _ProgressTracker:
    """Track migration progress and render Rich output."""

    def __init__(self, *, verbose: bool = False) -> None:
        self.verbose = verbose
        self.phase_status: dict[str, str] = {p: "pending" for p in PHASE_ORDER}
        self.messages_sent = 0
        self.error_count = 0
        self.warning_count = 0

    def on_event(self, event: MigrationEvent) -> None:
        """Handle a migration event — update state and print to console."""
        self.phase_status[event.phase] = event.status

        match event.status:
            case "started":
                icon = _STATUS_ICONS["started"]
                console.print(f"[bold cyan][{icon}][/] {event.phase}: {event.message}")
            case "completed":
                icon = _STATUS_ICONS["completed"]
                console.print(f"[bold green][{icon}][/] {event.phase}: {event.message}")
            case "skipped":
                icon = _STATUS_ICONS["skipped"]
                console.print(f"[dim][{icon}][/] {event.phase}: {event.message}")
            case "error":
                self.error_count += 1
                console.print(f"[bold red][!!][/] {event.phase}: {event.message}")
            case "warning":
                self.warning_count += 1
                if self.verbose:
                    console.print(f"[yellow][!!][/] {event.phase}: {event.message}")
            case "progress":
                if event.total > 0:
                    self.messages_sent = event.current
                if self.verbose:
                    console.print(f"[dim]    {event.message}[/]")

    def print_summary(self) -> None:
        """Print a final summary line."""
        console.print()
        console.print(f"[bold]Done.[/] Errors: {self.error_count}  Warnings: {self.warning_count}")


# ---------------------------------------------------------------------------
# Click commands
# ---------------------------------------------------------------------------

_common_options = [
    click.argument("export_dir", type=click.Path(exists=True)),
    click.option("--stoat-url", envvar="STOAT_URL", default=None, help="Stoat API base URL"),
    click.option("--token", envvar="STOAT_TOKEN", default=None, help="Stoat user/bot token"),
    click.option("--server-id", default=None, help="Use existing Stoat server"),
    click.option("--server-name", default=None, help="Name for new server"),
    click.option("--skip-messages", is_flag=True, help="Structure only"),
    click.option("--skip-emoji", is_flag=True, help="Skip emoji upload"),
    click.option("--skip-reactions", is_flag=True, help="Skip reactions"),
    click.option("--skip-threads", is_flag=True, help="Skip threads/forums"),
    click.option("--rate-limit", default=1.0, type=float, help="Seconds between messages"),
    click.option("--upload-delay", default=0.5, type=float, help="Seconds between uploads"),
    click.option("--output-dir", default="./ferry-output", help="Report output directory"),
    click.option("--resume", is_flag=True, help="Resume from state file"),
    click.option("--verbose", "-v", is_flag=True, help="Debug output"),
    click.option(
        "--dry-run",
        is_flag=True,
        default=False,
        help="Run all phases without API calls; test locally",
    ),
    click.option("--max-channels", default=200, type=int, help="Channel limit (self-hosted)"),
    click.option("--max-emoji", default=100, type=int, help="Emoji limit (self-hosted)"),
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
    return FerryConfig(
        export_dir=Path(kwargs["export_dir"]),
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
        verbose=kwargs.get("verbose", False),
        max_channels=kwargs.get("max_channels", 200),
        max_emoji=kwargs.get("max_emoji", 100),
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

    config = _build_config(kwargs)
    tracker = _ProgressTracker(verbose=config.verbose)

    console.print("[bold]Discord Ferry[/] — starting migration\n")

    try:
        asyncio.run(run_migration(config, on_event=tracker.on_event))
    except MigrationError as exc:
        console.print(f"\n[bold red]Migration failed:[/] {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/] State saved — use --resume to continue.")
        sys.exit(130)

    tracker.print_summary()


@main.command()
@click.argument("export_dir", type=click.Path(exists=True))
def validate(export_dir: str) -> None:
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
    eta = _format_eta(total_messages, 1.0)
    console.print(f"[bold]{total_messages:,}[/] messages at 1.0s/msg = {eta}")

    has_critical = any(w["type"] == "rendered_markdown" for w in warnings)
    if has_critical:
        console.print("\n[bold red]Critical warnings found.[/] Fix before migrating.")
        sys.exit(1)
    else:
        console.print("[bold green]Export looks good.[/]")


if __name__ == "__main__":
    main()
