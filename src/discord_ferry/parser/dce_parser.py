"""Parse DiscordChatExporter JSON export files."""

import json
import logging
import re
from pathlib import Path
from typing import Any

from discord_ferry.parser.models import (
    DCEAttachment,
    DCEAuthor,
    DCEChannel,
    DCEEmoji,
    DCEExport,
    DCEGuild,
    DCEMessage,
    DCEReaction,
    DCEReference,
    DCERole,
)

logger = logging.getLogger(__name__)

_THREE_SEGMENT_RE = re.compile(r"^(.+?) - (.+?) - (.+?) \[(\d+)\]$")
_TWO_SEGMENT_RE = re.compile(r"^(.+?) - (.+?) \[(\d+)\]$")


def parse_export_directory(export_dir: Path) -> list[DCEExport]:
    """Parse all DCE JSON files in a directory (top-level only).

    Args:
        export_dir: Directory containing DCE JSON export files.

    Returns:
        List of DCEExport objects sorted by channel name, one per valid file.
        Files that cannot be parsed as valid DCE JSON are skipped with a warning.
    """
    exports: list[DCEExport] = []
    for json_path in sorted(export_dir.glob("*.json")):
        try:
            export = parse_single_export(json_path)
            exports.append(export)
        except (ValueError, json.JSONDecodeError, KeyError):
            logger.warning("Skipping %s: not a valid DCE export", json_path.name)
    exports.sort(key=lambda e: e.channel.name)
    return exports


def parse_single_export(json_path: Path) -> DCEExport:
    """Parse a single DCE JSON export file.

    Args:
        json_path: Path to the DCE JSON export file.

    Returns:
        Parsed DCEExport dataclass.

    Raises:
        ValueError: If the file is not a valid DCE export (missing required keys).
        json.JSONDecodeError: If the file is not valid JSON.
    """
    raw: Any = json.loads(json_path.read_text(encoding="utf-8"))

    # Validate required top-level keys
    for key in ("guild", "channel", "messages", "messageCount"):
        if key not in raw:
            raise ValueError(f"Missing required key '{key}' in {json_path.name}")

    guild = _parse_guild(raw["guild"])
    channel = _parse_channel(raw["channel"])

    messages = [_parse_message(m) for m in raw["messages"]]
    messages.sort(key=lambda m: m.timestamp)

    is_thread, parent_channel_name = _infer_thread_info(json_path.stem)

    return DCEExport(
        guild=guild,
        channel=channel,
        messages=messages,
        message_count=int(raw["messageCount"]),
        exported_at=str(raw.get("exportedAt", "")),
        is_thread=is_thread,
        parent_channel_name=parent_channel_name,
    )


def validate_export(exports: list[DCEExport], export_dir: Path) -> list[dict[str, str]]:
    """Validate a list of parsed DCE exports and return any warnings.

    Args:
        exports: Parsed exports to validate.
        export_dir: Source directory (used for context in warning messages).

    Returns:
        List of warning dicts, each with 'type' and 'message' keys.
    """
    warnings: list[dict[str, str]] = []
    unique_channel_ids: set[str] = set()
    custom_emoji_ids: set[str] = set()

    for export in exports:
        channel_name = export.channel.name

        # Check for rendered markdown (mentions present but no raw <@ syntax in content)
        # Scan all messages that have mentions, not just first 20
        for msg in export.messages:
            if msg.mentions and "<@" not in msg.content and "<#" not in msg.content:
                warnings.append(
                    {
                        "type": "rendered_markdown",
                        "message": (
                            f"Channel '{channel_name}': message {msg.id} has mentions but no raw "
                            f"<@ syntax — export may have been created without --markdown false"
                        ),
                    }
                )
                break  # one warning per export is enough

        # Check for HTTP attachment URLs (media not downloaded)
        http_warned = False
        for msg in export.messages:
            if http_warned:
                break
            for att in msg.attachments:
                if att.url.startswith("http"):
                    warnings.append(
                        {
                            "type": "http_attachment",
                            "message": (
                                f"Channel '{channel_name}': attachment '{att.file_name}' "
                                f"has an HTTP URL — media was not downloaded locally"
                            ),
                        }
                    )
                    http_warned = True
                    break

        # Collect unique non-thread channel IDs
        if not export.is_thread:
            unique_channel_ids.add(export.channel.id)

        # Collect custom emoji IDs from reactions
        for msg in export.messages:
            for reaction in msg.reactions:
                if reaction.emoji.id:
                    custom_emoji_ids.add(reaction.emoji.id)

        # Check for empty exports
        if len(export.messages) == 0:
            warnings.append(
                {
                    "type": "empty_export",
                    "message": f"Channel '{channel_name}' has no messages",
                }
            )

    if len(unique_channel_ids) > 200:
        warnings.append(
            {
                "type": "channel_limit",
                "message": (
                    f"Export contains {len(unique_channel_ids)} channels; "
                    f"Stoat allows a maximum of 200 per server"
                ),
            }
        )

    if len(custom_emoji_ids) > 100:
        warnings.append(
            {
                "type": "emoji_limit",
                "message": (
                    f"Export contains {len(custom_emoji_ids)} unique custom emoji; "
                    f"Stoat allows a maximum of 100 per server"
                ),
            }
        )

    return warnings


