"""Parse DiscordChatExporter JSON export files."""

import json
import logging
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import ijson  # type: ignore[import-untyped]

from discord_ferry.parser.models import (
    DCEAttachment,
    DCEAuthor,
    DCEChannel,
    DCEEmoji,
    DCEExport,
    DCEForwardedMessage,
    DCEGuild,
    DCEMessage,
    DCEReaction,
    DCEReference,
    DCERole,
)

logger = logging.getLogger(__name__)

_CONTENT_EMOJI_RE = re.compile(r"<a?:[^:]+:(\d+)>")
_THREE_SEGMENT_RE = re.compile(r"^(.+?) - (.+?) - (.+?) \[(\d+)\]$")
# Discord channel-type codes that are genuinely threads (incl. forum posts = 11).
# Used to override the filename heuristic so a guild/channel name containing " - "
# can't misclassify a normal channel as a thread.
_THREAD_TYPE_CODES = frozenset({10, 11, 12})

# DCE 2.47.1 emits enum-named channel types; pre-2.47 emitted integers.
# Downstream callers (migrator/structure.py, migrator/messages.py, review.py)
# branch on Discord-canonical integer codes, so we normalize on parse.
_DCE_CHANNEL_TYPE_TO_INT: dict[str, int] = {
    "GuildTextChat": 0,
    "GuildVoiceChat": 2,
    "GuildCategory": 4,
    "GuildNews": 5,
    "GuildNewsThread": 10,
    "GuildPublicThread": 11,
    "GuildPrivateThread": 12,
    "GuildStageVoice": 13,
    "GuildDirectory": 14,
    "GuildForum": 15,
    "GuildMedia": 16,
}


def parse_export_directory(export_dir: Path, *, metadata_only: bool = False) -> list[DCEExport]:
    """Parse all DCE JSON files in a directory (top-level only).

    Args:
        export_dir: Directory containing DCE JSON export files.
        metadata_only: If True, skip loading messages into memory. The returned
            DCEExport objects will have an empty ``messages`` list but ``json_path``
            and ``message_count`` will be set for later streaming.

    Returns:
        List of DCEExport objects sorted by channel name, one per valid file.
        Files that cannot be parsed as valid DCE JSON are skipped with a warning.
    """
    exports: list[DCEExport] = []
    for json_path in sorted(export_dir.glob("*.json")):
        try:
            export = parse_single_export(json_path, metadata_only=metadata_only)
            exports.append(export)
        except (ValueError, json.JSONDecodeError, KeyError):
            logger.warning("Skipping %s: not a valid DCE export", json_path.name)
    exports.sort(key=lambda e: e.channel.name)
    return exports


