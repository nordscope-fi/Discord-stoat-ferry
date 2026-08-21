"""Tests for migration state persistence."""

import json
from pathlib import Path

import pytest

from discord_ferry.core.security import _TEXT_MEMBERS, register_secret
from discord_ferry.errors import StateError
from discord_ferry.state import (
    FailedMessage,
    MigrationState,
    RollbackFailure,
    RollbackProgress,
    load_state,
    save_state,
)


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    """State survives a save/load round-trip."""
    state = MigrationState(
        stoat_server_id="01ABC",
        current_phase="messages",
        started_at="2024-01-01T00:00:00+00:00",
    )
    save_state(state, tmp_path)
    loaded = load_state(tmp_path)
    assert loaded.stoat_server_id == "01ABC"
    assert loaded.current_phase == "messages"
    assert loaded.started_at == "2024-01-01T00:00:00+00:00"


def test_save_creates_output_dir(tmp_path: Path) -> None:
    """save_state creates the output directory if it doesn't exist."""
    nested = tmp_path / "deep" / "nested" / "dir"
    save_state(MigrationState(), nested)
    assert (nested / "state.json").exists()


def test_load_missing_file(tmp_path: Path) -> None:
    """load_state raises StateError for missing file."""
    with pytest.raises(StateError, match="not found"):
        load_state(tmp_path)


def test_load_corrupt_json(tmp_path: Path) -> None:
    """load_state raises StateError for corrupt JSON."""
    (tmp_path / "state.json").write_text("not valid json {{{", encoding="utf-8")
    with pytest.raises(StateError, match="Corrupt"):
        load_state(tmp_path)


def test_pending_pins_tuple_roundtrip(tmp_path: Path) -> None:
    """Pending pins tuples survive JSON serialization."""
    state = MigrationState(
        pending_pins=[("ch1", "msg1"), ("ch2", "msg2")],
    )
    save_state(state, tmp_path)
    loaded = load_state(tmp_path)
    assert loaded.pending_pins == [("ch1", "msg1"), ("ch2", "msg2")]
    assert isinstance(loaded.pending_pins[0], tuple)


def test_state_with_populated_maps(tmp_path: Path) -> None:
    """State with data in all maps round-trips correctly."""
    state = MigrationState(
        role_map={"d_role1": "s_role1"},
        channel_map={"d_ch1": "s_ch1", "d_ch2": "s_ch2"},
        category_map={"d_cat1": "s_cat1"},
        message_map={"d_msg1": "s_msg1"},
        emoji_map={"d_emoji1": "s_emoji1"},
        avatar_cache={"author1": "autumn_av1"},
        upload_cache={"/path/to/file.png": "autumn_file1"},
        author_names={"12345": "Alice"},
        pending_reactions=[{"channel_id": "ch1", "message_id": "msg1", "emoji": "👍"}],
        errors=[{"phase": "messages", "error": "timeout"}],
        warnings=[{"type": "http_attachment", "message": "missing media"}],
        stoat_server_id="01STOAT",
        current_phase="reactions",
        completed_channel_ids={"general"},
        channel_message_offsets={"general": "msg999"},
        started_at="2024-06-01T10:00:00+00:00",
        completed_at="2024-06-01T14:00:00+00:00",
    )
    save_state(state, tmp_path)
    loaded = load_state(tmp_path)
    assert loaded.role_map == {"d_role1": "s_role1"}
    assert loaded.channel_map == {"d_ch1": "s_ch1", "d_ch2": "s_ch2"}
    assert loaded.emoji_map == {"d_emoji1": "s_emoji1"}
    assert loaded.author_names == {"12345": "Alice"}
    assert loaded.upload_cache == {"/path/to/file.png": "autumn_file1"}
    assert loaded.completed_channel_ids == {"general"}
    assert loaded.channel_message_offsets == {"general": "msg999"}
    assert loaded.completed_at == "2024-06-01T14:00:00+00:00"
    assert len(loaded.errors) == 1
    assert len(loaded.warnings) == 1


def test_atomic_write_no_tmp_leftover(tmp_path: Path) -> None:
    """After save, the .tmp file should not remain."""
    save_state(MigrationState(), tmp_path)
    assert not (tmp_path / "state.json.tmp").exists()
    assert (tmp_path / "state.json").exists()


def test_load_state_missing_newer_fields(tmp_path: Path) -> None:
    """A minimal JSON (from an older version) fills missing fields with defaults."""
    import json

    minimal = {
        "stoat_server_id": "old-server",
        "current_phase": "messages",
        "role_map": {"r1": "sr1"},
    }
    (tmp_path / "state.json").write_text(json.dumps(minimal), encoding="utf-8")
    loaded = load_state(tmp_path)
    assert loaded.stoat_server_id == "old-server"
    assert loaded.role_map == {"r1": "sr1"}
    # Newer fields should be filled with defaults
    assert loaded.emoji_map == {}
    assert loaded.author_names == {}
    assert loaded.upload_cache == {}
    assert loaded.attachments_uploaded == 0
    assert loaded.attachments_skipped == 0
    assert loaded.reactions_applied == 0
    assert loaded.pins_applied == 0
    assert loaded.is_dry_run is False
    assert loaded.pending_pins == []
    assert loaded.pending_reactions == []


def test_export_completed_default_false() -> None:
    """New states default export_completed to False."""
    state = MigrationState()
    assert state.export_completed is False


def test_export_completed_round_trip(tmp_path: Path) -> None:
    """export_completed survives save/load cycle."""
    state = MigrationState()
    state.export_completed = True
    save_state(state, tmp_path)
    loaded = load_state(tmp_path)
    assert loaded.export_completed is True


def test_load_old_state_without_export_completed(tmp_path: Path) -> None:
    """Loading a state.json from before this field was added defaults to False."""
    import json

    old_data = {"role_map": {}, "channel_map": {}}  # minimal old state
    (tmp_path / "state.json").write_text(json.dumps(old_data))
    loaded = load_state(tmp_path)
    assert loaded.export_completed is False


def test_autumn_uploads_round_trip(tmp_path: Path) -> None:
    """autumn_uploads dict survives save/load round-trip."""
    state = MigrationState(
        autumn_uploads={"autumn_abc": "discord_att_1", "autumn_def": "discord_att_2"},
    )
    save_state(state, tmp_path)
    loaded = load_state(tmp_path)
    assert loaded.autumn_uploads == {"autumn_abc": "discord_att_1", "autumn_def": "discord_att_2"}


