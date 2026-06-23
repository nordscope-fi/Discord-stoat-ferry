"""Tests for incremental-mode structure phase behaviour (SC-3, SC-4, SC-5, SC-11, SC-12)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from aioresponses import aioresponses

from discord_ferry.config import FerryConfig
from discord_ferry.discord.metadata import (
    DiscordMetadata,
    PermissionPair,
    RoleMeta,
    save_discord_metadata,
)
from discord_ferry.errors import MigrationError
from discord_ferry.migrator.structure import run_roles, run_server
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


# ---------------------------------------------------------------------------
# Helpers for role tests
# ---------------------------------------------------------------------------


def _make_role_export(roles: list[DCERole], guild_id: str = "111") -> DCEExport:
    """Build a minimal DCEExport whose single message carries the given roles."""
    msg = DCEMessage(
        id="m1",
        type="Default",
        timestamp="2024-01-01T00:00:00Z",
        content="hi",
        author=DCEAuthor(id="u1", name="User", roles=roles),
    )
    return _make_export(guild_id=guild_id, messages=[msg])


# ---------------------------------------------------------------------------
# SC-3: Existing roles get zero attribute/permission edits on re-run
# ---------------------------------------------------------------------------


async def test_run_roles_skips_all_passes_for_already_migrated_roles(
    tmp_path: Path,
) -> None:
    """SC-3: re-run with all roles already in role_map → 0 creates, 0 edits, 0 perm calls.

    Registers create/edit/perm endpoints with call-counting callbacks.
    Any call to these endpoints means the guard is missing.
    """
    # Two roles; role r1 has position=2 (would trigger attributes pass) and we
    # supply Discord metadata with hoist=True so both passes would definitely fire
    # if the guard were absent.
    role_a = DCERole(id="r1", name="Admin", position=2)
    role_b = DCERole(id="r2", name="Mod", position=1)

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={"r1": PermissionPair(allow=8, deny=0)},
        channel_metadata={},
        role_metadata={
            "r1": RoleMeta(hoist=True, position=2),
            "r2": RoleMeta(hoist=False, position=1),
        },
    )
    save_discord_metadata(meta, tmp_path)

    # Prior state: both roles already mapped.
    state = MigrationState(
        stoat_server_id="srv1",
        role_map={"r1": "stoat-r1", "r2": "stoat-r2"},
    )
    config = _make_config(tmp_path, incremental=True)
    exports = [_make_role_export([role_a, role_b])]

    create_calls: list[object] = []
    edit_calls: list[object] = []
    perm_calls: list[object] = []

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/roles",
            repeat=True,
            callback=lambda url, **kw: create_calls.append(url),  # type: ignore[misc]
        )
        m.patch(
            f"{STOAT_URL}/servers/srv1/roles/stoat-r1",
            repeat=True,
            callback=lambda url, **kw: edit_calls.append(url),  # type: ignore[misc]
        )
        m.patch(
            f"{STOAT_URL}/servers/srv1/roles/stoat-r2",
            repeat=True,
            callback=lambda url, **kw: edit_calls.append(url),  # type: ignore[misc]
        )
        m.put(
            f"{STOAT_URL}/servers/srv1/permissions/stoat-r1",
            repeat=True,
            callback=lambda url, **kw: perm_calls.append(url),  # type: ignore[misc]
        )
        m.put(
            f"{STOAT_URL}/servers/srv1/permissions/stoat-r2",
            repeat=True,
            callback=lambda url, **kw: perm_calls.append(url),  # type: ignore[misc]
        )

        await run_roles(config, state, exports, lambda e: None)

    assert create_calls == [], f"Expected 0 create calls, got {len(create_calls)}"
    assert edit_calls == [], f"Expected 0 attribute edit calls, got {len(edit_calls)}"
    assert perm_calls == [], f"Expected 0 permission calls, got {len(perm_calls)}"
    # Maps unchanged.
    assert state.role_map == {"r1": "stoat-r1", "r2": "stoat-r2"}


# ---------------------------------------------------------------------------
# SC-4: New role since prior run IS created (only the new one)
# ---------------------------------------------------------------------------


async def test_run_roles_creates_only_new_roles_on_incremental_rerun(
    tmp_path: Path,
) -> None:
    """SC-4: prior role_map has r1; export now also has r2 → only r2 created."""
    role_a = DCERole(id="r1", name="Admin")
    role_b = DCERole(id="r2", name="Mod")

    # Prior state: only r1 mapped.
    state = MigrationState(
        stoat_server_id="srv1",
        role_map={"r1": "stoat-r1"},
    )
    config = _make_config(tmp_path, incremental=True)
    exports = [_make_role_export([role_a, role_b])]

    create_calls: list[object] = []

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/roles",
            payload={"id": "stoat-r2", "name": "Mod"},
            callback=lambda url, **kw: create_calls.append(url),  # type: ignore[misc]
        )

        await run_roles(config, state, exports, lambda e: None)

    assert len(create_calls) == 1, f"Expected exactly 1 create call, got {len(create_calls)}"
    assert state.role_map["r2"] == "stoat-r2"
    assert state.role_map["r1"] == "stoat-r1"  # untouched


# ---------------------------------------------------------------------------
# SC-5: Fresh run still creates + attributes ALL roles (snapshot empty)
# ---------------------------------------------------------------------------


async def test_run_roles_fresh_run_creates_and_attributes_all_roles(
    tmp_path: Path,
) -> None:
    """SC-5: empty state → pre_existing_role_ids is empty → every role created AND
    api_edit_role (attributes) fires for roles with position != 0 or hoist set.

    Guards against the trap where a guard on the live role_map would wrongly
    skip attributes for roles created earlier in the same pass.
    """
    # role_a: position=2 → attributes pass fires (rank)
    # role_b: position=1, hoist=True via metadata → attributes pass fires (hoist)
    role_a = DCERole(id="r1", name="Admin", position=2)
    role_b = DCERole(id="r2", name="Mod", position=1)

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
        role_metadata={
            "r2": RoleMeta(hoist=True, position=1),
        },
    )
    save_discord_metadata(meta, tmp_path)

    state = MigrationState(stoat_server_id="srv1")  # empty role_map
    config = _make_config(tmp_path)
    exports = [_make_role_export([role_a, role_b])]

    create_calls: list[object] = []
    edit_calls: list[object] = []

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/roles",
            payload={"id": "stoat-r1", "name": "Admin"},
            callback=lambda url, **kw: create_calls.append(url),  # type: ignore[misc]
        )
        m.post(
            f"{STOAT_URL}/servers/srv1/roles",
            payload={"id": "stoat-r2", "name": "Mod"},
            callback=lambda url, **kw: create_calls.append(url),  # type: ignore[misc]
        )
        m.patch(
            f"{STOAT_URL}/servers/srv1/roles/stoat-r1",
            payload={},
            repeat=True,
            callback=lambda url, **kw: edit_calls.append(url),  # type: ignore[misc]
        )
        m.patch(
            f"{STOAT_URL}/servers/srv1/roles/stoat-r2",
            payload={},
            repeat=True,
            callback=lambda url, **kw: edit_calls.append(url),  # type: ignore[misc]
        )
        m.put(f"{STOAT_URL}/servers/srv1/permissions/stoat-r1", payload={}, repeat=True)
        m.put(f"{STOAT_URL}/servers/srv1/permissions/stoat-r2", payload={}, repeat=True)

        await run_roles(config, state, exports, lambda e: None)

    assert len(create_calls) == 2, f"Expected 2 creates on fresh run, got {len(create_calls)}"
    # Both roles should have had attributes applied (r1: rank, r2: hoist).
    assert len(edit_calls) >= 2, (
        f"Expected >= 2 attribute edits on fresh run, got {len(edit_calls)}"
    )
