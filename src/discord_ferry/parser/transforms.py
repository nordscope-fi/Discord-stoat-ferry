"""Content transformation: markdown, mentions, emoji, spoilers, embeds."""

import logging
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from discord_ferry.parser.dce_parser import check_cdn_url_expiry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

# Matches fenced code blocks (``` ... ```) and inline code (` ... `).
# Group 1 captures the entire code span so it can be re-joined unchanged.
_CODE_SPAN_RE = re.compile(r"(```[\s\S]*?```|`[^`]*`)")


def _transform_outside_code(content: str, transform_fn: Callable[[str], str]) -> str:
    parts = _CODE_SPAN_RE.split(content)
    result: list[str] = []
    for i, part in enumerate(parts):
        # Even-indexed parts are plain text; odd-indexed parts are code spans
        if i % 2 == 0:
            result.append(transform_fn(part))
        else:
            result.append(part)
    return "".join(result)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_DISCORD_JUMP_LINK_RE = re.compile(
    r"https://(canary\.|ptb\.)?discord(app)?\.com/channels/(\d+)/(\d+)(?:/(\d+))?"
)
_DISCORD_INVITE_RE = re.compile(
    r"https://(discord\.gg|(canary\.|ptb\.)?discord(app)?\.com/invite)/(\w+)"
)

_SPOILER_RE = re.compile(r"\|\|(.+?)\|\|", re.DOTALL)
_UNDERLINE_RE = re.compile(r"__([^_]+?)__")
_USER_MENTION_RE = re.compile(r"<@!?(\d+)>")
_CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>")
_ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")
_EMOJI_RE = re.compile(r"<a?:([^:]+):(\d+)>")


def convert_spoilers(content: str) -> str:
    """Convert Discord spoiler syntax (||text||) to Stoat syntax (!!text!!).

    Code blocks and inline code spans are left unchanged.
    """
    return _transform_outside_code(content, lambda s: _SPOILER_RE.sub(r"!!\1!!", s))


def remap_mentions(
    content: str,
    channel_map: dict[str, str],
    role_map: dict[str, str],
    author_names: dict[str, str],
) -> str:
    """Remap Discord mention syntax to Stoat equivalents or plain-text fallbacks.

    Args:
        content: Raw message content from DCE export.
        channel_map: Mapping of Discord channel ID → Stoat channel ID.
        role_map: Mapping of Discord role ID → Stoat role ID.
        author_names: Mapping of Discord user ID → display name.

    Returns:
        Content with all mention syntax replaced.
    """

    def replace_user(m: re.Match[str]) -> str:
        uid = m.group(1)
        if uid in author_names:
            return f"@{author_names[uid]}"
        # Fallback: @Unknown# + first 4 chars of ID
        return f"@Unknown#{uid[:4]}"

    def replace_channel(m: re.Match[str]) -> str:
        cid = m.group(1)
        if cid in channel_map:
            return f"<#{channel_map[cid]}>"
        return "#deleted-channel"

    def replace_role(m: re.Match[str]) -> str:
        rid = m.group(1)
        if rid in role_map:
            return f"<@&{role_map[rid]}>"
        return "@deleted-role"

    def apply_all(text: str) -> str:
        text = _USER_MENTION_RE.sub(replace_user, text)
        text = _CHANNEL_MENTION_RE.sub(replace_channel, text)
        text = _ROLE_MENTION_RE.sub(replace_role, text)
        return text

    return _transform_outside_code(content, apply_all)


def remap_emoji(content: str, emoji_map: dict[str, str]) -> str:
    """Remap Discord custom emoji to Stoat emoji IDs or bracketed name fallbacks.

    Args:
        content: Raw message content.
        emoji_map: Mapping of Discord emoji ID → Stoat emoji ID.

    Returns:
        Content with custom emoji syntax replaced.
    """

    def replace_emoji(m: re.Match[str]) -> str:
        name = m.group(1)
        eid = m.group(2)
        if eid in emoji_map:
            return f":{emoji_map[eid]}:"
        return f"[:{name}:]"

    return _transform_outside_code(content, lambda s: _EMOJI_RE.sub(replace_emoji, s))