def parse_single_export(json_path: Path, *, metadata_only: bool = False) -> DCEExport:
    """Parse a single DCE JSON export file.

    Args:
        json_path: Path to the DCE JSON export file.
        metadata_only: If True, skip loading messages into memory. The returned
            DCEExport will have an empty ``messages`` list but ``json_path`` and
            ``message_count`` are set so callers can stream later via
            :func:`stream_messages`.

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

    if metadata_only:
        messages = []
    else:
        messages = [_parse_message(m) for m in raw["messages"]]
        messages.sort(key=lambda m: m.timestamp)

    is_thread, parent_channel_name = _infer_thread_info(json_path.stem, channel.type)

    return DCEExport(
        guild=guild,
        channel=channel,
        messages=messages,
        message_count=int(raw["messageCount"]),
        exported_at=str(raw.get("exportedAt") or ""),
        is_thread=is_thread,
        parent_channel_name=parent_channel_name,
        json_path=json_path,
    )


def stream_messages(json_path: Path) -> Iterator[DCEMessage]:
    """Yield messages one at a time from a DCE JSON file using streaming JSON.

    This avoids loading all messages into memory at once.

    Args:
        json_path: Path to the DCE JSON export file.

    Yields:
        Parsed DCEMessage objects in file order.
    """
    with open(json_path, "rb") as f:
        for raw_msg in ijson.items(f, "messages.item"):
            yield _parse_message(raw_msg)


def check_cdn_url_expiry(url: str) -> bool | None:
    """Check if a Discord CDN signed URL has expired.

    Parses the ``ex`` query parameter (hex-encoded Unix timestamp) from
    Discord's signed CDN URLs.

    Returns:
        True if the URL is expired, False if still valid, or None if the
        URL format is unrecognised (no ``ex`` parameter, non-hex value,
        or empty URL).
    """
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    qs = parse_qs(parsed.query)
    ex_values = qs.get("ex")
    if not ex_values:
        return None
    try:
        expiry_ts = int(ex_values[0], 16)
    except ValueError:
        return None
    return expiry_ts < time.time()


def validate_export(
    exports: list[DCEExport],
    export_dir: Path,
    author_names: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Validate a list of parsed DCE exports and return any warnings.

    When exports were parsed with ``metadata_only=True``, their ``messages`` list
    is empty but ``json_path`` is set. This function streams messages in a single
    pass in that case, keeping memory usage low.

    Args:
        exports: Parsed exports to validate.
        export_dir: Source directory (used for context in warning messages).
        author_names: Optional dict to populate with author ID -> display name
            mappings during the validation scan. This avoids a second full scan
            of all messages.

    Returns:
        List of warning dicts, each with 'type' and 'message' keys.
    """
    warnings: list[dict[str, str]] = []
    unique_channel_ids: set[str] = set()
    custom_emoji_ids: set[str] = set()
    expired_count = 0

    for export in exports:
        channel_name = export.channel.name

        # Stream or iterate messages in a single pass for all validation checks.
        if export.messages:
            msg_iter = iter(export.messages)
        elif export.json_path is not None:
            msg_iter = stream_messages(export.json_path)
        else:
            msg_iter = iter([])

        markdown_warned = False
        http_warned = False
        has_messages = False

        for msg in msg_iter:
            has_messages = True

            # Check for rendered markdown (first occurrence only)
            if (
                not markdown_warned
                and msg.mentions
                and "<@" not in msg.content
                and "<#" not in msg.content
            ):
                warnings.append(
                    {
                        "type": "rendered_markdown",
                        "message": (
                            f"Channel '{channel_name}': message {msg.id} has mentions but no raw "
                            f"<@ syntax — export may have been created without --markdown false"
                        ),
                    }
                )
                markdown_warned = True

            # Check for HTTP attachment URLs (first occurrence only) and expired CDN URLs
            for att in msg.attachments:
                if att.url.startswith("http"):
                    if not http_warned:
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
                    if check_cdn_url_expiry(att.url) is True:
                        expired_count += 1

            # Collect author names (if caller requested)
            if author_names is not None and msg.author.id not in author_names:
                author_names[msg.author.id] = msg.author.nickname or msg.author.name

            # Collect custom emoji IDs from reactions and content
            for reaction in msg.reactions:
                if reaction.emoji.id:
                    custom_emoji_ids.add(reaction.emoji.id)
            if msg.content:
                for match in _CONTENT_EMOJI_RE.finditer(msg.content):
                    custom_emoji_ids.add(match.group(1))

        # Collect unique non-thread channel IDs
        if not export.is_thread:
            unique_channel_ids.add(export.channel.id)

        # Check for empty exports (use message_count if messages were not iterated)
        if not has_messages and export.message_count == 0:
            warnings.append(
                {
                    "type": "empty_export",
                    "message": f"Channel '{channel_name}' has no messages",
                }
            )

    if expired_count > 0:
        warnings.append(
            {
                "phase": "validate",
                "type": "expired_cdn_url",
                "message": (
                    f"{expired_count} attachment URL(s) have expired. "
                    "Re-export with --media flag to download files locally."
                ),
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


def _infer_thread_info(filename: str, channel_type: int | None = None) -> tuple[bool, str]:
    """Infer whether a filename represents a thread and return its parent channel name.

    Args:
        filename: The stem (without extension) of the DCE export file.
        channel_type: The parsed Discord channel-type code. When known, it is
            authoritative: a non-thread type is never a thread, regardless of
            " - " in the guild/channel name. When ``None`` (e.g. filename-only
            callers), the 3-segment filename heuristic alone is used.

    Returns:
        Tuple of (is_thread, parent_channel_name).
        For regular channels: (False, "").
        For threads/forum posts: (True, "<parent channel name>").
    """
    # Type is authoritative when known: a non-thread Discord type is never a
    # thread, even if the guild/channel name contains " - ".
    if channel_type is not None and channel_type not in _THREAD_TYPE_CODES:
        return False, ""
    match = _THREE_SEGMENT_RE.match(filename)
    if match:
        parent_channel_name = match.group(2)
        return True, parent_channel_name
    return False, ""


def _parse_guild(raw: Any) -> DCEGuild:
    return DCEGuild(
        id=str(raw["id"]),
        name=str(raw["name"]),
        icon_url=str(raw.get("iconUrl") or ""),
    )


def _coerce_channel_type(raw: Any) -> int:
    """Map a DCE channel-type value to its Discord-canonical integer code.

    Accepts both DCE 2.47.1's enum strings (``"GuildTextChat"``) and the
    pre-2.47 integer form so the parser tolerates either shape on disk.
    """
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        if raw.isdigit():
            return int(raw)
        if raw in _DCE_CHANNEL_TYPE_TO_INT:
            return _DCE_CHANNEL_TYPE_TO_INT[raw]
    raise ValueError(f"Unrecognized DCE channel type: {raw!r}")


def _parse_channel(raw: Any) -> DCEChannel:
    # `raw.get(K, "")` would return None when the key exists with a null value
    # (the default only fires when the key is missing). DCE emits null for
    # `categoryId`/`category` on top-level channels and `topic` on threads,
    # so we collapse None to "" before stringifying — otherwise the field
    # becomes the truthy string "None" and slips past downstream truthy guards.
    return DCEChannel(
        id=str(raw["id"]),
        type=_coerce_channel_type(raw["type"]),
        name=str(raw["name"]),
        category_id=str(raw.get("categoryId") or ""),
        category=str(raw.get("category") or ""),
        topic=str(raw.get("topic") or ""),
    )


def _as_list(value: Any) -> list[Any]:
    """``value`` when it is a list, else ``[]``.

    Not the same as ``list(value or [])``: ``list()`` accepts any iterable, so a dict
    where a list was expected yields its **keys** instead of raising -- silent corruption
    rather than a loud failure.
    """
    return value if isinstance(value, list) else []


def _parse_message(raw: Any) -> DCEMessage:
    author = _parse_author(raw["author"])
    attachments = [_parse_attachment(a) for a in (raw.get("attachments") or [])]
    reactions = [_parse_reaction(r) for r in (raw.get("reactions") or [])]

    reference: DCEReference | None = None
    ref_raw = raw.get("reference")
    if ref_raw is not None:
        reference = DCEReference(
            # `messageId` is null on `ThreadCreated` system messages; collapse
            # None → "" (see _parse_channel for the same rationale).
            message_id=str(ref_raw.get("messageId") or ""),
            channel_id=str(ref_raw.get("channelId") or ""),
            guild_id=str(ref_raw.get("guildId") or ""),
            # "Default" | "Forward". Absent in pre-2.47 exports -> "", which the
            # migrator treats as "unknown kind", NOT as "Default".
            type=str(ref_raw.get("type") or ""),
        )

    # DCE 2.47+ (PR #1451) exports the full payload of a forwarded message. Nested
    # attachments/embeds/stickers use the same writers as their top-level counterparts,
    # so the same parsers apply.
    forwarded: DCEForwardedMessage | None = None
    fwd_raw = raw.get("forwardedMessage")
    if isinstance(fwd_raw, dict):
        forwarded = DCEForwardedMessage(
            timestamp=str(fwd_raw.get("timestamp") or ""),
            timestamp_edited=(
                str(fwd_raw["timestampEdited"]) if fwd_raw.get("timestampEdited") else None
            ),
            # str(): a non-string content would otherwise survive as e.g. an int and
            # raise TypeError inside the join in migrator.messages._merge_forwarded.
            content=str(fwd_raw.get("content") or ""),
            # isinstance(): `list(some_dict)` yields its KEYS rather than raising, so an
            # unexpected shape would corrupt silently rather than fail.
            attachments=[_parse_attachment(a) for a in _as_list(fwd_raw.get("attachments"))],
            embeds=list(_as_list(fwd_raw.get("embeds"))),
            stickers=list(_as_list(fwd_raw.get("stickers"))),
        )

    embeds: list[dict[str, object]] = list(raw.get("embeds") or [])
    stickers: list[dict[str, str]] = list(raw.get("stickers") or [])
    mentions: list[dict[str, str]] = list(raw.get("mentions") or [])
    poll: dict[str, object] | None = raw.get("poll")

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
        poll=poll,
        forwarded_message=forwarded,
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
        # `raw.get(K, default)` returns the default only when K is missing; if
        # K is present-but-null, `.get` returns None and `str(None) == "None"`.
        # Collapse via `or` to preserve the intended default for both shapes.
        discriminator=str(raw.get("discriminator") or "0000"),
        nickname=str(raw.get("nickname") or ""),
        color=str(raw["color"]) if raw.get("color") else None,
        is_bot=bool(raw.get("isBot", False)),
        avatar_url=str(raw.get("avatarUrl") or ""),
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
    # Unicode emojis emit `"id": null` in DCE output; without `or ""` we'd
    # produce the truthy 4-char string "None" instead of an empty ID.
    emoji = DCEEmoji(
        id=str(emoji_raw.get("id") or ""),
        name=str(emoji_raw.get("name") or ""),
        is_animated=bool(emoji_raw.get("isAnimated", False)),
        image_url=str(emoji_raw.get("imageUrl") or ""),
    )
    return DCEReaction(
        emoji=emoji,
        count=int(raw.get("count", 1)),
    )
