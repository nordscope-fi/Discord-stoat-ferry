"""Tests for forum-index repair in run_repair (#311).

The forum index lives under a synthetic ``forum-index-{forum_key}`` key that names no
Discord channel, so the generic recreation path cannot restore it. ``_recreate_forum_index``
recreates the channel and rebuilds its index message via the shared ``_rebuild_one_forum_index``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import aiohttp
from aioresponses import aioresponses

from discord_ferry.config import FerryConfig
from discord_ferry.core.engine import _recreate_forum_index, run_repair
from discord_ferry.migrator.verify import UNREPAIRED_WARNING_TYPES, CheckReport, CheckResult
from discord_ferry.state import MigrationState

if TYPE_CHECKING:
    from pathlib import Path

_ENGINE = "discord_ferry.core.engine"
_CHECK = "discord_ferry.migrator.verify.run_check"
_LIVE = f"{_ENGINE}._live_server_view"
_RECREATE = f"{_ENGINE}._recreate_forum_index"
_REBUILD = f"{_ENGINE}._rebuild_one_forum_index"


def _config(tmp_path: Path) -> FerryConfig:
    return FerryConfig(
        export_dir=tmp_path,
        stoat_url="https://api.test",
        token="t",
        upload_delay=0.0,
        output_dir=tmp_path,
    )


def _result() -> CheckResult:
    return CheckResult(
        name="channel:forum-index-f",
        status="fail",
        kind="channel_missing",
        detail="the server does not list this channel",
        discord_id="forum-index-f",
        stoat_id="old-idx",
    )


async def test_recreate_forum_index_creates_and_rebuilds(tmp_path: Path) -> None:
    """SC-3.1/SC-4.1: the channel is recreated at position 0 and its index message rebuilt."""
    config = _config(tmp_path)
    state = MigrationState(
        stoat_server_id="srv",
        category_map={"f": "cat-s"},
        created_channel_names={"forum-index-f": "F-index"},
        forum_channel_members={"f": ["d1"]},
        forum_category_names={"f": "F"},
        channel_map={"d1": "s1"},
        channel_message_counts={"d1": 2},
    )
    live_categories = [{"id": "cat-s", "title": "F", "channels": ["old-idx"]}]

    with aioresponses() as mock:
        mock.post(
            "https://api.test/servers/srv/channels",
            payload={"_id": "new-idx", "name": "F-index"},
        )
        mock.patch("https://api.test/servers/srv", payload={})
        mock.post("https://api.test/channels/new-idx/messages", payload={"_id": "msg-1"})
        mock.post("https://api.test/channels/new-idx/messages/msg-1/pin", payload={})
        async with aiohttp.ClientSession() as session:
            created = await _recreate_forum_index(
                session, config, state, _result(), set(), live_categories, lambda e: None
            )

    assert created is True
    assert state.channel_map["forum-index-f"] == "new-idx"
    assert state.forum_index_message_ids["f"] == "msg-1"
    # The recreated index sits at position 0 and the dead id is dropped.
    assert live_categories[0]["channels"] == ["new-idx"]


async def test_recreate_forum_index_declines_missing_category(tmp_path: Path) -> None:
    """SC-5.2: a gone forum category yields a distinct decline and creates nothing."""
    config = _config(tmp_path)
    state = MigrationState(
        stoat_server_id="srv",
        category_map={},  # the forum category itself is gone
        created_channel_names={"forum-index-f": "F-index"},
    )

    with aioresponses():  # no routes: any HTTP call would raise
        async with aiohttp.ClientSession() as session:
            created = await _recreate_forum_index(
                session, config, state, _result(), set(), [], lambda e: None
            )

    assert created is False
    assert any(w.get("type") == "forum_index_category_missing" for w in state.warnings)
    assert "forum-index-f" not in state.channel_map


# --- Routing through run_repair (Task 2.2) ---------------------------------


def _report(kind: str) -> CheckReport:
    report = CheckReport()
    report.add(
        name="channel:forum-index-f",
        status="fail",
        kind=kind,
        detail="forum index needs repair",
        discord_id="forum-index-f",
        stoat_id="old-idx",
    )
    return report


def _routing_state(tmp_path: Path) -> MigrationState:
    return MigrationState(
        stoat_server_id="srv",
        category_map={"f": "cat-s"},
        created_channel_names={"forum-index-f": "F-index"},
        forum_channel_members={"f": ["d1"]},
        forum_category_names={"f": "F"},
        channel_map={"d1": "s1", "forum-index-f": "old-idx"},
        forum_index_message_ids={"f": "old-idx-msg"},
    )


async def test_repair_recreates_lone_forum_index(tmp_path: Path) -> None:
    """SC-I2/SC-3.2/SC-3.3: the forum index as the ONLY broken entity is still repaired.

    structure_work is empty, so the round-2 session gap would have dropped this silently.
    """
    config = _config(tmp_path)
    state = _routing_state(tmp_path)
    with (
        patch(_CHECK, new=AsyncMock(return_value=_report("channel_missing"))),
        patch(_LIVE, new=AsyncMock(return_value=(set(), [{"id": "cat-s", "channels": []}]))),
        patch(_RECREATE, new=AsyncMock(return_value=True)) as recreate,
    ):
        await run_repair(config, state, [], [].append)
    recreate.assert_awaited_once()
    assert not any(w.get("type") == "no_discord_metadata" for w in state.warnings)
    assert not any(w.get("type") == "forum_index_not_repairable" for w in state.warnings)


async def test_repair_routes_tail_absent_to_rebuild_without_create(tmp_path: Path) -> None:
    """SC-I4: a forum index whose channel survives but message is gone rebuilds, no create."""
    config = _config(tmp_path)
    state = _routing_state(tmp_path)
    with (
        patch(_CHECK, new=AsyncMock(return_value=_report("tail_absent"))),
        patch(_LIVE, new=AsyncMock(return_value=(set(), []))),
        patch(_RECREATE, new=AsyncMock(return_value=True)) as recreate,
        patch(_REBUILD, new=AsyncMock()) as rebuild,
    ):
        await run_repair(config, state, [], [].append)
    rebuild.assert_awaited_once()
    assert rebuild.await_args.args[3] == "f"  # forum_key
    recreate.assert_not_awaited()


async def test_repair_forum_index_dry_run_mutates_nothing(tmp_path: Path) -> None:
    """SC-3.4: --dry-run recreates nothing and writes no state."""
    config = FerryConfig(
        export_dir=tmp_path,
        stoat_url="https://api.test",
        token="t",
        upload_delay=0.0,
        output_dir=tmp_path,
        dry_run=True,
    )
    state = _routing_state(tmp_path)
    with (
        patch(_CHECK, new=AsyncMock(return_value=_report("channel_missing"))),
        patch(_RECREATE, new=AsyncMock(return_value=True)) as recreate,
    ):
        await run_repair(config, state, [], [].append)
    recreate.assert_not_awaited()
    assert state.channel_map["forum-index-f"] == "old-idx"  # unchanged


def test_category_missing_decline_is_in_exit_set() -> None:
    """SC-5.2: the distinct decline forces a non-zero exit, like other unrepaired gaps."""
    assert "forum_index_category_missing" in UNREPAIRED_WARNING_TYPES
