"""Migration state management, ID mapping, and persistence."""

import dataclasses
import json
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from discord_ferry.core.security import scrub_document
from discord_ferry.errors import StateError


@dataclass
class FailedMessage:
    """A message that failed to send during migration."""

    discord_msg_id: str
    stoat_channel_id: str
    error: str
    retry_count: int = 0
    content_preview: str = ""


@dataclass
class RollbackFailure:
    """A single entity that failed to delete during rollback."""

    entity_type: str  # "channel" | "role" | "emoji" | "category"
    stoat_id: str
    error: str
    http_status: int | None = None


@dataclass
class RollbackProgress:
    """Progress + DLQ for an in-flight or completed rollback.

    ``rolled_back_ids`` is the source of truth for what's already been deleted
    (or already-404'd). ``channel_map`` / ``role_map`` / ``emoji_map`` are NEVER
    mutated by rollback — they preserve the migration's audit trail.
    """

    channels_deleted: int = 0
    roles_deleted: int = 0
    emoji_deleted: int = 0
    categories_cleaned: bool = False
    untracked_channels_deleted: int = 0
    rolled_back_ids: set[str] = field(default_factory=set)
    failures: list[RollbackFailure] = field(default_factory=list)
    started_at: str = ""
    completed_at: str = ""


@dataclass
class MigrationState:
    """Tracks all ID mappings and progress for resume support."""

    # Discord ID -> Stoat ID mappings
    role_map: dict[str, str] = field(default_factory=dict)
    channel_map: dict[str, str] = field(default_factory=dict)
    category_map: dict[str, str] = field(default_factory=dict)
    category_names: dict[str, str] = field(default_factory=dict)  # discord_cat_id -> title
    # discord_ch_id -> effective discord_cat_id (the forum key for forum channels).
    # Persisted so incremental re-runs can re-attach carried channels to the
    # CORRECT category in the full-replace upsert, even with multiple
    # simultaneously carried-only categories (exact, not best-effort).
    channel_categories: dict[str, str] = field(default_factory=dict)
    message_map: dict[str, str] = field(default_factory=dict)
    emoji_map: dict[str, str] = field(default_factory=dict)

    # Author ID -> uploaded Autumn avatar ID
    avatar_cache: dict[str, str] = field(default_factory=dict)

    # Autumn upload cache: local_path -> autumn_file_id
    upload_cache: dict[str, str] = field(default_factory=dict)

    # Author ID -> display name (for mention remapping)
    author_names: dict[str, str] = field(default_factory=dict)

    # Pending pins: list of (stoat_channel_id, stoat_message_id)
    pending_pins: list[tuple[str, str]] = field(default_factory=list)

    # Pending reactions
    pending_reactions: list[dict[str, object]] = field(default_factory=list)

    # Error and warning logs
    errors: list[dict[str, str]] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)

    # Native-fidelity attribute application counts (slowmode, user_limit, ...)
    native_fidelity_counts: dict[str, int] = field(default_factory=dict)

    # Stoat server ID and Autumn URL (discovered during CONNECT phase)
    stoat_server_id: str = ""
    autumn_url: str = ""

    # S17 lock marker the engine wrote this run (transient — re-acquired each run, NOT persisted).
    # run_server appends it to the description PATCH so the SERVER phase doesn't wipe the lock.
    migration_lock_marker: str = ""

    # Resume tracking
    current_phase: str = ""
    completed_channel_ids: set[str] = field(default_factory=set)
    channel_message_offsets: dict[str, str] = field(default_factory=dict)
    # Durable per-channel high-water mark (channel id -> highest copied message id).
    # Unlike channel_message_offsets (transient within-run resume state, popped on
    # completion), this survives completion and drives --incremental skipping.
    channel_high_water: dict[str, str] = field(default_factory=dict)

    # Counters (incremented by phase implementations)
    attachments_uploaded: int = 0
    attachments_skipped: int = 0
    reactions_applied: int = 0
    reactions_capped: int = 0  # Batch 4 (S4): reactions skipped at the 20/msg Stoat cap
    reactions_dropped: int = 0  # Batch 4 (S1): custom reactions whose emoji never mapped
    pins_applied: int = 0

    # Timing
    started_at: str = ""
    completed_at: str = ""

    # Dry-run flag — persisted so resume logic can reject dry-run states
    is_dry_run: bool = False

    # Export phase tracking (for smart resume)
    export_completed: bool = False

    # Orphan upload tracking: detect Autumn files uploaded but never referenced in a message
    autumn_uploads: dict[str, str] = field(default_factory=dict)  # autumn_id -> source_id
    referenced_autumn_ids: set[str] = field(default_factory=set)  # confirmed used

    # S3: roles whose attributes+permissions passes completed (a full run_roles). Gates the
    # attrs/perms passes so resume finalizes created-but-unfinished roles while --incremental
    # skips re-editing prior-completed ones.
    roles_finalized: set[str] = field(default_factory=set)

    # Dead-letter queue: messages that failed to send (typed, retryable)
    failed_messages: list[FailedMessage] = field(default_factory=list)

    # Post-migration validation results
    validation_results: dict[str, object] = field(default_factory=dict)

    # Incremental/delta migration tracking
    # prior_messages_total: message count from the prior run (set at init for delta mode)
    prior_messages_total: int = 0
    # source_messages_total: total source messages in this run's exports (post-filter),
    # set by the engine. Denominator for message fidelity in stats/reporter. 0 = unset (legacy).
    source_messages_total: int = 0

    # Forum index tracking: populated during CHANNELS phase, consumed by REPORT phase.
    # forum_channel_members: forum_cat_key -> list of discord_channel_ids in that forum
    forum_channel_members: dict[str, list[str]] = field(default_factory=dict)
    # forum_category_names: forum_cat_key -> display name of the forum
    forum_category_names: dict[str, str] = field(default_factory=dict)
    # Per-channel message counts: discord_channel_id -> messages migrated.
    # Incremented by _process_message; used by forum index rebuild in REPORT phase.
    channel_message_counts: dict[str, int] = field(default_factory=dict)
    # reaction_message_counts: Stoat message_id -> reactions applied, persisted so the
    # 20-per-message cap stays exact across a resume (reactions phase clears pending as it goes).
    reaction_message_counts: dict[str, int] = field(default_factory=dict)

    # Forum index message IDs: forum_cat_key -> stoat_message_id.
    # Populated by _rebuild_forum_indexes; used to PATCH (not re-POST) on re-runs.
    forum_index_message_ids: dict[str, str] = field(default_factory=dict)

    # Fidelity counters (S18)
    embeds_total: int = 0
    embeds_dropped: int = 0
    replies_linked: int = 0
    replies_total: int = 0

    # Rollback tracking (S8 — issue #10). ``None`` when no rollback has been
    # attempted; populated incrementally by ``run_rollback``. Forensic-preserving:
    # the entity maps above are never mutated by rollback.
    rollback_progress: RollbackProgress | None = None

    # Post-migration invite (S4). invite_code is the bare code; invite_url is the
    # best-effort full link ("" when the frontend base couldn't be discovered).
    invite_code: str = ""
    invite_url: str = ""


