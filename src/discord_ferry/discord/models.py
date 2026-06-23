"""Dataclasses for Discord REST API responses."""

from dataclasses import dataclass, field


@dataclass
class PermissionOverwrite:
    """A single permission overwrite entry from a Discord channel."""

    id: str
    type: int  # 0 = role, 1 = member
    allow: int  # Discord permission bitfield
    deny: int  # Discord permission bitfield


@dataclass
class DiscordRole:
    """A Discord guild role with permission bitfield."""

    id: str
    name: str
    permissions: int
    position: int = 0
    color: int = 0
    hoist: bool = False
    managed: bool = False


@dataclass
class DiscordChannel:
    """A Discord guild channel with NSFW flag and permission overwrites."""

    id: str
    name: str
    type: int
    nsfw: bool = False
    position: int = 0
    rate_limit_per_user: int = 0  # slowmode seconds (text channels)
    user_limit: int = 0  # voice channel capacity cap (0 = unlimited)
    permission_overwrites: list[PermissionOverwrite] = field(default_factory=list)
