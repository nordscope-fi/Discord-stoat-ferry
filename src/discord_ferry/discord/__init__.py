"""Discord API integration — guild metadata fetching and permission translation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from discord_ferry.core.security import register_secret
from discord_ferry.discord.client import fetch_guild, fetch_guild_channels, fetch_guild_roles
from discord_ferry.discord.metadata import (
    ChannelMeta,
    DiscordMetadata,
    PermissionPair,
    RoleMeta,
    RoleOverride,
    _bad_id,
    capture_permission_source,
    load_bound_discord_metadata,
    load_discord_metadata,
    load_discord_metadata_for_resume,
    read_cached_metadata,
    resume_refusal,
    save_discord_metadata,
    validate_permission_source,
)
from discord_ferry.discord.permissions import translate_permissions
from discord_ferry.errors import MigrationError

if TYPE_CHECKING:
    from pathlib import Path

    import aiohttp

    from discord_ferry.parser.models import DCEExport

__all__ = [
    "fetch_and_translate_guild_metadata",
    "load_bound_discord_metadata",
    "load_discord_metadata",
    "load_discord_metadata_for_resume",
    "read_cached_metadata",
    "refresh_permission_metadata",
    "resume_refusal",
    "save_discord_metadata",
    "translate_permissions",
]


async def fetch_and_translate_guild_metadata(
    session: aiohttp.ClientSession, token: str, guild_id: str
) -> DiscordMetadata:
    """Fetch guild roles + channels from Discord API and translate to Stoat permissions.

    Args:
        session: An active aiohttp ClientSession.
        token: Discord user token.
        guild_id: Discord guild/server ID.

    Returns:
        DiscordMetadata with all permissions translated to Stoat bit space.
    """
    guild_data = await fetch_guild(session, token, guild_id)
    banner_hash = str(guild_data.get("banner") or "")
    guild_description = str(guild_data.get("description") or "")
    # Discord nsfw_level is a classification enum, not a boolean:
    # 0=DEFAULT, 1=EXPLICIT, 2=SAFE, 3=AGE_RESTRICTED. Only EXPLICIT and
    # AGE_RESTRICTED denote an NSFW server (SAFE must NOT be flagged).
    guild_nsfw = guild_data.get("nsfw_level", 0) in (1, 3)

    roles = await fetch_guild_roles(session, token, guild_id)
    channels = await fetch_guild_channels(session, token, guild_id)
    permission_source = capture_permission_source(guild_id, roles, channels)

    # Identify @everyone role (id == guild_id) → server default permissions
    server_default = 0
    role_permissions: dict[str, PermissionPair] = {}
    role_metadata: dict[str, RoleMeta] = {}
    for role in roles:
        if role.id == guild_id:
            server_default = translate_permissions(role.permissions)
            continue
        if role.managed:
            continue
        translated = translate_permissions(role.permissions)
        role_permissions[role.id] = PermissionPair(allow=translated, deny=0)
        role_metadata[role.id] = RoleMeta(
            hoist=role.hoist,
            position=role.position,
            icon_hash=role.icon,
            unicode_emoji=role.unicode_emoji,
            name=role.name,
            color=f"#{role.color:06x}" if role.color > 0 else "",
        )

    # Build channel metadata (filter user overrides, translate permissions)
    channel_metadata: dict[str, ChannelMeta] = {}
    category_positions: dict[str, int] = {}
    user_override_channels: list[dict[str, object]] = []
    for channel in channels:
        if channel.type == 4 and channel.position is not None:
            category_positions[channel.id] = channel.position
        default_override: PermissionPair | None = None
        role_overrides: list[RoleOverride] = []
        user_override_count = 0
        for ow in channel.permission_overwrites:
            if ow.type == 1:  # User override — Stoat doesn't support these
                user_override_count += 1
                continue
            if ow.id == guild_id:  # @everyone channel override → default_override
                default_override = PermissionPair(
                    allow=translate_permissions(ow.allow, in_overwrite=True),
                    deny=translate_permissions(ow.deny, in_overwrite=True),
                )
            else:
                role_overrides.append(
                    RoleOverride(
                        discord_role_id=ow.id,
                        allow=translate_permissions(ow.allow, in_overwrite=True),
                        deny=translate_permissions(ow.deny, in_overwrite=True),
                    )
                )
        if user_override_count > 0:
            user_override_channels.append(
                {
                    "channel_id": channel.id,
                    "channel_name": channel.name,
                    "override_count": user_override_count,
                }
            )
        channel_metadata[channel.id] = ChannelMeta(
            nsfw=channel.nsfw,
            default_override=default_override,
            role_overrides=role_overrides,
            slowmode=channel.rate_limit_per_user,
            user_limit=channel.user_limit,
            position=channel.position,
        )

    return DiscordMetadata(
        guild_id=guild_id,
        fetched_at=datetime.now(UTC).isoformat(),
        server_default_permissions=server_default,
        role_permissions=role_permissions,
        channel_metadata=channel_metadata,
        user_override_channels=user_override_channels,
        banner_hash=banner_hash,
        role_metadata=role_metadata,
        category_positions=category_positions,
        guild_description=guild_description,
        guild_nsfw=guild_nsfw,
        permission_source=permission_source,
    )


async def refresh_permission_metadata(
    session: aiohttp.ClientSession,
    token: str,
    guild_id: str,
    exports: list[DCEExport],
    output_dir: Path,
) -> DiscordMetadata:
    """Replace only the selected local metadata after validating fresh source evidence."""
    register_secret("discord", token)
    if (
        _bad_id("guild", guild_id) is not None
        or {export.guild.id for export in exports} != {guild_id}
        or any(_bad_id("channel", export.channel.id) is not None for export in exports)
    ):
        raise MigrationError("Source permission evidence does not match the selected exports")
    cached = read_cached_metadata(
        output_dir, configured_guild_id=guild_id, export_guild_ids={guild_id}
    )
    if cached.problem is not None:
        raise MigrationError(
            "Selected output metadata is unusable or belongs to another source; "
            "choose the correct output directory"
        )
    channel_ids = frozenset(export.channel.id for export in exports)
    try:
        meta = await fetch_and_translate_guild_metadata(session, token, guild_id)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise MigrationError(
            "Source permission evidence is incomplete or malformed; refresh was not saved"
        ) from exc
    if meta.guild_id != guild_id or validate_permission_source(meta, channel_ids):
        raise MigrationError("Source permission evidence does not match the selected exports")
    # Deliver cancellation requested by the fetch before the synchronous atomic write.
    await asyncio.sleep(0)
    save_discord_metadata(meta, output_dir)
    return meta
