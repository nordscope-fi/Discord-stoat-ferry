"""Manifest schema, state fetcher, diff engine, and reconciler for provisioning.

This module is the logic layer between the CLI and the _bot_api transport.
It knows about manifest invariants, marker conventions, and the reconciler
modes; it does not know about Click, env vars, or exit codes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, assert_never

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


# ---- Reconciler diff ----


def diff(manifest: Manifest, actual: ActualState) -> Diff:
    """Compute the reconciler diff between desired manifest and actual state."""
    ops: list[DiffOpT] = []
    missing: list[str] = []
    extra_marker: list[ActualChannel] = []
    extra_foreign: list[ActualChannel] = []
    mismatched_embeds: list[EmbedMismatch] = []

    marker = manifest.marker

    actual_text_channels = {
        ch.name: ch
        for ch in actual.channels
        if ch.type == 0 and ch.topic and ch.topic.startswith(marker)
    }
    actual_forum_channels = {
        ch.name: ch
        for ch in actual.channels
        if ch.type == 15 and ch.topic and ch.topic.startswith(marker)
    }
    actual_threads = {ch.name: ch for ch in actual.channels if ch.type == 11}

    matched_actual_ids: set[str] = set()

    # Text channels and their messages
    for tc in manifest.text_channels:
        actual_ch = actual_text_channels.get(tc.name)
        if actual_ch is None:
            missing.append(tc.id)
            ops.append(CreateTextChannelOp(target=tc, reason="missing from guild"))
            for msg in tc.messages:
                ops.append(
                    CreateMessageOp(
                        target=msg,
                        parent_manifest_channel_id=tc.id,
                        reason="parent channel missing",
                    )
                )
        else:
            matched_actual_ids.add(actual_ch.discord_id)
            actual_msgs = actual.messages_by_channel.get(actual_ch.discord_id, ())
            actual_msg_by_marker_id: dict[str, ActualMessage] = {}
            for am in actual_msgs:
                for manifest_id in _extract_marker_ids(am.content):
                    actual_msg_by_marker_id[manifest_id] = am
            for msg in tc.messages:
                if msg.id not in actual_msg_by_marker_id:
                    missing.append(msg.id)
                    ops.append(
                        CreateMessageOp(
                            target=msg,
                            parent_manifest_channel_id=tc.id,
                            parent_discord_id=actual_ch.discord_id,
                            reason="missing marker in channel",
                        )
                    )
                else:
                    am = actual_msg_by_marker_id[msg.id]
                    mismatch = _diff_embed(msg, am)
                    if mismatch is not None:
                        mismatched_embeds.append(mismatch)

    # Threads
    for thread in manifest.threads:
        actual_t = actual_threads.get(thread.name)
        if actual_t is None or not _thread_has_marker(actual_t, thread, actual):
            missing.append(thread.id)
            ops.append(CreateThreadOp(target=thread, reason="thread missing or no marker"))
        else:
            matched_actual_ids.add(actual_t.discord_id)

    # Forum channels and posts
    for fc in manifest.forum_channels:
        actual_fc = actual_forum_channels.get(fc.name)
        if actual_fc is None:
            missing.append(fc.id)
            ops.append(CreateForumChannelOp(target=fc, reason="missing from guild"))
            for post in fc.posts:
                ops.append(
                    CreateForumPostOp(
                        target=post,
                        parent_manifest_forum_id=fc.id,
                        reason="parent forum missing",
                    )
                )
        else:
            matched_actual_ids.add(actual_fc.discord_id)
            for post in fc.posts:
                actual_post = next(
                    (
                        c
                        for c in actual.channels
                        if c.parent_id == actual_fc.discord_id
                        and c.name == post.name
                        and _post_has_marker(c, post, actual)
                    ),
                    None,
                )
                if actual_post is None:
                    missing.append(post.id)
                    ops.append(
                        CreateForumPostOp(
                            target=post,
                            parent_manifest_forum_id=fc.id,
                            parent_discord_id=actual_fc.discord_id,
                            reason="post missing or no marker",
                        )
                    )
                else:
                    matched_actual_ids.add(actual_post.discord_id)

    # Extras
    for ch in actual.channels:
        if ch.discord_id in matched_actual_ids:
            continue
        is_marker = (ch.topic and ch.topic.startswith(marker)) or _channel_has_first_message_marker(
            ch, actual
        )
        if is_marker:
            extra_marker.append(ch)
        elif ch.type != 11:
            extra_foreign.append(ch)

    return Diff(
        ops=tuple(ops),
        missing_entities=tuple(missing),
        extra_marker_entities=tuple(extra_marker),
        extra_foreign_entities=tuple(extra_foreign),
        mismatched_embeds=tuple(mismatched_embeds),
    )


_MARKER_PATTERN = re.compile(r"\[ferry:([a-zA-Z0-9_-]+)\]")


def _extract_marker_ids(content: str) -> list[str]:
    """Pull out all `[ferry:msg-id-XXX]` manifest IDs from message content."""
    return _MARKER_PATTERN.findall(content)


def _diff_embed(manifest_msg: ManifestMessage, actual_msg: ActualMessage) -> EmbedMismatch | None:
    if manifest_msg.embed is None and actual_msg.embed is None:
        return None
    if manifest_msg.embed is None or actual_msg.embed is None:
        return EmbedMismatch(
            manifest_message_id=manifest_msg.id,
            reason="embed presence differs between manifest and actual",
        )
    me, ae = manifest_msg.embed, actual_msg.embed
    if me.title != ae.title:
        return EmbedMismatch(manifest_msg.id, f"title differs: {me.title!r} vs {ae.title!r}")
    if me.description != ae.description:
        return EmbedMismatch(manifest_msg.id, "description differs")
    if me.color != ae.color:
        return EmbedMismatch(manifest_msg.id, f"color differs: {me.color} vs {ae.color}")
    if len(me.fields) != len(ae.fields):
        return EmbedMismatch(
            manifest_msg.id,
            f"field count differs: expected {len(me.fields)}, got {len(ae.fields)}",
        )
    for i, (mf, af) in enumerate(zip(me.fields, ae.fields, strict=False)):
        if mf.name != af.name:
            return EmbedMismatch(
                manifest_msg.id, f"field {i} name differs: {mf.name!r} vs {af.name!r}"
            )
        if mf.value != af.value:
            return EmbedMismatch(manifest_msg.id, f"field {i} value differs")
        if mf.inline != af.inline:
            return EmbedMismatch(
                manifest_msg.id,
                f"field {i} inline differs: expected {mf.inline}, got {af.inline}",
            )
    return None


def _thread_has_marker(
    actual_t: ActualChannel, thread: ManifestThread, actual: ActualState
) -> bool:
    msgs = actual.messages_by_channel.get(actual_t.discord_id, ())
    if not msgs:
        return False
    return f"[ferry:{thread.id}]" in msgs[0].content


def _post_has_marker(
    actual_post: ActualChannel, post: ManifestForumPost, actual: ActualState
) -> bool:
    msgs = actual.messages_by_channel.get(actual_post.discord_id, ())
    if not msgs:
        return False
    return f"[ferry:{post.id}]" in msgs[0].content


def _channel_has_first_message_marker(ch: ActualChannel, actual: ActualState) -> bool:
    """Used to identify orphan threads/posts created by past provisions."""
    msgs = actual.messages_by_channel.get(ch.discord_id, ())
    if not msgs:
        return False
    return "[ferry:" in msgs[0].content


# ---- Reconciler: apply_op + reconcile_provision ----


@dataclass(frozen=True)
class ProvisionResult:
    created_count: int
    created_summary: tuple[str, ...]
    skipped_count: int
    failed_op_index: int | None


class _ProvisionState:
    """Mutable scratch-state threaded through apply_op as ops complete."""

    def __init__(self, guild_id: str) -> None:
        self.guild_id = guild_id
        self.text_channel_discord_id: dict[str, str] = {}
        self.forum_channel_discord_id: dict[str, str] = {}
        self.message_discord_id: dict[str, str] = {}


async def reconcile_provision(
    d: Diff,
    api: BotApi,
    *,
    guild_id: str,
    audit_reason: str,
) -> ProvisionResult:
    """Apply create-ops in dependency order. Skips delete ops entirely.

    On op failure: stops, returns partial result with failed_op_index set.
    """
    state = _ProvisionState(guild_id=guild_id)
    created_lines: list[str] = []

    sorted_ops = sorted(d.ops, key=_op_priority)

    for idx, op in enumerate(sorted_ops):
        try:
            await apply_op(op, api, state, audit_reason=audit_reason)
            created_lines.append(_op_summary(op, state))
        except ProvisioningError:
            return ProvisionResult(
                created_count=len(created_lines),
                created_summary=tuple(created_lines),
                skipped_count=0,
                failed_op_index=idx,
            )

    return ProvisionResult(
        created_count=len(created_lines),
        created_summary=tuple(created_lines),
        skipped_count=0,
        failed_op_index=None,
    )


def _op_priority(op: DiffOpT) -> int:
    """Sort ops so parents come before children."""
    match op:
        case CreateTextChannelOp():
            return 0
        case CreateForumChannelOp():
            return 1
        case CreateMessageOp():
            return 2
        case CreateThreadOp():
            return 3
        case CreateForumPostOp():
            return 4
        case DeleteChannelOp():
            return 100
        case _ as unreachable:
            assert_never(unreachable)


def _op_summary(op: DiffOpT, state: _ProvisionState) -> str:
    match op:
        case CreateTextChannelOp(target=t):
            sid = state.text_channel_discord_id.get(t.id, "?")
            return f"created text channel #{t.name} ({sid})"
        case CreateForumChannelOp(target=t):
            sid = state.forum_channel_discord_id.get(t.id, "?")
            return f"created forum channel #{t.name} ({sid})"
        case CreateMessageOp(target=t):
            return f"created message {t.id}"
        case CreateThreadOp(target=t):
            return f"created thread {t.name}"
        case CreateForumPostOp(target=t):
            return f"created forum post {t.name}"
        case DeleteChannelOp(target=t):
            return f"deleted channel #{t.name}"
        case _ as unreachable:
            assert_never(unreachable)


async def apply_op(
    op: DiffOpT,
    api: BotApi,
    state: _ProvisionState,
    *,
    audit_reason: str,
) -> None:
    """Execute one diff operation against Discord. Updates state with new IDs."""
    match op:
        case CreateTextChannelOp(target=t):
            result = await api.create_channel(
                state.guild_id,
                name=t.name,
                channel_type=0,
                topic=f"[ferry-fixture] {t.topic_suffix}",
                audit_reason=audit_reason,
            )
            state.text_channel_discord_id[t.id] = str(result["id"])
        case CreateForumChannelOp(target=t):
            result = await api.create_channel(
                state.guild_id,
                name=t.name,
                channel_type=15,
                topic=f"[ferry-fixture] {t.topic_suffix}",
                audit_reason=audit_reason,
            )
            state.forum_channel_discord_id[t.id] = str(result["id"])
        case CreateMessageOp(target=t, parent_manifest_channel_id=pmc, parent_discord_id=p):
            parent_id = p if p is not None else state.text_channel_discord_id[pmc]
            embed_dict = None
            if t.embed is not None:
                embed_dict = {
                    "title": t.embed.title,
                    "description": t.embed.description,
                    "color": t.embed.color,
                    "fields": [
                        {"name": f.name, "value": f.value, "inline": f.inline}
                        for f in t.embed.fields
                    ],
                }
            result = await api.send_message(
                parent_id,
                content=f"{t.content} [ferry:{t.id}]",
                embed=embed_dict,
                audit_reason=audit_reason,
            )
            state.message_discord_id[t.id] = str(result["id"])
        case CreateThreadOp(target=t, parent_discord_id=p, anchor_message_discord_id=a):
            parent_id = p if p is not None else state.text_channel_discord_id[t.parent_channel_id]
            anchor_id = a if a is not None else state.message_discord_id[t.anchor_message_id]
            thread_result = await api.create_thread_from_message(
                parent_id, anchor_id, name=t.name, audit_reason=audit_reason
            )
            await api.send_message(
                str(thread_result["id"]),
                content=f"{t.first_message_content} [ferry:{t.id}]",
                embed=None,
                audit_reason=audit_reason,
            )
        case CreateForumPostOp(target=t, parent_manifest_forum_id=pmf, parent_discord_id=p):
            parent_id = p if p is not None else state.forum_channel_discord_id[pmf]
            await api.create_forum_post(
                parent_id,
                name=t.name,
                first_message_content=f"{t.first_message_content} [ferry:{t.id}]",
                audit_reason=audit_reason,
            )
        case DeleteChannelOp(target=t):
            await api.delete_channel(t.discord_id, audit_reason=audit_reason)
        case _ as unreachable:
            assert_never(unreachable)
