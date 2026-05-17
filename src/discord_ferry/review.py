"""Pre-creation review summary for blocking confirmation before migration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from discord_ferry.discord.metadata import DiscordMetadata
    from discord_ferry.parser.models import DCEExport
    from discord_ferry.state import MigrationState


@dataclass
class ReviewSummary:
    """Summary of what will be created during migration."""

    server_name: str
    role_count: int
    category_count: int
    channel_count: int
    emoji_count: int
    message_count: int
    thread_count: int
    has_permissions: bool
    nsfw_channel_count: int
    threads_filtered: int = 0
    user_override_count: int = 0
    warnings: list[str] = field(default_factory=list)


def build_review_summary(
    exports: list[DCEExport],
    discord_metadata: DiscordMetadata | None = None,
) -> ReviewSummary:
    """Build a review summary from parsed exports and optional Discord metadata.

    Args:
        exports: Parsed DCE exports.
        discord_metadata: Optional Discord metadata (for permission/NSFW info).

    Returns:
        ReviewSummary with counts and metadata availability info.
    """
    if not exports:
        return ReviewSummary(
            server_name="(empty)",
            role_count=0,
            category_count=0,
            channel_count=0,
            emoji_count=0,
            message_count=0,
            thread_count=0,
            has_permissions=False,
            nsfw_channel_count=0,
            warnings=["No exports found"],
        )

    server_name = exports[0].guild.name

    # Count unique roles, categories, channels
    role_ids: set[str] = set()
    category_ids: set[str] = set()
    channel_ids: set[str] = set()
    emoji_ids: set[str] = set()
    thread_count = 0
    total_messages = 0

    for export in exports:
        if export.channel.type != 4 and export.channel.id not in channel_ids:  # Skip categories
            channel_ids.add(export.channel.id)
        if export.channel.category_id:
            category_ids.add(export.channel.category_id)
        if export.is_thread:
            thread_count += 1
        total_messages += export.message_count
        for msg in export.messages:
            for role in msg.author.roles:
                role_ids.add(role.id)
            for reaction in msg.reactions:
                if reaction.emoji.id:
                    emoji_ids.add(reaction.emoji.id)

    # NSFW info and user override count from metadata
    nsfw_count = 0
    user_override_count = 0
    has_permissions = discord_metadata is not None
    if discord_metadata:
        for ch_meta in discord_metadata.channel_metadata.values():
            if ch_meta.nsfw:
                nsfw_count += 1
        user_override_count = len(discord_metadata.user_override_channels)

    # Build warnings
    warnings: list[str] = []
    if not has_permissions:
        warnings.append("No Discord token — permissions will not be migrated")
    if len(channel_ids) > 200:
        warnings.append(f"Channel count ({len(channel_ids)}) exceeds Stoat limit of 200")
    if len(emoji_ids) > 100:
        warnings.append(f"Emoji count ({len(emoji_ids)}) exceeds Stoat limit of 100")

    # Filter out @everyone role (id == guild_id)
    guild_id = exports[0].guild.id
    role_ids.discard(guild_id)

    return ReviewSummary(
        server_name=server_name,
        role_count=len(role_ids),
        category_count=len(category_ids),
        channel_count=len(channel_ids),
        emoji_count=len(emoji_ids),
        message_count=total_messages,
        thread_count=thread_count,
        has_permissions=has_permissions,
        nsfw_channel_count=nsfw_count,
        user_override_count=user_override_count,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Rollback summary (issue #10)
# ---------------------------------------------------------------------------


_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CROCKFORD_DECODE: dict[str, int] = {}
for _i, _c in enumerate(_CROCKFORD_ALPHABET):
    _CROCKFORD_DECODE[_c] = _i
    _CROCKFORD_DECODE[_c.lower()] = _i
# Crockford ambiguous-char aliases: I/L → 1, O → 0.
for _ambig, _val in (("I", 1), ("L", 1), ("O", 0)):
    _CROCKFORD_DECODE[_ambig] = _val
    _CROCKFORD_DECODE[_ambig.lower()] = _val


def _crockford_base32_decode(s: str) -> int:
    """Decode a Crockford base32 string to an int.

    Raises ValueError on any character outside the Crockford alphabet
    (the canonical 0-9 A-Z minus I, L, O, U, plus the I/L/O aliases).
    """
    result = 0
    for ch in s:
        if ch not in _CROCKFORD_DECODE:
            raise ValueError(f"Invalid Crockford base32 character: {ch!r}")
        result = result * 32 + _CROCKFORD_DECODE[ch]
    return result


def _decode_ulid_timestamp(stoat_id: str) -> str | None:
    """Decode the timestamp portion of a Stoat ULID to an ISO 8601 UTC string.

    Returns None on any failure — wrong length (not 26 chars), invalid
    Crockford chars, or platform timestamp overflow. The display layer
    (CLI prompt, GUI dialog) is responsible for rendering None as "unknown"
    when surfacing untracked-Ferry-suspect channels.
    """
    if len(stoat_id) != 26:
        return None
    try:
        timestamp_ms = _crockford_base32_decode(stoat_id[:10])
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


@dataclass
class UntrackedSuspectChannel:
    """A channel present on the Stoat server but absent from ``state.channel_map``.

    Either Ferry-created in a crashed prior migration that lost the ``save_state``
    write, or user-created post-migration. The confirmation gate surfaces these
    for per-item opt-in; auto-delete is forbidden (would violate the Tiger:
    "don't delete entities a different user created").
    """

    stoat_id: str
    name: str
    created_at_iso: str | None  # decoded from the ULID timestamp portion; None if not ULID-shaped
    opted_in: bool = False


@dataclass
class RollbackSummary:
    """Summary of what will be deleted during rollback — payload of the confirm gate."""

    stoat_server_id: str
    stoat_server_name: str
    channels_to_delete: list[tuple[str, str]]  # (stoat_id, name)
    untracked_ferry_suspect: list[UntrackedSuspectChannel]  # user opts in per item
    roles_to_delete: list[tuple[str, str]]
    emoji_to_delete: list[tuple[str, str]]
    categories_to_clean: int
    autumn_orphan_count: int  # informational — no public DELETE for Autumn
    has_failures_from_prior_run: bool


def _channel_names_from_server(server_obj: dict[str, Any]) -> dict[str, str]:
    """Extract a {channel_id: name} mapping from a Stoat server response.

    Stoat returns ``channels`` as a list of IDs (strings) — names aren't in
    the response. Returns ``{}`` for that shape; the display layer falls back
    to the channel ID when name is missing. If a future Stoat version returns
    channel objects, the dict comprehension covers that shape too.
    """
    channels = server_obj.get("channels", [])
    if not channels:
        return {}
    if isinstance(channels[0], dict):
        out: dict[str, str] = {}
        for entry in channels:
            cid = entry.get("_id") or entry.get("id")
            if cid:
                out[cid] = entry.get("name", "")
        return out
    return {}


def _role_names_from_server(server_obj: dict[str, Any]) -> dict[str, str]:
    """Extract a {role_id: name} mapping from a Stoat server response.

    Stoat returns ``roles`` as a dict {role_id: {name, ...}}.
    """
    roles = server_obj.get("roles", {})
    if not isinstance(roles, dict):
        return {}
    return {rid: r.get("name", "") for rid, r in roles.items() if isinstance(r, dict)}


def build_rollback_summary(
    state: MigrationState,
    server_obj: dict[str, Any],
    untracked_ferry_suspect: list[UntrackedSuspectChannel],
) -> RollbackSummary:
    """Build a RollbackSummary from current state + fresh server fetch.

    The ``untracked_ferry_suspect`` list is built by the engine
    (in ``_build_rollback_targets``) because resolving channel names may
    require additional API calls; passing it pre-built keeps this builder pure.

    Args:
        state: Current MigrationState (after any prior rollback's progress is loaded).
        server_obj: Result of ``api_fetch_server`` — used to skip already-deleted
            entities (those whose ID is no longer in ``server["channels"]``).
        untracked_ferry_suspect: Pre-built suspect list from the engine.

    Returns:
        RollbackSummary payload for the ``confirm_rollback`` event.
    """
    channel_names = _channel_names_from_server(server_obj)
    role_names = _role_names_from_server(server_obj)
    server_channel_ids: set[str] = set()
    raw_channels = server_obj.get("channels", [])
    for c in raw_channels:
        if isinstance(c, str):
            server_channel_ids.add(c)
        elif isinstance(c, dict):
            cid = c.get("_id") or c.get("id")
            if cid:
                server_channel_ids.add(cid)
    server_role_ids = set(server_obj.get("roles", {}).keys())

    already_done = (
        state.rollback_progress.rolled_back_ids if state.rollback_progress is not None else set()
    )

    channels_to_delete: list[tuple[str, str]] = []
    for stoat_id in state.channel_map.values():
        if stoat_id in already_done:
            continue
        if stoat_id not in server_channel_ids:
            # Already gone — engine pre-populates rolled_back_ids for these.
            continue
        channels_to_delete.append((stoat_id, channel_names.get(stoat_id, "")))

    roles_to_delete: list[tuple[str, str]] = []
    for stoat_id in state.role_map.values():
        if stoat_id in already_done:
            continue
        if stoat_id not in server_role_ids:
            continue
        roles_to_delete.append((stoat_id, role_names.get(stoat_id, "")))

    emoji_to_delete: list[tuple[str, str]] = []
    for stoat_id in state.emoji_map.values():
        if stoat_id in already_done:
            continue
        # Server response doesn't include per-emoji names; leave empty.
        emoji_to_delete.append((stoat_id, ""))

    autumn_orphan_count = len(set(state.autumn_uploads.keys()) - state.referenced_autumn_ids)

    has_failures = bool(state.rollback_progress and state.rollback_progress.failures)

    return RollbackSummary(
        stoat_server_id=server_obj.get("_id", state.stoat_server_id),
        stoat_server_name=server_obj.get("name", ""),
        channels_to_delete=channels_to_delete,
        untracked_ferry_suspect=untracked_ferry_suspect,
        roles_to_delete=roles_to_delete,
        emoji_to_delete=emoji_to_delete,
        categories_to_clean=len(state.category_map),
        autumn_orphan_count=autumn_orphan_count,
        has_failures_from_prior_run=has_failures,
    )
