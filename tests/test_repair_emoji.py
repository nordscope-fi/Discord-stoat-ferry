"""Tests for the emoji-repair pass in run_repair (#307)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

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
from discord_ferry.state import MigrationState, load_state

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
        patch(_UPLOAD, new=AsyncMock(return_value=NEW_ID)) as up,
        patch(_EDIT, new=AsyncMock()),
    ):
        outcome = await run_repair(config, state, [_export([_message()])], events.append)
    up.assert_awaited_once()
    assert not any(d.get("type") == "emoji_missing_media" for d in outcome.declined)
    assert [r["discord_id"] for r in outcome.recreated_emoji] == [EMOJI_ID]
    assert outcome.recreated_emoji[0]["new_id"] == NEW_ID


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
        patch(_UPLOAD, new=AsyncMock(return_value=NEW_ID)),
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
        patch(_UPLOAD, new=AsyncMock(return_value=NEW_ID)) as up,
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
        patch(_UPLOAD, new=AsyncMock(return_value="second")) as up,
        patch(_EDIT, new=AsyncMock()),
    ):
        outcome = await run_repair(config, state, [_export([_message()])], [].append)
    up.assert_not_awaited()
    assert outcome.recreated_emoji == []
