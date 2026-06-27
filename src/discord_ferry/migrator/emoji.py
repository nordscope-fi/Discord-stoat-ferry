"""Custom emoji upload and migration — Phase 7."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, TypedDict

from discord_ferry.core.events import MigrationEvent
from discord_ferry.core.security import safe_sanitize
from discord_ferry.migrator.api import api_create_emoji, get_session
from discord_ferry.migrator.sanitize import sanitize_emoji_name
from discord_ferry.parser.dce_parser import stream_messages
from discord_ferry.uploader.autumn import upload_with_cache

if TYPE_CHECKING:
    from pathlib import Path

    from discord_ferry.config import FerryConfig
    from discord_ferry.core.events import EventCallback
    from discord_ferry.parser.models import DCEExport
    from discord_ferry.state import MigrationState

logger = logging.getLogger(__name__)

_CONTENT_EMOJI_RE = re.compile(r"<(a?):([^:]+):(\d+)>")

# Delay between emoji creations — shares the /servers 5/10s rate bucket.
_CREATION_DELAY = 2.0


def _extract_emoji_from_content(content: str) -> list[tuple[str, str, bool]]:
    """Extract custom emoji references from message content.

    Args:
        content: Raw message content string.

    Returns:
        List of ``(emoji_id, emoji_name, is_animated)`` tuples. May contain duplicates.
    """
    results: list[tuple[str, str, bool]] = []
    for match in _CONTENT_EMOJI_RE.finditer(content):
        animated_flag, name, emoji_id = match.group(1), match.group(2), match.group(3)
        is_animated = animated_flag == "a"
        results.append((emoji_id, name, is_animated))
    return results


class _EmojiRecord(TypedDict):
    """A discovered custom emoji + an occurrence tally for usage-ranked truncation."""

    id: str
    name: str
    is_animated: bool
    image_url: str
    uses: int


def _record_emoji(
    discovered: dict[str, _EmojiRecord],
    emoji_id: str,
    name: str,
    is_animated: bool,
    image_url: str,
) -> None:
    """Record an emoji encounter (Batch 4 S2+S3).

    New emoji are added with ``uses=1``. On re-encounter, ``uses`` is incremented and the
    stored ``image_url`` is UPGRADED (never downgraded) when it is empty and the new source
    carries a usable path — so an emoji first seen in content/embeds (no path) is rescued by a
    later reaction that has the real downloaded asset.
    """
    rec = discovered.get(emoji_id)
    if rec is None:
        discovered[emoji_id] = {
            "id": emoji_id,
            "name": name,
            "is_animated": is_animated,
            "image_url": image_url,
            "uses": 1,
        }
        return
    rec["uses"] += 1
    if not rec["image_url"] and image_url:
        rec["image_url"] = image_url
        rec["name"] = name
        rec["is_animated"] = is_animated


def _numeric_id_key(value: str) -> tuple[int, int | str]:
    """Deterministic, non-numeric-safe sort key for an emoji id.

    Numeric snowflake ids sort by magnitude; non-numeric ids (test fixtures, odd exports) sort
    after, lexicographically. The ``0``/``1`` tuple head prevents any int-vs-str comparison.
    """
    return (0, int(value)) if value.isdigit() else (1, value)


async def run_emoji(
    config: FerryConfig,
    state: MigrationState,
    exports: list[DCEExport],
    on_event: EventCallback,
) -> None:
    """Phase 7 — Upload custom emoji to Autumn and create them on the Stoat server.

    Args:
        config: Ferry configuration (provides export_dir, stoat_url, token, upload_delay).
        state: Migration state; ``emoji_map`` will be populated with Discord ID -> Stoat ID.
        exports: Parsed DCE exports to scan for custom emoji.
        on_event: Event callback for progress reporting.
    """
    on_event(MigrationEvent(phase="emoji", status="started", message="Scanning exports for emoji"))

    # emoji_id -> _EmojiRecord (id, name, is_animated, image_url, uses)
    discovered: dict[str, _EmojiRecord] = {}

    for export in exports:
        msg_iter = (
            stream_messages(export.json_path)
            if export.json_path is not None
            else iter(export.messages)
        )
        for msg in msg_iter:
            # Source 1: reactions with a non-empty custom emoji ID.
            for reaction in msg.reactions:
                emoji = reaction.emoji
                if emoji.id:
                    _record_emoji(
                        discovered, emoji.id, emoji.name, emoji.is_animated, emoji.image_url
                    )

            # Source 2: inline emoji in message content.
            if msg.content:
                for emoji_id, emoji_name, is_animated in _extract_emoji_from_content(msg.content):
                    _record_emoji(discovered, emoji_id, emoji_name, is_animated, "")

            # Source 3: emoji in embed fields.
            for embed in msg.embeds:
                embed_fields: list[dict[str, object]] = (
                    embed.get("fields") if isinstance(embed.get("fields"), list) else []  # type: ignore[assignment]
                )
                embed_texts = [
                    embed.get("description", ""),
                    embed.get("title", ""),
                    *(f.get("value", "") for f in embed_fields),
                ]
                for text_field in embed_texts:
                    if text_field:
                        for emoji_id, emoji_name, is_animated in _extract_emoji_from_content(
                            str(text_field)
                        ):
                            _record_emoji(discovered, emoji_id, emoji_name, is_animated, "")

    if not discovered:
        on_event(MigrationEvent(phase="emoji", status="completed", message="No custom emoji found"))
        return

    # Enforce the 100-emoji server limit. Batch 4 (S3): rank UPLOADABLE emoji first (an emoji
    # with no local asset is skipped at upload below, so it must never occupy a kept slot ahead
    # of a creatable one), then by occurrence frequency (most-used kept), tie-break by numeric id
    # (non-numeric-safe) — NOT the old lexicographic str(id) slice. "Uploadable" matches the
    # upload gate: a non-empty local path that is not an un-downloaded http URL.
    emoji_list = sorted(
        discovered.values(),
        key=lambda e: (
            0 if (e["image_url"] and not e["image_url"].startswith("http")) else 1,
            -e["uses"],
            _numeric_id_key(e["id"]),
        ),
    )
    if len(emoji_list) > config.max_emoji:
        dropped_names = ", ".join(str(e["name"]) for e in emoji_list[config.max_emoji :])
        state.warnings.append(
            {
                "phase": "emoji",
                "type": "emoji_limit",
                "message": (
                    f"Found {len(emoji_list)} unique emoji; truncating to {config.max_emoji} "
                    f"by usage (Stoat server limit). Dropped: {dropped_names}"
                ),
            }
        )
        on_event(
            MigrationEvent(
                phase="emoji",
                status="warning",
                message=(
                    f"Found {len(emoji_list)} emoji but Stoat limit is {config.max_emoji}; "
                    f"truncating to the {config.max_emoji} most-used. Dropped: {dropped_names}"
                ),
            )
        )
        emoji_list = emoji_list[: config.max_emoji]

    total = len(emoji_list)
    on_event(
        MigrationEvent(
            phase="emoji",
            status="progress",
            message=f"Found {total} unique custom emoji to migrate",
            current=0,
            total=total,
        )
    )

    if config.dry_run:
        for emoji_info in emoji_list:
            state.emoji_map[str(emoji_info["id"])] = f"dry-emoji-{emoji_info['id']}"
        on_event(
            MigrationEvent(
                phase="emoji",
                status="completed",
                message=f"[DRY RUN] Mapped {total} emoji",
            )
        )
        return

    # Collision tracking: map sanitized name → occurrence count so that duplicate
    # names receive a numeric suffix (_2, _3, …) rather than overwriting each other.
    used_names: dict[str, int] = {}

    async with get_session(config) as session:
        for idx, emoji_info in enumerate(emoji_list, start=1):
            discord_id = str(emoji_info["id"])
            name = str(emoji_info["name"])
            image_url = str(emoji_info["image_url"])

            is_animated = bool(emoji_info["is_animated"])

            # Resume: skip emoji already in the map.
            if discord_id in state.emoji_map:
                on_event(
                    MigrationEvent(
                        phase="emoji",
                        status="progress",
                        message=f"Skipping :{name}: (already migrated)",
                        current=idx,
                        total=total,
                    )
                )
                continue

            # Skip emoji with no local image.
            if not image_url or image_url.startswith("http"):
                reason = "no local image path" if not image_url else "URL not downloaded"
                state.warnings.append(
                    {
                        "phase": "emoji",
                        "type": "missing_media",
                        "message": f"Skipping emoji :{name}: — {reason}",
                    }
                )
                on_event(
                    MigrationEvent(
                        phase="emoji",
                        status="warning",
                        message=f"Skipping :{name}: — {reason}",
                        current=idx,
                        total=total,
                    )
                )
                continue

            file_path: Path = config.export_dir / image_url
            if not file_path.exists():
                state.warnings.append(
                    {
                        "phase": "emoji",
                        "type": "missing_media",
                        "message": f"Skipping emoji :{name}: — file not found: {file_path}",
                    }
                )
                on_event(
                    MigrationEvent(
                        phase="emoji",
                        status="warning",
                        message=f"Skipping :{name}: — file not found",
                        current=idx,
                        total=total,
                    )
                )
                continue

            try:
                autumn_id = await upload_with_cache(
                    session,
                    state.autumn_url,
                    "emojis",
                    file_path,
                    config.token,
                    state.upload_cache,
                    config.upload_delay,
                )

                sanitized_name = sanitize_emoji_name(name, used_names)
                if sanitized_name != sanitize_emoji_name(name):
                    logger.warning(
                        "Emoji name collision: :%s: → :%s: (duplicate sanitized name)",
                        name,
                        sanitized_name,
                    )
                await api_create_emoji(
                    session,
                    config.stoat_url,
                    config.token,
                    autumn_id,
                    sanitized_name,
                    state.stoat_server_id,
                )
                state.emoji_map[discord_id] = autumn_id

                if is_animated:
                    state.warnings.append(
                        {
                            "phase": "emoji",
                            "type": "animated_emoji",
                            "message": (
                                f"Emoji :{name}: is animated — animation will be lost on Stoat"
                            ),
                        }
                    )
                    on_event(
                        MigrationEvent(
                            phase="emoji",
                            status="warning",
                            message=f"Emoji :{name}: is animated — animation will be lost",
                            current=idx,
                            total=total,
                        )
                    )

                on_event(
                    MigrationEvent(
                        phase="emoji",
                        status="progress",
                        message=f"Created emoji :{name}:",
                        current=idx,
                        total=total,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                state.errors.append(
                    {
                        "phase": "emoji",
                        "type": "emoji_create_failed",
                        "message": safe_sanitize(
                            config.token_store,
                            f"Failed to create emoji :{name}: — {exc}",
                        ),
                    }
                )
                on_event(
                    MigrationEvent(
                        phase="emoji",
                        status="error",
                        message=f"Failed emoji :{name}: — {exc}",
                        current=idx,
                        total=total,
                    )
                )

            # Rate-limit courtesy: /servers bucket is 5/10s and shared with channels/roles.
            await asyncio.sleep(_CREATION_DELAY)

    migrated = len(state.emoji_map)
    on_event(
        MigrationEvent(
            phase="emoji",
            status="completed",
            message=f"Emoji phase complete — {migrated} emoji migrated",
        )
    )
