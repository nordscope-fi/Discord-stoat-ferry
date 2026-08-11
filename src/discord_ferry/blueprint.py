"""Server blueprint export, import, and build — portable server structure definitions."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from discord_ferry.core.atomicio import atomic_write_text


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


def export_blueprint(blueprint: ServerBlueprint, output_path: Path) -> None:
    """Export a ServerBlueprint to a JSON file.

    Args:
        blueprint: The blueprint to export.
        output_path: Path to write the JSON file.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = _blueprint_to_dict(blueprint)
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
        json.JSONDecodeError: If the file is not valid JSON.
        KeyError: If required fields are missing.
    """
    raw = json.loads(input_path.read_text(encoding="utf-8"))
    return _dict_to_blueprint(raw)


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
                "channels": [
                    {"name": ch.name, "type": ch.type, "nsfw": ch.nsfw} for ch in cat.channels
                ],
            }
            for cat in bp.categories
        ],
        "uncategorized_channels": [
            {"name": ch.name, "type": ch.type, "nsfw": ch.nsfw} for ch in bp.uncategorized_channels
        ],
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
