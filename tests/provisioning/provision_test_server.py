"""Click CLI for Discord test-server provisioning.

Three subcommands: provision, teardown, verify. Reads bot token from the
DISCORD_TEST_BOT_TOKEN env var. Three-way exit codes per subcommand:
  0 = success / match
  1 = drift (verify) or partial failure (provision)
  2 = setup error (missing env var, malformed manifest, auth failure)
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp
import click

if TYPE_CHECKING:
    from types import FrameType

from tests.provisioning._applier import (
    Manifest,
    diff,
    fetch_actual_state,
    load_manifest,
    reconcile_provision,
    reconcile_teardown,
    reconcile_verify,
)
from tests.provisioning._bot_api import (
    BotApi,
    ProvisioningAuthError,
    ProvisioningError,
    ProvisioningPermissionError,
    TokenRedactingFilter,
    configure_aiohttp_logging,
)

DEFAULT_MANIFEST = Path(__file__).parent / "fixture-spec.json"

_sigint_count = 0


def _install_sigint_handler() -> None:
    """Install a SIGINT handler that escalates on the second Ctrl-C.

    First SIGINT: print interrupt message + raise KeyboardInterrupt so the
    async stack unwinds through the existing exception handlers. Second
    SIGINT: hard exit with code 130 (128 + SIGINT). Reset per subcommand
    so each invocation starts with a clean count.
    """
    global _sigint_count
    _sigint_count = 0

    def handler(signum: int, frame: FrameType | None) -> None:
        global _sigint_count
        _sigint_count += 1
        if _sigint_count == 1:
            click.echo(
                "\nInterrupted. Re-run with same --guild-id to resume; "
                "idempotency will skip already-created entities.",
                err=True,
            )
            raise KeyboardInterrupt
        click.echo("\nHard exit.", err=True)
        os._exit(130)

    signal.signal(signal.SIGINT, handler)


@click.group()
def cli() -> None:
    """Discord test-server provisioning. Human-run only, never in CI."""


def _setup_logging(token: str, *, verbose: bool) -> None:
    """Install token-redacting filter on root and quiet aiohttp by default."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")
    fltr = TokenRedactingFilter(token)
    logging.getLogger().addFilter(fltr)
    configure_aiohttp_logging(verbose=verbose)


def _require_token() -> str:
    token = os.environ.get("DISCORD_TEST_BOT_TOKEN")
    if not token:
        click.echo("error: DISCORD_TEST_BOT_TOKEN env var not set", err=True)
        sys.exit(2)
    return token


@cli.command()
@click.option("--guild-id", help="Existing guild ID to provision into.")
@click.option(
    "--create-guild",
    "create_guild_name",
    help="Bootstrap a new guild with the given name (unverified bot must be in <10 guilds).",
)
@click.option(
    "--manifest",
    "manifest_path",
    default=None,
    help="Path to fixture-spec.json (defaults to bundled manifest).",
)
@click.option("--dry-run", is_flag=True, help="Plan only; no writes.")
@click.option("--verbose", "-v", is_flag=True, help="DEBUG-level logging.")
def provision(
    guild_id: str | None,
    create_guild_name: str | None,
    manifest_path: str | None,
    dry_run: bool,
    verbose: bool,
) -> None:
    """Apply manifest to a guild, creating any missing entities idempotently."""
    if not guild_id and not create_guild_name:
        click.echo("error: must specify --guild-id or --create-guild", err=True)
        sys.exit(2)
    if guild_id and create_guild_name:
        click.echo("error: --guild-id and --create-guild are mutually exclusive", err=True)
        sys.exit(2)

    token = _require_token()
    _install_sigint_handler()
    _setup_logging(token, verbose=verbose)
    manifest = _load_manifest_or_exit(manifest_path)
    asyncio.run(_run_provision(token, guild_id, create_guild_name, manifest, dry_run))


def _load_manifest_or_exit(manifest_path: str | None) -> Manifest:
    path = Path(manifest_path) if manifest_path else DEFAULT_MANIFEST
    try:
        return load_manifest(path)
    except ProvisioningError as exc:
        click.echo(f"error: invalid manifest: {exc}", err=True)
        sys.exit(2)


