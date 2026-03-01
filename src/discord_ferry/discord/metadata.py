"""Save/load Discord guild metadata to/from discord_metadata.json."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PermissionPair:
    """Stoat allow/deny permission bitfield pair."""

    allow: int
    deny: int


@dataclass
class RoleOverride:
    """Per-role channel permission override (Stoat bit space, Discord role ID)."""

    discord_role_id: str
    allow: int
    deny: int


@dataclass
class ChannelMeta:
    """Per-channel metadata fetched from Discord API, translated to Stoat bit space."""

    nsfw: bool
    default_override: PermissionPair | None = None
    role_overrides: list[RoleOverride] = field(default_factory=list)


@dataclass
class DiscordMetadata:
    """Translated Discord guild metadata, persisted to discord_metadata.json."""

    guild_id: str
    fetched_at: str
    server_default_permissions: int
    role_permissions: dict[str, PermissionPair]
    channel_metadata: dict[str, ChannelMeta]


def save_discord_metadata(meta: DiscordMetadata, output_dir: Path) -> None:
    """Save to output_dir/discord_metadata.json using atomic write."""
    output_dir.mkdir(parents=True, exist_ok=True)
    data = _meta_to_dict(meta)
    tmp_path = output_dir / "discord_metadata.json.tmp"
    final_path = output_dir / "discord_metadata.json"
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp_path.rename(final_path)


def load_discord_metadata(output_dir: Path) -> DiscordMetadata | None:
    """Load from output_dir/discord_metadata.json, or return None if missing."""
    path = output_dir / "discord_metadata.json"
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    return _dict_to_meta(raw)


def _meta_to_dict(meta: DiscordMetadata) -> dict[str, Any]:
    return {
        "guild_id": meta.guild_id,
        "fetched_at": meta.fetched_at,
        "server_default_permissions": meta.server_default_permissions,
        "role_permissions": {
            k: {"allow": v.allow, "deny": v.deny} for k, v in meta.role_permissions.items()
        },
        "channel_metadata": {k: _channel_meta_to_dict(v) for k, v in meta.channel_metadata.items()},
    }


def _channel_meta_to_dict(cm: ChannelMeta) -> dict[str, Any]:
    d: dict[str, Any] = {"nsfw": cm.nsfw}
    if cm.default_override is not None:
        d["default_override"] = {
            "allow": cm.default_override.allow,
            "deny": cm.default_override.deny,
        }
    d["role_overrides"] = [
        {
            "discord_role_id": ro.discord_role_id,
            "allow": ro.allow,
            "deny": ro.deny,
        }
        for ro in cm.role_overrides
    ]
    return d


def _dict_to_meta(data: dict[str, Any]) -> DiscordMetadata:
    return DiscordMetadata(
        guild_id=data["guild_id"],
        fetched_at=data["fetched_at"],
        server_default_permissions=data.get("server_default_permissions", 0),
        role_permissions={
            k: PermissionPair(allow=v["allow"], deny=v["deny"])
            for k, v in data.get("role_permissions", {}).items()
        },
        channel_metadata={
            k: _dict_to_channel_meta(v) for k, v in data.get("channel_metadata", {}).items()
        },
    )


def _dict_to_channel_meta(data: dict[str, Any]) -> ChannelMeta:
    default_override = None
    if "default_override" in data:
        do = data["default_override"]
        default_override = PermissionPair(allow=do["allow"], deny=do["deny"])
    return ChannelMeta(
        nsfw=data.get("nsfw", False),
        default_override=default_override,
        role_overrides=[
            RoleOverride(
                discord_role_id=ro["discord_role_id"],
                allow=ro["allow"],
                deny=ro["deny"],
            )
            for ro in data.get("role_overrides", [])
        ],
    )