def test_referenced_autumn_ids_round_trip(tmp_path: Path) -> None:
    """referenced_autumn_ids set survives as list in JSON, reconstructed as set."""
    import json

    state = MigrationState(
        referenced_autumn_ids={"autumn_abc", "autumn_def"},
    )
    save_state(state, tmp_path)

    # Verify it's stored as a list in JSON
    raw = json.loads((tmp_path / "state.json").read_text())
    assert isinstance(raw["referenced_autumn_ids"], list)
    assert set(raw["referenced_autumn_ids"]) == {"autumn_abc", "autumn_def"}

    # Verify it loads back as a set
    loaded = load_state(tmp_path)
    assert isinstance(loaded.referenced_autumn_ids, set)
    assert loaded.referenced_autumn_ids == {"autumn_abc", "autumn_def"}


def test_old_state_without_orphan_fields(tmp_path: Path) -> None:
    """State JSON from before orphan tracking fields were added loads with empty defaults."""
    import json

    old_data = {"role_map": {"r1": "sr1"}, "channel_map": {}}
    (tmp_path / "state.json").write_text(json.dumps(old_data))
    loaded = load_state(tmp_path)
    assert loaded.autumn_uploads == {}
    assert loaded.referenced_autumn_ids == set()


# ---------------------------------------------------------------------------
# FailedMessage dataclass (S1)
# ---------------------------------------------------------------------------


def test_failed_message_round_trip(tmp_path: Path) -> None:
    """FailedMessage survives save/load round-trip as a typed dataclass."""
    from discord_ferry.state import FailedMessage

    fm = FailedMessage(
        discord_msg_id="msg123",
        stoat_channel_id="ch456",
        error="API timeout",
        retry_count=1,
        content_preview="Hello world...",
    )
    state = MigrationState(failed_messages=[fm])
    save_state(state, tmp_path)
    loaded = load_state(tmp_path)
    assert len(loaded.failed_messages) == 1
    assert isinstance(loaded.failed_messages[0], FailedMessage)
    assert loaded.failed_messages[0].discord_msg_id == "msg123"
    assert loaded.failed_messages[0].retry_count == 1


def test_old_state_without_failed_messages_loads(tmp_path: Path) -> None:
    """A state.json from before FailedMessage was added defaults to empty lists/dicts."""
    import json

    (tmp_path / "state.json").write_text(json.dumps({"role_map": {}}))
    loaded = load_state(tmp_path)
    assert loaded.failed_messages == []
    assert loaded.validation_results == {}


# ---------------------------------------------------------------------------
# S2 — Resume correctness: completed_channel_ids + channel_message_offsets
# ---------------------------------------------------------------------------


def test_completed_channel_ids_roundtrip(tmp_path: Path) -> None:
    """completed_channel_ids set survives JSON serialization (stored as list, loaded as set)."""
    import json

    state = MigrationState(completed_channel_ids={"ch_a", "ch_b", "ch_c"})
    save_state(state, tmp_path)

    # Verify stored as list in state.json.
    raw = json.loads((tmp_path / "state.json").read_text())
    assert isinstance(raw["completed_channel_ids"], list)
    assert set(raw["completed_channel_ids"]) == {"ch_a", "ch_b", "ch_c"}

    # Verify loads back as a set.
    loaded = load_state(tmp_path)
    assert isinstance(loaded.completed_channel_ids, set)
    assert loaded.completed_channel_ids == {"ch_a", "ch_b", "ch_c"}


def test_native_fidelity_counts_round_trip() -> None:
    """native_fidelity_counts dict survives _state_to_dict -> _dict_to_state."""
    from discord_ferry.state import _dict_to_state, _state_to_dict

    s = MigrationState()
    s.native_fidelity_counts = {"slowmode": 2, "user_limit": 1, "role_icons": 3}
    restored = _dict_to_state(_state_to_dict(s))
    assert restored.native_fidelity_counts == {"slowmode": 2, "user_limit": 1, "role_icons": 3}


def test_native_fidelity_counts_save_load_roundtrip(tmp_path: Path) -> None:
    """native_fidelity_counts survives a JSON save/load round-trip."""
    state = MigrationState()
    state.native_fidelity_counts = {"slowmode": 4}
    save_state(state, tmp_path)
    loaded = load_state(tmp_path)
    assert loaded.native_fidelity_counts == {"slowmode": 4}


def test_channel_message_offsets_roundtrip(tmp_path: Path) -> None:
    """channel_message_offsets dict survives JSON serialization."""
    state = MigrationState(channel_message_offsets={"ch1": "msg_123", "ch2": "msg_456"})
    save_state(state, tmp_path)
    loaded = load_state(tmp_path)
    assert loaded.channel_message_offsets == {"ch1": "msg_123", "ch2": "msg_456"}


def test_v1_state_migration(tmp_path: Path) -> None:
    """v1 last_completed_channel/message fields are converted to v2 format on load."""
    import json

    v1_data = {
        "channel_map": {"100": "s_ch100", "200": "s_ch200", "300": "s_ch300"},
        "last_completed_channel": "200",
        "last_completed_message": "msg_999",
        "role_map": {},
    }
    (tmp_path / "state.json").write_text(json.dumps(v1_data), encoding="utf-8")

    import warnings

    with warnings.catch_warnings(record=True):
        loaded = load_state(tmp_path)

    # Channel 100 is < 200, so it should be marked complete.
    assert "100" in loaded.completed_channel_ids
    # Channel 200 is NOT in completed_channel_ids (it was partial).
    assert "200" not in loaded.completed_channel_ids
    # Channel 300 is > 200, so it should NOT be marked complete.
    assert "300" not in loaded.completed_channel_ids
    # The last message for the resume channel (200) should be recorded.
    assert loaded.channel_message_offsets.get("200") == "msg_999"


def test_v1_state_migration_empty_fields(tmp_path: Path) -> None:
    """v1 state with empty last_completed_channel/message migrates cleanly."""
    import json
    import warnings

    v1_data = {
        "channel_map": {"100": "s_ch100"},
        "last_completed_channel": "",
        "last_completed_message": "",
        "role_map": {},
    }
    (tmp_path / "state.json").write_text(json.dumps(v1_data), encoding="utf-8")

    with warnings.catch_warnings(record=True):
        loaded = load_state(tmp_path)

    assert loaded.completed_channel_ids == set()
    assert loaded.channel_message_offsets == {}