async def _run_provision(
    token: str,
    guild_id: str | None,
    create_guild_name: str | None,
    manifest: Manifest,
    dry_run: bool,
) -> None:
    async with aiohttp.ClientSession() as session:
        api = BotApi(session, token)
        try:
            target_guild_id = guild_id
            if create_guild_name:
                # Preflight: unverified bots can be in at most 10 guilds
                my_guilds = await api.list_my_guilds()
                if len(my_guilds) >= 10:
                    raise ProvisioningError(
                        f"bot is already in {len(my_guilds)} guilds; "
                        f"unverified bots are limited to 10. Remove the bot from "
                        f"one before retrying --create-guild."
                    )
                new_guild = await api.create_guild(
                    name=create_guild_name, audit_reason="bootstrap (issue #35)"
                )
                target_guild_id = str(new_guild["id"])
                click.echo(f"created guild {create_guild_name!r} ({target_guild_id})")

            assert target_guild_id is not None  # narrowed above
            actual = await fetch_actual_state(api, target_guild_id)
            d = diff(manifest, actual)

            if dry_run:
                click.echo("=== DRY RUN PLAN ===")
                for op in d.ops:
                    click.echo(f"  would: {type(op).__name__} → {getattr(op.target, 'name', '?')}")
                click.echo(f"=== END DRY RUN ({len(d.ops)} ops would execute) ===")
                return

            result = await reconcile_provision(
                d, api, guild_id=target_guild_id, audit_reason="provision (issue #35)"
            )
            click.echo(f"created {result.created_count} entities:")
            for line in result.created_summary:
                click.echo(f"  {line}")
            if result.failed_op_index is not None:
                click.echo(
                    f"\nFAILED at op {result.failed_op_index}. "
                    f"Re-run `provision` with the same --guild-id to resume; "
                    f"idempotency will skip already-created entities.",
                    err=True,
                )
                sys.exit(1)
        except ProvisioningAuthError as exc:
            click.echo(f"auth error: {exc}", err=True)
            sys.exit(2)
        except ProvisioningPermissionError as exc:
            click.echo(f"permission error: {exc}", err=True)
            sys.exit(2)
        except ProvisioningError as exc:
            click.echo(f"provisioning error: {exc}", err=True)
            sys.exit(1)


@cli.command()
@click.option("--guild-id", required=True, help="Guild to teardown.")
@click.option("--manifest", "manifest_path", default=None)
@click.option("--yes", is_flag=True, help="Skip interactive confirmation.")
@click.option("--verbose", "-v", is_flag=True)
def teardown(guild_id: str, manifest_path: str | None, yes: bool, verbose: bool) -> None:
    """Delete every marker-carrying entity. Does NOT delete the guild itself."""
    token = _require_token()
    _install_sigint_handler()
    _setup_logging(token, verbose=verbose)
    manifest = _load_manifest_or_exit(manifest_path)
    asyncio.run(_run_teardown(token, guild_id, manifest, yes))


async def _run_teardown(token: str, guild_id: str, manifest: Manifest, yes: bool) -> None:
    async with aiohttp.ClientSession() as session:
        api = BotApi(session, token)
        try:
            actual = await fetch_actual_state(api, guild_id)
            to_delete = [
                ch
                for ch in actual.channels
                if ch.type in (0, 15) and ch.topic and ch.topic.startswith(manifest.marker)
            ]
            if not to_delete:
                click.echo("nothing to delete — guild is clean of marker entities")
                return
            click.echo("the following channels will be deleted:")
            for ch in to_delete:
                click.echo(f"  #{ch.name} ({ch.discord_id})")
            if not yes and not click.confirm("\nproceed?", default=False):
                click.echo("aborted")
                return
            result = await reconcile_teardown(
                actual, api, marker=manifest.marker, audit_reason="teardown (issue #35)"
            )
            click.echo(f"deleted {result.deleted_count} entities")
            if result.skipped_count > 0:
                click.echo(
                    f"WARNING: {result.skipped_count} entities skipped due to errors; "
                    f"re-run teardown to retry",
                    err=True,
                )
        except ProvisioningAuthError as exc:
            click.echo(f"auth error: {exc}", err=True)
            sys.exit(2)
        except ProvisioningPermissionError as exc:
            click.echo(f"permission error: {exc}", err=True)
            sys.exit(2)
        except ProvisioningError as exc:
            click.echo(f"teardown error: {exc}", err=True)
            sys.exit(1)


@cli.command()
@click.option("--guild-id", required=True)
@click.option("--manifest", "manifest_path", default=None)
@click.option("--verbose", "-v", is_flag=True)
def verify(guild_id: str, manifest_path: str | None, verbose: bool) -> None:
    """Compare manifest to live guild state; exit 0=match, 1=drift, 2=error."""
    token = _require_token()
    _install_sigint_handler()
    _setup_logging(token, verbose=verbose)
    manifest = _load_manifest_or_exit(manifest_path)
    asyncio.run(_run_verify(token, guild_id, manifest))


async def _run_verify(token: str, guild_id: str, manifest: Manifest) -> None:
    async with aiohttp.ClientSession() as session:
        api = BotApi(session, token)
        try:
            actual = await fetch_actual_state(api, guild_id)
            d = diff(manifest, actual)
            result = reconcile_verify(d)
            if result.exit_code == 0:
                click.echo("VERIFIED: manifest matches actual state")
                sys.exit(0)
            else:
                click.echo("DRIFT DETECTED:", err=True)
                for line in result.diff_lines:
                    click.echo(f"  {line}", err=True)
                sys.exit(1)
        except ProvisioningAuthError as exc:
            click.echo(f"auth error: {exc}", err=True)
            sys.exit(2)
        except ProvisioningPermissionError as exc:
            click.echo(f"permission error: {exc}", err=True)
            sys.exit(2)
        except ProvisioningError as exc:
            click.echo(f"verify error: {exc}", err=True)
            sys.exit(2)


if __name__ == "__main__":
    cli()
