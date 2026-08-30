"""Tests for the emoji-repair pass in run_repair (#307)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest

from discord_ferry.config import FerryConfig
from discord_ferry.core.engine import run_repair
from discord_ferry.migrator.verify import CheckReport
from discord_ferry.parser.models import (
    DCEAuthor,
    DCEChannel,
    DCEEmoji,
    DCEExport,
    DCEGuild,
    DCEMessage,
    DCEReaction,
)
from discord_ferry.state import MigrationState

if TYPE_CHECKING:
    from pathlib import Path

EMOJI_ID = "123"
OLD_ID = "old_id"
NEW_ID = "new_id"

_ENGINE = "discord_ferry.core.engine"
_UPLOAD = f"{_ENGINE}.upload_and_create_emoji"
_EDIT = f"{_ENGINE}.api_edit_message"
_CHECK = "discord_ferry.migrator.verify.run_check"


def _config(tmp_path: Path) -> FerryConfig:
    return FerryConfig(
        export_dir=tmp_path,
        stoat_url="https://api.test",
        token="t",
        upload_delay=0.0,
        output_dir=tmp_path,
    )


def _state() -> MigrationState:
    state = MigrationState()
    state.stoat_server_id = "srv"
    state.autumn_url = "https://autumn.test"
    state.emoji_map = {EMOJI_ID: OLD_ID}
    state.channel_map = {"ch1": "stoat_ch"}
    state.message_map = {"m1": "stoat_msg"}
    return state


def _message(content: str = "hi <:smile:123>", *, with_reaction: bool = True) -> DCEMessage:
    reactions = (
        [DCEReaction(emoji=DCEEmoji(id=EMOJI_ID, name="smile", image_url="smile.png"), count=1)]
        if with_reaction
        else []
    )
    return DCEMessage(
        id="m1",
        type="Default",
        timestamp="2024-01-01T00:00:00Z",
        content=content,
        author=DCEAuthor(id="u", name="U"),
        reactions=reactions,
    )


def _export(messages: list[DCEMessage]) -> DCEExport:
    return DCEExport(
        guild=DCEGuild(id="g", name="G", icon_url=""),
        channel=DCEChannel(id="ch1", type=0, category_id="", category="", name="general", topic=""),
        messages=messages,
    )


def _with_image(tmp_path: Path) -> None:
    (tmp_path / "smile.png").write_bytes(b"img")


def _missing_report() -> CheckReport:
    report = CheckReport()
    report.add(
        name=f"emoji:{EMOJI_ID}",
        status="fail",
        kind="emoji_missing",
        detail="the server does not list this emoji",
        discord_id=EMOJI_ID,
        stoat_id=OLD_ID,
    )
    return report


def _present_report() -> CheckReport:
    report = CheckReport()
    report.add(
        name=f"emoji:{EMOJI_ID}",
        status="ok",
        kind="emoji_present",
        detail="emoji exists under its recorded id",
        discord_id=EMOJI_ID,
        stoat_id=NEW_ID,
    )
    return report


async def test_repair_acts_on_emoji_missing(tmp_path: Path) -> None:
    """SC-1.1: repair recreates a missing emoji instead of declining it."""
    _with_image(tmp_path)
    config, state = _config(tmp_path), _state()
    events: list[Any] = []
    with (
        patch(_CHECK, new=AsyncMock(return_value=_missing_report())),
        patch(_UPLOAD, new=AsyncMock(return_value=(NEW_ID, "smile"))) as up,
        patch(_EDIT, new=AsyncMock()),
    ):
        outcome = await run_repair(config, state, [_export([_message()])], events.append)
    up.assert_awaited_once()
    assert not any(d.get("type") == "emoji_missing_media" for d in outcome.declined)
    assert [r["discord_id"] for r in outcome.recreated_emoji] == [EMOJI_ID]
    assert outcome.recreated_emoji[0]["new_id"] == NEW_ID
    assert state.created_emoji_names[EMOJI_ID] == "smile"


async def test_repair_recovers_an_inline_only_emoji_asset(tmp_path: Path) -> None:
    _with_image(tmp_path)
    config, state = _config(tmp_path), _state()
    message = _message(with_reaction=False)
    message.inline_emojis = [DCEEmoji(id=EMOJI_ID, name="smile", image_url="smile.png")]

    with (
        patch(_CHECK, new=AsyncMock(return_value=_missing_report())),
        patch(_UPLOAD, new=AsyncMock(return_value=(NEW_ID, "smile"))) as upload,
        patch(_EDIT, new=AsyncMock()),
    ):
        outcome = await run_repair(config, state, [_export([message])], [].append)

    upload.assert_awaited_once()
    assert [record["discord_id"] for record in outcome.recreated_emoji] == [EMOJI_ID]


async def test_recreate_writes_map_and_record_in_one_save(tmp_path: Path) -> None:
    """SC-1.3: the new id and the resume record land in a single save_state."""
    _with_image(tmp_path)
    config, state = _config(tmp_path), _state()
    saves: list[dict[str, Any]] = []
    from discord_ferry.core.engine import save_state as real_save

    def capture(st: MigrationState, out: Path) -> None:
        saves.append(
            {
                "emoji_map": dict(st.emoji_map),
                "pending": {k: dict(v) for k, v in st.pending_emoji_rewrites.items()},
            }
        )
        real_save(st, out)

    with (
        patch(_CHECK, new=AsyncMock(return_value=_missing_report())),
        patch(_UPLOAD, new=AsyncMock(return_value=(NEW_ID, "smile"))),
        patch(_EDIT, new=AsyncMock()),
        patch(f"{_ENGINE}.save_state", side_effect=capture),
    ):
        await run_repair(config, state, [_export([_message()])], [].append)

    first_new = next(s for s in saves if s["emoji_map"].get(EMOJI_ID) == NEW_ID)
    assert first_new["pending"].get(EMOJI_ID) == {"old": OLD_ID, "new": NEW_ID}


async def test_missing_image_declines_no_record(tmp_path: Path) -> None:
    """SC-1.5: no usable export image declines, writes no resume record."""
    # No image file on disk; the reaction still names smile.png but it is absent.
    config, state = _config(tmp_path), _state()
    with (
        patch(_CHECK, new=AsyncMock(return_value=_missing_report())),
        patch(_UPLOAD, new=AsyncMock(return_value=(NEW_ID, "smile"))) as up,
        patch(_EDIT, new=AsyncMock()),
    ):
        outcome = await run_repair(config, state, [_export([_message()])], [].append)
    up.assert_not_awaited()
    assert any(d.get("type") == "emoji_missing_media" for d in outcome.declined)
    assert state.emoji_map[EMOJI_ID] == OLD_ID
    assert EMOJI_ID not in state.pending_emoji_rewrites
    assert outcome.recreated_emoji == []


async def test_rerun_when_present_makes_no_second_emoji(tmp_path: Path) -> None:
    """SC-3.1: a check that reports the emoji present recreates nothing."""
    _with_image(tmp_path)
    config, state = _config(tmp_path), _state()
    state.emoji_map[EMOJI_ID] = NEW_ID
    with (
        patch(_CHECK, new=AsyncMock(return_value=_present_report())),
        patch(_UPLOAD, new=AsyncMock(return_value=("second", "second_name"))) as up,
        patch(_EDIT, new=AsyncMock()),
    ):
        outcome = await run_repair(config, state, [_export([_message()])], [].append)
    up.assert_not_awaited()
    assert outcome.recreated_emoji == []


# ---------------------------------------------------------------------------
# Task 3.2: rewrite references
# ---------------------------------------------------------------------------


async def _run_with_messages(tmp_path: Path, messages: list[DCEMessage]) -> Any:
    _with_image(tmp_path)
    config, state = _config(tmp_path), _state()
    with (
        patch(_CHECK, new=AsyncMock(return_value=_missing_report())),
        patch(_UPLOAD, new=AsyncMock(return_value=(NEW_ID, "smile"))),
        patch(_EDIT, new=AsyncMock()) as edit,
    ):
        outcome = await run_repair(config, state, [_export(messages)], [].append)
    return outcome, edit, state


async def test_rewrite_only_edits_message_map_messages(tmp_path: Path) -> None:
    """SC-2.1: a referencing message not in message_map is never edited."""
    m1 = _message()  # id "m1", in message_map
    m2 = DCEMessage(
        id="m2",  # NOT in message_map
        type="Default",
        timestamp="2024-01-01T00:00:00Z",
        content="also <:smile:123>",
        author=DCEAuthor(id="u", name="U"),
        reactions=[],
    )
    outcome, edit, _ = await _run_with_messages(tmp_path, [m1, m2])
    edited_ids = [call.args[4] for call in edit.await_args_list]
    assert edited_ids == ["stoat_msg"]  # only m1's mapped id
    assert outcome.recreated_emoji[0]["messages_rewritten"] == 1


async def test_rewrite_content_points_at_new_id(tmp_path: Path) -> None:
    """SC-2.2: the edited content carries the new emoji id, not the old one."""
    outcome, edit, _ = await _run_with_messages(tmp_path, [_message()])
    content = edit.await_args_list[0].kwargs["content"]
    assert f":{NEW_ID}:" in content
    assert f":{OLD_ID}:" not in content


async def test_rewrite_split_first_edits_first_part(tmp_path: Path) -> None:
    """SC-2.3: emoji in the first split part edits that part."""
    long_tail = " ".join(["word"] * 600)  # well over 2000 chars once rendered
    m = _message(content=f"<:smile:123> {long_tail}")
    outcome, edit, _ = await _run_with_messages(tmp_path, [m])
    assert edit.await_count == 1
    content = edit.await_args_list[0].kwargs["content"]
    assert f":{NEW_ID}:" in content
    assert len(content) <= 2000
    assert outcome.recreated_emoji[0]["messages_rewritten"] == 1


async def test_rewrite_split_first_edits_even_when_emoji_also_in_tail(tmp_path: Path) -> None:
    """Chunk-3 review guard: emoji in the first part AND a later part still edits.

    The addressable first send is fixed; a second, unaddressable send is left as
    is. Declining the whole message (as a naive 'token in any later part' check
    would) refuses a fixable rewrite.
    """
    long_mid = " ".join(["word"] * 600)
    m = _message(content=f"<:smile:123> {long_mid} <:smile:123>")
    outcome, edit, _ = await _run_with_messages(tmp_path, [m])
    assert edit.await_count == 1
    assert outcome.recreated_emoji[0]["messages_rewritten"] == 1
    assert outcome.recreated_emoji[0]["messages_declined"] == 0


async def test_rewrite_split_tail_declines(tmp_path: Path) -> None:
    """SC-2.4: emoji in a later split part is declined, not edited."""
    long_head = " ".join(["word"] * 600)
    m = _message(content=f"{long_head} <:smile:123>")
    outcome, edit, state = await _run_with_messages(tmp_path, [m])
    edit.assert_not_awaited()
    assert outcome.recreated_emoji[0]["messages_declined"] == 1
    assert any(d.get("type") == "emoji_in_split_tail" for d in outcome.declined)


async def test_rewrite_reaction_only_makes_no_edit(tmp_path: Path) -> None:
    """SC-2.5: an emoji used only in a reaction rewrites nothing."""
    m = _message(content="no emoji here", with_reaction=True)
    outcome, edit, _ = await _run_with_messages(tmp_path, [m])
    edit.assert_not_awaited()
    assert outcome.recreated_emoji[0]["messages_rewritten"] == 0


async def test_rewrite_counts_all_referencing_messages(tmp_path: Path) -> None:
    """SC-2.6: every mapped referencing message is counted."""
    msgs = []
    config_state_ids = {"m1": "stoat_msg", "m2": "s2", "m3": "s3"}
    for i, mid in enumerate(config_state_ids):
        # The first message carries the reaction so the image is recoverable;
        # all three reference the emoji in content and so are rewritten.
        reactions = (
            [DCEReaction(emoji=DCEEmoji(id=EMOJI_ID, name="smile", image_url="smile.png"), count=1)]
            if i == 0
            else []
        )
        msgs.append(
            DCEMessage(
                id=mid,
                type="Default",
                timestamp="2024-01-01T00:00:00Z",
                content="x <:smile:123>",
                author=DCEAuthor(id="u", name="U"),
                reactions=reactions,
            )
        )
    _with_image(tmp_path)
    config, state = _config(tmp_path), _state()
    state.message_map = dict(config_state_ids)
    with (
        patch(_CHECK, new=AsyncMock(return_value=_missing_report())),
        patch(_UPLOAD, new=AsyncMock(return_value=(NEW_ID, "smile"))),
        patch(_EDIT, new=AsyncMock()) as edit,
    ):
        outcome = await run_repair(config, state, [_export(msgs)], [].append)
    assert edit.await_count == 3
    assert outcome.recreated_emoji[0]["messages_rewritten"] == 3
    assert EMOJI_ID not in state.pending_emoji_rewrites  # cleared on full success


# ---------------------------------------------------------------------------
# Task 3.3: resume step and docstring
# ---------------------------------------------------------------------------


async def test_resume_finishes_a_crashed_rewrite(tmp_path: Path) -> None:
    """SC-3.2: a pending record from a prior crash is finished before the check."""
    _with_image(tmp_path)
    config, state = _config(tmp_path), _state()
    # A prior run recreated the emoji (map already points at new) and crashed
    # mid-rewrite, leaving the resume record. The server already has the new id,
    # so this run's check reports it present (no recreation to do).
    state.emoji_map[EMOJI_ID] = NEW_ID
    state.pending_emoji_rewrites[EMOJI_ID] = {"old": OLD_ID, "new": NEW_ID}
    with (
        patch(_CHECK, new=AsyncMock(return_value=_present_report())),
        patch(_UPLOAD, new=AsyncMock(return_value=("second", "second_name"))) as up,
        patch(_EDIT, new=AsyncMock()) as edit,
    ):
        outcome = await run_repair(config, state, [_export([_message()])], [].append)
    up.assert_not_awaited()  # no second emoji
    edit.assert_awaited_once()  # the stranded message is finished
    assert EMOJI_ID not in state.pending_emoji_rewrites
    # A resume-only run still reports the rewrite work in the outcome document.
    row = outcome.recreated_emoji[0]
    assert row["discord_id"] == EMOJI_ID
    assert row["new_id"] == NEW_ID
    assert row["messages_rewritten"] == 1


async def test_edit_failure_keeps_the_record(tmp_path: Path) -> None:
    """SC-3.3: a failed edit keeps the resume record and counts the failure."""
    _with_image(tmp_path)
    config, state = _config(tmp_path), _state()
    state.message_map = {"m1": "s1", "m2": "s2"}
    msgs = [
        _message(),  # id m1, carries the reaction/image
        DCEMessage(
            id="m2",
            type="Default",
            timestamp="2024-01-01T00:00:00Z",
            content="more <:smile:123>",
            author=DCEAuthor(id="u", name="U"),
            reactions=[],
        ),
    ]
    with (
        patch(_CHECK, new=AsyncMock(return_value=_missing_report())),
        patch(_UPLOAD, new=AsyncMock(return_value=(NEW_ID, "smile"))),
        patch(_EDIT, new=AsyncMock(side_effect=[None, RuntimeError("boom")])),
    ):
        outcome = await run_repair(config, state, [_export(msgs)], [].append)
    row = outcome.recreated_emoji[0]
    assert row["messages_rewritten"] == 1
    assert row["messages_failed"] == 1
    assert state.pending_emoji_rewrites[EMOJI_ID] == {"old": OLD_ID, "new": NEW_ID}


def test_run_repair_docstring_admits_the_checkpoint() -> None:
    """SC-3.4: the docstring no longer claims nothing is checkpointed."""
    doc = run_repair.__doc__ or ""
    assert "nothing to checkpoint" not in doc
    assert "pending_emoji_rewrites" in doc


# ---------------------------------------------------------------------------
# Task 4.1: outcome surface
# ---------------------------------------------------------------------------


async def test_human_output_names_emoji_and_rewrite_count(tmp_path: Path) -> None:
    """SC-4.1: progress events name the recreated emoji and the rewrite count."""
    _with_image(tmp_path)
    config, state = _config(tmp_path), _state()
    events: list[Any] = []
    with (
        patch(_CHECK, new=AsyncMock(return_value=_missing_report())),
        patch(_UPLOAD, new=AsyncMock(return_value=(NEW_ID, "smile"))),
        patch(_EDIT, new=AsyncMock()),
    ):
        await run_repair(config, state, [_export([_message()])], events.append)
    messages = [e.message for e in events]
    assert any("Recreated emoji :smile:" in m for m in messages)
    assert any("Rewrote 1 reference" in m for m in messages)


async def test_outcome_to_dict_carries_recreated_emoji_end_to_end(tmp_path: Path) -> None:
    """SC-4.2: a real run's outcome document carries recreated_emoji under actions."""
    outcome, _, _ = await _run_with_messages(tmp_path, [_message()])
    row = outcome.to_dict()["actions"]["recreated_emoji"][0]
    assert row["discord_id"] == EMOJI_ID
    assert row["new_id"] == NEW_ID
    assert row["messages_rewritten"] == 1


