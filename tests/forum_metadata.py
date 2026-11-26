"""A minimal cached discord_metadata.json that names one forum channel (#1641)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from discord_ferry.discord import client
from discord_ferry.discord.metadata import (
    DiscordMetadata,
    capture_permission_source,
    save_discord_metadata,
)
from discord_ferry.discord.permissions import translate_permissions

if TYPE_CHECKING:
    from pathlib import Path


def write_forum_metadata(output_dir: Path, *, guild_id: str, forum_id: str) -> None:
    """Save metadata whose source evidence holds one forum channel (type 15)."""
    default_bits = (1 << 10) | (1 << 11) | (1 << 38)
    roles = [
        client._parse_role(
            {
                "id": guild_id,
                "name": "default",
                "permissions": str(default_bits),
                "position": 0,
                "managed": False,
            }
        )
    ]
    channels = [
        client._parse_channel(
            {
                "id": forum_id,
                "name": "ideas",
                "type": 15,
                "guild_id": guild_id,
                "nsfw": False,
                "permission_overwrites": [],
            }
        )
    ]
    meta = DiscordMetadata(
        guild_id,
        "2026-10-20T08:00:00+00:00",
        translate_permissions(default_bits),
        {},
        {},
        permission_source=capture_permission_source(guild_id, roles, channels),
    )
    save_discord_metadata(meta, output_dir)
