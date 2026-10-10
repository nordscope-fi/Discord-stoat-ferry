"""Save/load Discord guild metadata to/from discord_metadata.json."""

import json
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from discord_ferry.core.atomicio import atomic_write_text
from discord_ferry.discord.permissions import ALL_STOAT_PERMISSIONS
from discord_ferry.errors import MigrationError


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
class RoleMeta:
    """Per-role Discord attributes not carried by the DCE export."""

    hoist: bool = False
    position: int = 0
    icon_hash: str = ""
    unicode_emoji: str = ""
    name: str = ""
    color: str = ""  # hex "#rrggbb", or "" for no color


@dataclass
class ChannelMeta:
    """Per-channel metadata fetched from Discord API, translated to Stoat bit space."""

    nsfw: bool
    default_override: PermissionPair | None = None
    role_overrides: list[RoleOverride] = field(default_factory=list)
    slowmode: int = 0
    user_limit: int = 0


@dataclass
class DiscordMetadata:
    """Translated Discord guild metadata, persisted to discord_metadata.json."""

    guild_id: str
    fetched_at: str
    server_default_permissions: int
    role_permissions: dict[str, PermissionPair]
    channel_metadata: dict[str, ChannelMeta]
    user_override_channels: list[dict[str, object]] = field(default_factory=list)
    banner_hash: str = ""
    role_metadata: dict[str, RoleMeta] = field(default_factory=dict)
    category_positions: dict[str, int] = field(default_factory=dict)
    guild_description: str = ""
    guild_nsfw: bool = False


def save_discord_metadata(meta: DiscordMetadata, output_dir: Path) -> None:
    """Save to output_dir/discord_metadata.json using atomic write."""
    output_dir.mkdir(parents=True, exist_ok=True)
    data = _meta_to_dict(meta)
    atomic_write_text(output_dir / "discord_metadata.json", json.dumps(data, indent=2))


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
        "user_override_channels": meta.user_override_channels,
        "banner_hash": meta.banner_hash,
        "role_metadata": {
            k: {
                "hoist": v.hoist,
                "position": v.position,
                "icon_hash": v.icon_hash,
                "unicode_emoji": v.unicode_emoji,
                "name": v.name,
                "color": v.color,
            }
            for k, v in meta.role_metadata.items()
        },
        "category_positions": meta.category_positions,
        "guild_description": meta.guild_description,
        "guild_nsfw": meta.guild_nsfw,
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
    d["slowmode"] = cm.slowmode
    d["user_limit"] = cm.user_limit
    return d


def _known_bits(value: Any) -> Any:
    """Drop bits Stoat does not define from a saved permission value.

    ``translate_permissions`` drops Discord bits that have no Stoat equivalent, so
    a fresh fetch can never produce a bit outside ``STOAT_PERMISSION_BITS``. A saved
    value holding one was not written by Ferry. Dropping it matches the fetch path
    and can only narrow what is sent. A value that is not an int that is not a bool and not negative
    is returned untouched for ``validate_cached_metadata`` to reject.
    """
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value & ALL_STOAT_PERMISSIONS
    return value


def _dict_to_meta(data: dict[str, Any]) -> DiscordMetadata:
    return DiscordMetadata(
        guild_id=data["guild_id"],
        fetched_at=data["fetched_at"],
        server_default_permissions=_known_bits(data.get("server_default_permissions", 0)),
        role_permissions={
            k: PermissionPair(allow=_known_bits(v["allow"]), deny=_known_bits(v["deny"]))
            for k, v in data.get("role_permissions", {}).items()
        },
        channel_metadata={
            k: _dict_to_channel_meta(v) for k, v in data.get("channel_metadata", {}).items()
        },
        user_override_channels=data.get("user_override_channels", []),
        banner_hash=data.get("banner_hash", ""),
        role_metadata={
            k: RoleMeta(
                hoist=v.get("hoist", False),
                position=v.get("position", 0),
                icon_hash=v.get("icon_hash", ""),
                unicode_emoji=v.get("unicode_emoji", ""),
                name=v.get("name", ""),
                color=v.get("color", ""),
            )
            for k, v in data.get("role_metadata", {}).items()
        },
        category_positions=data.get("category_positions", {}),
        guild_description=data.get("guild_description", ""),
        guild_nsfw=data.get("guild_nsfw", False),
    )


