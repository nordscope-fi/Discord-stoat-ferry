"""Tests for incremental-mode structure phase behaviour (SC-11, SC-12)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from aioresponses import aioresponses

from discord_ferry.config import FerryConfig
from discord_ferry.errors import MigrationError
from discord_ferry.migrator.structure import run_server
from discord_ferry.parser.models import (
    DCEAuthor,
    DCEChannel,
    DCEExport,
    DCEGuild,
    DCEMessage,
    DCERole,
)
from discord_ferry.state import MigrationState

if TYPE_CHECKING:
    from pathlib import Path

    from discord_ferry.core.events import MigrationEvent

STOAT_URL = "https://api.test"


# ---------------------------------------------------------------------------
# Helpers (mirrors tests/test_structure.py)
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path, **overrides: object) -> FerryConfig:
    defaults: dict[str, object] = {
        "export_dir": tmp_path,
        "stoat_url": STOAT_URL,
        "token": "tok",
        "output_dir": tmp_path,
    }
    defaults.update(overrides)
    return FerryConfig(**defaults)  # type: ignore[arg-type]


def _make_export(
    guild_id: str = "111",
    guild_name: str = "Test",
    guild_icon_url: str = "",
    channel_id: str = "222",
    channel_name: str = "general",
    channel_type: int = 0,
    category_id: str = "cat1",
    category: str = "General",
    is_thread: bool = False,
    parent_channel_name: str = "",
    messages: list[DCEMessage] | None = None,
    message_count: int = 0,
) -> DCEExport:
    guild = DCEGuild(id=guild_id, name=guild_name, icon_url=guild_icon_url)
    channel = DCEChannel(
        id=channel_id,
        type=channel_type,
        name=channel_name,
        category_id=category_id,
        category=category,
    )
    return DCEExport(
        guild=guild,
        channel=channel,
        messages=messages or [],
        message_count=message_count,
        is_thread=is_thread,
        parent_channel_name=parent_channel_name,
    )


# ---------------------------------------------------------------------------
# SC-12: Server reuse via carried id (no --server-id)
# ---------------------------------------------------------------------------


async def test_run_server_reuses_carried_stoat_server_id(tmp_path: Path) -> None:
    """SC-12: run_server uses state.stoat_server_id when config.server_id is None.

    POST /servers/create is intentionally NOT registered. If run_server were to
    attempt a create, aioresponses would raise a ConnectionError for the
    unmatched request and the test would fail.
    """
    state = MigrationState(stoat_server_id="srv1")
    config = _make_config(tmp_path, incremental=True, server_id=None)
    exports = [_make_export()]

    with aioresponses() as m:
        m.get(f"{STOAT_URL}/servers/srv1", payload={"_id": "srv1"})
        # /servers/create intentionally absent — a create attempt would error.
        await run_server(config, state, exports, lambda e: None)

    assert state.stoat_server_id == "srv1"


# ---------------------------------------------------------------------------
# SC-11: Deleted prior server → clear MigrationError
# ---------------------------------------------------------------------------


async def test_run_server_deleted_prior_server_raises_clear_error(tmp_path: Path) -> None:
    """SC-11: run_server raises MigrationError naming the id when server is 404.

    No POST /servers/create should be attempted after a 404 on the carried id.
    """
    state = MigrationState(stoat_server_id="gone")
    config = _make_config(tmp_path, incremental=True, server_id=None)
    exports = [_make_export()]

    with aioresponses() as m:
        m.get(f"{STOAT_URL}/servers/gone", status=404, payload={"type": "NotFound"})
        with pytest.raises(MigrationError, match="gone"):
            await run_server(config, state, exports, lambda e: None)