def test_v1_state_backup_created(tmp_path: Path) -> None:
    """A backup file state.json.v1.bak is created during v1→v2 migration."""
    import json
    import warnings

    v1_data = {
        "channel_map": {"100": "s_ch100"},
        "last_completed_channel": "100",
        "last_completed_message": "msg_42",
    }
    (tmp_path / "state.json").write_text(json.dumps(v1_data), encoding="utf-8")

    with warnings.catch_warnings(record=True):
        load_state(tmp_path)

    backup = tmp_path / "state.json.v1.bak"
    assert backup.exists()
    bak_data = json.loads(backup.read_text())
    assert bak_data["last_completed_channel"] == "100"


# ---------------------------------------------------------------------------
# S5 — Separate message_map.json file
# ---------------------------------------------------------------------------


def test_message_map_saved_to_separate_file(tmp_path: Path) -> None:
    """After save_state, message_map.json exists and state.json does not contain message_map."""
    import json

    state = MigrationState(message_map={"d_msg1": "s_msg1", "d_msg2": "s_msg2"})
    save_state(state, tmp_path)

    # state.json must NOT contain message_map key.
    raw = json.loads((tmp_path / "state.json").read_text())
    assert "message_map" not in raw

    # message_map.json must exist and contain the map.
    mm_path = tmp_path / "message_map.json"
    assert mm_path.exists()
    mm_data = json.loads(mm_path.read_text())
    assert mm_data == {"d_msg1": "s_msg1", "d_msg2": "s_msg2"}


def test_message_map_loaded_from_separate_file(tmp_path: Path) -> None:
    """Round-trip through separate message_map.json restores the message_map correctly."""
    state = MigrationState(message_map={"d_msg1": "s_msg1"})
    save_state(state, tmp_path)
    loaded = load_state(tmp_path)
    assert loaded.message_map == {"d_msg1": "s_msg1"}


def test_message_map_v1_fallback(tmp_path: Path) -> None:
    """v1 state.json with embedded message_map loads correctly (no message_map.json)."""
    import json

    v1_data = {
        "message_map": {"old_msg1": "old_stoat1"},
        "role_map": {},
        "channel_map": {},
    }
    (tmp_path / "state.json").write_text(json.dumps(v1_data), encoding="utf-8")
    # No message_map.json exists.

    loaded = load_state(tmp_path)
    assert loaded.message_map == {"old_msg1": "old_stoat1"}


def test_message_map_empty_dict(tmp_path: Path) -> None:
    """Empty message_map round-trips cleanly through the separate file."""
    import json

    state = MigrationState(message_map={})
    save_state(state, tmp_path)

    mm_path = tmp_path / "message_map.json"
    assert mm_path.exists()
    assert json.loads(mm_path.read_text()) == {}

    loaded = load_state(tmp_path)
    assert loaded.message_map == {}


# ---------------------------------------------------------------------------
# RollbackProgress / RollbackFailure round-trip (SC-4, SC-13, plus edges)
# ---------------------------------------------------------------------------


def test_rollback_progress_round_trip(tmp_path: Path) -> None:
    """SC-4: a fully-populated RollbackProgress round-trips through state.json."""
    state = MigrationState(
        rollback_progress=RollbackProgress(
            channels_deleted=3,
            roles_deleted=2,
            emoji_deleted=1,
            categories_cleaned=True,
            untracked_channels_deleted=1,
            rolled_back_ids={"a", "b", "c"},
            failures=[
                RollbackFailure(
                    entity_type="role",
                    stoat_id="r1",
                    error="permission denied",
                    http_status=403,
                ),
            ],
            started_at="2026-05-13T10:00:00Z",
            completed_at="2026-05-13T10:00:42Z",
        )
    )
    save_state(state, tmp_path)
    loaded = load_state(tmp_path)

    rp = loaded.rollback_progress
    assert rp is not None
    assert rp.channels_deleted == 3
    assert rp.roles_deleted == 2
    assert rp.emoji_deleted == 1
    assert rp.categories_cleaned is True
    assert rp.untracked_channels_deleted == 1
    assert isinstance(rp.rolled_back_ids, set)
    assert rp.rolled_back_ids == {"a", "b", "c"}
    assert len(rp.failures) == 1
    assert isinstance(rp.failures[0], RollbackFailure)
    assert rp.failures[0].http_status == 403
    assert rp.failures[0].entity_type == "role"
    assert rp.started_at == "2026-05-13T10:00:00Z"


def test_old_state_json_without_rollback_progress_loads_clean(tmp_path: Path) -> None:
    """SC-13: state.json without rollback_progress key loads with field=None."""
    import json

    # Hand-crafted v1.7.0-shape state.json.
    raw = {
        "role_map": {"d1": "s1"},
        "channel_map": {},
        "category_map": {},
        "message_map": {},
        "emoji_map": {},
        "stoat_server_id": "srv01",
    }
    (tmp_path / "state.json").write_text(json.dumps(raw), encoding="utf-8")
    (tmp_path / "message_map.json").write_text("{}", encoding="utf-8")

    loaded = load_state(tmp_path)

    assert loaded.rollback_progress is None
    assert loaded.role_map == {"d1": "s1"}


def test_rollback_failure_with_none_http_status(tmp_path: Path) -> None:
    """Edge: RollbackFailure(http_status=None) round-trips cleanly (network error)."""
    state = MigrationState(
        rollback_progress=RollbackProgress(
            failures=[
                RollbackFailure(
                    entity_type="channel",
                    stoat_id="ch1",
                    error="connection reset",
                    http_status=None,
                )
            ]
        )
    )
    save_state(state, tmp_path)
    loaded = load_state(tmp_path)

    assert loaded.rollback_progress is not None
    assert loaded.rollback_progress.failures[0].http_status is None


def test_empty_rolled_back_ids_serializes_as_empty_list(tmp_path: Path) -> None:
    """Edge: empty set serializes as `[]`, not as `null` or `{}`."""
    import json

    state = MigrationState(
        rollback_progress=RollbackProgress(
            rolled_back_ids=set(),
        )
    )
    save_state(state, tmp_path)
    raw = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))

    assert raw["rollback_progress"]["rolled_back_ids"] == []
    # And it round-trips back to an empty set.
    loaded = load_state(tmp_path)
    assert loaded.rollback_progress is not None
    assert loaded.rollback_progress.rolled_back_ids == set()