# ---------------------------------------------------------------------------
# Task 4.2: integration
# ---------------------------------------------------------------------------


def _two_referencing_messages() -> list[DCEMessage]:
    return [
        _message(),  # m1, carries the reaction/image
        DCEMessage(
            id="m2",
            type="Default",
            timestamp="2024-01-01T00:00:00Z",
            content="again <:smile:123>",
            author=DCEAuthor(id="u", name="U"),
            reactions=[],
        ),
    ]


async def test_integration_recreate_rewrite_and_verifiable(tmp_path: Path) -> None:
    """SC-I1: end to end. The emoji is recreated once, both messages rewritten, and
    emoji_map now points at the new id, which is exactly what makes a later check
    report emoji_present.
    """
    _with_image(tmp_path)
    config, state = _config(tmp_path), _state()
    state.message_map = {"m1": "s1", "m2": "s2"}
    with (
        patch(_CHECK, new=AsyncMock(return_value=_missing_report())),
        patch(_UPLOAD, new=AsyncMock(return_value=(NEW_ID, "smile"))) as up,
        patch(_EDIT, new=AsyncMock()) as edit,
    ):
        outcome = await run_repair(config, state, [_export(_two_referencing_messages())], [].append)
    assert up.await_count == 1
    assert edit.await_count == 2
    assert all(f":{NEW_ID}:" in c.kwargs["content"] for c in edit.await_args_list)
    assert state.emoji_map[EMOJI_ID] == NEW_ID  # a follow-up check would report present
    assert EMOJI_ID not in state.pending_emoji_rewrites
    assert outcome.recreated_emoji[0]["messages_rewritten"] == 2


