"""Migration state management, ID mapping, and persistence."""

import dataclasses
import hashlib
import json
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit
from uuid import uuid4

from discord_ferry.core.atomicio import atomic_write_text, ensure_output_subdir
from discord_ferry.core.permissions import PermissionRuntime
from discord_ferry.core.security import scrub_document
from discord_ferry.errors import StateError

DeliveryFamily = Literal["ordinary", "merged", "header", "separator", "forum_index"]
PartStatus = Literal["prepared", "uncertain", "acknowledged", "confirmed_rejected"]


@dataclass(frozen=True)
class DeliveryRef:
    """Structural identity of a selected delivery, not a filesystem path."""

    source_channel_id: str
    source_message_id: str
    destination_channel_id: str
    family: DeliveryFamily
    generation: str = "base"


@dataclass
class DeliveryPart:
    """One part's saved attempt and original acknowledged media references."""

    index: int
    key: str
    attempt: int = 0
    status: PartStatus = "prepared"
    message_id: str = ""
    autumn_ids: list[str] = field(default_factory=list)


@dataclass
class DeliveryReceipt:
    """Delivery evidence only. Normal migration owners still commit progress."""

    version: int
    namespace: str
    service_digest: str
    ref: DeliveryRef
    render_digest: str
    parts: list[DeliveryPart]


@dataclass(frozen=True)
class DeliveryToken:
    """Identifies the exact receipt revision a checkpoint may consume."""

    receipt_id: str
    receipt_digest: str


@dataclass
class FailedMessage:
    """A message that failed to send during migration."""

    discord_msg_id: str
    stoat_channel_id: str
    error: str
    retry_count: int = 0
    content_preview: str = ""
    delivery_ref: DeliveryRef | None = None


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

    # Structural identity only; lifecycle and checkpoint handoff are separate.
    delivery_namespace: str = ""
    delivery_service_digest: str = ""
    delivery_bookkeeping: dict[str, str] = field(default_factory=dict)
    pending_delivery_replacements: dict[str, str] = field(default_factory=dict)
    # Caller-issued cleanup capabilities. Never restored from JSON.
    delivery_ready: dict[str, str] = field(default_factory=dict, repr=False, compare=False)
    permission_runtime: PermissionRuntime | None = field(default=None, repr=False, compare=False)

    # Discord ID -> Stoat ID mappings
    role_map: dict[str, str] = field(default_factory=dict)
    channel_map: dict[str, str] = field(default_factory=dict)
    category_map: dict[str, str] = field(default_factory=dict)
    category_names: dict[str, str] = field(default_factory=dict)  # discord_cat_id -> title
    # The names Ferry SENT to Stoat, keyed exactly as the id maps above.
    #
    # NOT the Discord name, and the difference is not cosmetic.
    # make_unique_channel_name truncates to 32 characters and appends "-1" on a
    # collision; roles pass through truncate_name. Recording the Discord name
    # would report a rename for every channel whose name exceeds 32 characters
    # and every collision-suffixed duplicate, against a server nobody edited.
    #
    # The value is taken from the create RESPONSE, so it is what the server
    # stored rather than what Ferry believes it sent.
    #
    # Contrast category_names directly above, which records the DISCORD title.
    # Same suffix, opposite meaning, which is why these are not called
    # channel_names.
    created_channel_names: dict[str, str] = field(default_factory=dict)
    created_role_names: dict[str, str] = field(default_factory=dict)
    # discord_emoji_id -> the name the server stored (echoed from the create
    # response, falling back to the sanitized input name). Structural, not
    # free text: an emoji name matches ^[a-z0-9_]+$ and never carries a Ferry
    # secret, matching created_channel_names / created_role_names.
    created_emoji_names: dict[str, str] = field(default_factory=dict)
    # discord_ch_id -> effective discord_cat_id (the forum key for forum channels).
    # Persisted so incremental re-runs can re-attach carried channels to the
    # CORRECT category in the full-replace upsert, even with multiple
    # simultaneously carried-only categories (exact, not best-effort).
    channel_categories: dict[str, str] = field(default_factory=dict)
    message_map: dict[str, str] = field(default_factory=dict)
    emoji_map: dict[str, str] = field(default_factory=dict)

    # discord_emoji_id -> {"old": <old stoat id>, "new": <new stoat id>}.
    # A repair resume record: written the moment a missing emoji is recreated,
    # cleared once every referencing message has been rewritten. Identifiers
    # only, never free text, so it is structural and NOT in _TEXT_MEMBERS.
    pending_emoji_rewrites: dict[str, dict[str, str]] = field(default_factory=dict)

    # Author ID -> uploaded Autumn avatar ID
    avatar_cache: dict[str, str] = field(default_factory=dict)

    # Autumn upload cache: "<tag>:<local_path>" -> autumn_file_id (see upload_cache_key).
    # Entries from before #984 have a bare-path key; they are never matched, so a
    # resume re-uploads those files once.
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
    merge_duplicates_suppressed: int = 0  # #239: merge messages skipped (already in parent)

    # Timing
    started_at: str = ""
    completed_at: str = ""

    # Dry-run flag — persisted so resume logic can reject dry-run states
    is_dry_run: bool = False

    # The thread strategy of the MOST RECENT run. "" means never recorded, which
    # is what every migration predating this field reports.
    #
    # "" and "flatten" are different claims: unknown, against chosen. A default
    # of "flatten" would assert a strategy nobody selected, and ferry check
    # would then name a cause it cannot know.
    thread_strategy: str = ""

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
    # Populated by run_channels (the create send, #560) and _rebuild_forum_indexes; used to
    # PATCH (not re-POST) in the REPORT rebuild and on re-runs.
    forum_index_message_ids: dict[str, str] = field(default_factory=dict)
    # Forum keys whose index message exists on the server but whose id was lost to a
    # DuplicateSendError (#215). Distinct from a known id (edit) and absence (send).
    # Holds internal keys only, no free text.
    forum_index_present_unknown_id: set[str] = field(default_factory=set)

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