def save_state(state: MigrationState, output_dir: Path) -> None:
    """Save migration state to state.json using atomic write.

    The message_map is written to a separate message_map.json file to keep
    state.json small and avoid O(n) growth with message count.

    Args:
        state: Current migration state.
        output_dir: Directory to write state.json into. Created if missing.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    data = _state_to_dict(state)

    # Extract message_map and write it to its own file atomically.
    message_map = data.pop("message_map", {})
    mm_tmp = output_dir / "message_map.json.tmp"
    mm_final = output_dir / "message_map.json"
    mm_tmp.write_text(json.dumps(message_map, indent=2), encoding="utf-8")
    # replace, not rename: os.rename refuses an existing destination on Windows and
    # replaces it everywhere else. save_state runs at every phase boundary and every
    # checkpoint_interval messages, so the SECOND save of any run died here, not only
    # a second run into an existing directory (#172).
    mm_tmp.replace(mm_final)

    tmp_path = output_dir / "state.json.tmp"
    final_path = output_dir / "state.json"
    # Redact free-text fields on the way out, never in place: state.warnings must keep
    # its raw text in memory. save_state takes no config, so this relies on the
    # process-wide registry, which is where the proxy secrets live.
    #
    # message_map was popped above and is deliberately NOT scrubbed. It holds identifier
    # pairs and no free text, and walking it measured ~290ms at 100k entries against
    # ~3ms for everything else, on a path that runs every 50 messages. Issue #140, ADR-014.
    tmp_path.write_text(json.dumps(scrub_document(data), indent=2), encoding="utf-8")
    tmp_path.replace(final_path)


def load_state(output_dir: Path) -> MigrationState:
    """Load migration state from state.json.

    Handles v1→v2 migration (last_completed_channel/message → completed_channel_ids)
    and loads message_map from message_map.json if present (v2+), falling back to
    embedded message_map in state.json (v1 compat).

    Args:
        output_dir: Directory containing state.json.

    Raises:
        StateError: If the file doesn't exist or contains invalid JSON.
    """
    state_path = output_dir / "state.json"
    if not state_path.exists():
        raise StateError(f"State file not found: {state_path}")
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise StateError(f"Corrupt state file: {e}") from e

    # v1→v2 migration: convert snowflake resume fields to completed_channel_ids.
    if "last_completed_channel" in raw and "completed_channel_ids" not in raw:
        raw = _migrate_v1_to_v2(raw, output_dir)

    # Load message_map: prefer separate file (v2), fall back to embedded (v1 compat).
    mm_path = output_dir / "message_map.json"
    if mm_path.exists():
        try:
            raw["message_map"] = json.loads(mm_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise StateError(f"Corrupt message_map.json: {e}") from e
    # If "message_map" is already in raw (v1 embedded), it will be used as-is.

    return _dict_to_state(raw)


def _state_to_dict(state: MigrationState) -> dict[str, Any]:
    rp_data: dict[str, Any] | None
    if state.rollback_progress is not None:
        rp_data = dataclasses.asdict(state.rollback_progress)
        # set → list for JSON.
        rp_data["rolled_back_ids"] = sorted(state.rollback_progress.rolled_back_ids)
    else:
        rp_data = None

    return {
        "role_map": state.role_map,
        "channel_map": state.channel_map,
        "category_map": state.category_map,
        "category_names": state.category_names,
        "channel_categories": state.channel_categories,
        "message_map": state.message_map,
        "emoji_map": state.emoji_map,
        "avatar_cache": state.avatar_cache,
        "upload_cache": state.upload_cache,
        "author_names": state.author_names,
        "pending_pins": [list(p) for p in state.pending_pins],
        "pending_reactions": state.pending_reactions,
        "errors": state.errors,
        "warnings": state.warnings,
        "stoat_server_id": state.stoat_server_id,
        "autumn_url": state.autumn_url,
        "current_phase": state.current_phase,
        "completed_channel_ids": list(state.completed_channel_ids),
        "channel_message_offsets": state.channel_message_offsets,
        "channel_high_water": state.channel_high_water,
        "attachments_uploaded": state.attachments_uploaded,
        "attachments_skipped": state.attachments_skipped,
        "reactions_applied": state.reactions_applied,
        "reactions_capped": state.reactions_capped,
        "reactions_dropped": state.reactions_dropped,
        "pins_applied": state.pins_applied,
        "started_at": state.started_at,
        "completed_at": state.completed_at,
        "is_dry_run": state.is_dry_run,
        "export_completed": state.export_completed,
        "autumn_uploads": state.autumn_uploads,
        "referenced_autumn_ids": list(state.referenced_autumn_ids),
        "roles_finalized": list(state.roles_finalized),
        "failed_messages": [dataclasses.asdict(fm) for fm in state.failed_messages],
        "validation_results": state.validation_results,
        "prior_messages_total": state.prior_messages_total,
        "source_messages_total": state.source_messages_total,
        "forum_channel_members": state.forum_channel_members,
        "forum_category_names": state.forum_category_names,
        "channel_message_counts": state.channel_message_counts,
        "reaction_message_counts": state.reaction_message_counts,
        "forum_index_message_ids": state.forum_index_message_ids,
        "embeds_total": state.embeds_total,
        "embeds_dropped": state.embeds_dropped,
        "replies_linked": state.replies_linked,
        "replies_total": state.replies_total,
        "rollback_progress": rp_data,
        "invite_code": state.invite_code,
        "invite_url": state.invite_url,
        "native_fidelity_counts": state.native_fidelity_counts,
    }


def _dict_to_state(data: dict[str, Any]) -> MigrationState:
    rp_data = data.get("rollback_progress")
    rollback_progress: RollbackProgress | None
    if rp_data is None:
        rollback_progress = None
    else:
        failures = [RollbackFailure(**f) for f in rp_data.get("failures", [])]
        rollback_progress = RollbackProgress(
            channels_deleted=rp_data.get("channels_deleted", 0),
            roles_deleted=rp_data.get("roles_deleted", 0),
            emoji_deleted=rp_data.get("emoji_deleted", 0),
            categories_cleaned=rp_data.get("categories_cleaned", False),
            untracked_channels_deleted=rp_data.get("untracked_channels_deleted", 0),
            rolled_back_ids=set(rp_data.get("rolled_back_ids", [])),
            failures=failures,
            started_at=rp_data.get("started_at", ""),
            completed_at=rp_data.get("completed_at", ""),
        )

    try:
        state = MigrationState(
            role_map=data.get("role_map", {}),
            channel_map=data.get("channel_map", {}),
            category_map=data.get("category_map", {}),
            category_names=data.get("category_names", {}),
            channel_categories=data.get("channel_categories", {}),
            message_map=data.get("message_map", {}),
            emoji_map=data.get("emoji_map", {}),
            avatar_cache=data.get("avatar_cache", {}),
            upload_cache=data.get("upload_cache", {}),
            author_names=data.get("author_names", {}),
            pending_pins=[(p[0], p[1]) for p in data.get("pending_pins", []) if len(p) == 2],
            pending_reactions=data.get("pending_reactions", []),
            errors=data.get("errors", []),
            warnings=data.get("warnings", []),
            stoat_server_id=data.get("stoat_server_id", ""),
            autumn_url=data.get("autumn_url", ""),
            current_phase=data.get("current_phase", ""),
            completed_channel_ids=set(data.get("completed_channel_ids", [])),
            channel_message_offsets=data.get("channel_message_offsets", {}),
            channel_high_water=data.get("channel_high_water", {}),
            attachments_uploaded=data.get("attachments_uploaded", 0),
            attachments_skipped=data.get("attachments_skipped", 0),
            reactions_applied=data.get("reactions_applied", 0),
            reactions_capped=data.get("reactions_capped", 0),
            reactions_dropped=data.get("reactions_dropped", 0),
            pins_applied=data.get("pins_applied", 0),
            started_at=data.get("started_at", ""),
            completed_at=data.get("completed_at", ""),
            is_dry_run=data.get("is_dry_run", False),
            export_completed=data.get("export_completed", False),
            autumn_uploads=data.get("autumn_uploads", {}),
            referenced_autumn_ids=set(data.get("referenced_autumn_ids", [])),
            roles_finalized=set(data.get("roles_finalized", [])),
            failed_messages=[FailedMessage(**d) for d in data.get("failed_messages", [])],
            validation_results=data.get("validation_results", {}),
            prior_messages_total=data.get("prior_messages_total", 0),
            source_messages_total=data.get("source_messages_total", 0),
            forum_channel_members=data.get("forum_channel_members", {}),
            forum_category_names=data.get("forum_category_names", {}),
            channel_message_counts=data.get("channel_message_counts", {}),
            reaction_message_counts=data.get("reaction_message_counts", {}),
            forum_index_message_ids=data.get("forum_index_message_ids", {}),
            embeds_total=data.get("embeds_total", 0),
            embeds_dropped=data.get("embeds_dropped", 0),
            replies_linked=data.get("replies_linked", 0),
            replies_total=data.get("replies_total", 0),
            rollback_progress=rollback_progress,
            invite_code=data.get("invite_code", ""),
            invite_url=data.get("invite_url", ""),
            native_fidelity_counts=data.get("native_fidelity_counts", {}),
        )
        # S3 back-compat seed: an older completed migration has no roles_finalized; treat all
        # mapped roles as finalized so its first --incremental run does not re-edit them. A
        # crashed migration has no completed_at -> not seeded -> resume finalizes the remainder.
        if state.completed_at and state.role_map and not state.roles_finalized:
            state.roles_finalized = set(state.role_map)
        return state
    except (TypeError, ValueError) as e:
        raise StateError(f"Invalid state data: {e}") from e


def _migrate_v1_to_v2(data: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    """Migrate v1 resume fields to v2 completed_channel_ids format.

    v1 used last_completed_channel (a single snowflake string) and
    last_completed_message (a single snowflake string) to track resume position.
    v2 uses completed_channel_ids (a set of all fully-done channel IDs) and
    channel_message_offsets (a per-channel dict for within-channel resume).

    NOTE: Channel ordering uses snowflake comparison, which is best-effort for
    bot-created channels (snowflakes are time-ordered, so lower ID ≈ created earlier).

    Args:
        data: Raw state dict from state.json (v1 format).
        output_dir: Directory containing state.json (used for backup).

    Returns:
        Modified dict with v2 resume fields and v1 fields removed.
    """
    # Back up the original state.json before migrating.
    backup_path = output_dir / "state.json.v1.bak"
    backup_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    lcc: str = data.get("last_completed_channel", "")
    lcm: str = data.get("last_completed_message", "")
    channel_map: dict[str, Any] = data.get("channel_map", {})

    completed_channel_ids: list[str] = []
    channel_message_offsets: dict[str, str] = {}

    if lcc:
        # All channels with snowflake ID strictly less than lcc are fully done.
        for ch_id in channel_map:
            try:
                if int(ch_id) < int(lcc):
                    completed_channel_ids.append(ch_id)
            except ValueError:
                # Non-numeric channel ID — skip comparison.
                pass
        # Within the resume channel, record the last processed message.
        if lcm:
            channel_message_offsets[lcc] = lcm

    warnings.warn(
        f"Discord Ferry state v1→v2 migration: converted last_completed_channel={lcc!r} "
        f"to completed_channel_ids ({len(completed_channel_ids)} channels). "
        f"Backup saved to {backup_path}.",
        UserWarning,
        stacklevel=4,
    )

    # Build v2 dict: remove v1 fields, add v2 fields.
    migrated = dict(data)
    migrated.pop("last_completed_channel", None)
    migrated.pop("last_completed_message", None)
    migrated["completed_channel_ids"] = completed_channel_ids
    migrated["channel_message_offsets"] = channel_message_offsets
    return migrated
