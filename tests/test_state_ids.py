"""Saved destination ids are checked before resume and recovery use them (#970).

Every recovery path (--resume, retry, rollback, repair, check) reads Stoat ids out of
state.json. These tests cover the shape check in ``load_state``, the binding checks in the
paths that replay saved ids, and that an older, valid state still loads unchanged.

A state that fails here was written by hand or by a different Ferry run: nothing in this
file describes an attacker. The point is a clear error, and no request to the wrong place.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from aioresponses import aioresponses
from click.testing import CliRunner
from yarl import URL

from discord_ferry.cli import main
from discord_ferry.config import FerryConfig
from discord_ferry.core.engine import (
    _owned_pending_rewrites,
    _run_emoji_repair_pass,
    run_retry_failed,
    run_rollback,
)
from discord_ferry.errors import StateError
from discord_ferry.migrator.pins import run_pins
from discord_ferry.migrator.reactions import run_reactions
from discord_ferry.migrator.verify import RepairOutcome
from discord_ferry.state import FailedMessage, MigrationState, load_state, save_state
from tests.stoat_ids import SERVER_ID, stoat_id
from tests.test_engine import retry_permission_unit_boundary as retry_permission_unit_boundary

if TYPE_CHECKING:
    from discord_ferry.core.events import MigrationEvent

FIXTURES_DIR = Path(__file__).parent / "fixtures"
BASE_URL = "https://api.test"

CH_A = stoat_id(101)
CH_B = stoat_id(102)
MSG = stoat_id(301)


def _write(tmp_path: Path, **fields: Any) -> Path:
    """Write a state.json holding a valid base plus the given fields."""
    data: dict[str, Any] = {"stoat_server_id": SERVER_ID, **fields}
    (tmp_path / "state.json").write_text(json.dumps(data), encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# Shape: a malformed id in each place is refused, and the error names the field
# ---------------------------------------------------------------------------

_BAD = "not-an-id"


@pytest.mark.parametrize(
    ("fields", "field_name"),
    [
        ({"stoat_server_id": _BAD}, "stoat_server_id"),
        ({"role_map": {"111": _BAD}}, "role_map"),
        ({"channel_map": {"111": _BAD}}, "channel_map"),
        ({"category_map": {"111": "has space"}}, "category_map"),
        ({"category_map": {"111": "x" * 33}}, "category_map"),
        ({"emoji_map": {"111": "../servers"}}, "emoji_map"),
        ({"emoji_map": {"111": "a/b"}}, "emoji_map"),
        ({"message_map": {"111": _BAD}}, "message_map"),
        ({"forum_index_message_ids": {"forum-news": _BAD}}, "forum_index_message_ids"),
        (
            {
                "failed_messages": [
                    {"discord_msg_id": "111", "stoat_channel_id": _BAD, "error": "x"}
                ]
            },
            "failed_messages[0].stoat_channel_id",
        ),
        ({"pending_pins": [[_BAD, MSG]]}, "pending_pins[0] channel"),
        ({"pending_pins": [[CH_A, _BAD]]}, "pending_pins[0] message"),
        (
            {"pending_reactions": [{"channel_id": _BAD, "message_id": MSG, "emoji": "x"}]},
            "pending_reactions[0].channel_id",
        ),
        (
            {"pending_reactions": [{"channel_id": CH_A, "message_id": _BAD, "emoji": "x"}]},
            "pending_reactions[0].message_id",
        ),
        (
            {"pending_emoji_rewrites": {"111": {"old": "a", "new": "../b"}}},
            "pending_emoji_rewrites['111'].new",
        ),
    ],
)
def test_a_malformed_id_is_refused_and_the_field_is_named(
    tmp_path: Path, fields: dict[str, Any], field_name: str
) -> None:
    _write(tmp_path, **fields)
    with pytest.raises(StateError) as excinfo:
        load_state(tmp_path)
    assert field_name in str(excinfo.value)


@pytest.mark.parametrize(
    "key",
    ["abc", "1.5", "-1", "1" * 21, "", "forum-index-"],
)
def test_a_malformed_discord_key_is_refused(tmp_path: Path, key: str) -> None:
    _write(tmp_path, channel_map={key: CH_A})
    with pytest.raises(StateError, match="channel_map"):
        load_state(tmp_path)


def test_the_synthetic_keys_ferry_writes_are_allowed(tmp_path: Path) -> None:
    _write(
        tmp_path,
        channel_map={"111": CH_A, "forum-index-forum-news": CH_B},
        category_map={"forum-news": stoat_id(1)[:26], "222": "cat_ok-1"},
        forum_index_message_ids={"forum-news": MSG},
    )
    loaded = load_state(tmp_path)
    assert loaded.channel_map["forum-index-forum-news"] == CH_B
    assert loaded.category_map["forum-news"] == stoat_id(1)[:26]


@pytest.mark.parametrize("junk", [True, 7, None, ["x"], {"a": "b"}, 1.5])
@pytest.mark.parametrize("where", ["role_map", "channel_map", "emoji_map", "message_map"])
def test_a_value_of_the_wrong_type_is_refused(tmp_path: Path, where: str, junk: Any) -> None:
    _write(tmp_path, **{where: {"111": junk}})
    with pytest.raises(StateError, match=where):
        load_state(tmp_path)


@pytest.mark.parametrize("where", ["role_map", "channel_map", "category_map", "emoji_map"])
def test_a_map_that_is_not_an_object_is_refused(tmp_path: Path, where: str) -> None:
    _write(tmp_path, **{where: ["111", CH_A]})
    with pytest.raises(StateError, match=where):
        load_state(tmp_path)


def test_a_state_that_is_not_an_object_is_refused(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(StateError, match="JSON object"):
        load_state(tmp_path)


@pytest.mark.parametrize(
    "fields",
    [
        {"failed_messages": ["nope"]},
        {"failed_messages": [{"unexpected": 1}]},
        {"rollback_progress": "nope"},
        {"rollback_progress": {"failures": ["nope"]}},
        {"pending_pins": [5]},
        {"pending_pins": [{"a": 1, "b": 2}]},
        {"pending_reactions": [5]},
        {"pending_emoji_rewrites": {"111": "nope"}},
        {"completed_channel_ids": [[1]]},
    ],
)
def test_wrongly_shaped_records_raise_state_error_not_a_traceback(
    tmp_path: Path, fields: dict[str, Any]
) -> None:
    _write(tmp_path, **fields)
    with pytest.raises(StateError):
        load_state(tmp_path)


def test_the_error_counts_what_else_is_wrong_and_stays_short(tmp_path: Path) -> None:
    _write(tmp_path, channel_map={str(i): _BAD for i in range(5000)})
    with pytest.raises(StateError) as excinfo:
        load_state(tmp_path)
    assert "and 4999 more" in str(excinfo.value)
    assert len(str(excinfo.value)) < 300


def test_a_long_bad_value_is_truncated_in_the_message(tmp_path: Path) -> None:
    _write(tmp_path, role_map={"111": "x" * 5000})
    with pytest.raises(StateError) as excinfo:
        load_state(tmp_path)
    assert len(str(excinfo.value)) < 300


def test_a_bad_message_map_file_is_refused_like_a_bad_state(tmp_path: Path) -> None:
    _write(tmp_path)
    (tmp_path / "message_map.json").write_text(json.dumps({"111": _BAD}), encoding="utf-8")
    with pytest.raises(StateError, match="message_map"):
        load_state(tmp_path)


def test_a_dry_run_state_may_hold_its_placeholders(tmp_path: Path) -> None:
    placeholders = {
        "is_dry_run": True,
        "channel_map": {"111": "dry-ch-111"},
        "role_map": {"222": "dry-role-222"},
        "category_map": {"333": "dry-cat-333"},
        "emoji_map": {"444": "dry-emoji-444"},
        "message_map": {"555": "dry-msg-555"},
    }
    _write(tmp_path, **placeholders)
    assert load_state(tmp_path).is_dry_run is True


def test_a_real_state_may_not_hold_a_dry_run_placeholder(tmp_path: Path) -> None:
    _write(tmp_path, channel_map={"111": "dry-ch-111"})
    with pytest.raises(StateError, match="channel_map"):
        load_state(tmp_path)


def test_a_saved_state_round_trips(tmp_path: Path) -> None:
    state = MigrationState(
        stoat_server_id=SERVER_ID,
        channel_map={"111": CH_A},
        message_map={"555": MSG},
        failed_messages=[FailedMessage("555", CH_A, "boom")],
        pending_pins=[(CH_A, MSG)],
        pending_reactions=[{"channel_id": CH_A, "message_id": MSG, "emoji": "x"}],
    )
    save_state(state, tmp_path)
    loaded = load_state(tmp_path)
    assert loaded.failed_messages[0].stoat_channel_id == CH_A
    assert loaded.pending_pins == [(CH_A, MSG)]


def test_an_older_state_from_the_fixtures_loads_unchanged(tmp_path: Path) -> None:
    """rollback_state.json is a v1.7.0-shaped file: no thread_strategy, no name maps."""
    fixture = FIXTURES_DIR / "state" / "rollback_state.json"
    raw = json.loads(fixture.read_text(encoding="utf-8"))
    shutil.copy(fixture, tmp_path / "state.json")
    loaded = load_state(tmp_path)
    assert loaded.stoat_server_id == raw["stoat_server_id"]
    assert loaded.channel_map == raw["channel_map"]
    assert loaded.role_map == raw["role_map"]
    assert loaded.emoji_map == raw["emoji_map"]
    assert loaded.category_map == raw["category_map"]
    assert loaded.thread_strategy == ""


# ---------------------------------------------------------------------------
# Entry points turn the error into a sentence, not a traceback
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _bad_state_dir(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    out.mkdir()
    _write(out, channel_map={"111": _BAD})
    return out


@pytest.mark.parametrize(
    ("command", "extra", "code"),
    [
        ("rollback", ["--yes"], 2),
        ("retry", [], 2),
        ("repair", [], 2),
        ("check", [], 1),
    ],
)
def test_recovery_commands_refuse_a_malformed_state_with_a_sentence(
    runner: CliRunner, tmp_path: Path, command: str, extra: list[str], code: int
) -> None:
    out = _bad_state_dir(tmp_path)
    export_dir = tmp_path / "exp"
    export_dir.mkdir()
    argv = [command, "--stoat-url", "https://api.test", "--token", "t", *extra]
    if command == "rollback":
        argv += ["--output-dir", str(out)]
    else:
        argv.insert(1, str(out))
    if command in {"retry", "repair"}:
        argv += ["--export-dir", str(export_dir)]
    result = runner.invoke(main, argv)
    assert isinstance(result.exception, SystemExit), result.output
    assert result.exit_code == code, result.output
    assert "channel_map" in result.output
    assert "Traceback" not in result.output


def test_resume_refuses_a_malformed_state_with_a_sentence(
    runner: CliRunner, tmp_path: Path
) -> None:
    out = _bad_state_dir(tmp_path)
    result = runner.invoke(
        main,
        [
            "migrate",
            "--export-dir",
            str(FIXTURES_DIR),
            "--output-dir",
            str(out),
            "--stoat-url",
            "https://api.test",
            "--token",
            "t",
            "--resume",
        ],
    )
    assert isinstance(result.exception, SystemExit), result.output
    assert result.exit_code == 1
    assert "channel_map" in result.output


# ---------------------------------------------------------------------------
# Binding: retry
# ---------------------------------------------------------------------------


def _retry_config(tmp_path: Path) -> FerryConfig:
    export_dir = tmp_path / "exports"
    export_dir.mkdir(exist_ok=True)
    return FerryConfig(
        export_dir=export_dir,
        stoat_url=BASE_URL,
        token="test-token",
        output_dir=tmp_path / "output",
        message_rate_limit=0.0,
        upload_delay=0.0,
    )


def _one_message_export(config: FerryConfig, msg_id: str) -> list[Any]:
    from discord_ferry.parser.dce_parser import parse_export_directory
    from tests.test_engine import _dce_msg_dict, _write_dce_json

    _write_dce_json(config.export_dir, "111", [_dce_msg_dict(msg_id)])
    return parse_export_directory(config.export_dir, metadata_only=True)


@pytest.mark.usefixtures("retry_permission_unit_boundary")
async def test_retry_does_not_send_to_a_channel_this_state_did_not_create(tmp_path: Path) -> None:
    config = _retry_config(tmp_path)
    exports = _one_message_export(config, "555")
    state = MigrationState(
        channel_map={"111": CH_A},
        failed_messages=[FailedMessage("555", CH_B, "timeout")],  # CH_B is not mapped
    )

    events: list[MigrationEvent] = []
    process = AsyncMock()
    with patch("discord_ferry.core.engine._process_message", new=process):
        await run_retry_failed(config, state, exports, events.append)

    process.assert_not_awaited()
    assert [fm.stoat_channel_id for fm in state.failed_messages] == [CH_B]  # kept, not lost
    assert state.failed_messages[0].retry_count == 0
    assert any(e.status == "warning" and CH_B in e.message for e in events)
    assert any(w["type"] == "retry_unmapped_channel" for w in state.warnings)


@pytest.mark.usefixtures("retry_permission_unit_boundary")
async def test_retry_still_sends_to_a_mapped_channel(tmp_path: Path) -> None:
    config = _retry_config(tmp_path)
    exports = _one_message_export(config, "555")
    state = MigrationState(
        channel_map={"111": CH_A},
        failed_messages=[FailedMessage("555", CH_A, "timeout")],
    )

    process = AsyncMock()
    with patch("discord_ferry.core.engine._process_message", new=process):
        await run_retry_failed(config, state, exports, lambda _e: None)

    process.assert_awaited_once()
    assert process.await_args.kwargs["stoat_channel_id"] == CH_A
    assert state.failed_messages == []


# ---------------------------------------------------------------------------
# Binding: rollback only deletes channels the live server lists
# ---------------------------------------------------------------------------


async def test_rollback_does_not_delete_a_channel_the_server_does_not_list(
    tmp_path: Path,
) -> None:
    state = MigrationState(
        stoat_server_id=SERVER_ID,
        channel_map={"111": CH_A, "222": CH_B},  # CH_B is not on this server
    )
    save_state(state, tmp_path)
    config = FerryConfig(
        export_dir=FIXTURES_DIR,
        stoat_url=BASE_URL,
        token="test-token",
        output_dir=tmp_path,
        skip_export=True,
        force_unlock=True,
    )

    events: list[MigrationEvent] = []
    with aioresponses() as m:
        m.get(
            f"{BASE_URL}/servers/{SERVER_ID}",
            payload={"_id": SERVER_ID, "description": "", "channels": [CH_A], "roles": {}},
            repeat=True,
        )
        m.patch(f"{BASE_URL}/servers/{SERVER_ID}", payload={}, repeat=True)
        m.delete(f"{BASE_URL}/channels/{CH_A}", status=204)
        m.delete(f"{BASE_URL}/channels/{CH_B}", status=204)
        result = await run_rollback(config, state, exports=[], on_event=events.append)

    deleted = {str(url) for (method, url) in m.requests if method == "DELETE"}
    assert deleted == {str(URL(f"{BASE_URL}/channels/{CH_A}"))}
    assert result.rollback_progress is not None
    assert result.rollback_progress.channels_deleted == 1
    assert CH_B not in result.rollback_progress.rolled_back_ids
    assert any(e.status == "warning" and CH_B in e.message for e in events)
    # The confirmation preview was built from the same set.
    confirm = next(e for e in events if e.status == "confirm_rollback")
    assert [cid for cid, _ in confirm.detail["summary"].channels_to_delete] == [CH_A]


# ---------------------------------------------------------------------------
# Binding: pins and reactions replayed from state
# ---------------------------------------------------------------------------


def _phase_config(tmp_path: Path) -> FerryConfig:
    return FerryConfig(
        export_dir=Path("/tmp"), stoat_url=BASE_URL, token="test-token", output_dir=tmp_path
    )


async def test_a_pin_for_an_unmapped_channel_is_dropped_with_a_warning(tmp_path: Path) -> None:
    state = MigrationState(
        stoat_server_id=SERVER_ID,
        channel_map={"111": CH_A},
        pending_pins=[(CH_A, MSG), (CH_B, MSG)],
    )
    events: list[MigrationEvent] = []
    pin = AsyncMock(return_value={})
    with (
        patch("discord_ferry.migrator.pins.api_pin_message", new=pin),
        patch("discord_ferry.migrator.pins.asyncio.sleep", new=AsyncMock()),
    ):
        await run_pins(_phase_config(tmp_path), state, [], events.append)

    assert [call.args[3] for call in pin.await_args_list] == [CH_A]
    assert state.pins_applied == 1
    assert state.pending_pins == []
    assert any(e.status == "warning" and CH_B in e.message for e in events)
    assert any(w["type"] == "pin_unmapped_channel" for w in state.warnings)


async def test_a_reaction_for_an_unmapped_channel_is_dropped_with_a_warning(
    tmp_path: Path,
) -> None:
    def reaction(channel: str) -> dict[str, object]:
        return {"channel_id": channel, "message_id": MSG, "emoji": "\U0001f44d"}

    state = MigrationState(
        stoat_server_id=SERVER_ID,
        channel_map={"111": CH_A},
        pending_reactions=[reaction(CH_A), reaction(CH_B)],
    )
    events: list[MigrationEvent] = []
    add = AsyncMock(return_value={})
    with (
        patch("discord_ferry.migrator.reactions.api_add_reaction", new=add),
        patch("discord_ferry.migrator.reactions.asyncio.sleep", new=AsyncMock()),
    ):
        await run_reactions(_phase_config(tmp_path), state, [], events.append)

    assert [call.args[3] for call in add.await_args_list] == [CH_A]
    assert any(w["type"] == "reaction_unbound" for w in state.warnings)


async def test_a_custom_emoji_reaction_must_be_one_the_migration_uploaded(
    tmp_path: Path,
) -> None:
    def reaction(emoji: str) -> dict[str, object]:
        return {"channel_id": CH_A, "message_id": MSG, "emoji": emoji}

    state = MigrationState(
        stoat_server_id=SERVER_ID,
        channel_map={"111": CH_A},
        emoji_map={"444": "ownedEmojiId"},
        pending_reactions=[
            reaction("ownedEmojiId"),  # ours
            reaction("someoneElsesEmoji"),  # a custom id we never uploaded
            reaction("\U0001f44d"),  # Unicode, always allowed
        ],
    )
    add = AsyncMock(return_value={})
    with (
        patch("discord_ferry.migrator.reactions.api_add_reaction", new=add),
        patch("discord_ferry.migrator.reactions.asyncio.sleep", new=AsyncMock()),
    ):
        await run_reactions(_phase_config(tmp_path), state, [], lambda _e: None)

    assert [call.args[5] for call in add.await_args_list] == ["ownedEmojiId", "\U0001f44d"]


# ---------------------------------------------------------------------------
# Binding: the repair emoji resume record
# ---------------------------------------------------------------------------


def test_a_resume_record_is_owned_only_when_emoji_map_points_at_its_new_id() -> None:
    state = MigrationState(
        emoji_map={"444": "newId", "555": "somethingElse"},
        pending_emoji_rewrites={
            "444": {"old": "oldId", "new": "newId"},  # written with the map switch
            "555": {"old": "oldId", "new": "foreignId"},  # names an emoji we never mapped
            "666": {"old": "oldId", "new": "newId"},  # no map entry at all
        },
    )
    owned, declined = _owned_pending_rewrites(state)
    assert set(owned) == {"444"}
    assert len(declined) == 2


async def test_repair_does_not_rewrite_messages_to_an_emoji_it_does_not_own(
    tmp_path: Path,
) -> None:
    state = MigrationState(
        stoat_server_id=SERVER_ID,
        emoji_map={"555": "ourEmoji"},
        pending_emoji_rewrites={"555": {"old": "oldId", "new": "foreignId"}},
    )
    config = _retry_config(tmp_path)
    events: list[MigrationEvent] = []
    rewrite = AsyncMock(return_value=(0, 0, 0))
    with patch("discord_ferry.core.engine._rewrite_emoji_references", new=rewrite):
        await _run_emoji_repair_pass(
            AsyncMock(), config, state, [], [], RepairOutcome(dry_run=False), events.append
        )

    rewrite.assert_not_awaited()
    assert "555" in state.pending_emoji_rewrites  # left for the owner to look at
    assert any(w["type"] == "emoji_rewrite_not_owned" for w in state.warnings)
    assert any(e.status == "warning" for e in events)


@pytest.mark.parametrize(
    "placeholder",
    [
        "dry-ch-111/../../servers/x",
        "dry-ch-111\\x",
        "dry-ch-111?x=1",
        "dry-ch-111#x",
        "dry-ch-%2e%2e",
        "dry-ch-1 1",
        "dry-zzz-111",
        "dry-ch-",
    ],
)
def test_a_dry_run_placeholder_must_be_a_known_kind_and_url_safe(
    tmp_path: Path, placeholder: str
) -> None:
    """A dry-run placeholder is only ever dry-<kind>-<id>; anything else is junk that could
    steer a request path if a recovery command ever used it."""
    _write(tmp_path, is_dry_run=True, channel_map={"111": placeholder})
    with pytest.raises(StateError, match="channel_map"):
        load_state(tmp_path)


async def test_retry_refuses_a_dry_run_state(tmp_path: Path) -> None:
    config = _retry_config(tmp_path)
    state = MigrationState(is_dry_run=True, channel_map={"111": "dry-ch-111"})
    with pytest.raises(StateError, match="dry run"):
        await run_retry_failed(config, state, [], lambda _e: None)


async def test_rollback_refuses_a_dry_run_state(tmp_path: Path) -> None:
    config = _retry_config(tmp_path)
    state = MigrationState(is_dry_run=True, stoat_server_id="dry-server-1")
    with pytest.raises(StateError, match="dry run"):
        await run_rollback(config, state, exports=[], on_event=lambda _e: None)


def test_gui_reports_unreadable_saved_progress_instead_of_treating_it_as_absent(
    tmp_path: Path,
) -> None:
    """#970 review: the migrate page used to suppress a load error and start fresh on its
    own, overwriting a state the stricter checks had refused, with no message."""
    from discord_ferry.gui import _read_previous_state

    _write(tmp_path, channel_map={"111": "not-a-stoat-id"})
    state, problem = _read_previous_state(tmp_path)
    assert state is None
    assert problem is not None and "channel_map" in problem


def test_gui_previous_state_read_handles_absent_and_valid_state(tmp_path: Path) -> None:
    from discord_ferry.gui import _read_previous_state

    assert _read_previous_state(tmp_path) == (None, None)
    _write(tmp_path)
    state, problem = _read_previous_state(tmp_path)
    assert state is not None and problem is None
