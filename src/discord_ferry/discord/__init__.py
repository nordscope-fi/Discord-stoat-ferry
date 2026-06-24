"""Discord API integration — guild metadata fetching and permission translation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from discord_ferry.discord.client import fetch_guild, fetch_guild_channels, fetch_guild_roles
from discord_ferry.discord.metadata import (
    ChannelMeta,
    DiscordMetadata,
    PermissionPair,
    RoleMeta,
    RoleOverride,
    load_discord_metadata,
    save_discord_metadata,
)
from discord_ferry.discord.permissions import translate_permissions

if TYPE_CHECKING:
    import aiohttp

__all__ = [
    "fetch_and_translate_guild_metadata",
    "load_discord_metadata",
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
        )

    # Build channel metadata (filter user overrides, translate permissions)
    channel_metadata: dict[str, ChannelMeta] = {}
    category_positions: dict[str, int] = {}
    user_override_channels: list[dict[str, object]] = []
    for channel in channels:
        if channel.type == 4:  # GUILD_CATEGORY — capture position for ordering
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
                    allow=translate_permissions(ow.allow),
                    deny=translate_permissions(ow.deny, is_deny=True),
                )
            else:
                role_overrides.append(
                    RoleOverride(
                        discord_role_id=ow.id,
                        allow=translate_permissions(ow.allow),
                        deny=translate_permissions(ow.deny, is_deny=True),
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
    )
