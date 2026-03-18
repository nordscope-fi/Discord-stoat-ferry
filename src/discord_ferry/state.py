"""Migration state management, ID mapping, and persistence."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from discord_ferry.errors import StateError


@dataclass
class MigrationState:
    """Tracks all ID mappings and progress for resume support."""

    # Discord ID -> Stoat ID mappings
    role_map: dict[str, str] = field(default_factory=dict)
    channel_map: dict[str, str] = field(default_factory=dict)
    category_map: dict[str, str] = field(default_factory=dict)
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

    # Stoat server ID and Autumn URL (discovered during CONNECT phase)
    stoat_server_id: str = ""
    autumn_url: str = ""

    # Resume tracking
    current_phase: str = ""
    last_completed_channel: str = ""
    last_completed_message: str = ""

    # Counters (incremented by phase implementations)
    attachments_uploaded: int = 0
    attachments_skipped: int = 0
    reactions_applied: int = 0
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


def save_state(state: MigrationState, output_dir: Path) -> None:
    """Save migration state to state.json using atomic write.

    Args:
        state: Current migration state.
        output_dir: Directory to write state.json into. Created if missing.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    data = _state_to_dict(state)
    tmp_path = output_dir / "state.json.tmp"
    final_path = output_dir / "state.json"
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp_path.rename(final_path)


def load_state(output_dir: Path) -> MigrationState:
    """Load migration state from state.json.

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
    return _dict_to_state(raw)


def _state_to_dict(state: MigrationState) -> dict[str, Any]:
    return {
        "role_map": state.role_map,
        "channel_map": state.channel_map,
        "category_map": state.category_map,
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
        "last_completed_channel": state.last_completed_channel,
        "last_completed_message": state.last_completed_message,
        "attachments_uploaded": state.attachments_uploaded,
        "attachments_skipped": state.attachments_skipped,
        "reactions_applied": state.reactions_applied,
        "pins_applied": state.pins_applied,
        "started_at": state.started_at,
        "completed_at": state.completed_at,
        "is_dry_run": state.is_dry_run,
        "export_completed": state.export_completed,
        "autumn_uploads": state.autumn_uploads,
        "referenced_autumn_ids": list(state.referenced_autumn_ids),
    }


def _dict_to_state(data: dict[str, Any]) -> MigrationState:
    try:
        return MigrationState(
            role_map=data.get("role_map", {}),
            channel_map=data.get("channel_map", {}),
            category_map=data.get("category_map", {}),
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
            last_completed_channel=data.get("last_completed_channel", ""),
            last_completed_message=data.get("last_completed_message", ""),
            attachments_uploaded=data.get("attachments_uploaded", 0),
            attachments_skipped=data.get("attachments_skipped", 0),
            reactions_applied=data.get("reactions_applied", 0),
            pins_applied=data.get("pins_applied", 0),
            started_at=data.get("started_at", ""),
            completed_at=data.get("completed_at", ""),
            is_dry_run=data.get("is_dry_run", False),
            export_completed=data.get("export_completed", False),
            autumn_uploads=data.get("autumn_uploads", {}),
            referenced_autumn_ids=set(data.get("referenced_autumn_ids", [])),
        )
    except (TypeError, ValueError) as e:
        raise StateError(f"Invalid state data: {e}") from e