async def test_integration_interrupted_run_matches_clean_run(tmp_path: Path) -> None:
    """SC-I2: a failed edit keeps the record; a second run resumes to the same end
    state as an uninterrupted run (both messages on the new id, one emoji, cleared).
    """
    _with_image(tmp_path)
    config, state = _config(tmp_path), _state()
    state.message_map = {"m1": "s1", "m2": "s2"}
    exports = [_export(_two_referencing_messages())]

    # Run 1: the second edit fails, standing in for a crash mid-rewrite.
    with (
        patch(_CHECK, new=AsyncMock(return_value=_missing_report())),
        patch(_UPLOAD, new=AsyncMock(return_value=(NEW_ID, "smile"))),
        patch(_EDIT, new=AsyncMock(side_effect=[None, RuntimeError("crash")])),
    ):
        await run_repair(config, state, exports, [].append)
    assert state.pending_emoji_rewrites[EMOJI_ID] == {"old": OLD_ID, "new": NEW_ID}

    # Run 2: the emoji is present now, so the resume step finishes the stragglers.
    with (
        patch(_CHECK, new=AsyncMock(return_value=_present_report())),
        patch(_UPLOAD, new=AsyncMock(return_value=("should-not-run", "should-not-run"))) as up2,
        patch(_EDIT, new=AsyncMock()) as edit2,
    ):
        await run_repair(config, state, exports, [].append)
    up2.assert_not_awaited()  # no second emoji
    assert edit2.await_count >= 1  # the stranded message is finished
    assert EMOJI_ID not in state.pending_emoji_rewrites  # same end state as a clean run