def test_rollback_progress_none_serializes_as_null(tmp_path: Path) -> None:
    """Edge: rollback_progress=None serializes as JSON null, not omitted."""
    import json

    state = MigrationState()
    save_state(state, tmp_path)
    raw = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))

    assert "rollback_progress" in raw
    assert raw["rollback_progress"] is None


def test_invite_fields_roundtrip(tmp_path: Path) -> None:
    """invite_code/invite_url survive save/load."""
    state = MigrationState(invite_code="inv_R", invite_url="https://app/invite/inv_R")
    save_state(state, tmp_path)
    loaded = load_state(tmp_path)
    assert loaded.invite_code == "inv_R"
    assert loaded.invite_url == "https://app/invite/inv_R"


def test_invite_fields_default_when_absent(tmp_path: Path) -> None:
    """Old state.json without invite fields loads with empty defaults."""
    minimal = {"stoat_server_id": "s", "current_phase": "report"}
    (tmp_path / "state.json").write_text(json.dumps(minimal), encoding="utf-8")
    loaded = load_state(tmp_path)
    assert loaded.invite_code == ""
    assert loaded.invite_url == ""


def test_new_reporting_fields_round_trip(tmp_path: Path) -> None:
    """source_messages_total and reaction_message_counts survive save/load."""
    from discord_ferry.state import MigrationState, load_state, save_state

    state = MigrationState()
    state.source_messages_total = 1234
    state.reaction_message_counts = {"01HMSG": 7}
    save_state(state, tmp_path)

    loaded = load_state(tmp_path)
    assert loaded.source_messages_total == 1234
    assert loaded.reaction_message_counts == {"01HMSG": 7}


def test_new_reporting_fields_default_when_absent(tmp_path: Path) -> None:
    """Legacy state.json without the new fields loads with safe defaults."""
    import json

    from discord_ferry.state import load_state

    (tmp_path / "state.json").write_text(json.dumps({"stoat_server_id": "srv1"}), encoding="utf-8")
    (tmp_path / "message_map.json").write_text(json.dumps({}), encoding="utf-8")

    loaded = load_state(tmp_path)
    assert loaded.source_messages_total == 0
    assert loaded.reaction_message_counts == {}


def test_channel_high_water_round_trips(tmp_path: Path) -> None:
    """channel_high_water survives save/load unchanged."""
    state = MigrationState(channel_high_water={"ch1": "500", "ch2": "42"})
    save_state(state, tmp_path)
    loaded = load_state(tmp_path)
    assert loaded.channel_high_water == {"ch1": "500", "ch2": "42"}


def test_load_state_defaults_channel_high_water(tmp_path: Path) -> None:
    """A pre-feature state.json without channel_high_water loads with {} (back-compat)."""
    state = MigrationState(channel_map={"a": "b"})
    save_state(state, tmp_path)
    path = tmp_path / "state.json"
    data = json.loads(path.read_text())
    data.pop("channel_high_water", None)  # emulate a v2.6.2 file
    path.write_text(json.dumps(data))
    loaded = load_state(tmp_path)
    assert loaded.channel_high_water == {}


# ---------------------------------------------------------------------------
# Batch 2 — S3: roles_finalized round-trip + seed; S1 marker not persisted
# ---------------------------------------------------------------------------


def test_roles_finalized_roundtrip(tmp_path: Path) -> None:
    """S3 SC-16: roles_finalized survives save/load."""
    state = MigrationState(roles_finalized={"r1", "r2"})
    save_state(state, tmp_path)
    loaded = load_state(tmp_path)
    assert loaded.roles_finalized == {"r1", "r2"}


def test_load_seeds_roles_finalized_for_completed_migration(tmp_path: Path) -> None:
    """S3 SC-17: a completed migration with no roles_finalized key is seeded from role_map."""
    state = MigrationState(
        completed_at="2026-06-26T00:00:00+00:00", role_map={"r1": "s1", "r2": "s2"}
    )
    save_state(state, tmp_path)
    path = tmp_path / "state.json"
    data = json.loads(path.read_text())
    data.pop("roles_finalized", None)  # emulate an older ferry state file
    path.write_text(json.dumps(data))
    loaded = load_state(tmp_path)
    assert loaded.roles_finalized == {"r1", "r2"}


def test_load_does_not_seed_roles_finalized_without_completed_at(tmp_path: Path) -> None:
    """S3 SC-17 (control): a crashed (not completed) migration is NOT seeded."""
    state = MigrationState(role_map={"r1": "s1"})  # no completed_at
    save_state(state, tmp_path)
    path = tmp_path / "state.json"
    data = json.loads(path.read_text())
    data.pop("roles_finalized", None)
    path.write_text(json.dumps(data))
    loaded = load_state(tmp_path)
    assert loaded.roles_finalized == set()


def test_migration_lock_marker_not_persisted(tmp_path: Path) -> None:
    """S1 SC-18: the transient lock marker is never serialized."""
    state = MigrationState(migration_lock_marker="[FERRY_LOCK:1:h]")
    save_state(state, tmp_path)
    path = tmp_path / "state.json"
    assert "migration_lock_marker" not in json.loads(path.read_text())
    loaded = load_state(tmp_path)
    assert loaded.migration_lock_marker == ""


def test_reaction_counters_round_trip(tmp_path: Path) -> None:
    """SC-20: reactions_capped + reactions_dropped survive a save/load round-trip."""
    state = MigrationState(reactions_capped=5, reactions_dropped=3)
    save_state(state, tmp_path)
    loaded = load_state(tmp_path)
    assert loaded.reactions_capped == 5
    assert loaded.reactions_dropped == 3


def test_reaction_counters_backcompat_missing_keys(tmp_path: Path) -> None:
    """SC-21: an old state.json without the new keys loads with both defaulting to 0."""
    save_state(MigrationState(), tmp_path)
    path = tmp_path / "state.json"
    data = json.loads(path.read_text())
    data.pop("reactions_capped", None)
    data.pop("reactions_dropped", None)
    path.write_text(json.dumps(data), encoding="utf-8")
    loaded = load_state(tmp_path)
    assert loaded.reactions_capped == 0
    assert loaded.reactions_dropped == 0


# ---------------------------------------------------------------------------
# Writer-level redaction -- issue #140, ADR-014
# ---------------------------------------------------------------------------


def test_state_json_masks_a_registered_secret(tmp_path: Path) -> None:
    """state.json is written after every checkpoint and shipped in bug reports."""
    register_secret("proxy_password", "hunter2horse")
    state = MigrationState()
    state.warnings.append({"type": "x", "phase": "structure", "message": "proxy: hunter2horse"})

    save_state(state, tmp_path)

    written = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert "hunter2horse" not in written["warnings"][0]["message"]
    assert written["warnings"][0]["phase"] == "structure"


