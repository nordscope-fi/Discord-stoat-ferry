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

from tests.provisioning._bot_api import ProvisioningError

if TYPE_CHECKING:
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
