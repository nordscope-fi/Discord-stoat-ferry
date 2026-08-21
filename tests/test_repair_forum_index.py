"""Tests for forum-index repair in run_repair (#311).

The forum index lives under a synthetic ``forum-index-{forum_key}`` key that names no
Discord channel, so the generic recreation path cannot restore it. ``_recreate_forum_index``
recreates the channel and rebuilds its index message via the shared ``_rebuild_one_forum_index``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiohttp
from aioresponses import aioresponses

from discord_ferry.config import FerryConfig
from discord_ferry.core.engine import _recreate_forum_index
from discord_ferry.migrator.verify import CheckResult
from discord_ferry.state import MigrationState

if TYPE_CHECKING:
    from pathlib import Path


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