def rewrite_discord_links(content: str, channel_map: dict[str, str]) -> str:
    """Rewrite Discord jump links and annotate invite links.

    Args:
        content: Raw message content.
        channel_map: Mapping of Discord channel ID -> Stoat channel ID.

    Returns:
        Content with jump links replaced by channel mentions (or annotated)
        and invite links annotated as no longer valid.
    """

    def _replace_jump(m: re.Match[str]) -> str:
        channel_id = m.group(4)
        stoat_id = channel_map.get(channel_id)
        if stoat_id:
            return f"<#{stoat_id}>"
        return f"{m.group(0)} [original Discord link]"

    def _replace_invite(m: re.Match[str]) -> str:
        return f"{m.group(0)} [Discord invite \u2014 no longer valid]"

    def _transform(text: str) -> str:
        text = _DISCORD_JUMP_LINK_RE.sub(_replace_jump, text)
        text = _DISCORD_INVITE_RE.sub(_replace_invite, text)
        return text

    return _transform_outside_code(content, _transform)


def _flush_inline_row(row: list[tuple[str, str]], parts: list[str]) -> None:
    names = " | ".join(f"**{name}**" for name, _ in row)
    values = " | ".join(value for _, value in row)
    parts.append(names)
    if any(v for _, v in row):
        parts.append(values)
    row.clear()


def flatten_embed(
    embed: dict[str, object],
    export_dir: Path | None = None,
) -> tuple[dict[str, object], Path | None]:
    """Convert a Discord embed dict to a Stoat-compatible SendableEmbed dict.

    Stoat supports title, description, url, icon_url, colour, and media.
    Rich embed sections (author, fields, footer) are flattened into description.

    Args:
        embed: Discord embed as parsed from DCE JSON.
        export_dir: Root export directory for resolving local media paths.

    Returns:
        Tuple of (embed dict, local media path or None). The media path is set
        when a thumbnail or image has a local file (downloaded via ``--media``).
    """
    parts: list[str] = []

    # Author name
    author = embed.get("author")
    if isinstance(author, dict):
        author_name = author.get("name")
        if author_name:
            parts.append(f"**{author_name}**")

    # Main description
    description = embed.get("description")
    if description:
        parts.append(str(description))

    # Fields — inline fields are grouped into pipe-separated rows (max 3 per row)
    fields = embed.get("fields")
    if isinstance(fields, list):
        inline_row: list[tuple[str, str]] = []
        for field_obj in fields:
            if not isinstance(field_obj, dict):
                continue
            fname = str(field_obj.get("name", ""))
            fvalue = str(field_obj.get("value", ""))
            if not fname and not fvalue:
                continue
            is_inline = bool(field_obj.get("inline", False))
            if is_inline:
                inline_row.append((fname, fvalue))
                if len(inline_row) >= 3:
                    _flush_inline_row(inline_row, parts)
            else:
                if inline_row:
                    _flush_inline_row(inline_row, parts)
                if fvalue:
                    parts.append(f"**{fname}**\n{fvalue}")
                else:
                    parts.append(f"**{fname}**")
        if inline_row:
            _flush_inline_row(inline_row, parts)

    # Footer
    footer = embed.get("footer")
    if isinstance(footer, dict):
        footer_text = footer.get("text")
        if footer_text:
            parts.append(f"_{footer_text}_")

    result: dict[str, object] = {}

    # Simple scalar fields
    title = embed.get("title")
    if title is not None:
        result["title"] = title

    url = embed.get("url")
    if url is not None:
        result["url"] = url

    # Colour — Discord uses "color" (American); Stoat uses "colour" (British)
    color = embed.get("color")
    if color is not None:
        result["colour"] = color

    # icon_url from author
    if isinstance(author, dict):
        icon_url = author.get("iconUrl")
        if icon_url is not None:
            result["icon_url"] = icon_url

    # Flattened description
    if parts:
        result["description"] = "\n\n".join(parts)

    # Extract media path from thumbnail or image (local files from --media export).
    media_path: Path | None = None
    if export_dir is not None:
        for media_key in ("thumbnail", "image"):
            media_obj = embed.get(media_key)
            if isinstance(media_obj, dict):
                media_url = media_obj.get("url", "")
                if isinstance(media_url, str) and not media_url.startswith(("http://", "https://")):
                    candidate = export_dir / media_url
                    if candidate.exists():
                        media_path = candidate
                        break

    # Check remote Discord CDN URLs for expiry (only if no local file was found).
    if media_path is None:
        for media_key in ("thumbnail", "image"):
            media_obj = embed.get(media_key)
            if isinstance(media_obj, dict):
                media_url = media_obj.get("url", "")
                if (
                    isinstance(media_url, str)
                    and media_url.startswith(("http://", "https://"))
                    and ("cdn.discordapp.com" in media_url or "media.discordapp.net" in media_url)
                    and check_cdn_url_expiry(media_url) is True
                ):
                    logger.warning("Expired embed media URL stripped: %s", media_url)
                    break  # Don't set media_path — expired URL stripped

    return result, media_path


