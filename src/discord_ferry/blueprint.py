"""Server blueprint export, import, and build — portable server structure definitions."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from discord_ferry.core.atomicio import atomic_write_text
from discord_ferry.core.boundedjson import load_bounded_json
from discord_ferry.discord.metadata import read_cached_metadata
from discord_ferry.errors import BlueprintValidationError as BlueprintValidationError

if TYPE_CHECKING:
    from pathlib import Path

    from discord_ferry.parser.models import DCEExport

# A blueprint holds roles, categories and channels, no messages: the shipped templates are
# about 1.5 KB and 500 channels with 250 roles is about 80 KB. 10 MiB is wide headroom over that.
MAX_BLUEPRINT_BYTES = 10 * 1024 * 1024


@dataclass
class BlueprintRole:
    """A role in a server blueprint."""

    name: str
    colour: int = 0
    permissions: int = 0
    rank: int = 0


@dataclass
class BlueprintChannel:
    """A channel in a server blueprint."""

    name: str
    type: str = "Text"  # "Text" or "Voice"
    nsfw: bool = False
    user_limit: int = 0

    def __post_init__(self) -> None:
        self.validate_user_limit()

    def validate_user_limit(self) -> None:
        """Reject caps that cannot be represented by a Voice channel."""
        if type(self.user_limit) is not int or self.user_limit < 0:
            raise BlueprintValidationError("Channel user_limit must be a non-negative integer")
        if self.user_limit > 0 and self.type != "Voice":
            raise BlueprintValidationError(
                "Channel user_limit is only supported for Voice channels"
            )


@dataclass
class BlueprintCategory:
    """A category containing channels in a server blueprint."""

    name: str
    channels: list[BlueprintChannel] = field(default_factory=list)


@dataclass
class ServerBlueprint:
    """Complete server structure blueprint — portable, uses names not IDs."""

    name: str
    description: str = ""
    roles: list[BlueprintRole] = field(default_factory=list)
    categories: list[BlueprintCategory] = field(default_factory=list)
    uncategorized_channels: list[BlueprintChannel] = field(default_factory=list)


def blueprint_from_exports(
    exports: list[DCEExport],
    name: str | None = None,
    *,
    metadata_dir: Path | None = None,
) -> ServerBlueprint:
    """Build a ServerBlueprint from parsed DCE exports.

    The single home for the channel-mapping rule both shells depend on: a
    category-type channel (DCE ``type == 4``) is skipped, a voice channel
    (``type == 2``) or stage channel (``type == 13``) maps to a Stoat
    ``"Voice"`` channel and everything else to ``"Text"``, and a channel
    with no category lands in
    ``uncategorized_channels``. Keeping it here, rather than a copy in each
    shell, is why the CLI and GUI cannot diverge on the mapping.
    """
    metadata = None
    if metadata_dir is not None:
        guild_ids = {export.guild.id for export in exports}
        if len(guild_ids) != 1:
            raise BlueprintValidationError(
                "Selected metadata requires exports from one Discord server"
            )
        cached = read_cached_metadata(metadata_dir, export_guild_ids=guild_ids)
        if cached.meta is None:
            raise BlueprintValidationError(
                "Selected metadata is missing, malformed or belongs to another Discord server"
            )
        metadata = cached.meta
    guild_name = name or exports[0].guild.name
    categories: dict[str, list[BlueprintChannel]] = {}
    uncategorized: list[BlueprintChannel] = []

    for export in exports:
        ch = export.channel
        if ch.type == 4:  # category-type channel, not a real channel
            continue
        stoat_type = "Voice" if ch.type in (2, 13) else "Text"
        ch_meta = metadata.channel_metadata.get(ch.id) if metadata else None
        bp_channel = BlueprintChannel(
            name=ch.name,
            type=stoat_type,
            user_limit=ch_meta.user_limit if stoat_type == "Voice" and ch_meta else 0,
        )
        if ch.category:
            categories.setdefault(ch.category, []).append(bp_channel)
        else:
            uncategorized.append(bp_channel)

    return ServerBlueprint(
        name=guild_name,
        description=f"Exported from Discord server '{guild_name}'",
        categories=[
            BlueprintCategory(name=cat_name, channels=channels)
            for cat_name, channels in categories.items()
        ],
        uncategorized_channels=uncategorized,
    )


def export_blueprint(blueprint: ServerBlueprint, output_path: Path) -> None:
    """Export a ServerBlueprint to a JSON file.

    Args:
        blueprint: The blueprint to export.
        output_path: Path to write the JSON file.
    """
    data = _blueprint_to_dict(blueprint)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic: import_blueprint reads this file back, so a truncated export outlives
    # the run that produced it and there is no way to tell it apart from a good one
    # until the import fails or builds a partial server (#175).
    atomic_write_text(output_path, json.dumps(data, indent=2))


def import_blueprint(input_path: Path) -> ServerBlueprint:
    """Import a ServerBlueprint from a JSON file.

    Args:
        input_path: Path to the JSON file.

    Returns:
        Parsed ServerBlueprint.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file is larger than ``MAX_BLUEPRINT_BYTES`` or nested too deeply.
        json.JSONDecodeError: If the file is not valid JSON.
        KeyError: If required fields are missing.
    """
    raw = load_bounded_json(input_path, MAX_BLUEPRINT_BYTES)
    return _dict_to_blueprint(raw)


def _channel_to_dict(ch: BlueprintChannel) -> dict[str, Any]:
    ch.validate_user_limit()
    result: dict[str, Any] = {"name": ch.name, "type": ch.type, "nsfw": ch.nsfw}
    if ch.user_limit:
        result["user_limit"] = ch.user_limit
    return result


def _blueprint_to_dict(bp: ServerBlueprint) -> dict[str, Any]:
    return {
        "name": bp.name,
        "description": bp.description,
        "roles": [
            {
                "name": r.name,
                "colour": r.colour,
                "permissions": r.permissions,
                "rank": r.rank,
            }
            for r in bp.roles
        ],
        "categories": [
            {
                "name": cat.name,
                "channels": [_channel_to_dict(ch) for ch in cat.channels],
            }
            for cat in bp.categories
        ],
        "uncategorized_channels": [_channel_to_dict(ch) for ch in bp.uncategorized_channels],
    }


def _dict_to_blueprint(data: dict[str, Any]) -> ServerBlueprint:
    roles = [
        BlueprintRole(
            name=r["name"],
            colour=r.get("colour", 0),
            permissions=r.get("permissions", 0),
            rank=r.get("rank", 0),
        )
        for r in data.get("roles", [])
    ]
    categories = [
        BlueprintCategory(
            name=cat["name"],
            channels=[
                BlueprintChannel(
                    name=ch["name"],
                    type=ch.get("type", "Text"),
                    nsfw=ch.get("nsfw", False),
                    user_limit=ch.get("user_limit", 0),
                )
                for ch in cat.get("channels", [])
            ],
        )
        for cat in data.get("categories", [])
    ]
    uncategorized = [
        BlueprintChannel(
            name=ch["name"],
            type=ch.get("type", "Text"),
            nsfw=ch.get("nsfw", False),
            user_limit=ch.get("user_limit", 0),
        )
        for ch in data.get("uncategorized_channels", [])
    ]
    return ServerBlueprint(
        name=data["name"],
        description=data.get("description", ""),
        roles=roles,
        categories=categories,
        uncategorized_channels=uncategorized,
    )