async def test_integration_emoji_pass_runs_before_structure(tmp_path: Path) -> None:
    """SC-I3 (ordering): the load-bearing guarantee behind the self-healing channel
    case. When both an emoji and a channel are missing, the emoji pass runs before
    the structure pass, so the rewrite re-renders through pristine maps.
    """
    order: list[str] = []

    async def emoji_pass(*_a: Any, **_k: Any) -> None:
        order.append("emoji")

    async def live_view(*_a: Any, **_k: Any) -> Any:
        order.append("structure")
        raise RuntimeError("stop after recording order")

    report = CheckReport()
    report.add(
        name="emoji:123",
        status="fail",
        kind="emoji_missing",
        detail="",
        discord_id="123",
        stoat_id="old_id",
    )
    report.add(
        name="channel:ch1",
        status="fail",
        kind="channel_missing",
        detail="",
        discord_id="ch1",
        stoat_id="sc",
    )
    config, state = _config(tmp_path), _state()
    with (
        patch(_CHECK, new=AsyncMock(return_value=report)),
        patch(f"{_ENGINE}._run_emoji_repair_pass", new=emoji_pass),
        patch(f"{_ENGINE}._live_server_view", new=live_view),
        pytest.raises(RuntimeError),
    ):
        await run_repair(config, state, [_export([_message()])], [].append)
    assert order == ["emoji", "structure"]