def _delivery_string(value: object, pattern: str) -> str:
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise StateError("Invalid delivery evidence")
    return value


def _delivery_record(value: object, members: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != members:
        raise StateError("Invalid delivery evidence members")
    return cast("dict[str, Any]", value)


def _decode_delivery_ref(value: object) -> DeliveryRef:
    raw = _delivery_record(
        value,
        {
            "source_channel_id",
            "source_message_id",
            "destination_channel_id",
            "family",
            "generation",
        },
    )
    source_channel = _delivery_string(raw["source_channel_id"], r"[A-Za-z0-9_-]{1,128}")
    source_message = _delivery_string(raw["source_message_id"], r"[A-Za-z0-9_-]{1,128}")
    destination = raw["destination_channel_id"]
    if not is_stoat_id(destination):
        raise StateError("Invalid delivery destination")
    family = _delivery_string(raw["family"], r"ordinary|merged|header|separator|forum_index")
    generation = _delivery_string(raw["generation"], r"base|[0-9a-f]{32}")
    return DeliveryRef(
        source_channel, source_message, destination, cast("DeliveryFamily", family), generation
    )


def _validate_delivery_binding(state: MigrationState, *, required: bool = False) -> None:
    if (
        not required
        and not state.delivery_bookkeeping
        and not state.pending_delivery_replacements
        and not state.delivery_ready
        and state.delivery_namespace == state.delivery_service_digest == ""
    ):
        return
    _delivery_string(state.delivery_namespace, r"[0-9a-f]{32}")
    _delivery_string(state.delivery_service_digest, r"[0-9a-f]{64}")


def _delivery_map(value: object, *, messages: bool = False) -> dict[str, str]:
    if not isinstance(value, dict):
        raise StateError("Invalid delivery checkpoint markers")
    result: dict[str, str] = {}
    for key, item in value.items():
        key = _delivery_string(key, r"[0-9a-f]{64}")
        if messages:
            if not is_stoat_id(item):
                raise StateError("Invalid delivery bookkeeping identifier")
            result[key] = item
        else:
            result[key] = _delivery_string(item, r"[0-9a-f]{32}")
    return result


def _service_digest(stoat_url: str) -> str:
    try:
        if not isinstance(stoat_url, str) or any(ord(c) < 33 or ord(c) == 127 for c in stoat_url):
            raise ValueError
        parsed = urlsplit(stoat_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or (parsed.username is not None or parsed.query or parsed.fragment)
        ):
            raise ValueError
        host = parsed.hostname.lower()
        authority = f"[{host}]" if ":" in host else host
        if parsed.port is not None and parsed.port != (443 if parsed.scheme == "https" else 80):
            authority += f":{parsed.port}"
        canonical = f"{parsed.scheme}://{authority}{parsed.path.rstrip('/')}"
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    except (ValueError, UnicodeError) as exc:
        raise StateError("Invalid delivery service binding") from exc


def ensure_delivery_namespace(state: MigrationState, output_dir: Path, stoat_url: str) -> None:
    """Bind delivery evidence once and save that identity before any dispatch."""
    if state.is_dry_run:
        return
    digest = _service_digest(stoat_url)
    _validate_delivery_binding(state)
    if state.delivery_namespace:
        if digest != state.delivery_service_digest:
            raise StateError("Delivery service does not match saved migration")
        return
    state.delivery_namespace = uuid4().hex
    state.delivery_service_digest = digest
    try:
        save_state(state, output_dir)
    except BaseException:
        state.delivery_namespace = state.delivery_service_digest = ""
        raise


def _decode_delivery_receipt(value: object) -> DeliveryReceipt:
    raw = _delivery_record(
        value,
        {
            "version",
            "namespace",
            "service_digest",
            "ref",
            "render_digest",
            "parts",
        },
    )
    if type(raw["version"]) is not int or raw["version"] != 1:
        raise StateError("Unsupported delivery receipt version")
    namespace = _delivery_string(raw["namespace"], r"[0-9a-f]{32}")
    service = _delivery_string(raw["service_digest"], r"[0-9a-f]{64}")
    render = _delivery_string(raw["render_digest"], r"[0-9a-f]{64}")
    ref = _decode_delivery_ref(raw["ref"])
    if not isinstance(raw["parts"], list) or not raw["parts"]:
        raise StateError("Invalid delivery parts")
    parts: list[DeliveryPart] = []
    keys: set[str] = set()
    for index, value in enumerate(raw["parts"]):
        part = _delivery_record(
            value,
            {
                "index",
                "key",
                "attempt",
                "status",
                "message_id",
                "autumn_ids",
            },
        )
        if type(part["index"]) is not int or part["index"] != index:
            raise StateError("Invalid delivery part position")
        if type(part["attempt"]) is not int or part["attempt"] < 0:
            raise StateError("Invalid delivery attempt")
        key = _delivery_string(part["key"], r"ferry-v2-[0-9a-f]{40}")
        if key in keys:
            raise StateError("Duplicate delivery part key")
        keys.add(key)
        status = _delivery_string(
            part["status"], r"prepared|uncertain|acknowledged|confirmed_rejected"
        )
        message_id = part["message_id"]
        if (status == "acknowledged" and not is_stoat_id(message_id)) or (
            status != "acknowledged" and message_id != ""
        ):
            raise StateError("Invalid delivery acknowledgement")
        autumn_ids = part["autumn_ids"]
        if not isinstance(autumn_ids, list) or any(
            not is_stoat_file_id(item) for item in autumn_ids
        ):
            raise StateError("Invalid delivery media references")
        parts.append(
            DeliveryPart(
                index, key, part["attempt"], cast("PartStatus", status), message_id, autumn_ids
            )
        )
    return DeliveryReceipt(1, namespace, service, ref, render, parts)


def _delivery_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def delivery_receipt_id(ref: DeliveryRef) -> str:
    """Return a generated filename stem; source identifiers never become paths."""
    validated = _decode_delivery_ref(dataclasses.asdict(ref))
    return hashlib.sha256(_delivery_json(dataclasses.asdict(validated)).encode()).hexdigest()


def _receipt_path(
    state: MigrationState, output_dir: Path, receipt_id: str, *, create: bool = False
) -> Path:
    _validate_delivery_binding(state, required=True)
    _delivery_string(receipt_id, r"[0-9a-f]{64}")
    folder = output_dir / "delivery-receipts" / state.delivery_namespace
    if create:
        ensure_output_subdir(folder, output_dir)
    else:
        for directory in (folder.parent, folder):
            if directory.is_symlink():
                raise StateError("Linked delivery receipt directory")
        if not folder.resolve().is_relative_to(output_dir.resolve()):
            raise StateError("Delivery receipt directory is outside output")
    path = folder / f"{receipt_id}.json"
    if path.is_symlink():
        raise StateError("Linked delivery receipt file")
    return path


def _delivery_path(
    state: MigrationState, output_dir: Path, ref: DeliveryRef, *, create: bool
) -> Path:
    return _receipt_path(state, output_dir, delivery_receipt_id(ref), create=create)


def write_delivery_receipt(
    state: MigrationState, output_dir: Path, receipt: DeliveryReceipt
) -> DeliveryToken:
    """Validate and safely replace one structural receipt before dispatch."""
    validated = _decode_delivery_receipt(dataclasses.asdict(receipt))
    if (validated.namespace, validated.service_digest) != (
        state.delivery_namespace,
        state.delivery_service_digest,
    ):
        raise StateError("Delivery receipt does not match migration")
    text = _delivery_json(dataclasses.asdict(validated))
    try:
        path = _delivery_path(state, output_dir, validated.ref, create=True)
        atomic_write_text(path, text)
    except OSError as exc:
        raise StateError("Could not save delivery receipt") from exc
    return DeliveryToken(path.stem, hashlib.sha256(text.encode()).hexdigest())


def read_delivery_receipt(
    state: MigrationState, output_dir: Path, ref: DeliveryRef
) -> DeliveryReceipt | None:
    """Read one bound receipt, rejecting malformed evidence rather than replaying."""
    try:
        path = _delivery_path(state, output_dir, ref, create=False)
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise StateError("Could not read delivery receipt") from exc
    try:
        receipt = _decode_delivery_receipt(json.loads(text))
    except (ValueError, RecursionError) as exc:
        raise StateError("Invalid delivery receipt JSON") from exc
    if (receipt.namespace, receipt.service_digest, receipt.ref) != (
        state.delivery_namespace,
        state.delivery_service_digest,
        ref,
    ):
        raise StateError("Delivery receipt does not match migration")
    return receipt


def _decode_failed_message(value: dict[str, Any]) -> FailedMessage:
    raw = dict(value)
    if raw.get("delivery_ref") is not None:
        raw["delivery_ref"] = _decode_delivery_ref(raw["delivery_ref"])
        if (raw["delivery_ref"].source_message_id, raw["delivery_ref"].destination_channel_id) != (
            raw.get("discord_msg_id"),
            raw.get("stoat_channel_id"),
        ):
            raise StateError("Failed message delivery reference does not match")
    return FailedMessage(**raw)


def consume_delivery(state: MigrationState, token: DeliveryToken, first_message_id: str) -> None:
    """Record normal bookkeeping and authorize retirement of this exact revision."""
    _delivery_string(token.receipt_id, r"[0-9a-f]{64}")
    _delivery_string(token.receipt_digest, r"[0-9a-f]{64}")
    if not is_stoat_id(first_message_id):
        raise StateError("Invalid consumed delivery identifier")
    state.delivery_bookkeeping[token.receipt_id] = first_message_id
    state.delivery_ready[token.receipt_id] = token.receipt_digest


@dataclass(frozen=True)
class _Retirement:
    token: DeliveryToken
    receipt: DeliveryReceipt
    path: Path


def _consumed_deliveries(state: MigrationState, output_dir: Path) -> list[_Retirement]:
    result: list[_Retirement] = []
    for receipt_id, digest in state.delivery_ready.items():
        _delivery_string(digest, r"[0-9a-f]{64}")
        path = _receipt_path(state, output_dir, receipt_id)
        try:
            contents = path.read_bytes()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise StateError("Could not inspect consumed delivery evidence") from exc
        if hashlib.sha256(contents).hexdigest() != digest:
            continue
        try:
            receipt = _decode_delivery_receipt(json.loads(contents))
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise StateError("Invalid consumed delivery evidence") from exc
        if delivery_receipt_id(receipt.ref) != receipt_id or (
            receipt.namespace,
            receipt.service_digest,
        ) != (state.delivery_namespace, state.delivery_service_digest):
            raise StateError("Consumed delivery does not match migration")
        if any(part.status != "acknowledged" for part in receipt.parts):
            continue
        if state.delivery_bookkeeping.get(receipt_id) != receipt.parts[0].message_id:
            continue
        result.append(_Retirement(DeliveryToken(receipt_id, digest), receipt, path))
    return result


def _clear_completed_replacement(replacements: dict[str, str], receipt: DeliveryReceipt) -> None:
    base = delivery_receipt_id(dataclasses.replace(receipt.ref, generation="base"))
    if replacements.get(base) == receipt.ref.generation:
        replacements.pop(base)


def _retire_consumed_deliveries(
    state: MigrationState, output_dir: Path, retirements: list[_Retirement]
) -> None:
    for retirement in retirements:
        token = retirement.token
        # This removal was already committed in state.json before cleanup began.
        _clear_completed_replacement(state.pending_delivery_replacements, retirement.receipt)
        try:
            if hashlib.sha256(retirement.path.read_bytes()).hexdigest() != token.receipt_digest:
                continue
            retirement.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            warning = {
                "phase": "messages",
                "type": "delivery_receipt_cleanup_failed",
                "message": "Delivery evidence retained after receipt cleanup failed.",
            }
            if warning not in state.warnings:
                state.warnings.append(warning)
            continue
        state.delivery_ready.pop(token.receipt_id, None)
        state.delivery_bookkeeping.pop(token.receipt_id, None)
    # A crash can leave already-retired markers in the prior successful snapshot.
    for receipt_id in list(state.delivery_bookkeeping):
        try:
            if not _receipt_path(state, output_dir, receipt_id).exists():
                state.delivery_bookkeeping.pop(receipt_id)
                state.delivery_ready.pop(receipt_id, None)
        except (OSError, StateError):
            continue


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
    retirements = _consumed_deliveries(state, output_dir)
    replacements = dict(state.pending_delivery_replacements)
    for retirement in retirements:
        _clear_completed_replacement(replacements, retirement.receipt)
    data["pending_delivery_replacements"] = replacements

    # Extract message_map and write it to its own file.
    message_map = data.pop("message_map", {})
    atomic_write_text(output_dir / "message_map.json", json.dumps(message_map, indent=2))

    # Redact free-text fields on the way out, never in place: state.warnings must keep
    # its raw text in memory. save_state takes no config, so this relies on the
    # process-wide registry, which is where the proxy secrets live.
    #
    # message_map was popped above and is deliberately NOT scrubbed. It holds identifier
    # pairs and no free text, and walking it measured ~290ms at 100k entries against
    # ~3ms for everything else, on a path that runs every 50 messages. Issue #140, ADR-014.
    atomic_write_text(output_dir / "state.json", json.dumps(scrub_document(data), indent=2))
    _retire_consumed_deliveries(state, output_dir, retirements)


def load_state(output_dir: Path) -> MigrationState:
    """Load migration state from state.json.

    Handles v1→v2 migration (last_completed_channel/message → completed_channel_ids)
    and loads message_map from message_map.json if present (v2+), falling back to
    embedded message_map in state.json (v1 compat).

    Args:
        output_dir: Directory containing state.json.

    Raises:
        StateError: If the file doesn't exist, contains invalid JSON, or holds a saved
            destination id that is not shaped like one (see ``check_state_ids``).
    """
    state_path = output_dir / "state.json"
    if not state_path.exists():
        raise StateError(f"State file not found: {state_path}")
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise StateError(f"Corrupt state file: {e}") from e
    if not isinstance(raw, dict):
        raise StateError(f"Corrupt state file: expected a JSON object in {state_path}")

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

    try:
        state = _dict_to_state(raw)
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        # A wrongly shaped container (a list where an object belongs, a string where a
        # record belongs) surfaces as one of these. Say which file, not a traceback.
        raise StateError(f"Invalid state data: {type(e).__name__}: {e}") from e
    check_state_ids(state)
    return state


# ---------------------------------------------------------------------------
# Saved destination ids (#970)
# ---------------------------------------------------------------------------
#
# Every recovery path (--resume, retry, rollback, repair, check) reads Stoat ids
# out of state.json and sends requests to them. A value that is not shaped like an
# id either crashes the run late or aims it at the wrong thing, so load_state
# refuses it here, once, before any of those paths see it.
#
# Formats, and what each rests on:
#   Stoat server, channel, role and message ids: 26-character Crockford base32
#     ULIDs. Messages and the 26-character `before`/`after` bounds are verified in
#     the memory note gotcha-stoat-response-shapes-and-ids; the server id is called
#     a ULID in gui_tools.py; review.py decodes channel ids as ULIDs. Case is not
#     enforced, because Ferry's own category ids (uuid4 hex) use the same alphabet.
#   Category ids: Ferry generates them (_generate_category_id, 26 hex chars) and
#     Stoat accepts a 1-32 character string, so the rule is a 1-32 character token.
#   Emoji ids: the Autumn file id. Its format is NOT verified, so the rule is the
#     shape a URL path segment needs (1-128 characters of [A-Za-z0-9_-]), which is
#     the only property the delete and create routes depend on. 128 is the bound
#     recorded for role icons, which are also Autumn ids.
#   Discord ids (map keys): digit snowflakes, plus the synthetic keys Ferry writes:
#     `forum-index-<key>` in channel_map and `forum-<name>` in category_map.
#
# A dry-run state also holds `dry-<kind>-<id>` placeholders, written by structure.py,
# messages.py and avatars.py. Nothing is ever sent to them: resume, retry, rollback,
# check and repair (through check) all refuse a dry-run state. They are still held to
# the exact shapes Ferry writes, so a hand-edited value cannot carry `/`, `?` or `%`
# into a request path. Category placeholders hold a forum name (`dry-cat-forum-<name>`)
# and category ids only ever travel in a request body, so they allow any printable text.

_STOAT_ULID = re.compile(r"[0-9A-HJKMNP-TV-Za-hjkmnp-tv-z]{26}")
_STOAT_CATEGORY_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")
_STOAT_FILE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_SNOWFLAKE = re.compile(r"[0-9]{1,20}")
_DRY_PLACEHOLDER = re.compile(r"dry-(?:ch|role|emoji|msg|server|avatar)-[A-Za-z0-9_-]+")
_DRY_CATEGORY_PLACEHOLDER = re.compile(r"dry-cat-[^\x00-\x1f\x7f]+")

#: Synthetic Discord-side keys Ferry's own writers add, by map. structure.py writes
#: `channel_map["forum-index-<forum key>"]` and `category_map["forum-<parent name>"]`.
_FORUM_INDEX_KEY_PREFIX = "forum-index-"
_FORUM_CATEGORY_KEY_PREFIX = "forum-"

_MAX_PROBLEMS = 50


def is_stoat_id(value: object) -> bool:
    """True when ``value`` is a well-formed Stoat server, channel, role or message id."""
    return isinstance(value, str) and _STOAT_ULID.fullmatch(value) is not None


def is_stoat_file_id(value: object) -> bool:
    """True when ``value`` is shaped like an emoji (Autumn file) id."""
    return isinstance(value, str) and _STOAT_FILE_ID.fullmatch(value) is not None


def _show(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 40 else f"{text[:37]}..."


class _Problems:
    """Collects what is wrong, without letting a huge bad file build a huge message."""

    def __init__(self, *, dry_run: bool) -> None:
        self.dry_run = dry_run
        self.items: list[str] = []
        self.total = 0

    def add(self, where: str, what: str) -> None:
        self.total += 1
        if len(self.items) < _MAX_PROBLEMS:
            self.items.append(f"{where}: {what}")

    def stoat(
        self,
        where: str,
        value: object,
        kind: str,
        pattern: re.Pattern[str],
        placeholder: re.Pattern[str] = _DRY_PLACEHOLDER,
    ) -> None:
        if not isinstance(value, str):
            self.add(where, f"expected a text {kind}, got {type(value).__name__} {_show(value)}")
        elif pattern.fullmatch(value) is None and not (
            self.dry_run and placeholder.fullmatch(value) is not None
        ):
            self.add(where, f"{_show(value)} is not a valid {kind}")

    def discord(self, where: str, key: object, prefix: str | None = None) -> None:
        if not isinstance(key, str):
            self.add(where, f"expected a text Discord id, got {type(key).__name__} {_show(key)}")
        elif _SNOWFLAKE.fullmatch(key) is None and not (
            prefix is not None and key.startswith(prefix) and len(key) > len(prefix)
        ):
            self.add(where, f"key {_show(key)} is not a Discord id")


def _check_map(
    problems: _Problems,
    name: str,
    mapping: object,
    kind: str,
    pattern: re.Pattern[str],
    key_prefix: str | None = None,
    placeholder: re.Pattern[str] = _DRY_PLACEHOLDER,
) -> None:
    if not isinstance(mapping, dict):
        problems.add(name, f"expected an object of id pairs, got {type(mapping).__name__}")
        return
    for key, value in mapping.items():
        where = f"{name}[{_show(key)}]"
        problems.discord(where, key, key_prefix)
        problems.stoat(where, value, kind, pattern, placeholder)


def check_state_ids(state: MigrationState) -> None:
    """Refuse a state whose saved destination ids are not shaped like ids.

    Raises:
        StateError: naming the first offending field, and how many more there are.
            Nothing is repaired or dropped: a state that fails here is not used.
    """
    p = _Problems(dry_run=state.is_dry_run is True)

    if state.stoat_server_id != "":
        p.stoat("stoat_server_id", state.stoat_server_id, "Stoat server id", _STOAT_ULID)
    _check_map(p, "role_map", state.role_map, "Stoat role id", _STOAT_ULID)
    _check_map(
        p,
        "channel_map",
        state.channel_map,
        "Stoat channel id",
        _STOAT_ULID,
        _FORUM_INDEX_KEY_PREFIX,
    )
    _check_map(
        p,
        "category_map",
        state.category_map,
        "Stoat category id",
        _STOAT_CATEGORY_ID,
        _FORUM_CATEGORY_KEY_PREFIX,
        _DRY_CATEGORY_PLACEHOLDER,
    )
    _check_map(p, "emoji_map", state.emoji_map, "Stoat emoji id", _STOAT_FILE_ID)
    _check_map(p, "message_map", state.message_map, "Stoat message id", _STOAT_ULID)

    if isinstance(state.forum_index_message_ids, dict):
        for key, value in state.forum_index_message_ids.items():
            p.stoat(
                f"forum_index_message_ids[{_show(key)}]", value, "Stoat message id", _STOAT_ULID
            )
    else:
        p.add("forum_index_message_ids", "expected an object of id pairs")

    for i, fm in enumerate(state.failed_messages):
        p.discord(f"failed_messages[{i}].discord_msg_id", fm.discord_msg_id)
        p.stoat(
            f"failed_messages[{i}].stoat_channel_id",
            fm.stoat_channel_id,
            "Stoat channel id",
            _STOAT_ULID,
        )

    for i, pin in enumerate(state.pending_pins):
        p.stoat(f"pending_pins[{i}] channel", pin[0], "Stoat channel id", _STOAT_ULID)
        p.stoat(f"pending_pins[{i}] message", pin[1], "Stoat message id", _STOAT_ULID)

    for i, entry in enumerate(state.pending_reactions):
        if not isinstance(entry, dict):
            p.add(f"pending_reactions[{i}]", f"expected an object, got {type(entry).__name__}")
            continue
        p.stoat(
            f"pending_reactions[{i}].channel_id",
            entry.get("channel_id"),
            "Stoat channel id",
            _STOAT_ULID,
        )
        p.stoat(
            f"pending_reactions[{i}].message_id",
            entry.get("message_id"),
            "Stoat message id",
            _STOAT_ULID,
        )
        emoji = entry.get("emoji")
        if not isinstance(emoji, str) or not emoji:
            p.add(f"pending_reactions[{i}].emoji", f"expected a text emoji, got {_show(emoji)}")

    if isinstance(state.pending_emoji_rewrites, dict):
        for key, record in state.pending_emoji_rewrites.items():
            where = f"pending_emoji_rewrites[{_show(key)}]"
            p.discord(where, key)
            if not isinstance(record, dict):
                p.add(where, f"expected an old/new pair, got {type(record).__name__}")
                continue
            for side in ("old", "new"):
                p.stoat(f"{where}.{side}", record.get(side), "Stoat emoji id", _STOAT_FILE_ID)
    else:
        p.add("pending_emoji_rewrites", "expected an object")

    if p.total:
        more = f" (and {p.total - 1} more)" if p.total > 1 else ""
        raise StateError(f"state.json holds a saved id that is not valid: {p.items[0]}{more}")


def _state_to_dict(state: MigrationState) -> dict[str, Any]:
    _validate_delivery_binding(state)
    rp_data: dict[str, Any] | None
    if state.rollback_progress is not None:
        rp_data = dataclasses.asdict(state.rollback_progress)
        # set → list for JSON.
        rp_data["rolled_back_ids"] = sorted(state.rollback_progress.rolled_back_ids)
    else:
        rp_data = None

    return {
        "delivery_namespace": state.delivery_namespace,
        "delivery_service_digest": state.delivery_service_digest,
        "delivery_bookkeeping": _delivery_map(state.delivery_bookkeeping, messages=True),
        "pending_delivery_replacements": _delivery_map(state.pending_delivery_replacements),
        "role_map": state.role_map,
        "channel_map": state.channel_map,
        "category_map": state.category_map,
        "category_names": state.category_names,
        "created_channel_names": state.created_channel_names,
        "created_role_names": state.created_role_names,
        "created_emoji_names": state.created_emoji_names,
        "thread_strategy": state.thread_strategy,
        "channel_categories": state.channel_categories,
        "message_map": state.message_map,
        "emoji_map": state.emoji_map,
        "pending_emoji_rewrites": state.pending_emoji_rewrites,
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
        "merge_duplicates_suppressed": state.merge_duplicates_suppressed,
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
        "forum_index_present_unknown_id": sorted(state.forum_index_present_unknown_id),
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
            delivery_namespace=data.get("delivery_namespace", ""),
            delivery_service_digest=data.get("delivery_service_digest", ""),
            delivery_bookkeeping=_delivery_map(data.get("delivery_bookkeeping", {}), messages=True),
            pending_delivery_replacements=_delivery_map(
                data.get("pending_delivery_replacements", {})
            ),
            role_map=data.get("role_map", {}),
            channel_map=data.get("channel_map", {}),
            category_map=data.get("category_map", {}),
            category_names=data.get("category_names", {}),
            created_channel_names=data.get("created_channel_names", {}),
            created_role_names=data.get("created_role_names", {}),
            created_emoji_names=data.get("created_emoji_names", {}),
            thread_strategy=data.get("thread_strategy", ""),
            channel_categories=data.get("channel_categories", {}),
            message_map=data.get("message_map", {}),
            emoji_map=data.get("emoji_map", {}),
            pending_emoji_rewrites=data.get("pending_emoji_rewrites", {}),
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
            merge_duplicates_suppressed=data.get("merge_duplicates_suppressed", 0),
            started_at=data.get("started_at", ""),
            completed_at=data.get("completed_at", ""),
            is_dry_run=data.get("is_dry_run", False),
            export_completed=data.get("export_completed", False),
            autumn_uploads=data.get("autumn_uploads", {}),
            referenced_autumn_ids=set(data.get("referenced_autumn_ids", [])),
            roles_finalized=set(data.get("roles_finalized", [])),
            failed_messages=[_decode_failed_message(d) for d in data.get("failed_messages", [])],
            validation_results=data.get("validation_results", {}),
            prior_messages_total=data.get("prior_messages_total", 0),
            source_messages_total=data.get("source_messages_total", 0),
            forum_channel_members=data.get("forum_channel_members", {}),
            forum_category_names=data.get("forum_category_names", {}),
            channel_message_counts=data.get("channel_message_counts", {}),
            reaction_message_counts=data.get("reaction_message_counts", {}),
            forum_index_message_ids=data.get("forum_index_message_ids", {}),
            forum_index_present_unknown_id=set(data.get("forum_index_present_unknown_id", [])),
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
        _validate_delivery_binding(state)
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
    # Back up the original state.json before migrating. Atomic like the rest: state.json
    # still holds the v1 content at this moment, but the run's first save_state overwrites
    # it, so this backup becomes the only copy. A truncated one is a safety net with a
    # hole in it (#175). The path is named because the warning below reports it.
    backup_path = output_dir / "state.json.v1.bak"
    atomic_write_text(backup_path, json.dumps(data, indent=2))

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
