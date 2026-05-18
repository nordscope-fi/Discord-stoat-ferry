"""Manifest schema, state fetcher, diff engine, and reconciler for provisioning.

This module is the logic layer between the CLI and the _bot_api transport.
It knows about manifest invariants, marker conventions, and the reconciler
modes; it does not know about Click, env vars, or exit codes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tests.provisioning._bot_api import BotApi, ProvisioningError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

MARKER_SAFE_REGEX = re.compile(r"^[a-zA-Z0-9 \-\[\]:]+$")


@dataclass(frozen=True)
class ManifestEmbedField:
    name: str
    value: str
    inline: bool


@dataclass(frozen=True)
class ManifestEmbed:
    title: str
    description: str
    color: int
    fields: tuple[ManifestEmbedField, ...]


@dataclass(frozen=True)
class ManifestMessage:
    id: str
    content: str
    embed: ManifestEmbed | None = None


@dataclass(frozen=True)
class ManifestTextChannel:
    id: str
    name: str
    topic_suffix: str
    messages: tuple[ManifestMessage, ...]


@dataclass(frozen=True)
class ManifestThread:
    id: str
    name: str
    parent_channel_id: str
    anchor_message_id: str
    first_message_content: str


@dataclass(frozen=True)
class ManifestForumPost:
    id: str
    name: str
    first_message_content: str


@dataclass(frozen=True)
class ManifestForumChannel:
    id: str
    name: str
    topic_suffix: str
    posts: tuple[ManifestForumPost, ...]


@dataclass(frozen=True)
class Manifest:
    version: int
    marker: str
    guild_name_for_bootstrap: str
    text_channels: tuple[ManifestTextChannel, ...]
    threads: tuple[ManifestThread, ...]
    forum_channels: tuple[ManifestForumChannel, ...]


def load_manifest(path: Path) -> Manifest:
    """Parse manifest JSON and enforce all load-time invariants.

    Enforces:
    - version == 1
    - marker matches MARKER_SAFE_REGEX (no HTML / shell-metachar attacks)
    - all IDs unique across the entire manifest
    - every thread.anchor_message_id references a real message in its
      parent_channel_id text channel
    - every text channel has exactly 1 message with an embed, and that
      embed has exactly 3 inline + 2 non-inline fields

    Raises ProvisioningError with a field-path pointer on any violation.
    """
    raw = json.loads(path.read_text())
    if raw.get("version") != 1:
        raise ProvisioningError(
            f"manifest version mismatch: expected 1, got {raw.get('version')!r}"
        )
    marker = raw.get("marker", "")
    if not marker or not MARKER_SAFE_REGEX.match(marker):
        raise ProvisioningError(
            f"manifest marker must match {MARKER_SAFE_REGEX.pattern!r}; got {marker!r}"
        )

    text_channels = tuple(_parse_text_channel(tc) for tc in raw["text_channels"])
    threads = tuple(_parse_thread(t) for t in raw["threads"])
    forum_channels = tuple(_parse_forum_channel(fc) for fc in raw["forum_channels"])
    manifest = Manifest(
        version=raw["version"],
        marker=marker,
        guild_name_for_bootstrap=raw["guild_name_for_bootstrap"],
        text_channels=text_channels,
        threads=threads,
        forum_channels=forum_channels,
    )

    _validate_unique_ids(manifest)
    _validate_anchor_message_refs(manifest)
    _validate_embed_inline_ratios(manifest)
    return manifest


def _parse_text_channel(data: dict[str, Any]) -> ManifestTextChannel:
    return ManifestTextChannel(
        id=data["id"],
        name=data["name"],
        topic_suffix=data["topic_suffix"],
        messages=tuple(_parse_message(m) for m in data["messages"]),
    )


def _parse_message(data: dict[str, Any]) -> ManifestMessage:
    embed = data.get("embed")
    return ManifestMessage(
        id=data["id"],
        content=data["content"],
        embed=_parse_embed(embed) if embed is not None else None,
    )


def _parse_embed(data: dict[str, Any]) -> ManifestEmbed:
    return ManifestEmbed(
        title=data["title"],
        description=data["description"],
        color=data["color"],
        fields=tuple(_parse_embed_field(f) for f in data["fields"]),
    )


def _parse_embed_field(data: dict[str, Any]) -> ManifestEmbedField:
    return ManifestEmbedField(name=data["name"], value=data["value"], inline=data["inline"])


def _parse_thread(data: dict[str, Any]) -> ManifestThread:
    return ManifestThread(
        id=data["id"],
        name=data["name"],
        parent_channel_id=data["parent_channel_id"],
        anchor_message_id=data["anchor_message_id"],
        first_message_content=data["first_message_content"],
    )


def _parse_forum_channel(data: dict[str, Any]) -> ManifestForumChannel:
    return ManifestForumChannel(
        id=data["id"],
        name=data["name"],
        topic_suffix=data["topic_suffix"],
        posts=tuple(_parse_forum_post(p) for p in data["posts"]),
    )


def _parse_forum_post(data: dict[str, Any]) -> ManifestForumPost:
    return ManifestForumPost(
        id=data["id"],
        name=data["name"],
        first_message_content=data["first_message_content"],
    )


def _validate_unique_ids(m: Manifest) -> None:
    seen: set[str] = set()
    for tc in m.text_channels:
        _check_id(tc.id, seen, f"text_channels[id={tc.id}]")
        for msg in tc.messages:
            _check_id(msg.id, seen, f"text_channels[{tc.id}].messages[id={msg.id}]")
    for t in m.threads:
        _check_id(t.id, seen, f"threads[id={t.id}]")
    for fc in m.forum_channels:
        _check_id(fc.id, seen, f"forum_channels[id={fc.id}]")
        for post in fc.posts:
            _check_id(post.id, seen, f"forum_channels[{fc.id}].posts[id={post.id}]")


def _check_id(id_: str, seen: set[str], path: str) -> None:
    if id_ in seen:
        raise ProvisioningError(f"duplicate id {id_!r} at {path}")
    seen.add(id_)


def _validate_anchor_message_refs(m: Manifest) -> None:
    msg_ids_by_channel: dict[str, set[str]] = {
        tc.id: {msg.id for msg in tc.messages} for tc in m.text_channels
    }
    tc_ids = set(msg_ids_by_channel.keys())
    for t in m.threads:
        if t.parent_channel_id not in tc_ids:
            raise ProvisioningError(
                f"thread {t.id!r}: parent_channel_id {t.parent_channel_id!r} "
                f"does not reference an existing text channel"
            )
        if t.anchor_message_id not in msg_ids_by_channel[t.parent_channel_id]:
            raise ProvisioningError(
                f"thread {t.id!r}: anchor_message_id {t.anchor_message_id!r} "
                f"not found in parent channel {t.parent_channel_id!r}"
            )


def _validate_embed_inline_ratios(m: Manifest) -> None:
    for tc in m.text_channels:
        embed_messages = [msg for msg in tc.messages if msg.embed is not None]
        if len(embed_messages) != 1:
            raise ProvisioningError(
                f"text_channel {tc.id!r}: expected exactly 1 message with an "
                f"embed; got {len(embed_messages)}"
            )
        embed = embed_messages[0].embed
        assert embed is not None  # narrowed by filter above
        inline_count = sum(1 for f in embed.fields if f.inline)
        non_inline_count = sum(1 for f in embed.fields if not f.inline)
        if inline_count != 3 or non_inline_count != 2:
            raise ProvisioningError(
                f"text_channel {tc.id!r} embed at message {embed_messages[0].id!r}: "
                f"expected 3 inline / 2 non-inline fields; got "
                f"{inline_count} inline / {non_inline_count} non-inline"
            )


# ---- Actual state (fetched from Discord) ----


@dataclass(frozen=True)
class ActualEmbedField:
    name: str
    value: str
    inline: bool


@dataclass(frozen=True)
class ActualEmbed:
    title: str
    description: str
    color: int
    fields: tuple[ActualEmbedField, ...]


@dataclass(frozen=True)
class ActualMessage:
    discord_id: str
    channel_discord_id: str
    content: str
    embed: ActualEmbed | None


@dataclass(frozen=True)
class ActualChannel:
    discord_id: str
    name: str
    type: int  # 0=text, 15=forum, 11=public thread
    topic: str | None
    parent_id: str | None  # for threads: the parent text channel's snowflake


@dataclass(frozen=True)
class ActualState:
    guild_id: str
    channels: tuple[ActualChannel, ...]
    messages_by_channel: Mapping[str, tuple[ActualMessage, ...]]


# ---- Diff types: sealed discriminated union for mypy --strict ----
# Note: no shared base/Protocol. The reconciler relies on the union type alias
# `DiffOpT` for type narrowing via `match`. A Protocol with named fields would
# be inert here — `match` dispatch doesn't trigger structural checks, and the
# union alias is what apply_op annotates against. Each per-kind dataclass
# carries `parent_discord_id` and `reason` as its own fields (consistency by
# convention, enforced by code review rather than the type system).


@dataclass(frozen=True)
class CreateTextChannelOp:
    target: ManifestTextChannel
    parent_discord_id: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class CreateForumChannelOp:
    target: ManifestForumChannel
    parent_discord_id: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class CreateMessageOp:
    target: ManifestMessage
    parent_manifest_channel_id: str  # which manifest text channel (for state lookup)
    parent_discord_id: str | None = None  # resolved by apply_op via state if None
    reason: str = ""


@dataclass(frozen=True)
class CreateThreadOp:
    target: ManifestThread
    parent_discord_id: str | None = None  # text channel's Discord ID
    anchor_message_discord_id: str | None = None  # resolved after messages created
    reason: str = ""


@dataclass(frozen=True)
class CreateForumPostOp:
    target: ManifestForumPost
    parent_manifest_forum_id: str  # which manifest forum channel (for state lookup)
    parent_discord_id: str | None = None  # resolved by apply_op via state if None
    reason: str = ""


@dataclass(frozen=True)
class DeleteChannelOp:
    target: ActualChannel
    parent_discord_id: str | None = None  # always None
    reason: str = ""


# Type alias for any op (used in Diff.ops and apply_op):
DiffOpT = (
    CreateTextChannelOp
    | CreateForumChannelOp
    | CreateMessageOp
    | CreateThreadOp
    | CreateForumPostOp
    | DeleteChannelOp
)


@dataclass(frozen=True)
class EmbedMismatch:
    manifest_message_id: str
    reason: str  # e.g. "field 2 inline flag differs: expected False, got True"


@dataclass(frozen=True)
class Diff:
    ops: tuple[DiffOpT, ...]
    missing_entities: tuple[str, ...]  # manifest ids not present in actual
    extra_marker_entities: tuple[ActualChannel, ...]  # actual+marker+not-in-manifest
    extra_foreign_entities: tuple[ActualChannel, ...]  # actual+no-marker (informational)
    mismatched_embeds: tuple[EmbedMismatch, ...]


# ---- State fetcher ----


async def fetch_actual_state(api: BotApi, guild_id: str) -> ActualState:
    """Fetch the live guild state for diffing against the manifest.

    Fans out:
      - GET /guilds/{id}/channels (text + forum, NOT threads)
      - GET /guilds/{id}/threads/active (guild-wide active threads)
      - GET /channels/{parent}/threads/archived/public per text channel
      - GET /channels/{id}/messages?limit=100 for each channel and thread

    The merged channels tuple includes archived threads so that `verify`
    works after the 24h auto-archive boundary.
    """
    raw_channels = await api.list_channels(guild_id)
    raw_active_threads = await api.list_active_threads(guild_id)

    # Archived public threads — per parent text channel only
    text_channel_ids = [str(ch["id"]) for ch in raw_channels if ch["type"] == 0]
    raw_archived: list[dict[str, Any]] = []
    for parent_id in text_channel_ids:
        raw_archived.extend(await api.list_archived_public_threads(parent_id))

    # Build ActualChannel tuple
    all_raw = raw_channels + raw_active_threads + raw_archived
    channels = tuple(
        ActualChannel(
            discord_id=str(c["id"]),
            name=c["name"],
            type=c["type"],
            topic=c.get("topic"),
            parent_id=str(c["parent_id"]) if c.get("parent_id") else None,
        )
        for c in all_raw
    )

    # Fetch messages for every channel (text + threads — but NOT forum channels;
    # forum channels reject /messages by design)
    messages_by_channel: dict[str, tuple[ActualMessage, ...]] = {}
    for ch in channels:
        if ch.type == 15:  # forum channels: no direct messages
            continue
        raw_msgs = await api.list_messages(ch.discord_id)
        messages_by_channel[ch.discord_id] = tuple(
            _parse_actual_message(m, ch.discord_id) for m in raw_msgs
        )

    return ActualState(
        guild_id=guild_id,
        channels=channels,
        messages_by_channel=messages_by_channel,
    )


def _parse_actual_message(raw: dict[str, Any], channel_id: str) -> ActualMessage:
    embed = None
    embeds = raw.get("embeds") or []
    if embeds:
        e = embeds[0]
        embed = ActualEmbed(
            title=e.get("title", ""),
            description=e.get("description", ""),
            color=e.get("color", 0),
            fields=tuple(
                ActualEmbedField(name=f["name"], value=f["value"], inline=f.get("inline", False))
                for f in (e.get("fields") or [])
            ),
        )
    return ActualMessage(
        discord_id=str(raw["id"]),
        channel_discord_id=channel_id,
        content=raw.get("content", ""),
        embed=embed,
    )