async def test_integration_dry_run_writes_nothing(tmp_path: Path) -> None:
    """SC-I4: a dry run performs no upload, create, edit or save, and lists the work."""
    _with_image(tmp_path)
    config, state = _config(tmp_path), _state()
    config = FerryConfig(
        export_dir=tmp_path,
        stoat_url="https://api.test",
        token="t",
        upload_delay=0.0,
        output_dir=tmp_path,
        dry_run=True,
    )
    with (
        patch(_CHECK, new=AsyncMock(return_value=_missing_report())),
        patch(_UPLOAD, new=AsyncMock(return_value=(NEW_ID, "smile"))) as up,
        patch(_EDIT, new=AsyncMock()) as edit,
        patch(f"{_ENGINE}.save_state", side_effect=AssertionError("dry run must not save")),
    ):
        outcome = await run_repair(config, state, [_export([_message()])], [].append)
    up.assert_not_awaited()
    edit.assert_not_awaited()
    assert outcome.dry_run is True
    assert outcome.recreated_emoji[0]["discord_id"] == EMOJI_ID
    assert outcome.recreated_emoji[0]["messages_rewritten"] == 1  # would-rewrite count
    assert state.emoji_map[EMOJI_ID] == OLD_ID  # unchanged


async def test_dry_run_lists_an_outstanding_resume_record(tmp_path: Path) -> None:
    """F4: a dry run names a leftover resume record a real run would finish first."""
    _with_image(tmp_path)
    config = FerryConfig(
        export_dir=tmp_path,
        stoat_url="https://api.test",
        token="t",
        upload_delay=0.0,
        output_dir=tmp_path,
        dry_run=True,
    )
    state = _state()
    state.emoji_map[EMOJI_ID] = NEW_ID
    state.pending_emoji_rewrites[EMOJI_ID] = {"old": OLD_ID, "new": NEW_ID}
    with (
        patch(_CHECK, new=AsyncMock(return_value=_present_report())),
        patch(_UPLOAD, new=AsyncMock()) as up,
        patch(_EDIT, new=AsyncMock()) as edit,
        patch(f"{_ENGINE}.save_state", side_effect=AssertionError("dry run must not save")),
    ):
        outcome = await run_repair(config, state, [_export([_message()])], [].append)
    up.assert_not_awaited()
    edit.assert_not_awaited()
    row = next(r for r in outcome.recreated_emoji if r["discord_id"] == EMOJI_ID)
    assert row["new_id"] == NEW_ID
    assert row["messages_rewritten"] == 1  # would finish the stranded message
