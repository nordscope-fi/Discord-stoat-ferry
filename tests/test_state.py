"""Tests for migration state persistence."""

from pathlib import Path

import pytest

from discord_ferry.errors import StateError
from discord_ferry.state import MigrationState, load_state, save_state


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