def test_save_state_keeps_its_two_argument_signature() -> None:
    """Ten call sites pass no config, so none of them can supply a token store.

    Widening the signature would mean touching every phase module. The helper
    reads the process registry instead, which needs no plumbing.
    """
    import inspect

    assert list(inspect.signature(save_state).parameters) == ["state", "output_dir"]


def test_state_warnings_stay_raw_in_memory_after_a_save(tmp_path: Path) -> None:
    """Redaction happens on the way out, never in place.

    22 assertions in test_structure.py read exact warning text from the in-memory
    list. Scrubbing in place would break every one of them.
    """
    register_secret("proxy_password", "hunter2horse")
    state = MigrationState()
    state.warnings.append({"type": "x", "message": "proxy: hunter2horse"})

    save_state(state, tmp_path)

    assert state.warnings[0]["message"] == "proxy: hunter2horse"


def test_identifier_maps_survive_a_secret_that_is_a_substring(tmp_path: Path) -> None:
    """The case earlier revisions of the #140 design failed.

    "12345678" is both a plausible human-chosen proxy password and a substring of a
    real Discord snowflake. SecureTokenStore.sanitize does unbounded substring
    replacement, so scrubbing the whole document rewrote these identifiers. A
    rewritten channel_message_offsets value is a silently wrong resume position.
    """
    register_secret("proxy_password", "12345678")
    state = MigrationState()
    state.role_map = {"1123456789012345678": "01ABCDEF"}
    state.channel_map = {"7712345678901234567": "01MNOPQR"}
    state.channel_message_offsets = {"7712345678901234567": "1123456789012345678"}
    state.channel_high_water = {"7712345678901234567": "1123456789012345678"}
    state.current_phase = "structure"

    save_state(state, tmp_path)

    written = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert written["role_map"] == state.role_map
    assert written["channel_map"] == state.channel_map
    assert written["channel_message_offsets"] == state.channel_message_offsets
    assert written["channel_high_water"] == state.channel_high_water
    assert written["current_phase"] == "structure"


def test_message_map_json_is_untouched(tmp_path: Path) -> None:
    """message_map holds identifier pairs and no free text, so it is never walked.

    Excluding it also removes the cost: scrubbing it measured ~290ms at 100k entries
    against ~3ms for everything else, on a path that runs every 50 messages.
    """
    register_secret("proxy_password", "12345678")
    state = MigrationState()
    state.message_map = {"1123456789012345678": "01ABCDEF12345678901234567"}

    save_state(state, tmp_path)

    written = json.loads((tmp_path / "message_map.json").read_text(encoding="utf-8"))
    assert written == state.message_map


def test_save_load_save_is_byte_identical(tmp_path: Path) -> None:
    """A resumed run loads already-scrubbed warnings and writes them again."""
    register_secret("proxy_password", "hunter2horse")
    state = MigrationState()
    state.warnings.append({"type": "x", "message": "proxy: hunter2horse"})
    state.role_map = {"111": "01A"}
    state.current_phase = "structure"

    save_state(state, tmp_path)
    first = (tmp_path / "state.json").read_bytes()
    save_state(load_state(tmp_path), tmp_path)
    second = (tmp_path / "state.json").read_bytes()

    assert first == second


def test_rollback_failure_error_is_masked_in_state_json(tmp_path: Path) -> None:
    """Closes a gap that is live in shipped code, not a hypothetical one.

    The engine's five rollback-failure sites build error=_safe(config, str(exc)), and
    _safe consults only config.token_store. The proxy password lives in the process
    registry, which that store never reads, so it reaches state.json unmasked today.
    """
    register_secret("proxy_password", "hunter2horse")
    state = MigrationState()
    state.rollback_progress = RollbackProgress(
        channels_deleted=3,
        failures=[
            RollbackFailure(
                entity_type="channel",
                stoat_id="01ABCDEF",
                error="delete failed: proxy rejected hunter2horse",
                http_status=500,
            )
        ],
    )

    save_state(state, tmp_path)

    written = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    failure = written["rollback_progress"]["failures"][0]
    assert "hunter2horse" not in failure["error"]
    assert failure["entity_type"] == "channel"
    assert failure["stoat_id"] == "01ABCDEF"
    assert failure["http_status"] == 500
    assert written["rollback_progress"]["channels_deleted"] == 3


def test_failed_message_members_are_masked_in_state_json(tmp_path: Path) -> None:
    """failed_messages carries both an exception and a slice of message content."""
    register_secret("proxy_password", "hunter2horse")
    state = MigrationState()
    state.failed_messages.append(
        FailedMessage(
            discord_msg_id="1123456789012345678",
            stoat_channel_id="01ABC",
            error="send failed: hunter2horse",
            retry_count=2,
            content_preview="hi hunter2horse",
        )
    )

    save_state(state, tmp_path)

    written = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    failed = written["failed_messages"][0]
    assert "hunter2horse" not in failed["error"]
    assert "hunter2horse" not in failed["content_preview"]
    assert failed["discord_msg_id"] == "1123456789012345678"
    assert failed["retry_count"] == 2


# ---------------------------------------------------------------------------
# The field-classification guard -- issue #140, ADR-014
# ---------------------------------------------------------------------------
#
# Redaction of state.json and report.json is scoped to named fields, which keeps it
# from ever touching an identifier. The cost of that choice is that a new free-text
# field would go unprotected. These three tests convert that from a silent runtime gap
# into a failing build, at each level where a new field can appear.