def _dict_to_channel_meta(data: dict[str, Any]) -> ChannelMeta:
    default_override = None
    if "default_override" in data:
        do = data["default_override"]
        default_override = PermissionPair(
            allow=_known_bits(do["allow"]), deny=_known_bits(do["deny"])
        )
    return ChannelMeta(
        nsfw=data.get("nsfw", False),
        default_override=default_override,
        role_overrides=[
            RoleOverride(
                discord_role_id=ro["discord_role_id"],
                allow=_known_bits(ro["allow"]),
                deny=_known_bits(ro["deny"]),
            )
            for ro in data.get("role_overrides", [])
        ],
        slowmode=data.get("slowmode", 0),
        user_limit=data.get("user_limit", 0),
    )


# --- Resume checks -----------------------------------------------------------
#
# A resumed run reuses discord_metadata.json without fetching. These checks run
# only on that path (#971). The loader above stays permissive because fixtures
# and older files use arbitrary ids.

_SNOWFLAKE = re.compile(r"[0-9]{1,20}")


def _shown(value: object) -> str:
    return repr(value)[:40]


def _bad_id(label: str, value: object) -> str | None:
    if isinstance(value, str) and _SNOWFLAKE.fullmatch(value):
        return None
    return f"{label} {_shown(value)} is not a Discord id (digits only)"


def _bad_perm(label: str, value: object) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return None
    return f"{label} {_shown(value)} is not a non-negative whole number"


def _problems(meta: DiscordMetadata) -> list[str]:
    found: list[str | None] = [
        _bad_id("guild_id", meta.guild_id),
        _bad_perm("server_default_permissions", meta.server_default_permissions),
    ]
    for rid, pair in meta.role_permissions.items():
        found.append(_bad_id("role_permissions key", rid))
        found.append(_bad_perm(f"role {rid} allow", pair.allow))
        found.append(_bad_perm(f"role {rid} deny", pair.deny))
    for cid, cm in meta.channel_metadata.items():
        found.append(_bad_id("channel_metadata key", cid))
        if cm.default_override is not None:
            found.append(_bad_perm(f"channel {cid} default allow", cm.default_override.allow))
            found.append(_bad_perm(f"channel {cid} default deny", cm.default_override.deny))
        for ro in cm.role_overrides:
            found.append(_bad_id(f"channel {cid} override role id", ro.discord_role_id))
            found.append(_bad_perm(f"channel {cid} override allow", ro.allow))
            found.append(_bad_perm(f"channel {cid} override deny", ro.deny))
    for rid in meta.role_metadata:
        found.append(_bad_id("role_metadata key", rid))
    for cat_id in meta.category_positions:
        found.append(_bad_id("category_positions key", cat_id))
    return [p for p in found if p is not None]


def load_discord_metadata_for_resume(
    output_dir: Path, *, configured_guild_id: str | None = None
) -> DiscordMetadata | None:
    """Load the cached metadata for a resume, or None when there is no file.

    Raises ``MigrationError`` when the file cannot be read, holds a malformed
    identifier or permission value, or names a guild other than
    ``configured_guild_id``. The cache is never used in those cases. It does not
    fall back to a fresh fetch: that needs a Discord token, which a resume may not
    carry, and a mismatch means the output directory or the configuration is wrong,
    which a refetch would paper over.
    """
    path = output_dir / "discord_metadata.json"
    try:
        meta = load_discord_metadata(output_dir)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise MigrationError(
            f"Cannot resume: {path} is not a valid Ferry metadata file "
            f"({type(exc).__name__}). Delete it to fetch fresh metadata, or restore it."
        ) from exc
    if meta is None:
        return None
    problems = _problems(meta)
    if problems:
        raise MigrationError(
            f"Cannot resume: {path} holds invalid values: {problems[0]}"
            + (f" (and {len(problems) - 1} more)" if len(problems) > 1 else "")
            + ". Delete it to fetch fresh metadata, or restore it."
        )
    if configured_guild_id and meta.guild_id != configured_guild_id:
        raise MigrationError(
            f"Cannot resume: {path} belongs to Discord server {meta.guild_id}, but the "
            f"configured server is {configured_guild_id}. Delete it to fetch fresh "
            "metadata, or point at the right output directory."
        )
    return meta


def ensure_metadata_matches_export(
    meta: DiscordMetadata, export_guild_ids: Collection[str], output_dir: Path
) -> None:
    """Raise ``MigrationError`` unless the cached guild is one the export names."""
    if export_guild_ids and meta.guild_id not in export_guild_ids:
        raise MigrationError(
            f"Cannot resume: {output_dir / 'discord_metadata.json'} belongs to Discord "
            f"server {meta.guild_id}, but the export is from "
            f"{', '.join(sorted(export_guild_ids))}. Delete it to fetch fresh metadata, "
            "or point at the right output directory."
        )