def _infer_thread_info(filename: str) -> tuple[bool, str]:
    """Infer whether a filename represents a thread and return its parent channel name.

    Args:
        filename: The stem (without extension) of the DCE export file.

    Returns:
        Tuple of (is_thread, parent_channel_name).
        For regular channels: (False, "").
        For threads/forum posts: (True, "<parent channel name>").
    """
    match = _THREE_SEGMENT_RE.match(filename)
    if match:
        parent_channel_name = match.group(2)
        return True, parent_channel_name
    return False, ""


def _parse_guild(raw: Any) -> DCEGuild:
    return DCEGuild(
        id=str(raw["id"]),
        name=str(raw["name"]),
        icon_url=str(raw.get("iconUrl", "")),
    )


def _parse_channel(raw: Any) -> DCEChannel:
    return DCEChannel(
        id=str(raw["id"]),
        type=int(raw["type"]),
        name=str(raw["name"]),
        category_id=str(raw.get("categoryId", "")),
        category=str(raw.get("category", "")),
        topic=str(raw.get("topic", "")),
    )


def _parse_message(raw: Any) -> DCEMessage:
    author = _parse_author(raw["author"])
    attachments = [_parse_attachment(a) for a in (raw.get("attachments") or [])]
    reactions = [_parse_reaction(r) for r in (raw.get("reactions") or [])]

    reference: DCEReference | None = None
    ref_raw = raw.get("reference")
    if ref_raw is not None:
        reference = DCEReference(
            message_id=str(ref_raw.get("messageId", "")),
            channel_id=str(ref_raw.get("channelId", "")),
            guild_id=str(ref_raw.get("guildId", "")),
        )

    embeds: list[dict[str, object]] = list(raw.get("embeds") or [])
    stickers: list[dict[str, str]] = list(raw.get("stickers") or [])
    mentions: list[dict[str, str]] = list(raw.get("mentions") or [])

    return DCEMessage(
        id=str(raw["id"]),
        type=str(raw["type"]),
        timestamp=str(raw["timestamp"]),
        content=raw.get("content", "") or "",
        author=author,
        is_pinned=bool(raw.get("isPinned", False)),
        timestamp_edited=str(raw["timestampEdited"]) if raw.get("timestampEdited") else None,
        attachments=attachments,
        embeds=embeds,
        stickers=stickers,
        reactions=reactions,
        mentions=mentions,
        reference=reference,
    )


def _parse_author(raw: Any) -> DCEAuthor:
    roles = [
        DCERole(
            id=str(r["id"]),
            name=str(r["name"]),
            color=str(r["color"]) if r.get("color") else None,
            position=int(r.get("position", 0)),
        )
        for r in (raw.get("roles") or [])
    ]
    return DCEAuthor(
        id=str(raw["id"]),
        name=str(raw["name"]),
        discriminator=str(raw.get("discriminator", "0000")),
        nickname=str(raw.get("nickname", "")),
        color=str(raw["color"]) if raw.get("color") else None,
        is_bot=bool(raw.get("isBot", False)),
        avatar_url=str(raw.get("avatarUrl", "")),
        roles=roles,
    )


def _parse_attachment(raw: Any) -> DCEAttachment:
    return DCEAttachment(
        id=str(raw["id"]),
        url=str(raw["url"]),
        file_name=str(raw["fileName"]),
        file_size_bytes=int(raw.get("fileSizeBytes", 0)),
    )


def _parse_reaction(raw: Any) -> DCEReaction:
    emoji_raw = raw["emoji"]
    emoji = DCEEmoji(
        id=str(emoji_raw.get("id", "")),
        name=str(emoji_raw.get("name", "")),
        is_animated=bool(emoji_raw.get("isAnimated", False)),
        image_url=str(emoji_raw.get("imageUrl", "")),
    )
    return DCEReaction(
        emoji=emoji,
        count=int(raw.get("count", 1)),
    )