# Every top-level key state.json carries. Do not add a key here without deciding
# whether it can hold free text: if it can, add it to _TEXT_MEMBERS in
# core/security.py at the same time, or it is written unredacted.
_KNOWN_STATE_FIELDS = frozenset(
    {
        "role_map",
        "channel_map",
        "category_map",
        "category_names",
        # All three below are STRUCTURAL, not free text, and the reason differs
        # per field rather than being one blanket claim.
        #
        # created_channel_names / created_role_names hold attacker-influenced
        # text, since a Discord user chooses a channel name. They are still
        # structural, because what _TEXT_MEMBERS guards is a FERRY SECRET
        # reaching disk through an exception repr, and a name never travels that
        # path.
        #
        # Listing them here would in fact be INERT rather than harmful, measured
        # by mutation rather than assumed: scrub_document descends one level into
        # LISTS of dicts, and these are dict[str, str], so _mask_entries returns
        # them untouched. Structural is still the correct classification, on the
        # reason above. See
        # test_a_recorded_name_containing_a_secret_is_not_rewritten for what does
        # threaten them, which is a future widening of scrub_document itself.
        #
        # thread_strategy is validated against a closed set of three values at
        # the CLI boundary by click.Choice. That does NOT hold for the GUI, which
        # builds its config from a storage file it does not re-validate, which is
        # why messages.py falls back to "flatten" when the value is not in
        # _THREAD_STRATEGIES. The widest value it can hold is a short string from
        # a local user's own storage file, and it never carries a Ferry secret.
        "created_channel_names",
        "created_role_names",
        "thread_strategy",
        "channel_categories",
        "message_map",
        "emoji_map",
        # Structural: identifiers only (discord id -> {old,new} stoat ids),
        # never a Ferry secret, so it stays out of _TEXT_MEMBERS.
        "pending_emoji_rewrites",
        "avatar_cache",
        "upload_cache",
        "author_names",
        "pending_pins",
        "pending_reactions",
        "errors",
        "warnings",
        "stoat_server_id",
        "autumn_url",
        "current_phase",
        "completed_channel_ids",
        "channel_message_offsets",
        "channel_high_water",
        "attachments_uploaded",
        "attachments_skipped",
        "reactions_applied",
        "reactions_capped",
        "reactions_dropped",
        "pins_applied",
        "started_at",
        "completed_at",
        "is_dry_run",
        "export_completed",
        "autumn_uploads",
        "referenced_autumn_ids",
        "roles_finalized",
        "failed_messages",
        "validation_results",
        "prior_messages_total",
        "source_messages_total",
        "forum_channel_members",
        "forum_category_names",
        "channel_message_counts",
        "reaction_message_counts",
        "forum_index_message_ids",
        "forum_index_present_unknown_id",
        "embeds_total",
        "embeds_dropped",
        "replies_linked",
        "replies_total",
        "rollback_progress",
        "invite_code",
        "invite_url",
        "native_fidelity_counts",
    }
)


def test_state_document_has_no_unclassified_fields() -> None:
    """Level 1: a brand new top-level field fails here until it is classified."""
    from discord_ferry.state import _state_to_dict

    emitted = set(_state_to_dict(MigrationState()))

    assert emitted - _KNOWN_STATE_FIELDS == set(), (
        "a new state.json field appeared. Decide whether it can hold free text: if it "
        "can, add it to _TEXT_MEMBERS in core/security.py, then list it here"
    )
    assert _KNOWN_STATE_FIELDS - emitted == set(), (
        "a state.json field was removed; update this list"
    )


def test_pending_emoji_rewrites_round_trips(tmp_path: Path) -> None:
    """SC-ST.1: the resume record survives save then load."""
    state = MigrationState(pending_emoji_rewrites={"d_emo": {"old": "a", "new": "b"}})
    save_state(state, tmp_path)
    loaded = load_state(tmp_path)
    assert loaded.pending_emoji_rewrites == {"d_emo": {"old": "a", "new": "b"}}


def test_pending_emoji_rewrites_defaults_empty(tmp_path: Path) -> None:
    """SC-ST.1: a state file written before the field existed loads it as {}."""
    save_state(MigrationState(), tmp_path)
    raw = json.loads((tmp_path / "state.json").read_text())
    del raw["pending_emoji_rewrites"]
    (tmp_path / "state.json").write_text(json.dumps(raw))
    assert load_state(tmp_path).pending_emoji_rewrites == {}


def test_forum_index_present_unknown_id_round_trips(tmp_path: Path) -> None:
    """SC-2.2: the #215 marker survives save then load, as a set."""
    state = MigrationState(forum_index_present_unknown_id={"forum-news"})
    save_state(state, tmp_path)
    assert load_state(tmp_path).forum_index_present_unknown_id == {"forum-news"}


def test_forum_index_present_unknown_id_defaults_empty(tmp_path: Path) -> None:
    """SC-2.3: a state file written before the field existed loads it as an empty set."""
    save_state(MigrationState(), tmp_path)
    raw = json.loads((tmp_path / "state.json").read_text())
    del raw["forum_index_present_unknown_id"]
    (tmp_path / "state.json").write_text(json.dumps(raw))
    assert load_state(tmp_path).forum_index_present_unknown_id == set()


def test_failed_message_fields_are_classified() -> None:
    """Level 3: adding a dataclass field fails here, at the point of definition.

    This is the level that makes classification close to automatic, rather than
    waiting for someone to notice a new member reached a document.
    """
    import dataclasses

    assert {f.name for f in dataclasses.fields(FailedMessage)} == {
        "discord_msg_id",
        "stoat_channel_id",
        "error",
        "retry_count",
        "content_preview",
    }, "FailedMessage changed. If the new field holds free text, add it to _TEXT_MEMBERS"


def test_rollback_failure_fields_are_classified() -> None:
    """Level 3 again, for the sibling that went unnoticed until critique round 3.

    RollbackFailure.error is populated with exception text at five engine sites and
    was missing from the first three revisions of the field list.
    """
    import dataclasses

    assert {f.name for f in dataclasses.fields(RollbackFailure)} == {
        "entity_type",
        "stoat_id",
        "error",
        "http_status",
    }, "RollbackFailure changed. If the new field holds free text, add it to _TEXT_MEMBERS"


def test_text_member_map_matches_its_documented_shape() -> None:
    """A documentation pin, NOT a guard. The guard is the AST sweep below.

    This asserts the module constant against a literal, so it catches an accidental
    edit to _TEXT_MEMBERS and nothing else. It cannot catch a new append site using a
    new member key, because it never looks at an append site. That distinction was
    missed when this test was written and caught by the whole-branch review.
    """
    assert _TEXT_MEMBERS["warnings"] == frozenset({"message"})
    assert _TEXT_MEMBERS["errors"] == frozenset({"message", "error"})
    assert _TEXT_MEMBERS["failed_messages"] == frozenset({"error", "content_preview"})


