"""Content transformation: markdown, mentions, emoji, spoilers, embeds."""

import re
from collections.abc import Callable
from datetime import datetime, timezone

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


def flatten_embed(embed: dict[str, object]) -> dict[str, object]:
    """Convert a Discord embed dict to a Stoat-compatible SendableEmbed dict.

    Stoat supports title, description, url, icon_url, and colour (British spelling).
    Rich embed sections (author, fields, footer) are flattened into description.

    Args:
        embed: Discord embed as parsed from DCE JSON.

    Returns:
        Dict with only non-None values, ready for Stoat's SendableEmbed.
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

    # Fields
    fields = embed.get("fields")
    if isinstance(fields, list):
        for field in fields:
            if isinstance(field, dict):
                fname = field.get("name", "")
                fvalue = field.get("value", "")
                if fname or fvalue:
                    parts.append(f"**{fname}:** {fvalue}")

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

    return result


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


def handle_stickers(stickers: list[dict[str, str]]) -> str:
    """Build a string representation of Discord stickers for appending to content.

    Args:
        stickers: List of sticker dicts from DCE export. Each may have a "name" key.

    Returns:
        Newline-prefixed sticker lines concatenated, or empty string if none.
    """
    parts: list[str] = []
    for sticker in stickers:
        name = sticker.get("name") or "unknown"
        parts.append(f"\n[Sticker: {name}]")
    return "".join(parts)


def strip_underline(content: str) -> str:
    """Convert Discord underline syntax (__text__) to Stoat bold (**text**).

    Stoat has no underline support; bold is the closest visual equivalent.
    Code blocks and inline code spans are left unchanged.

    Args:
        content: Raw message content.

    Returns:
        Content with underline syntax converted to bold outside code spans.
    """
    return _transform_outside_code(content, lambda s: _UNDERLINE_RE.sub(r"**\1**", s))
