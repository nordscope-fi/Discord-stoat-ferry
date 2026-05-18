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
import sys
from pathlib import Path

import aiohttp
import click

from tests.provisioning._applier import (
    Manifest,
    diff,
    fetch_actual_state,
    load_manifest,
    reconcile_provision,
)
from tests.provisioning._bot_api import (
    BotApi,
    ProvisioningAuthError,
    ProvisioningError,
    TokenRedactingFilter,
    configure_aiohttp_logging,
)

DEFAULT_MANIFEST = Path(__file__).parent / "fixture-spec.json"


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
        except ProvisioningError as exc:
            click.echo(f"provisioning error: {exc}", err=True)
            sys.exit(1)


if __name__ == "__main__":
    cli()