def test_warning_and_error_append_sites_use_only_classified_member_keys() -> None:
    """Level 2, done by observation rather than by pinning a constant.

    The first version of this test asserted _TEXT_MEMBERS against a hardcoded literal,
    which is the module's own constant compared to itself. It could not fail against
    the defect it named: a new append site introducing a new member key would pass it
    cleanly. Caught by the whole-branch review.

    This walks the real source instead. Every dict literal appended to state.warnings
    or state.errors anywhere in src/ contributes its keys, and any key that is neither
    a known structural key nor a classified text member fails the build.
    """
    import ast

    src_root = Path(__file__).resolve().parents[1] / "src" / "discord_ferry"

    # Keys that carry no free text. A new one here is a deliberate decision.
    structural = {"phase", "type", "nsfw", "count", "channel"}
    classified = _TEXT_MEMBERS["warnings"] | _TEXT_MEMBERS["errors"]

    observed: dict[str, set[str]] = {}
    for path in sorted(src_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "append" or not isinstance(node.func.value, ast.Attribute):
                continue
            target = node.func.value.attr
            if target not in {"warnings", "errors"} or not node.args:
                continue
            arg = node.args[0]
            if not isinstance(arg, ast.Dict):
                continue
            for key in arg.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    observed.setdefault(key.value, set()).add(f"{path.name}:{node.lineno}")

    assert observed, "the sweep found no append sites, so it cannot be guarding anything"

    unclassified = {k: sorted(v) for k, v in observed.items() if k not in structural | classified}
    assert unclassified == {}, (
        "an append site uses a member key that is neither structural nor classified as "
        f"free text: {unclassified}. If it can hold an exception or user text, add it to "
        "_TEXT_MEMBERS in core/security.py; if not, add it to `structural` here"
    )


def test_every_classified_text_member_is_actually_used() -> None:
    """The mirror of the sweep: a classified key nobody appends is dead configuration.

    Without this, _TEXT_MEMBERS could accumulate stale entries that read as coverage.
    failed_messages members are excluded: they come from a dataclass, not a dict
    literal, and are guarded by test_failed_message_fields_are_classified.
    """
    import ast

    src_root = Path(__file__).resolve().parents[1] / "src" / "discord_ferry"
    observed: set[str] = set()
    for path in sorted(src_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr in {"warnings", "errors"}
                and node.args
                and isinstance(node.args[0], ast.Dict)
            ):
                observed |= {
                    k.value
                    for k in node.args[0].keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }

    classified = _TEXT_MEMBERS["warnings"] | _TEXT_MEMBERS["errors"]
    assert classified <= observed, (
        "_TEXT_MEMBERS classifies a member key no append site uses: "
        f"{sorted(classified - observed)}"
    )


def test_save_state_overwrites_existing_files_on_windows(
    windows_filesystem: None, tmp_path: Path
) -> None:
    """Issue #172: a second run into an existing output directory must not crash.

    One test covers both swap sites. save_state has no branch between them
    (state.py:198-214), so reverting the message_map swap raises on the first and
    reverting the state.json swap raises on the second.
    """
    save_state(MigrationState(stoat_server_id="first"), tmp_path)
    save_state(MigrationState(stoat_server_id="second"), tmp_path)

    assert load_state(tmp_path).stoat_server_id == "second"
    assert (tmp_path / "message_map.json").exists()


def test_save_state_first_write_succeeds_on_windows(
    windows_filesystem: None, tmp_path: Path
) -> None:
    """The fixture must only refuse an EXISTING destination.

    If it refused unconditionally, a genuine first-run break would look identical
    to the bug under test and this suite could not tell them apart.
    """
    save_state(MigrationState(stoat_server_id="only"), tmp_path)

    assert load_state(tmp_path).stoat_server_id == "only"


def test_repeated_checkpoints_overwrite_on_windows(
    windows_filesystem: None, tmp_path: Path
) -> None:
    """save_state runs every checkpoint_interval messages, 50 by default."""
    for n in range(5):
        save_state(MigrationState(stoat_server_id=f"run-{n}"), tmp_path)

    assert load_state(tmp_path).stoat_server_id == "run-4"


def test_contract_fields_roundtrip(tmp_path: Path) -> None:
    """thread_strategy and the two name maps survive save/load.

    The name maps record what Ferry SENT to Stoat, never the Discord name, so a
    fixture here uses a value that has already been through truncation.
    """
    state = MigrationState(
        thread_strategy="merge",
        created_channel_names={"d-100": "general"},
        created_role_names={"d-role-1": "mods"},
    )
    save_state(state, tmp_path)

    loaded = load_state(tmp_path)
    assert loaded.thread_strategy == "merge"
    assert loaded.created_channel_names == {"d-100": "general"}
    assert loaded.created_role_names == {"d-role-1": "mods"}


def test_contract_fields_default_when_absent(tmp_path: Path) -> None:
    """A state.json written before 2.17.0 loads with safe defaults.

    The empty string is NOT the same claim as "flatten": it means no strategy was
    recorded, which is what every migration predating the field will report. A
    default of "flatten" would assert a strategy that was never chosen.
    """
    minimal = {"stoat_server_id": "s", "current_phase": "report"}
    (tmp_path / "state.json").write_text(json.dumps(minimal), encoding="utf-8")

    loaded = load_state(tmp_path)
    assert loaded.thread_strategy == ""
    assert loaded.created_channel_names == {}
    assert loaded.created_role_names == {}


def test_a_recorded_name_containing_a_secret_is_not_rewritten() -> None:
    """SC-5.4. A recorded name reaches disk byte-identical, so the rename
    comparison in verify.py compares like with like.

    WHAT THIS DOES NOT GUARD, corrected after running the mutation rather than
    reasoning about it. Adding created_channel_names to _TEXT_MEMBERS does
    NOTHING: scrub_document's _mask_entries returns early on anything that is not
    a list, and this field is a dict[str, str]. The mutant survives. The spec and
    design for this change both claimed otherwise, through two review rounds.

    WHAT IT DOES GUARD is the widening ADR-014 actually warns against. If
    scrub_document ever descends into dicts, SecureTokenStore.sanitize's
    unbounded substring replacement rewrites any recorded name containing a
    registered secret, verify.py then compares a scrubbed string against a live
    one, and ferry check reports a rename nobody made. That is the same failure
    shape ADR-014 measured for channel_message_offsets, where a rewritten value
    is a silently wrong resume position.

    THE POSITIVE CONTROL IS NOT OPTIONAL. The first assertion says a value is
    unchanged, which passes just as well against a registry that never received
    the secret. The second fails in that case, so the two together distinguish
    "correctly left alone" from "nothing was scrubbed at all".
    """
    from discord_ferry.core.security import scrub_document
    from discord_ferry.state import _state_to_dict

    register_secret("proxy_password", "swordfish")
    state = MigrationState(created_channel_names={"d-100": "team-swordfish-chat"})
    state.warnings.append({"type": "x", "phase": "structure", "message": "proxy: swordfish"})

    doc = scrub_document(_state_to_dict(state))

    # The subject: a structural field is passed through untouched.
    assert doc["created_channel_names"]["d-100"] == "team-swordfish-chat"
    # The positive control: the same call DID scrub a classified field, so the
    # assertion above cannot be passing because the registry was empty.
    assert "swordfish" not in doc["warnings"][0]["message"]


def test_every_integer_counter_starts_at_zero() -> None:
    """Every `int` field declared with a zero default must actually be zero.

    Found by the first mutation sweep: changing `retry_count: int = 0` to `= 1`
    left all 64 tests in this file passing. Around thirty of that run's
    ninety-six survivors were this one shape, across three dataclasses.

    A counter that starts at one is not a crash. It is a silently wrong number in
    every total the migration reports, and the kind of defect nobody writes a
    single test for. One introspective assertion kills the whole class and covers
    fields added later without anyone remembering to.

    Derived from the dataclass fields rather than a hardcoded list on purpose: a
    literal list drifts the moment a field is added, and the emptiness guard below
    means a refactor that removes the fields fails here instead of turning this
    into a test that checks nothing.
    """
    import dataclasses

    from discord_ferry.migrator.messages import ChannelResult

    # ChannelResult is here because the ship audit went looking for the same shape
    # elsewhere and found it: eight counters, all defaulting to zero, feeding the
    # same totals. Twenty other dataclasses also carry int or bool fields and are
    # deliberately absent. FerryConfig is the clearest reason why: `max_channels =
    # 200` and `create_invite = True` are correct non-zero defaults, so the
    # invariant is "counters start at zero", not "every int is zero".
    #
    # ReviewSummary looked like a candidate and is not one: all nine of its fields
    # are required, so it has no defaults to assert. A check over zero fields
    # passes for the wrong reason.
    cases = [
        MigrationState(),
        RollbackProgress(),
        FailedMessage(discord_msg_id="1", stoat_channel_id="2", error="boom"),
        ChannelResult(),
    ]

    bools_checked = 0

    for instance in cases:
        cls = type(instance)
        # Select by TYPE, assert on VALUE. Selecting by `default == 0` and then
        # asserting the value is 0 is circular: change a default to 1 and the
        # field drops out of the selection, so nothing is checked and the test
        # passes. The first version of this test did exactly that, and the
        # mutation run that was meant to prove it caught only the one class where
        # the emptiness guard below happened to fire.
        # `("int", int)` is not belt and braces, it is required, and narrowing it
        # to `f.type is int` would silently drop coverage rather than fail. The
        # modules disagree on PEP 563: state.py has no `from __future__ import
        # annotations`, so its field types are real type objects, while
        # messages.py has it, so ChannelResult's are the strings "int" and "bool".
        # Measured: `f.type is int` matches 0 of ChannelResult's 8 int fields.
        int_fields = [f.name for f in dataclasses.fields(cls) if f.type in ("int", int)]
        assert int_fields, (
            f"{cls.__name__} declares no int fields. If that is deliberate, drop it "
            "from this test; otherwise this check has stopped checking anything."
        )

        for name in int_fields:
            actual = getattr(instance, name)
            assert actual == 0, (
                f"{cls.__name__}.{name} is an int field whose fresh-instance value "
                f"is {actual!r}, not 0. Every int on these documents is a counter, "
                "and one that does not start at zero misreports every total derived "
                "from it. If a non-zero default is genuinely wanted, this test is "
                "the place to say so explicitly."
            )

        # Same invariant, same reasoning, one type over. A flag that starts True
        # makes the migration believe it already did something it has not done:
        # `categories_cleaned` and `is_dry_run` both gate real work.
        for field_ in dataclasses.fields(cls):
            if field_.type not in ("bool", bool):
                continue
            bools_checked += 1
            actual = getattr(instance, field_.name)
            assert actual is False, (
                f"{cls.__name__}.{field_.name} is a bool field whose fresh-instance "
                f"value is {actual!r}, not False. These flags record what has already "
                "happened, so one that starts True skips the work it guards."
            )

    # The bool loop gets an aggregate guard rather than a per-class one, because
    # FailedMessage and ChannelResult legitimately have no bool fields and a
    # per-class assertion would fail on them today. Without this the bool half
    # could quietly drop to zero iterations everywhere and still pass, which is
    # the exact failure the int half's guard exists to prevent. Three bool fields
    # are checked at the time of writing.
    assert bools_checked, (
        "no bool field was checked on any of the cases above. The bool half of "
        "this test has stopped checking anything."
    )


def test_role_ordering_warnings_are_classified_for_redaction() -> None:
    """SC-I4. The two role-ordering warning types keep their structure and lose their secret.

    #380 adds `role_ordering_failed` and `role_ordering_not_permitted`. Both use
    the existing phase/type/message keys, so `_TEXT_MEMBERS["warnings"]` needs no
    new member. This asserts that rather than reasoning about it: a new member key
    on a warnings entry is invisible to the writer until it is classified, which is
    the failure ADR-014 exists to prevent.

    THE POSITIVE CONTROL MATTERS. The type/phase assertions pass just as well
    against a registry that never received the secret, so the message assertion is
    what distinguishes "structure preserved" from "nothing was scrubbed at all".
    """
    from discord_ferry.core.security import scrub_document
    from discord_ferry.state import _state_to_dict

    register_secret("proxy_password", "hunter2horse")
    state = MigrationState()
    state.warnings.append(
        {
            "phase": "roles",
            "type": "role_ordering_not_permitted",
            "message": "Role ordering was refused, proxy hunter2horse in the text",
        }
    )
    state.warnings.append(
        {
            "phase": "roles",
            "type": "role_ordering_failed",
            "message": "Failed to apply role ordering: proxy hunter2horse",
        }
    )

    doc = scrub_document(_state_to_dict(state))

    # Structure survives: the machine-readable discriminators are untouched.
    assert [w["type"] for w in doc["warnings"]] == [
        "role_ordering_not_permitted",
        "role_ordering_failed",
    ]
    assert all(w["phase"] == "roles" for w in doc["warnings"])
    # The positive control: free text was actually scrubbed.
    assert all("hunter2horse" not in w["message"] for w in doc["warnings"])