def flatten_poll(poll: dict[str, Any]) -> str:
    """Render a Discord poll as plain text for inclusion in message content.

    Args:
        poll: Poll dict from DCE JSON with ``question`` and ``answers`` keys.

    Returns:
        Formatted poll text like ``**Poll: question**\\n• Option — N votes``.
    """
    question = ""
    q = poll.get("question")
    if isinstance(q, dict):
        question = q.get("text", "")
    elif isinstance(q, str):
        question = q

    lines = [f"**Poll: {question}**"]
    answers = poll.get("answers")
    if isinstance(answers, list):
        for answer in answers:
            if not isinstance(answer, dict):
                continue
            text = ""
            a = answer.get("text")
            if isinstance(a, str):
                text = a
            elif isinstance(a, dict):
                text = a.get("text", "")
            votes = answer.get("votes", 0)
            lines.append(f"\u2022 {text} \u2014 {votes} votes")

    return "\n".join(lines)


def format_original_timestamp(iso_timestamp: str) -> str:
    """Format a DCE ISO 8601 timestamp for prepending to migrated message content.

    Args:
        iso_timestamp: ISO 8601 string from DCE export (e.g. "2024-01-15T12:00:00+00:00").

    Returns:
        Formatted string like ``*[2024-01-15 12:00 UTC]*``.
    """
    dt = datetime.fromisoformat(iso_timestamp)
    utc_dt = dt.astimezone(timezone.utc)
    return f"*[{utc_dt.strftime('%Y-%m-%d %H:%M')} UTC]*"


def handle_stickers(
    stickers: list[dict[str, str]],
    export_dir: Path | None = None,
) -> tuple[str, list[Path]]:
    """Build a string representation of Discord stickers and collect local image paths.

    Args:
        stickers: List of sticker dicts from DCE export. Each may have "name"
            and "sourceUrl" keys.
        export_dir: Root export directory for resolving local sticker images.

    Returns:
        Tuple of (text fallback, list of local image paths to upload as attachments).
    """
    text_parts: list[str] = []
    image_paths: list[Path] = []
    for sticker in stickers:
        name = sticker.get("name") or "unknown"
        text_parts.append(f"\n[Sticker: {name}]")

        # Check for locally downloaded sticker image.
        if export_dir is not None:
            source_url = sticker.get("sourceUrl", "")
            if source_url and not source_url.startswith(("http://", "https://")):
                local = export_dir / source_url
                if local.exists():
                    image_paths.append(local)

    return "".join(text_parts), image_paths


def strip_underline(content: str) -> str:
    """Convert Discord underline syntax (__text__) to Stoat bold (**text**).

    Stoat has no underline support; bold is the closest visual equivalent.
    Code blocks and inline code spans are left unchanged.

    Args:
        content: Raw message content.

    Returns:
        Content with underline syntax converted to bold outside code spans.
    """
    result = _transform_outside_code(content, lambda s: _UNDERLINE_RE.sub(r"**\1**", s))
    # Collapse **** sequences that arise when underline wraps bold: **__text__** → ****text****
    return result.replace("****", "**")
