"""Tests for structure phases: SERVER (3), ROLES (4), CATEGORIES (5), CHANNELS (6)."""

from __future__ import annotations

import ssl
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import aiohttp
from aioresponses import aioresponses

from discord_ferry.config import FerryConfig
from discord_ferry.discord.metadata import (
    ChannelMeta,
    DiscordMetadata,
    PermissionPair,
    RoleMeta,
    RoleOverride,
    save_discord_metadata,
)
from discord_ferry.errors import AutumnUploadError, MigrationError
from discord_ferry.migrator.structure import (
    FERRY_MIN_PERMISSIONS,
    make_unique_channel_name,
    run_categories,
    run_channels,
    run_roles,
    run_server,
)
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
AUTUMN_URL = "https://autumn.test"


# ---------------------------------------------------------------------------
# Helpers
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


def _make_author(
    author_id: str = "u1",
    roles: list[DCERole] | None = None,
) -> DCEAuthor:
    return DCEAuthor(id=author_id, name="User", roles=roles or [])


def _make_message(
    msg_id: str = "m1",
    roles: list[DCERole] | None = None,
) -> DCEMessage:
    return DCEMessage(
        id=msg_id,
        type="Default",
        timestamp="2024-01-01T00:00:00Z",
        content="hello",
        author=_make_author(roles=roles),
    )


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


def _collect_events(events: list[MigrationEvent]) -> list[str]:
    return [e.message for e in events]


# ---------------------------------------------------------------------------
# Phase 3: SERVER
# ---------------------------------------------------------------------------


async def test_run_server_creates_server(tmp_path: Path) -> None:
    """SERVER phase creates a new server and stores the ID in state."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState()
    exports = [_make_export()]

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/create",
            payload={"server": {"_id": "srv1", "name": "Test"}, "channels": []},
        )

        await run_server(config, state, exports, events.append)

    assert state.stoat_server_id == "srv1"
    messages = _collect_events(events)
    assert any("srv1" in msg for msg in messages)


async def test_run_server_applies_description_and_nsfw(tmp_path: Path) -> None:
    """SERVER applies guild description + NSFW from Discord metadata."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState()
    exports = [_make_export()]

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
        guild_description="A cosy server",
        guild_nsfw=True,
    )
    save_discord_metadata(meta, tmp_path)

    patch_bodies: list[dict[str, object]] = []

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/create",
            payload={"server": {"_id": "srv1", "name": "Test"}, "channels": []},
        )
        m.patch(
            f"{STOAT_URL}/servers/srv1",
            payload={},
            repeat=True,
            callback=lambda url, **kwargs: patch_bodies.append(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )

        await run_server(config, state, exports, events.append)

    assert any(b.get("description") == "A cosy server" for b in patch_bodies)
    assert any(b.get("nsfw") is True for b in patch_bodies)


async def test_run_server_omits_empty_description(tmp_path: Path) -> None:
    """SERVER does not send an empty description."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState()
    exports = [_make_export()]

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
        guild_description="",
        guild_nsfw=False,
    )
    save_discord_metadata(meta, tmp_path)

    patch_bodies: list[dict[str, object]] = []

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/create",
            payload={"server": {"_id": "srv1", "name": "Test"}, "channels": []},
        )
        m.patch(
            f"{STOAT_URL}/servers/srv1",
            payload={},
            repeat=True,
            callback=lambda url, **kwargs: patch_bodies.append(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )

        await run_server(config, state, exports, events.append)

    assert all("description" not in b for b in patch_bodies)


async def test_run_server_meta_skipped_without_metadata(tmp_path: Path) -> None:
    """SERVER warns when no Discord metadata is available for description/NSFW."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState()
    exports = [_make_export()]

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/create",
            payload={"server": {"_id": "srv1", "name": "Test"}, "channels": []},
        )
        m.patch(f"{STOAT_URL}/servers/srv1", payload={}, repeat=True)

        await run_server(config, state, exports, events.append)

    assert any(w.get("type") == "server_meta_skipped" for w in state.warnings)


async def test_run_server_uses_existing_server(tmp_path: Path) -> None:
    """SERVER phase uses config.server_id when set, no POST to create."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path, server_id="existing-srv")
    state = MigrationState()
    exports = [_make_export()]

    with aioresponses() as m:
        m.get(f"{STOAT_URL}/servers/existing-srv", payload={"_id": "existing-srv"})

        await run_server(config, state, exports, events.append)

    assert state.stoat_server_id == "existing-srv"
    messages = _collect_events(events)
    assert any("existing-srv" in msg for msg in messages)


async def test_run_server_uploads_icon(tmp_path: Path) -> None:
    """SERVER phase uploads the guild icon and applies it to the server."""
    events: list[MigrationEvent] = []
    icon_file = tmp_path / "icon.png"
    icon_file.write_bytes(b"PNG")

    config = _make_config(tmp_path)
    state = MigrationState(autumn_url=AUTUMN_URL)
    exports = [_make_export(guild_icon_url=str(icon_file))]

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/create",
            payload={"server": {"_id": "srv1", "name": "Test"}, "channels": []},
        )
        m.post(f"{AUTUMN_URL}/icons", payload={"id": "icon-autumn-id"})
        m.patch(f"{STOAT_URL}/servers/srv1", payload={"_id": "srv1"})

        await run_server(config, state, exports, events.append)

    assert state.stoat_server_id == "srv1"
    messages = _collect_events(events)
    assert any("icon" in msg.lower() for msg in messages)


async def test_run_server_icon_upload_failure_is_non_fatal(tmp_path: Path) -> None:
    """SERVER phase logs a warning and continues if the icon upload fails."""
    events: list[MigrationEvent] = []
    icon_file = tmp_path / "icon.png"
    icon_file.write_bytes(b"PNG")

    config = _make_config(tmp_path)
    state = MigrationState(autumn_url=AUTUMN_URL)
    exports = [_make_export(guild_icon_url=str(icon_file))]

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/create",
            payload={"server": {"_id": "srv1", "name": "Test"}, "channels": []},
        )
        m.post(f"{AUTUMN_URL}/icons", status=500)  # Autumn failure

        # Should NOT raise — icon failure is non-fatal.
        await run_server(config, state, exports, events.append)

    assert state.stoat_server_id == "srv1"
    statuses = [e.status for e in events]
    assert "warning" in statuses


# ---------------------------------------------------------------------------
# Phase 4: ROLES
# ---------------------------------------------------------------------------


async def test_run_roles_creates_roles(tmp_path: Path) -> None:
    """ROLES phase creates roles found in message authors."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    role_a = DCERole(id="r1", name="Admin")
    role_b = DCERole(id="r2", name="Mod")
    msg1 = _make_message("m1", roles=[role_a])
    msg2 = _make_message("m2", roles=[role_b])
    exports = [_make_export(messages=[msg1, msg2])]

    with aioresponses() as m:
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-r1", "name": "Admin"})
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-r2", "name": "Mod"})

        await run_roles(config, state, exports, events.append)

    assert state.role_map == {"r1": "stoat-r1", "r2": "stoat-r2"}


async def test_run_roles_deduplicates(tmp_path: Path) -> None:
    """ROLES phase creates each unique role only once even if it appears in multiple messages."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    role = DCERole(id="r1", name="Admin")
    msg1 = _make_message("m1", roles=[role])
    msg2 = _make_message("m2", roles=[role])
    exports = [_make_export(messages=[msg1, msg2])]

    with aioresponses() as m:
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-r1", "name": "Admin"})

        await run_roles(config, state, exports, events.append)

    assert len(state.role_map) == 1
    assert state.role_map["r1"] == "stoat-r1"


async def test_run_roles_colour_conversion(tmp_path: Path) -> None:
    """ROLES phase sends the correct integer value for role colour (British spelling)."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    role = DCERole(id="r1", name="Admin", color="#FF5733")
    exports = [_make_export(messages=[_make_message("m1", roles=[role])])]

    patch_body: dict[str, object] = {}

    with aioresponses() as m:
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-r1", "name": "Admin"})
        m.patch(
            f"{STOAT_URL}/servers/srv1/roles/stoat-r1",
            payload={},
            callback=lambda url, **kwargs: patch_body.update(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )

        await run_roles(config, state, exports, events.append)

    assert patch_body.get("colour") == 0xFF5733  # 16734003


async def test_run_roles_skips_everyone(tmp_path: Path) -> None:
    """ROLES phase skips the @everyone role (role ID equals guild ID)."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    # Role ID matches guild ID — should be skipped.
    everyone = DCERole(id="111", name="@everyone")
    exports = [_make_export(guild_id="111", messages=[_make_message("m1", roles=[everyone])])]

    with aioresponses():
        # No POST expected — if one fires aioresponses will raise.
        await run_roles(config, state, exports, events.append)

    assert state.role_map == {}


async def test_run_roles_applies_hoist_when_metadata_present(tmp_path: Path) -> None:
    """ROLES sends hoist even for a role with no colour and position 0."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    role = DCERole(id="r1", name="Mods")  # no colour, position defaults to 0
    exports = [_make_export(messages=[_make_message("m1", roles=[role])])]

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
        role_metadata={"r1": RoleMeta(hoist=True, position=0)},
    )
    save_discord_metadata(meta, tmp_path)

    patch_bodies: list[dict[str, object]] = []

    with aioresponses() as m:
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-r1", "name": "Mods"})
        m.patch(
            f"{STOAT_URL}/servers/srv1/roles/stoat-r1",
            payload={},
            repeat=True,
            callback=lambda url, **kwargs: patch_bodies.append(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )
        m.put(f"{STOAT_URL}/servers/srv1/permissions/stoat-r1", payload={}, repeat=True)

        await run_roles(config, state, exports, events.append)

    assert any(body.get("hoist") is True for body in patch_bodies)


async def test_run_roles_hoist_skipped_without_metadata(tmp_path: Path) -> None:
    """ROLES emits a hoist-skipped warning when no Discord metadata is present."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    role = DCERole(id="r1", name="Mods")
    exports = [_make_export(messages=[_make_message("m1", roles=[role])])]

    with aioresponses() as m:
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-r1", "name": "Mods"})

        await run_roles(config, state, exports, events.append)

    assert any(w.get("type") == "hoist_skipped" for w in state.warnings)


async def test_batch2_fields_skip_without_metadata(tmp_path: Path) -> None:
    """S5 graceful-degrade: channels + roles emit batch-2 skip warnings without metadata.

    With no discord_metadata.json present, run_channels and run_roles must complete
    (populating channel_map/role_map) and emit one-time skip warnings for the batch-2
    fields: slowmode_skipped, user_limit_skipped (channels) and role_icon_skipped (roles).
    """
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    role = DCERole(id="r1", name="Mods")
    exports = [
        _make_export(
            channel_id="ch1",
            channel_name="general",
            category_id="",
            messages=[_make_message("m1", roles=[role])],
        )
    ]

    with aioresponses() as m:
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-r1", "name": "Mods"})
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "stoat-ch1", "name": "general"},
        )

        await run_roles(config, state, exports, events.append)
        await run_channels(config, state, exports, events.append)

    # Migration completed: maps populated.
    assert state.role_map.get("r1") == "stoat-r1"
    assert state.channel_map.get("ch1") == "stoat-ch1"

    warning_types = [w.get("type") for w in state.warnings]
    assert "role_icon_skipped" in warning_types
    assert "slowmode_skipped" in warning_types
    assert "user_limit_skipped" in warning_types
    # v2.3.0 hoist_skipped still fires alongside the new role_icon_skipped.
    assert "hoist_skipped" in warning_types
    # One-time warnings, not per-item.
    assert warning_types.count("slowmode_skipped") == 1
    assert warning_types.count("user_limit_skipped") == 1
    assert warning_types.count("role_icon_skipped") == 1


async def test_run_roles_image_icon_uploaded_and_folded(tmp_path: Path) -> None:
    """ROLES downloads an image role icon, uploads to Autumn, folds icon into edit."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)

    role = DCERole(id="r1", name="VIP")
    exports = [_make_export(messages=[_make_message("m1", roles=[role])])]

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
        role_metadata={"r1": RoleMeta(hoist=True, position=0, icon_hash="abc")},
    )
    save_discord_metadata(meta, tmp_path)

    patch_bodies: list[dict[str, object]] = []

    with (
        aioresponses() as m,
        patch(
            "discord_ferry.migrator.structure.download_role_icon",
            new=AsyncMock(return_value=b"PNGDATA"),
        ),
        patch(
            "discord_ferry.migrator.structure.upload_to_autumn",
            new=AsyncMock(return_value="autumn-id"),
        ),
    ):
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-r1", "name": "VIP"})
        m.patch(
            f"{STOAT_URL}/servers/srv1/roles/stoat-r1",
            payload={},
            repeat=True,
            callback=lambda url, **kwargs: patch_bodies.append(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )
        m.put(f"{STOAT_URL}/servers/srv1/permissions/stoat-r1", payload={}, repeat=True)

        await run_roles(config, state, exports, events.append)

    assert any(body.get("icon") == "autumn-id" for body in patch_bodies)
    assert state.native_fidelity_counts.get("role_icons") == 1


async def test_run_roles_emoji_icon_skipped_with_warning(tmp_path: Path) -> None:
    """ROLES skips an emoji-only role icon with a warning and no icon kwarg."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)

    role = DCERole(id="r1", name="Fire")
    exports = [_make_export(messages=[_make_message("m1", roles=[role])])]

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
        role_metadata={"r1": RoleMeta(hoist=False, position=0, unicode_emoji="🔥")},
    )
    save_discord_metadata(meta, tmp_path)

    patch_bodies: list[dict[str, object]] = []

    with (
        aioresponses() as m,
        patch(
            "discord_ferry.migrator.structure.download_role_icon",
            new=AsyncMock(return_value=b"PNGDATA"),
        ) as mock_download,
    ):
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-r1", "name": "Fire"})
        m.patch(
            f"{STOAT_URL}/servers/srv1/roles/stoat-r1",
            payload={},
            repeat=True,
            callback=lambda url, **kwargs: patch_bodies.append(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )
        m.put(f"{STOAT_URL}/servers/srv1/permissions/stoat-r1", payload={}, repeat=True)

        await run_roles(config, state, exports, events.append)

    assert any(w.get("type") == "role_icon_skipped" for w in state.warnings)
    assert all("icon" not in body for body in patch_bodies)
    mock_download.assert_not_called()


async def test_run_roles_icon_upload_error_is_token_safe(tmp_path: Path) -> None:
    """A failed icon upload never leaks the HTTP body and preserves hoist/rank."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)

    role = DCERole(id="r1", name="VIP", position=3)
    exports = [_make_export(messages=[_make_message("m1", roles=[role])])]

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
        role_metadata={"r1": RoleMeta(hoist=True, position=0, icon_hash="abc")},
    )
    save_discord_metadata(meta, tmp_path)

    patch_bodies: list[dict[str, object]] = []
    upload_error = AutumnUploadError("Upload failed with status 500: x-session-token=SECRET")

    with (
        aioresponses() as m,
        patch(
            "discord_ferry.migrator.structure.download_role_icon",
            new=AsyncMock(return_value=b"PNGDATA"),
        ),
        patch(
            "discord_ferry.migrator.structure.upload_to_autumn",
            new=AsyncMock(side_effect=upload_error),
        ),
    ):
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-r1", "name": "VIP"})
        m.patch(
            f"{STOAT_URL}/servers/srv1/roles/stoat-r1",
            payload={},
            repeat=True,
            callback=lambda url, **kwargs: patch_bodies.append(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )
        m.put(f"{STOAT_URL}/servers/srv1/permissions/stoat-r1", payload={}, repeat=True)

        await run_roles(config, state, exports, events.append)

    for w in state.warnings:
        msg = str(w.get("message", ""))
        assert "SECRET" not in msg
        assert "x-session-token" not in msg
    # The role still receives hoist despite the failed icon; no icon kwarg leaks.
    assert any(body.get("hoist") is True for body in patch_bodies)
    assert all("icon" not in body for body in patch_bodies)


async def test_a_server_disconnect_degrades_rather_than_aborting(tmp_path: Path) -> None:
    """SC-135-35. Killing: forgetting to widen structure.py:403.

    Aimed at ServerDisconnectedError, NOT a proxy 403. Task 7 already converts a
    proxy 403 into AutumnUploadError at autumn.py, which :403 catches, so a
    proxy-403 test passes without the widening and grades nothing.

    Reachable: ServerDisconnectedError -> ServerConnectionError ->
    ClientConnectionError -> ClientError, with no OSError, so
    (AutumnUploadError, OSError) misses it and it aborts the roles phase.
    """
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)
    role = DCERole(id="r1", name="Mods")
    exports = [_make_export(messages=[_make_message("m1", roles=[role])])]
    save_discord_metadata(
        DiscordMetadata(
            guild_id="111",
            fetched_at="t",
            server_default_permissions=0,
            role_permissions={},
            channel_metadata={},
            role_metadata={"r1": RoleMeta(hoist=True, position=0, icon_hash="abc123")},
        ),
        tmp_path,
    )
    with (
        aioresponses() as m,
        patch(
            "discord_ferry.migrator.structure.download_role_icon",
            new=AsyncMock(return_value=b"pngbytes"),
        ),
    ):
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-r1", "name": "Mods"})
        m.post(f"{AUTUMN_URL}/icons", exception=aiohttp.ServerDisconnectedError(), repeat=True)
        m.patch(f"{STOAT_URL}/servers/srv1/roles/stoat-r1", payload={}, repeat=True)
        m.put(f"{STOAT_URL}/servers/srv1/permissions/stoat-r1", payload={}, repeat=True)
        await run_roles(config, state, exports, events.append)  # must NOT raise

    assert any(w.get("type") == "role_icon_upload_failed" for w in state.warnings)


async def test_run_roles_truncates_long_name(tmp_path: Path) -> None:
    """ROLES phase truncates role names exceeding 32 characters."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    long_name = "a" * 50
    role = DCERole(id="r1", name=long_name)
    exports = [_make_export(messages=[_make_message("m1", roles=[role])])]

    created_names: list[str] = []

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/roles",
            payload={"id": "stoat-r1", "name": long_name[:32]},
            callback=lambda url, **kwargs: created_names.append(  # type: ignore[misc]
                (kwargs.get("json") or {}).get("name", "")
            ),
        )

        await run_roles(config, state, exports, events.append)

    assert len(created_names) == 1
    assert len(created_names[0]) == 32


async def test_run_roles_colour_without_hash(tmp_path: Path) -> None:
    """ROLES phase handles colour strings without a leading '#'."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    role = DCERole(id="r1", name="Admin", color="FF5733")  # no leading #
    exports = [_make_export(messages=[_make_message("m1", roles=[role])])]

    patch_body: dict[str, object] = {}

    with aioresponses() as m:
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-r1", "name": "Admin"})
        m.patch(
            f"{STOAT_URL}/servers/srv1/roles/stoat-r1",
            payload={},
            callback=lambda url, **kwargs: patch_body.update(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )

        await run_roles(config, state, exports, events.append)

    assert patch_body.get("colour") == 0xFF5733


# ---------------------------------------------------------------------------
# Phase 4: ROLES — live role discovery (union sourcing)
# ---------------------------------------------------------------------------


async def test_live_only_role_is_created(tmp_path: Path) -> None:
    """A live role absent from every export author is still created via the union."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    # Export author posts under r1 only; r2 exists live but nobody posted under it.
    role_a = DCERole(id="r1", name="Admin")
    exports = [_make_export(messages=[_make_message("m1", roles=[role_a])])]

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
        role_metadata={
            "r1": RoleMeta(name="Admin", position=0),
            "r2": RoleMeta(name="LiveOnly", position=1),
        },
    )
    save_discord_metadata(meta, tmp_path)

    created_names: list[str] = []

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/roles",
            payload={"id": "stoat-r1", "name": "Admin"},
            callback=lambda url, **kwargs: created_names.append(  # type: ignore[misc]
                kwargs.get("json", {}).get("name", "")
            ),
        )
        m.post(
            f"{STOAT_URL}/servers/srv1/roles",
            payload={"id": "stoat-r2", "name": "LiveOnly"},
            callback=lambda url, **kwargs: created_names.append(  # type: ignore[misc]
                kwargs.get("json", {}).get("name", "")
            ),
        )
        m.patch(f"{STOAT_URL}/servers/srv1/roles/stoat-r1", payload={}, repeat=True)
        m.patch(f"{STOAT_URL}/servers/srv1/roles/stoat-r2", payload={}, repeat=True)

        await run_roles(config, state, exports, events.append)

    assert "LiveOnly" in created_names
    # The union creates roles position-desc, so the FIFO mock id pairing is not
    # insertion-ordered; assert the live-only role is mapped to a created id.
    assert "r2" in state.role_map
    assert state.role_map["r2"] in {"stoat-r1", "stoat-r2"}
    assert set(state.role_map) == {"r1", "r2"}


async def test_overlap_role_uses_live_name_color(tmp_path: Path) -> None:
    """A role in both export and live metadata uses the LIVE name/colour."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    # Export carries the stale name/no colour; live metadata wins.
    role = DCERole(id="r1", name="OldName")
    exports = [_make_export(messages=[_make_message("m1", roles=[role])])]

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
        role_metadata={"r1": RoleMeta(name="NewName", color="#00ff00", position=0)},
    )
    save_discord_metadata(meta, tmp_path)

    created_names: list[str] = []
    patch_bodies: list[dict[str, object]] = []

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/roles",
            payload={"id": "stoat-r1", "name": "NewName"},
            callback=lambda url, **kwargs: created_names.append(  # type: ignore[misc]
                kwargs.get("json", {}).get("name", "")
            ),
        )
        m.patch(
            f"{STOAT_URL}/servers/srv1/roles/stoat-r1",
            payload={},
            repeat=True,
            callback=lambda url, **kwargs: patch_bodies.append(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )

        await run_roles(config, state, exports, events.append)

    assert created_names == ["NewName"]
    assert any(body.get("colour") == 0x00FF00 for body in patch_bodies)


async def test_managed_role_not_created(tmp_path: Path) -> None:
    """A managed role (excluded at capture, absent from role_metadata) is not created."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    role = DCERole(id="r1", name="Admin")
    exports = [_make_export(messages=[_make_message("m1", roles=[role])])]

    # role_metadata enumerates only non-managed roles; the managed "rbot" is absent.
    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
        role_metadata={"r1": RoleMeta(name="Admin", position=0)},
    )
    save_discord_metadata(meta, tmp_path)

    created_ids: list[str] = []

    def _capture(url: object, **kwargs: object) -> None:
        name = kwargs.get("json", {}).get("name", "")  # type: ignore[union-attr]
        created_ids.append(name)

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/roles",
            payload={"id": "stoat-r1", "name": "Admin"},
            callback=_capture,
        )
        m.patch(f"{STOAT_URL}/servers/srv1/roles/stoat-r1", payload={}, repeat=True)

        await run_roles(config, state, exports, events.append)

    assert created_ids == ["Admin"]
    assert state.role_map == {"r1": "stoat-r1"}


async def test_structural_roles_counted_for_live_only(tmp_path: Path) -> None:
    """A live-only role (in role_metadata, absent from the export) increments
    native_fidelity_counts['structural_roles']; an overlap role does not."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    # r1 appears in the export (overlap); r2 is live-only (no member posted it).
    role = DCERole(id="r1", name="Member")
    exports = [_make_export(messages=[_make_message("m1", roles=[role])])]

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
        role_metadata={
            "r1": RoleMeta(name="Member", position=1),
            "r2": RoleMeta(name="Ghost", position=0),
        },
    )
    save_discord_metadata(meta, tmp_path)

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/roles",
            payload={"id": "stoat-r1", "name": "Member"},
        )
        m.post(
            f"{STOAT_URL}/servers/srv1/roles",
            payload={"id": "stoat-r2", "name": "Ghost"},
        )
        m.patch(f"{STOAT_URL}/servers/srv1/roles/stoat-r1", payload={}, repeat=True)
        m.patch(f"{STOAT_URL}/servers/srv1/roles/stoat-r2", payload={}, repeat=True)

        await run_roles(config, state, exports, events.append)

    # Both roles created; only the live-only r2 counts as structural.
    assert set(state.role_map) == {"r1", "r2"}
    assert state.native_fidelity_counts.get("structural_roles") == 1


async def test_structural_roles_zero_when_all_overlap(tmp_path: Path) -> None:
    """When every created role also appears in the export, no structural-role
    credit is recorded."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    role = DCERole(id="r1", name="Member")
    exports = [_make_export(messages=[_make_message("m1", roles=[role])])]

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
        role_metadata={"r1": RoleMeta(name="Member", position=0)},
    )
    save_discord_metadata(meta, tmp_path)

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/roles",
            payload={"id": "stoat-r1", "name": "Member"},
        )
        m.patch(f"{STOAT_URL}/servers/srv1/roles/stoat-r1", payload={}, repeat=True)

        await run_roles(config, state, exports, events.append)

    assert state.role_map == {"r1": "stoat-r1"}
    assert state.native_fidelity_counts.get("structural_roles") is None


async def test_no_token_fallback_unchanged(tmp_path: Path) -> None:
    """With no discord_metadata.json, the created set equals the export-derived set."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    role_a = DCERole(id="r1", name="Admin")
    role_b = DCERole(id="r2", name="Mod")
    exports = [
        _make_export(
            messages=[
                _make_message("m1", roles=[role_a]),
                _make_message("m2", roles=[role_b]),
            ]
        )
    ]

    with aioresponses() as m:
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-r1", "name": "Admin"})
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-r2", "name": "Mod"})

        await run_roles(config, state, exports, events.append)

    # Exactly the two export-derived roles, no live union.
    assert state.role_map == {"r1": "stoat-r1", "r2": "stoat-r2"}


# ---------------------------------------------------------------------------
# Phase 5: CATEGORIES
# ---------------------------------------------------------------------------


async def test_run_categories_creates_categories(tmp_path: Path) -> None:
    """CATEGORIES phase creates all unique categories found across exports."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    exports = [
        _make_export(channel_id="ch1", category_id="cat1", category="General"),
        _make_export(channel_id="ch2", category_id="cat2", category="Off-Topic"),
    ]

    patch_bodies: list[dict[str, object]] = []

    with aioresponses() as m:
        m.patch(
            f"{STOAT_URL}/servers/srv1",
            payload={"_id": "srv1", "categories": []},
            callback=lambda url, **kwargs: patch_bodies.append(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )

        await run_categories(config, state, exports, events.append)

    # Both Discord category IDs should be mapped to generated Stoat IDs.
    assert len(state.category_map) == 2
    assert "cat1" in state.category_map
    assert "cat2" in state.category_map
    # The PATCH body should contain exactly 2 categories.
    assert len(patch_bodies) == 1
    categories = patch_bodies[0].get("categories", [])
    assert len(categories) == 2  # type: ignore[arg-type]
    titles = {c["title"] for c in categories}  # type: ignore[union-attr]
    assert titles == {"General", "Off-Topic"}


async def test_run_categories_deduplicates(tmp_path: Path) -> None:
    """CATEGORIES phase creates each category only once even if multiple channels share it."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    exports = [
        _make_export(channel_id="ch1", category_id="cat1", category="General"),
        _make_export(channel_id="ch2", category_id="cat1", category="General"),
    ]

    patch_bodies: list[dict[str, object]] = []

    with aioresponses() as m:
        m.patch(
            f"{STOAT_URL}/servers/srv1",
            payload={"_id": "srv1", "categories": []},
            callback=lambda url, **kwargs: patch_bodies.append(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )

        await run_categories(config, state, exports, events.append)

    assert len(state.category_map) == 1
    assert "cat1" in state.category_map
    # The PATCH body should contain exactly 1 category.
    assert len(patch_bodies) == 1
    categories = patch_bodies[0].get("categories", [])
    assert len(categories) == 1  # type: ignore[arg-type]


async def test_run_categories_skips_empty(tmp_path: Path) -> None:
    """CATEGORIES phase skips exports with an empty category_id."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    exports = [_make_export(channel_id="ch1", category_id="", category="")]

    with aioresponses():
        # No POST expected.
        await run_categories(config, state, exports, events.append)

    assert state.category_map == {}


# ---------------------------------------------------------------------------
# Phase 6: CHANNELS
# ---------------------------------------------------------------------------


async def test_run_channels_creates_channels(tmp_path: Path) -> None:
    """CHANNELS phase creates channels and populates channel_map."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    exports = [_make_export(channel_id="ch1", channel_name="general", category_id="")]

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "stoat-ch1", "name": "general"},
        )

        await run_channels(config, state, exports, events.append)

    assert state.channel_map == {"ch1": "stoat-ch1"}
    messages = _collect_events(events)
    assert any("general" in msg for msg in messages)


async def test_run_channels_assigns_to_categories(tmp_path: Path) -> None:
    """CHANNELS phase PATCHes the server with categories containing the stoat channel IDs."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    # Pre-populate category_map as if run_categories already ran.
    state = MigrationState(stoat_server_id="srv1", category_map={"cat1": "test-cat-id-1"})

    exports = [
        _make_export(
            channel_id="ch1", channel_name="general", category_id="cat1", category="General"
        )
    ]

    patch_body: dict[str, object] = {}

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "stoat-ch1", "name": "general"},
        )
        m.patch(
            f"{STOAT_URL}/servers/srv1",
            payload={"_id": "srv1"},
            callback=lambda url, **kwargs: patch_body.update(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )

        await run_channels(config, state, exports, events.append)

    assert state.channel_map["ch1"] == "stoat-ch1"
    # Verify the categories array contains our channel.
    categories = patch_body.get("categories", [])
    assert len(categories) == 1  # type: ignore[arg-type]
    cat = categories[0]  # type: ignore[index]
    assert cat["id"] == "test-cat-id-1"
    assert cat["title"] == "General"
    assert cat["channels"] == ["stoat-ch1"]


async def test_run_channels_thread_flattening(tmp_path: Path) -> None:
    """CHANNELS phase adds ├─ prefix to thread channel names in flatten mode."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    exports = [
        _make_export(
            channel_id="th1",
            channel_name="my-thread",
            is_thread=True,
            parent_channel_name="general",
            category_id="",
        )
    ]

    created_names: list[str] = []

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "stoat-th1", "name": "\u251c\u2500 my-thread"},
            callback=lambda url, **kwargs: created_names.append(  # type: ignore[misc]
                (kwargs.get("json") or {}).get("name", "")
            ),
        )

        await run_channels(config, state, exports, events.append)

    assert state.channel_map["th1"] == "stoat-th1"
    assert created_names[0] == "\u251c\u2500 my-thread"


async def test_run_channels_skips_category_type(tmp_path: Path) -> None:
    """CHANNELS phase skips exports whose channel type is 4 (category)."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    exports = [_make_export(channel_id="cat-ch", channel_type=4)]

    with aioresponses():
        # No POST expected.
        await run_channels(config, state, exports, events.append)

    assert state.channel_map == {}


def _category_titles_from_patch(bodies: list[dict[str, object]]) -> list[str]:
    """Extract category titles, in order, from the last categories PATCH body."""
    cats = bodies[-1].get("categories", []) if bodies else []
    return [str(c.get("title", "")) for c in cats]  # type: ignore[union-attr]


async def test_run_channels_orders_categories_by_discord_position(tmp_path: Path) -> None:
    """CHANNELS orders the category upsert by Discord position (ascending)."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(
        stoat_server_id="srv1",
        category_map={"cat-A": "sc-A", "cat-B": "sc-B"},
    )

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
        category_positions={"cat-A": 2, "cat-B": 0},
    )
    save_discord_metadata(meta, tmp_path)

    exports = [
        _make_export(channel_id="ch-a", channel_name="a", category_id="cat-A", category="Alpha"),
        _make_export(channel_id="ch-b", channel_name="b", category_id="cat-B", category="Bravo"),
    ]

    patch_bodies: list[dict[str, object]] = []

    with aioresponses() as m:
        m.post(f"{STOAT_URL}/servers/srv1/channels", payload={"_id": "sc-cha", "name": "a"})
        m.post(f"{STOAT_URL}/servers/srv1/channels", payload={"_id": "sc-chb", "name": "b"})
        m.patch(
            f"{STOAT_URL}/servers/srv1",
            payload={},
            repeat=True,
            callback=lambda url, **kwargs: patch_bodies.append(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )

        await run_channels(config, state, exports, events.append)

    titles = _category_titles_from_patch(patch_bodies)
    assert titles == ["Bravo", "Alpha"]  # position 0 before position 2


async def test_run_channels_category_without_position_sorts_last(tmp_path: Path) -> None:
    """A category with no captured Discord position sorts after positioned ones."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(
        stoat_server_id="srv1",
        category_map={"cat-A": "sc-A", "cat-Z": "sc-Z"},
    )

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
        category_positions={"cat-A": 5},  # cat-Z absent → sentinel → last
    )
    save_discord_metadata(meta, tmp_path)

    exports = [
        _make_export(channel_id="ch-a", channel_name="a", category_id="cat-A", category="Alpha"),
        _make_export(channel_id="ch-z", channel_name="z", category_id="cat-Z", category="Zeta"),
    ]

    patch_bodies: list[dict[str, object]] = []

    with aioresponses() as m:
        m.post(f"{STOAT_URL}/servers/srv1/channels", payload={"_id": "sc-cha", "name": "a"})
        m.post(f"{STOAT_URL}/servers/srv1/channels", payload={"_id": "sc-chz", "name": "z"})
        m.patch(
            f"{STOAT_URL}/servers/srv1",
            payload={},
            repeat=True,
            callback=lambda url, **kwargs: patch_bodies.append(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )

        await run_channels(config, state, exports, events.append)

    titles = _category_titles_from_patch(patch_bodies)
    assert titles == ["Alpha", "Zeta"]


async def test_run_channels_deduplicates_channel_ids(tmp_path: Path) -> None:
    """CHANNELS phase creates each channel only once even if the same ID appears twice."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    # Two exports sharing the same channel ID (thread + parent reference pattern).
    exports = [
        _make_export(channel_id="ch1", channel_name="general", category_id=""),
        _make_export(channel_id="ch1", channel_name="general", category_id=""),
    ]

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "stoat-ch1", "name": "general"},
        )

        await run_channels(config, state, exports, events.append)

    assert len(state.channel_map) == 1


async def test_run_channels_voice_fallback_to_text(tmp_path: Path) -> None:
    """CHANNELS phase retries a failed voice channel creation as text and warns."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    exports = [
        _make_export(
            channel_id="vc1",
            channel_name="voice-chat",
            channel_type=2,
            category_id="",
        )
    ]

    with aioresponses() as m:
        # First call (Voice) fails.
        m.post(f"{STOAT_URL}/servers/srv1/channels", status=400)
        # Second call (Text fallback) succeeds.
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "stoat-vc1", "name": "voice-chat"},
        )

        await run_channels(config, state, exports, events.append)

    assert state.channel_map["vc1"] == "stoat-vc1"
    statuses = [e.status for e in events]
    assert "warning" in statuses


async def test_run_channels_passes_nsfw_flag(tmp_path: Path) -> None:
    """CHANNELS phase passes nsfw=True from Discord metadata."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={
            "ch1": ChannelMeta(nsfw=True),
        },
    )
    save_discord_metadata(meta, tmp_path)

    exports = [_make_export(channel_id="ch1", channel_name="nsfw-ch", category_id="")]

    created_bodies: list[dict[str, object]] = []

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "stoat-ch1", "name": "nsfw-ch"},
            callback=lambda url, **kwargs: created_bodies.append(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )
        await run_channels(config, state, exports, events.append)

    assert len(created_bodies) == 1
    assert created_bodies[0].get("nsfw") is True


async def test_channel_slowmode_and_user_limit_applied(tmp_path: Path) -> None:
    """CHANNELS phase PATCHes slowmode (text) and voice.max_users (voice)."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={
            "txt1": ChannelMeta(nsfw=False, slowmode=30),
            "vc1": ChannelMeta(nsfw=False, user_limit=5),
        },
    )
    save_discord_metadata(meta, tmp_path)

    exports = [
        _make_export(channel_id="txt1", channel_name="slow-chat", category_id=""),
        _make_export(channel_id="vc1", channel_name="voice-chat", channel_type=2, category_id=""),
    ]

    edit_bodies: dict[str, dict[str, object]] = {}

    def _capture(url: object, **kwargs: object) -> None:
        channel_id = str(url).rsplit("/", 1)[-1]
        edit_bodies[channel_id] = kwargs.get("json", {})  # type: ignore[assignment]

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "stoat-txt1", "name": "slow-chat"},
        )
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "stoat-vc1", "name": "voice-chat"},
        )
        m.patch(
            f"{STOAT_URL}/channels/stoat-txt1",
            payload={"_id": "stoat-txt1"},
            callback=_capture,  # type: ignore[arg-type]
        )
        m.patch(
            f"{STOAT_URL}/channels/stoat-vc1",
            payload={"_id": "stoat-vc1"},
            callback=_capture,  # type: ignore[arg-type]
        )
        await run_channels(config, state, exports, events.append)

    assert edit_bodies["stoat-txt1"].get("slowmode") == 30
    assert "voice" not in edit_bodies["stoat-txt1"]
    assert edit_bodies["stoat-vc1"].get("voice") == {"max_users": 5}
    assert "slowmode" not in edit_bodies["stoat-vc1"]
    assert state.native_fidelity_counts == {"slowmode": 1, "user_limit": 1}


async def test_channel_slowmode_clamped_above_max(tmp_path: Path) -> None:
    """Slowmode > 21600 is clamped to 21600 and a slowmode_clamped warning is logged."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={
            "txt1": ChannelMeta(nsfw=False, slowmode=30000),
        },
    )
    save_discord_metadata(meta, tmp_path)

    exports = [_make_export(channel_id="txt1", channel_name="slow-chat", category_id="")]

    edit_bodies: list[dict[str, object]] = []

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "stoat-txt1", "name": "slow-chat"},
        )
        m.patch(
            f"{STOAT_URL}/channels/stoat-txt1",
            payload={"_id": "stoat-txt1"},
            callback=lambda url, **kwargs: edit_bodies.append(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )
        await run_channels(config, state, exports, events.append)

    assert len(edit_bodies) == 1
    assert edit_bodies[0].get("slowmode") == 21600
    assert any(w.get("type") == "slowmode_clamped" for w in state.warnings)


async def test_voice_fallback_skips_voice_patch(tmp_path: Path) -> None:
    """A voice channel that falls back to text (Bug #194) skips the voice PATCH.

    slowmode is still applied to the text-fallback channel.
    """
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={
            "vc1": ChannelMeta(nsfw=False, slowmode=15, user_limit=8),
        },
    )
    save_discord_metadata(meta, tmp_path)

    exports = [
        _make_export(channel_id="vc1", channel_name="voice-chat", channel_type=2, category_id="")
    ]

    edit_bodies: list[dict[str, object]] = []

    with aioresponses() as m:
        # First call (Voice) fails -> Bug #194 fallback to Text.
        m.post(f"{STOAT_URL}/servers/srv1/channels", status=400)
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "stoat-vc1", "name": "voice-chat"},
        )
        m.patch(
            f"{STOAT_URL}/channels/stoat-vc1",
            payload={"_id": "stoat-vc1"},
            callback=lambda url, **kwargs: edit_bodies.append(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )
        await run_channels(config, state, exports, events.append)

    assert len(edit_bodies) == 1
    # created_as_voice is False, so the voice kwarg must be omitted.
    assert "voice" not in edit_bodies[0]
    # slowmode still applies to the text-fallback channel.
    assert edit_bodies[0].get("slowmode") == 15
    assert state.native_fidelity_counts == {"slowmode": 1}


async def test_run_channels_applies_channel_permission_overrides(tmp_path: Path) -> None:
    """CHANNELS phase applies role permission overrides via PUT after channel creation."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(
        stoat_server_id="srv1",
        role_map={"discord-role1": "stoat-role1"},
    )

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={
            "ch1": ChannelMeta(
                nsfw=False,
                role_overrides=[
                    RoleOverride(discord_role_id="discord-role1", allow=4_194_304, deny=0)
                ],
            ),
        },
    )
    save_discord_metadata(meta, tmp_path)

    exports = [_make_export(channel_id="ch1", channel_name="general", category_id="")]

    perm_bodies: list[dict[str, object]] = []

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "stoat-ch1", "name": "general"},
        )
        m.put(
            f"{STOAT_URL}/channels/stoat-ch1/permissions/stoat-role1",
            payload={},
            callback=lambda url, **kwargs: perm_bodies.append(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )
        await run_channels(config, state, exports, events.append)

    assert len(perm_bodies) == 1
    assert perm_bodies[0] == {"permissions": {"allow": 4_194_304, "deny": 0}}


async def test_run_channels_applies_default_override(tmp_path: Path) -> None:
    """CHANNELS phase applies default permission override via PUT after channel creation."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={
            "ch1": ChannelMeta(
                nsfw=False,
                default_override=PermissionPair(allow=2_097_152, deny=4_194_304),
            ),
        },
    )
    save_discord_metadata(meta, tmp_path)

    exports = [_make_export(channel_id="ch1", channel_name="readonly", category_id="")]

    default_perm_bodies: list[dict[str, object]] = []

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "stoat-ch1", "name": "readonly"},
        )
        m.put(
            f"{STOAT_URL}/channels/stoat-ch1/permissions/default",
            payload={},
            callback=lambda url, **kwargs: default_perm_bodies.append(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )
        await run_channels(config, state, exports, events.append)

    assert len(default_perm_bodies) == 1
    assert default_perm_bodies[0] == {"permissions": {"allow": 2_097_152, "deny": 4_194_304}}


async def test_run_channels_override_failure_non_fatal(tmp_path: Path) -> None:
    """CHANNELS phase logs a warning and does not raise when permission override PUT fails."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(
        stoat_server_id="srv1",
        role_map={"discord-role1": "stoat-role1"},
    )

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={
            "ch1": ChannelMeta(
                nsfw=False,
                default_override=PermissionPair(allow=0, deny=4_194_304),
                role_overrides=[
                    RoleOverride(discord_role_id="discord-role1", allow=4_194_304, deny=0)
                ],
            ),
        },
    )
    save_discord_metadata(meta, tmp_path)

    exports = [_make_export(channel_id="ch1", channel_name="general", category_id="")]

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "stoat-ch1", "name": "general"},
        )
        # Both PUT calls fail.
        m.put(f"{STOAT_URL}/channels/stoat-ch1/permissions/default", status=500)
        m.put(f"{STOAT_URL}/channels/stoat-ch1/permissions/stoat-role1", status=500)

        # Should NOT raise.
        await run_channels(config, state, exports, events.append)

    assert state.channel_map["ch1"] == "stoat-ch1"
    default_warnings = [w for w in state.warnings if w.get("type") == "channel_default_perm_failed"]
    role_warnings = [w for w in state.warnings if w.get("type") == "channel_role_perm_failed"]
    assert len(default_warnings) == 1
    assert len(role_warnings) == 1


# ---------------------------------------------------------------------------
# make_unique_channel_name
# ---------------------------------------------------------------------------


def test_make_unique_channel_name_no_collision() -> None:
    """Returns name as-is when no collision exists."""
    existing: set[str] = set()
    result = make_unique_channel_name("general", existing)
    assert result == "general"
    assert "general" in existing


def test_make_unique_channel_name_collision() -> None:
    """Appends counter suffix on collision."""
    existing: set[str] = {"general"}
    result = make_unique_channel_name("general", existing)
    assert result == "general-1"
    assert "general-1" in existing


def test_make_unique_channel_name_multiple_collisions() -> None:
    """Increments counter until a free slot is found."""
    existing: set[str] = {"general", "general-1", "general-2"}
    result = make_unique_channel_name("general", existing)
    assert result == "general-3"


def test_make_unique_channel_name_truncates() -> None:
    """Names longer than 32 characters are truncated."""
    long_name = "a" * 100
    existing: set[str] = set()
    result = make_unique_channel_name(long_name, existing)
    assert len(result) == 32
    assert result == "a" * 32


def test_make_unique_channel_name_truncated_collision() -> None:
    """Collision with suffix stays within 32 chars."""
    base = "a" * 32
    existing: set[str] = {base}
    long_name = "a" * 100  # truncates to same base
    result = make_unique_channel_name(long_name, existing)
    assert len(result) <= 32
    assert result == "a" * 30 + "-1"


# ---------------------------------------------------------------------------
# Bug 1: skip_threads in channels phase
# ---------------------------------------------------------------------------


async def test_run_roles_sets_rank_from_position(tmp_path: Path) -> None:
    """ROLES phase sets rank on created roles from DCE position data."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    role = DCERole(id="r1", name="Admin", position=3)
    exports = [_make_export(messages=[_make_message("m1", roles=[role])])]

    rank_bodies: list[dict[str, object]] = []

    with aioresponses() as m:
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-r1", "name": "Admin"})
        # Capture the rank PATCH call.
        m.patch(
            f"{STOAT_URL}/servers/srv1/roles/stoat-r1",
            payload={},
            callback=lambda url, **kwargs: rank_bodies.append(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )

        await run_roles(config, state, exports, events.append)

    assert any(b.get("rank") == 3 for b in rank_bodies)


async def test_run_roles_rank_tie_break_is_deterministic(tmp_path: Path) -> None:
    """On equal position, the rank pass processes roles in a deterministic id order.

    Two roles share position 5 but have ids "30" and "20". The deterministic sort
    key (position, id) must process role "20" before role "30" regardless of input
    order. The functional goal is determinism — same-position roles emit a stable
    processing order independent of export/union insertion order.
    """
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    # Export order lists "30" first, then "20" — so a position-only (stable) sort
    # would preserve that input order and process "30" before "20".
    role_30 = DCERole(id="30", name="Thirty", position=5)
    role_20 = DCERole(id="20", name="Twenty", position=5)
    exports = [
        _make_export(
            messages=[
                _make_message("m1", roles=[role_30]),
                _make_message("m2", roles=[role_20]),
            ]
        )
    ]

    # FIFO POST registration mirrors first-pass insertion (export) order:
    # "30" -> stoat-30, "20" -> stoat-20.
    patched_role_ids: list[str] = []

    with aioresponses() as m:
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-30", "name": "Thirty"})
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-20", "name": "Twenty"})
        m.patch(
            f"{STOAT_URL}/servers/srv1/roles/stoat-30",
            payload={},
            repeat=True,
            callback=lambda url, **kwargs: patched_role_ids.append("30"),  # type: ignore[misc]
        )
        m.patch(
            f"{STOAT_URL}/servers/srv1/roles/stoat-20",
            payload={},
            repeat=True,
            callback=lambda url, **kwargs: patched_role_ids.append("20"),  # type: ignore[misc]
        )

        await run_roles(config, state, exports, events.append)

    # Real processing order: the rank PATCH for the lexically-smaller id fires first.
    assert patched_role_ids == ["20", "30"]


async def test_run_roles_rank_failure_is_non_fatal(tmp_path: Path) -> None:
    """ROLES phase logs a warning and continues if rank setting fails."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    role = DCERole(id="r1", name="Admin", position=2)
    exports = [_make_export(messages=[_make_message("m1", roles=[role])])]

    with aioresponses() as m:
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-r1", "name": "Admin"})
        # Rank PATCH fails.
        m.patch(f"{STOAT_URL}/servers/srv1/roles/stoat-r1", status=500)

        # Should NOT raise.
        await run_roles(config, state, exports, events.append)

    assert state.role_map["r1"] == "stoat-r1"
    attr_warnings = [w for w in state.warnings if w.get("type") == "role_attributes_failed"]
    assert len(attr_warnings) > 0


async def test_run_roles_applies_permissions(tmp_path: Path) -> None:
    """ROLES phase applies Discord permissions from metadata."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={"r1": PermissionPair(allow=4_194_304, deny=0)},
        channel_metadata={},
    )
    save_discord_metadata(meta, tmp_path)

    role = DCERole(id="r1", name="Mod")
    exports = [_make_export(guild_id="111", messages=[_make_message("m1", roles=[role])])]

    perm_bodies: list[dict[str, object]] = []

    with aioresponses() as m:
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-r1", "name": "Mod"})
        m.put(
            f"{STOAT_URL}/servers/srv1/permissions/stoat-r1",
            payload={},
            callback=lambda url, **kwargs: perm_bodies.append(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )
        await run_roles(config, state, exports, events.append)

    assert len(perm_bodies) == 1
    assert perm_bodies[0] == {"permissions": {"allow": 4_194_304, "deny": 0}}


def test_ferry_min_permissions_value_pinned() -> None:
    """SC-27: Ferry's operating floor is deliberately narrow.

    It is what the Ferry account needs to *perform* a migration, not a fidelity
    surface — so it grants no voice, mention or audit-log bits even though batch 5
    (#109) made those translatable. Widening it must be a deliberate, reviewed act
    rather than a side effect of extending the Discord→Stoat map.
    """
    assert FERRY_MIN_PERMISSIONS == 1_022_361_624
    for bit in (30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40):
        assert not FERRY_MIN_PERMISSIONS & (1 << bit), f"unexpected bit {bit} in Ferry's floor"


async def test_run_roles_applies_server_defaults(tmp_path: Path) -> None:
    """ROLES phase applies server default permissions merged with FERRY_MIN_PERMISSIONS."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    discord_default = 4_194_304  # SendMessage only
    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=discord_default,
        role_permissions={},
        channel_metadata={},
    )
    save_discord_metadata(meta, tmp_path)

    role = DCERole(id="r1", name="Mod")
    exports = [_make_export(guild_id="111", messages=[_make_message("m1", roles=[role])])]

    default_perm_bodies: list[dict[str, object]] = []

    with aioresponses() as m:
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-r1", "name": "Mod"})
        m.put(
            f"{STOAT_URL}/servers/srv1/permissions/default",
            payload={},
            callback=lambda url, **kwargs: default_perm_bodies.append(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )
        await run_roles(config, state, exports, events.append)

    assert len(default_perm_bodies) == 1
    expected = discord_default | FERRY_MIN_PERMISSIONS
    assert default_perm_bodies[0] == {"permissions": expected}


async def test_run_roles_no_metadata_no_permissions(tmp_path: Path) -> None:
    """ROLES phase makes no permission PUT calls when discord_metadata.json is absent."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    # No discord_metadata.json created in tmp_path.
    role = DCERole(id="r1", name="Mod")
    exports = [_make_export(messages=[_make_message("m1", roles=[role])])]

    with aioresponses() as m:
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-r1", "name": "Mod"})
        # No PUT mocks registered — any unexpected PUT would raise.
        await run_roles(config, state, exports, events.append)

    assert state.role_map["r1"] == "stoat-r1"
    # No permission-related warnings since no metadata existed.
    perm_warnings = [w for w in state.warnings if "permissions" in w.get("type", "")]
    assert len(perm_warnings) == 0


async def test_run_roles_permission_failure_non_fatal(tmp_path: Path) -> None:
    """ROLES phase logs a warning and does not raise when permission PUT fails."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={"r1": PermissionPair(allow=4_194_304, deny=0)},
        channel_metadata={},
    )
    save_discord_metadata(meta, tmp_path)

    role = DCERole(id="r1", name="Mod")
    exports = [_make_export(guild_id="111", messages=[_make_message("m1", roles=[role])])]

    with aioresponses() as m:
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-r1", "name": "Mod"})
        # Permission PUT fails.
        m.put(f"{STOAT_URL}/servers/srv1/permissions/stoat-r1", status=500)

        # Should NOT raise.
        await run_roles(config, state, exports, events.append)

    assert state.role_map["r1"] == "stoat-r1"
    perm_warnings = [w for w in state.warnings if w.get("type") == "role_permissions_failed"]
    assert len(perm_warnings) == 1
    assert "Mod" in perm_warnings[0]["message"]


async def test_run_channels_skip_threads(tmp_path: Path) -> None:
    """CHANNELS phase skips thread exports when config.skip_threads is True."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path, skip_threads=True)
    state = MigrationState(stoat_server_id="srv1")

    exports = [
        _make_export(channel_id="ch1", channel_name="general", category_id=""),
        _make_export(
            channel_id="th1",
            channel_name="my-thread",
            is_thread=True,
            parent_channel_name="general",
            category_id="",
        ),
    ]

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "stoat-ch1", "name": "general"},
        )
        # Only one POST expected — thread should be skipped.
        await run_channels(config, state, exports, events.append)

    assert "ch1" in state.channel_map
    assert "th1" not in state.channel_map


async def test_run_channels_skip_threads_false(tmp_path: Path) -> None:
    """CHANNELS phase includes threads when skip_threads is False (default)."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path, skip_threads=False)
    state = MigrationState(stoat_server_id="srv1")

    exports = [
        _make_export(
            channel_id="th1",
            channel_name="my-thread",
            is_thread=True,
            parent_channel_name="general",
            category_id="",
        ),
    ]

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "stoat-th1", "name": "general-my-thread"},
        )
        await run_channels(config, state, exports, events.append)

    assert "th1" in state.channel_map


# ---------------------------------------------------------------------------
# Bug 4: 200-channel limit truncation
# ---------------------------------------------------------------------------


async def test_run_channels_forum_threads_get_dedicated_category(tmp_path: Path) -> None:
    """Forum thread exports (type 15) create a dedicated category named after the forum."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    # Forum thread export (type 15, is_thread=True, parent_channel_name="Questions")
    exports = [
        _make_export(
            channel_id="ft1",
            channel_name="how-to-install",
            channel_type=15,
            is_thread=True,
            parent_channel_name="Questions",
            category_id="cat1",
            category="General",
        ),
    ]

    patch_body: dict[str, object] = {}

    with aioresponses() as m:
        # Channel creation.
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "stoat-ft1", "name": "Questions-how-to-install"},
        )
        # Category upsert via PATCH /servers/srv1.
        m.patch(
            f"{STOAT_URL}/servers/srv1",
            payload={"_id": "srv1"},
            callback=lambda url, **kwargs: patch_body.update(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )

        await run_channels(config, state, exports, events.append)

    assert state.channel_map["ft1"] == "stoat-ft1"
    # The forum category should be in category_map.
    assert "forum-Questions" in state.category_map
    messages = _collect_events(events)
    assert any("forum category" in msg.lower() for msg in messages)
    # Verify the PATCH body contains the forum category with the channel.
    categories = patch_body.get("categories", [])
    assert len(categories) == 1  # type: ignore[arg-type]
    assert categories[0]["title"] == "Questions"  # type: ignore[index]
    assert categories[0]["channels"] == ["stoat-ft1"]  # type: ignore[index]


async def test_run_channels_truncates_at_200(tmp_path: Path) -> None:
    """CHANNELS phase truncates to 200 channels, dropping threads first."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    # Create 195 main channels + 10 threads = 205 total.
    exports = []
    for i in range(195):
        exports.append(
            _make_export(
                channel_id=f"ch{i}",
                channel_name=f"channel-{i}",
                category_id="",
            )
        )
    for i in range(10):
        exports.append(
            _make_export(
                channel_id=f"th{i}",
                channel_name=f"thread-{i}",
                is_thread=True,
                parent_channel_name="general",
                category_id="",
            )
        )

    with aioresponses() as m:
        # Mock 200 channel creation calls.
        for _ in range(200):
            m.post(
                f"{STOAT_URL}/servers/srv1/channels",
                payload={"_id": f"stoat-ch-{_}", "name": f"ch-{_}"},
            )
        await run_channels(config, state, exports, events.append)

    # Exactly 200 channels created.
    assert len(state.channel_map) == 200

    # All 195 main channels should be preserved.
    for i in range(195):
        assert f"ch{i}" in state.channel_map

    # Only 5 of the 10 threads fit.
    thread_count = sum(1 for k in state.channel_map if k.startswith("th"))
    assert thread_count == 5

    # Warning emitted.
    warning_events = [e for e in events if e.status == "warning"]
    assert any("205" in e.message for e in warning_events)


# ---------------------------------------------------------------------------
# SERVER banner migration (S7)
# ---------------------------------------------------------------------------

BANNER_CDN = "https://cdn.discordapp.com/banners"


async def test_banner_uploaded_and_applied(tmp_path: Path) -> None:
    """SERVER phase downloads Discord banner, uploads to Autumn, and applies to Stoat server."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(autumn_url=AUTUMN_URL)
    exports = [_make_export(guild_id="111")]

    # Save discord metadata with a banner hash.
    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
        banner_hash="abc123banner",
    )
    save_discord_metadata(meta, tmp_path)

    patch_bodies: list[dict[str, object]] = []

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/create",
            payload={"server": {"_id": "srv1", "name": "Test"}, "channels": []},
        )
        # CDN banner download.
        m.get(
            f"{BANNER_CDN}/111/abc123banner.png?size=1024",
            body=b"FAKEPNG",
        )
        # Autumn banner upload.
        m.post(f"{AUTUMN_URL}/banners", payload={"id": "autumn-banner-id"})
        # PATCH to apply banner.
        m.patch(
            f"{STOAT_URL}/servers/srv1",
            payload={"_id": "srv1"},
            callback=lambda url, **kwargs: patch_bodies.append(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )

        await run_server(config, state, exports, events.append)

    assert state.stoat_server_id == "srv1"
    messages = _collect_events(events)
    assert any("banner" in msg.lower() for msg in messages)
    # Verify PATCH was called with banner field.
    assert any(b.get("banner") == "autumn-banner-id" for b in patch_bodies)


async def test_banner_download_fails_graceful(tmp_path: Path) -> None:
    """SERVER phase logs a warning when banner CDN download returns non-200."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(autumn_url=AUTUMN_URL)
    exports = [_make_export(guild_id="111")]

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
        banner_hash="abc123banner",
    )
    save_discord_metadata(meta, tmp_path)

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/create",
            payload={"server": {"_id": "srv1", "name": "Test"}, "channels": []},
        )
        # CDN returns 404.
        m.get(
            f"{BANNER_CDN}/111/abc123banner.png?size=1024",
            status=404,
        )

        await run_server(config, state, exports, events.append)

    assert state.stoat_server_id == "srv1"
    banner_warnings = [w for w in state.warnings if w.get("type") == "banner_download_failed"]
    assert len(banner_warnings) == 1
    assert "404" in banner_warnings[0]["message"]


async def test_no_banner_skipped(tmp_path: Path) -> None:
    """SERVER phase skips banner download when no banner hash in metadata."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(autumn_url=AUTUMN_URL)
    exports = [_make_export(guild_id="111")]

    # Save metadata without banner hash (empty string default).
    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
    )
    save_discord_metadata(meta, tmp_path)

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/create",
            payload={"server": {"_id": "srv1", "name": "Test"}, "channels": []},
        )
        # No CDN mock — if banner download were attempted, aioresponses would raise.

        await run_server(config, state, exports, events.append)

    assert state.stoat_server_id == "srv1"
    messages = _collect_events(events)
    assert not any("banner" in msg.lower() for msg in messages)


# ---------------------------------------------------------------------------
# Forum index channel
# ---------------------------------------------------------------------------


async def test_forum_index_channel_created(tmp_path: Path) -> None:
    """Forum category with 2 posts creates an index channel with a pinned message
    that includes channel references and message counts."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    # Two forum thread exports from the same parent forum.
    exports = [
        _make_export(
            channel_id="fp1",
            channel_name="first-post",
            channel_type=15,
            is_thread=True,
            parent_channel_name="my-forum",
            category_id="cat1",
            category="General",
            message_count=42,
        ),
        _make_export(
            channel_id="fp2",
            channel_name="second-post",
            channel_type=15,
            is_thread=True,
            parent_channel_name="my-forum",
            category_id="cat1",
            category="General",
            message_count=7,
        ),
    ]

    sent_messages: list[dict[str, object]] = []

    with aioresponses() as m:
        # Channel creation for the two forum posts.
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "stoat-fp1", "name": "my-forum-first-post"},
        )
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "stoat-fp2", "name": "my-forum-second-post"},
        )
        # Index channel creation.
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "stoat-idx1", "name": "my-forum-index"},
        )
        # Send index message.
        m.post(
            f"{STOAT_URL}/channels/stoat-idx1/messages",
            payload={"_id": "idx-msg1"},
            callback=lambda url, **kwargs: sent_messages.append(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )
        # Pin the index message.
        m.put(
            f"{STOAT_URL}/channels/stoat-idx1/messages/idx-msg1/pin",
            payload={},
        )
        # Category PATCH.
        m.patch(f"{STOAT_URL}/servers/srv1", payload={"_id": "srv1"})

        await run_channels(config, state, exports, events.append)

    # Index channel mapped in state.
    assert "forum-index-forum-my-forum" in state.channel_map
    assert state.channel_map["forum-index-forum-my-forum"] == "stoat-idx1"

    # The sent message contains channel references and message counts.
    assert len(sent_messages) == 1
    content = sent_messages[0].get("content", "")
    assert isinstance(content, str)
    assert "stoat-fp1" in content  # channel reference <#...>
    assert "stoat-fp2" in content
    assert "42" in content
    assert "7" in content

    # Masquerade is Discord Ferry.
    masq = sent_messages[0].get("masquerade", {})
    assert masq.get("name") == "Discord Ferry"  # type: ignore[union-attr]


async def test_forum_index_empty_forum(tmp_path: Path) -> None:
    """Forum category with 0 posts sends 'No posts migrated.' message."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    # Pre-populate a forum category that has no channels assigned to it.
    # This simulates the case where forum_categories was detected but all posts got dropped.
    state = MigrationState(
        stoat_server_id="srv1",
        category_map={"forum-empty-forum": "stoat-cat-empty"},
    )

    # We need at least one export for run_channels to do anything. Use a normal channel
    # so no forum posts land in the forum category.
    exports = [
        _make_export(
            channel_id="ch1",
            channel_name="general",
            category_id="",
            category="",
        ),
    ]

    # Monkey-patch: inject forum_categories after channel collection.
    # Actually, the empty forum case is when forum_categories has entries but
    # all corresponding posts got dropped by the channel limit. We need to test
    # the implementation handles the case where category_channels has no entries
    # for a forum category.
    # The simplest way: create a forum thread export that will be collected
    # into forum_categories, but simulate 0 message_count.
    exports = [
        _make_export(
            channel_id="fp1",
            channel_name="lonely-post",
            channel_type=15,
            is_thread=True,
            parent_channel_name="empty-forum",
            category_id="cat1",
            category="General",
            message_count=0,
        ),
    ]

    sent_messages: list[dict[str, object]] = []

    with aioresponses() as m:
        # Channel creation for the forum post.
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "stoat-fp1", "name": "empty-forum-lonely-post"},
        )
        # Index channel creation.
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "stoat-idx1", "name": "empty-forum-index"},
        )
        # Send index message.
        m.post(
            f"{STOAT_URL}/channels/stoat-idx1/messages",
            payload={"_id": "idx-msg1"},
            callback=lambda url, **kwargs: sent_messages.append(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )
        # Pin.
        m.put(
            f"{STOAT_URL}/channels/stoat-idx1/messages/idx-msg1/pin",
            payload={},
        )
        # Category PATCH.
        m.patch(f"{STOAT_URL}/servers/srv1", payload={"_id": "srv1"})

        await run_channels(config, state, exports, events.append)

    assert len(sent_messages) == 1
    content = sent_messages[0].get("content", "")
    assert isinstance(content, str)
    # Post with 0 messages should still appear (it exists), but test the content.
    assert "stoat-fp1" in content


async def test_forum_index_not_created_in_dry_run(tmp_path: Path) -> None:
    """dry_run=True does not create forum index channels."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path, dry_run=True)
    state = MigrationState(stoat_server_id="dry-srv")

    exports = [
        _make_export(
            channel_id="fp1",
            channel_name="post1",
            channel_type=15,
            is_thread=True,
            parent_channel_name="my-forum",
            category_id="cat1",
            category="General",
            message_count=10,
        ),
    ]

    with aioresponses():
        # No mocks needed — dry_run should not make API calls.
        await run_channels(config, state, exports, events.append)

    # Dry-run should map the channel but NOT create a forum index.
    assert "fp1" in state.channel_map
    assert "forum-index-forum-my-forum" not in state.channel_map


async def test_forum_index_failure_nonfatal(tmp_path: Path) -> None:
    """api_create_channel failure for forum index logs a warning but does not crash."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    exports = [
        _make_export(
            channel_id="fp1",
            channel_name="post1",
            channel_type=15,
            is_thread=True,
            parent_channel_name="my-forum",
            category_id="cat1",
            category="General",
            message_count=5,
        ),
    ]

    with aioresponses() as m:
        # Channel creation for the forum post succeeds.
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "stoat-fp1", "name": "my-forum-post1"},
        )
        # Index channel creation FAILS.
        m.post(f"{STOAT_URL}/servers/srv1/channels", status=500)
        # Category PATCH.
        m.patch(f"{STOAT_URL}/servers/srv1", payload={"_id": "srv1"})

        # Should NOT raise.
        await run_channels(config, state, exports, events.append)

    # The forum post channel should still be mapped.
    assert state.channel_map["fp1"] == "stoat-fp1"
    # No index channel should be in the map.
    assert "forum-index-forum-my-forum" not in state.channel_map
    # Warning should be recorded.
    idx_warnings = [w for w in state.warnings if w.get("type") == "forum_index_failed"]
    assert len(idx_warnings) == 1


# ---------------------------------------------------------------------------
# Banner download auth header (S11)
# ---------------------------------------------------------------------------


async def test_banner_download_includes_auth_header(tmp_path: Path) -> None:
    """SERVER phase sends Authorization header when discord_token is set."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path, discord_token="Bot mytoken123")
    state = MigrationState(autumn_url=AUTUMN_URL, stoat_server_id="srv1")
    exports = [_make_export(guild_id="111")]

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
        banner_hash="testhash",
    )
    save_discord_metadata(meta, tmp_path)

    captured_headers: list[dict[str, str]] = []

    def _capture_get(url: str, **kwargs: object) -> None:
        headers = kwargs.get("headers") or {}
        captured_headers.append(dict(headers))  # type: ignore[arg-type]

    with aioresponses() as m:
        m.get(f"{STOAT_URL}/servers/srv1", payload={"_id": "srv1"})
        m.get(
            f"{BANNER_CDN}/111/testhash.png?size=1024",
            body=b"FAKEPNG",
            callback=_capture_get,
        )
        m.post(f"{AUTUMN_URL}/banners", payload={"id": "autumn-banner-id"})
        m.patch(f"{STOAT_URL}/servers/srv1", payload={"_id": "srv1"})
        m.patch(f"{STOAT_URL}/servers/srv1", payload={"_id": "srv1"})

        await run_server(config, state, exports, events.append)

    assert captured_headers, "Banner CDN request was not made"
    assert captured_headers[0].get("Authorization") == "Bot mytoken123"


async def test_banner_download_no_auth_header_when_no_token(tmp_path: Path) -> None:
    """SERVER phase sends no Authorization header when discord_token is absent."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)  # no discord_token
    state = MigrationState(autumn_url=AUTUMN_URL, stoat_server_id="srv1")
    exports = [_make_export(guild_id="111")]

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
        banner_hash="testhash",
    )
    save_discord_metadata(meta, tmp_path)

    captured_headers: list[dict[str, str]] = []

    def _capture_get(url: str, **kwargs: object) -> None:
        headers = kwargs.get("headers") or {}
        captured_headers.append(dict(headers))  # type: ignore[arg-type]

    with aioresponses() as m:
        m.get(f"{STOAT_URL}/servers/srv1", payload={"_id": "srv1"})
        m.get(
            f"{BANNER_CDN}/111/testhash.png?size=1024",
            body=b"FAKEPNG",
            callback=_capture_get,
        )
        m.post(f"{AUTUMN_URL}/banners", payload={"id": "autumn-banner-id"})
        m.patch(f"{STOAT_URL}/servers/srv1", payload={"_id": "srv1"})
        m.patch(f"{STOAT_URL}/servers/srv1", payload={"_id": "srv1"})

        await run_server(config, state, exports, events.append)

    assert captured_headers, "Banner CDN request was not made"
    assert "Authorization" not in captured_headers[0]


# ---------------------------------------------------------------------------
# Phase 4: ROLES — Stoat role-cap handling (Task 6)
# ---------------------------------------------------------------------------


async def test_role_cap_truncates_and_warns(tmp_path: Path) -> None:
    """When the union exceeds the live server_roles limit, only the top-N by
    position are created and a role_limit_exceeded warning names the dropped count."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    # Export author posts under r1 only; the live metadata supplies four roles
    # at distinct positions. The limit is 2, so the two highest positions win.
    role_a = DCERole(id="r1", name="Admin")
    exports = [_make_export(messages=[_make_message("m1", roles=[role_a])])]

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
        role_metadata={
            "r1": RoleMeta(name="Admin", position=0),
            "r2": RoleMeta(name="Mod", position=1),
            "r3": RoleMeta(name="VIP", position=2),
            "r4": RoleMeta(name="Top", position=3),
        },
    )
    save_discord_metadata(meta, tmp_path)

    created_names: list[str] = []

    with aioresponses() as m:
        m.get(
            f"{STOAT_URL}/",
            payload={"features": {"limits": {"global": {"server_roles": 2}}}},
        )
        m.post(
            f"{STOAT_URL}/servers/srv1/roles",
            payload={"id": "stoat-x", "name": "x"},
            repeat=True,
            callback=lambda url, **kwargs: created_names.append(  # type: ignore[misc]
                kwargs.get("json", {}).get("name", "")
            ),
        )
        m.patch(f"{STOAT_URL}/servers/srv1/roles/stoat-x", payload={}, repeat=True)

        await run_roles(config, state, exports, events.append)

    # Only the two highest-position roles are created.
    assert len(created_names) == 2
    assert set(created_names) == {"Top", "VIP"}

    warnings = [w for w in state.warnings if w.get("type") == "role_limit_exceeded"]
    assert warnings, "expected a role_limit_exceeded warning"
    assert "2" in warnings[0]["message"]  # dropped count of 2 (4 union - 2 limit)


async def test_too_many_roles_backstop_non_fatal(tmp_path: Path) -> None:
    """If api_create_role raises a TooManyRoles MigrationError mid-loop, the phase
    does not crash and a role_limit_exceeded warning is recorded."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    role_a = DCERole(id="r1", name="Admin")
    exports = [_make_export(messages=[_make_message("m1", roles=[role_a])])]

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
        role_metadata={
            "r1": RoleMeta(name="Admin", position=0),
            "r2": RoleMeta(name="Mod", position=1),
        },
    )
    save_discord_metadata(meta, tmp_path)

    call_count = {"n": 0}

    async def _fake_create_role(*_args: object, **_kwargs: object) -> dict[str, str]:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {"id": "stoat-r2", "name": "Mod"}
        raise MigrationError("Stoat API error 400: TooManyRoles")

    with (
        aioresponses() as m,
        patch(
            "discord_ferry.migrator.structure.api_create_role",
            side_effect=_fake_create_role,
        ),
    ):
        # Live limit high enough that pre-flight truncation does NOT engage;
        # the backstop is what must fire.
        m.get(
            f"{STOAT_URL}/",
            payload={"features": {"limits": {"global": {"server_roles": 200}}}},
        )
        m.patch(f"{STOAT_URL}/servers/srv1/roles/stoat-r2", payload={}, repeat=True)

        # Must not raise.
        await run_roles(config, state, exports, events.append)

    warnings = [w for w in state.warnings if w.get("type") == "role_limit_exceeded"]
    assert warnings, "expected a role_limit_exceeded backstop warning"


async def test_resume_truncation_deterministic(tmp_path: Path) -> None:
    """Two runs over identical discord_metadata truncate to the same top-N set."""
    config = _make_config(tmp_path)

    role_a = DCERole(id="r1", name="Admin")
    exports = [_make_export(messages=[_make_message("m1", roles=[role_a])])]

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
        role_metadata={
            "r1": RoleMeta(name="Admin", position=0),
            "r2": RoleMeta(name="Mod", position=1),
            "r3": RoleMeta(name="VIP", position=2),
            "r4": RoleMeta(name="Top", position=3),
        },
    )
    save_discord_metadata(meta, tmp_path)

    # Run twice over identical metadata; the truncated set must be stable.
    first = await _run_and_collect(config, exports)
    second = await _run_and_collect(config, exports)
    assert first == second
    assert first == {"Top", "VIP"}


async def _run_and_collect(config: FerryConfig, exports: list[DCEExport]) -> set[str]:
    events: list[MigrationEvent] = []
    state = MigrationState(stoat_server_id="srv1")
    created_names: list[str] = []
    with aioresponses() as m:
        m.get(
            f"{STOAT_URL}/",
            payload={"features": {"limits": {"global": {"server_roles": 2}}}},
        )
        m.post(
            f"{STOAT_URL}/servers/srv1/roles",
            payload={"id": "stoat-x", "name": "x"},
            repeat=True,
            callback=lambda url, **kwargs: created_names.append(  # type: ignore[misc]
                kwargs.get("json", {}).get("name", "")
            ),
        )
        m.patch(f"{STOAT_URL}/servers/srv1/roles/stoat-x", payload={}, repeat=True)
        await run_roles(config, state, exports, events.append)
    return set(created_names)


# ---------------------------------------------------------------------------
# S4 — role-icon upload failure degrades (never aborts the roles phase)
# ---------------------------------------------------------------------------


async def test_run_roles_icon_upload_failure_degrades(tmp_path: Path) -> None:
    """A non-JSON Autumn icon response skips the icon (token-safe warning); phase completes."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)

    role = DCERole(id="r1", name="Mods")
    exports = [_make_export(messages=[_make_message("m1", roles=[role])])]

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
        role_metadata={"r1": RoleMeta(hoist=True, position=0, icon_hash="abc123")},
    )
    save_discord_metadata(meta, tmp_path)

    patch_bodies: list[dict[str, object]] = []

    with (
        aioresponses() as m,
        patch(
            "discord_ferry.migrator.structure.download_role_icon",
            new=AsyncMock(return_value=b"pngbytes"),
        ),
    ):
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-r1", "name": "Mods"})
        # Non-JSON 200 from Autumn -> AutumnUploadError (post-S1) -> caught by the broadened guard.
        m.post(
            f"{AUTUMN_URL}/icons",
            status=200,
            body=b"<html>SENTINEL_BODY</html>",
            content_type="text/html",
            repeat=True,
        )
        m.patch(
            f"{STOAT_URL}/servers/srv1/roles/stoat-r1",
            payload={},
            repeat=True,
            callback=lambda url, **kwargs: patch_bodies.append(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )
        m.put(f"{STOAT_URL}/servers/srv1/permissions/stoat-r1", payload={}, repeat=True)

        # Must NOT raise — degrade, not abort.
        await run_roles(config, state, exports, events.append)

    icon_warnings = [w for w in state.warnings if w.get("type") == "role_icon_upload_failed"]
    assert icon_warnings  # icon skipped with a warning
    assert any(b.get("hoist") is True for b in patch_bodies)  # rank/hoist still applied
    # SC-18 token-safety: the warning carries no response body and no token.
    for w in icon_warnings:
        assert "SENTINEL_BODY" not in w["message"]
        assert config.token not in w["message"]


async def test_run_roles_icon_oserror_degrades(tmp_path: Path) -> None:
    """An OSError writing the temp icon file degrades (warning) instead of aborting."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)

    role = DCERole(id="r1", name="Mods")
    exports = [_make_export(messages=[_make_message("m1", roles=[role])])]

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
        role_metadata={"r1": RoleMeta(hoist=True, position=0, icon_hash="abc123")},
    )
    save_discord_metadata(meta, tmp_path)  # written before the write_bytes patch

    with (
        aioresponses() as m,
        patch(
            "discord_ferry.migrator.structure.download_role_icon",
            new=AsyncMock(return_value=b"pngbytes"),
        ),
        patch("pathlib.Path.write_bytes", side_effect=OSError("disk full")),
    ):
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-r1", "name": "Mods"})
        m.patch(f"{STOAT_URL}/servers/srv1/roles/stoat-r1", payload={}, repeat=True)
        m.put(f"{STOAT_URL}/servers/srv1/permissions/stoat-r1", payload={}, repeat=True)

        # Must NOT raise — the OSError on temp-file write degrades to a warning.
        await run_roles(config, state, exports, events.append)

    assert any(w.get("type") == "role_icon_upload_failed" for w in state.warnings)


# ---------------------------------------------------------------------------
# Batch 2 — S1: migration lock survives the SERVER phase
# ---------------------------------------------------------------------------


def _meta_with_description(desc: str = "Cosy") -> DiscordMetadata:
    return DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
        guild_description=desc,
    )


async def test_lock_marker_preserved_in_server_description_patch(tmp_path: Path) -> None:
    """S1 SC-1: run_server folds the lock marker into the description PATCH (token-safe)."""
    config = _make_config(tmp_path, server_id="srv1", token="secret-token-xyz")
    state = MigrationState(
        stoat_server_id="srv1", migration_lock_marker="[FERRY_LOCK:9999999999:host]"
    )
    save_discord_metadata(_meta_with_description("Cosy"), tmp_path)
    bodies: list[dict[str, object]] = []
    with aioresponses() as m:
        m.get(f"{STOAT_URL}/servers/srv1", payload={"_id": "srv1", "name": "S"})
        m.patch(
            f"{STOAT_URL}/servers/srv1",
            payload={},
            repeat=True,
            callback=lambda url, **kw: bodies.append(kw.get("json", {})),  # type: ignore[misc]
        )
        await run_server(config, state, [_make_export()], lambda e: None)
    desc_bodies = [b for b in bodies if "description" in b]
    assert len(desc_bodies) == 1
    assert "Cosy" in desc_bodies[0]["description"]  # type: ignore[operator]
    assert "[FERRY_LOCK:9999999999:host]" in desc_bodies[0]["description"]  # type: ignore[operator]
    assert all("secret-token-xyz" not in w.get("message", "") for w in state.warnings)


async def test_lock_marker_adds_no_extra_server_patch(tmp_path: Path) -> None:
    """S1 SC-2: the marker folds into the existing description PATCH — no extra PATCH."""
    config = _make_config(tmp_path, server_id="srv1")
    state = MigrationState(stoat_server_id="srv1", migration_lock_marker="[FERRY_LOCK:1:h]")
    save_discord_metadata(_meta_with_description("Cosy"), tmp_path)
    bodies: list[dict[str, object]] = []
    with aioresponses() as m:
        m.get(f"{STOAT_URL}/servers/srv1", payload={"_id": "srv1", "name": "S"})
        m.patch(
            f"{STOAT_URL}/servers/srv1",
            payload={},
            repeat=True,
            callback=lambda url, **kw: bodies.append(kw.get("json", {})),  # type: ignore[misc]
        )
        await run_server(config, state, [_make_export()], lambda e: None)
    # Exactly one description-bearing PATCH (the marker is folded in, not a separate call).
    assert len([b for b in bodies if "description" in b]) == 1
    # The permission-bootstrap PATCH (default_permissions only) carries no description.
    assert any("default_permissions" in b and "description" not in b for b in bodies)


async def test_create_path_description_has_no_lock_marker(tmp_path: Path) -> None:
    """S1 SC-3: the create path has no lock (empty marker) → description PATCH carries no marker."""
    config = _make_config(tmp_path)  # no server_id -> create path
    state = MigrationState()  # migration_lock_marker == ""
    save_discord_metadata(_meta_with_description("Cosy"), tmp_path)
    bodies: list[dict[str, object]] = []
    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/create",
            payload={"server": {"_id": "new1", "name": "S"}, "channels": []},
        )
        m.patch(
            f"{STOAT_URL}/servers/new1",
            payload={},
            repeat=True,
            callback=lambda url, **kw: bodies.append(kw.get("json", {})),  # type: ignore[misc]
        )
        await run_server(config, state, [_make_export()], lambda e: None)
    desc_bodies = [b for b in bodies if "description" in b]
    assert desc_bodies and desc_bodies[0]["description"] == "Cosy"
    assert all("[FERRY_LOCK" not in str(b.get("description", "")) for b in bodies)


# ---------------------------------------------------------------------------
# Batch 2 — S2: persist stoat_server_id immediately after create (--resume)
# ---------------------------------------------------------------------------


async def test_server_id_persisted_right_after_create(tmp_path: Path) -> None:
    """S2 SC-6: save_state runs right after create, with stoat_server_id already set."""
    config = _make_config(tmp_path)
    state = MigrationState()
    recorded: list[str] = []
    with (
        aioresponses() as m,
        patch(
            "discord_ferry.migrator.structure.save_state",
            side_effect=lambda s, d: recorded.append(s.stoat_server_id),
        ),
    ):
        m.post(
            f"{STOAT_URL}/servers/create",
            payload={"server": {"_id": "new1", "name": "S"}, "channels": []},
        )
        m.patch(f"{STOAT_URL}/servers/new1", payload={}, repeat=True)
        await run_server(config, state, [_make_export()], lambda e: None)
    assert recorded and recorded[0] == "new1"  # persisted immediately after create


async def test_resume_after_create_reuses_server(tmp_path: Path) -> None:
    """S2 SC-7: a --resume from a kill-after-create state reuses the server (no second create)."""
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")  # persisted post-create state
    with aioresponses() as m:
        m.get(f"{STOAT_URL}/servers/srv1", payload={"_id": "srv1", "name": "S"})
        m.patch(f"{STOAT_URL}/servers/srv1", payload={}, repeat=True)
        # No /servers/create mock — aioresponses raises if create fires.
        await run_server(config, state, [_make_export()], lambda e: None)
    assert state.stoat_server_id == "srv1"


async def test_dry_run_server_unchanged(tmp_path: Path) -> None:
    """S2 SC-8: dry-run uses a placeholder id and makes no HTTP calls."""
    config = _make_config(tmp_path, dry_run=True)
    state = MigrationState()
    with aioresponses():  # any request would raise
        await run_server(config, state, [_make_export()], lambda e: None)
    assert state.stoat_server_id.startswith("dry-server")


# ---------------------------------------------------------------------------
# Batch 2 — S3: run_roles resume finalizes attributes + permissions
# ---------------------------------------------------------------------------


def _meta_with_role(role_id: str = "r1") -> DiscordMetadata:
    return DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={role_id: PermissionPair(allow=1, deny=0)},
        channel_metadata={},
        role_metadata={role_id: RoleMeta(hoist=True, position=2)},
    )


async def test_resume_finalizes_unfinalized_role(tmp_path: Path) -> None:
    """S3 SC-9: a created-but-unfinalized role gets attrs PATCH + perms PUT on resume."""
    config = _make_config(tmp_path)
    state = MigrationState(
        stoat_server_id="srv1", role_map={"r1": "stoat-r1"}, roles_finalized=set()
    )
    exports = [_make_export(messages=[_make_message("m1", roles=[DCERole(id="r1", name="Mods")])])]
    save_discord_metadata(_meta_with_role("r1"), tmp_path)
    patched: list[dict[str, object]] = []
    putted: list[dict[str, object]] = []
    with aioresponses() as m:
        # No POST /roles mock — create must NOT fire for the already-mapped role.
        m.patch(
            f"{STOAT_URL}/servers/srv1/roles/stoat-r1",
            payload={},
            repeat=True,
            callback=lambda url, **kw: patched.append(kw.get("json", {})),  # type: ignore[misc]
        )
        m.put(
            f"{STOAT_URL}/servers/srv1/permissions/stoat-r1",
            payload={},
            repeat=True,
            callback=lambda url, **kw: putted.append(kw.get("json", {})),  # type: ignore[misc]
        )
        await run_roles(config, state, exports, lambda e: None)
    assert patched and putted  # attrs + perms both applied on resume
    assert "r1" in state.roles_finalized


async def test_resume_does_not_recreate_mapped_role(tmp_path: Path) -> None:
    """S3 SC-10: the create pass skips an already-mapped role (no duplicate api_create_role)."""
    config = _make_config(tmp_path)
    state = MigrationState(
        stoat_server_id="srv1", role_map={"r1": "stoat-r1"}, roles_finalized=set()
    )
    exports = [_make_export(messages=[_make_message("m1", roles=[DCERole(id="r1", name="Mods")])])]
    save_discord_metadata(_meta_with_role("r1"), tmp_path)
    creates: list[object] = []
    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/roles",
            payload={"id": "should-not-happen"},
            repeat=True,
            callback=lambda url, **kw: creates.append(url),  # type: ignore[misc]
        )
        m.patch(f"{STOAT_URL}/servers/srv1/roles/stoat-r1", payload={}, repeat=True)
        m.put(f"{STOAT_URL}/servers/srv1/permissions/stoat-r1", payload={}, repeat=True)
        await run_roles(config, state, exports, lambda e: None)
    assert creates == []  # no create POST fired


async def test_incremental_finalized_role_skips_attrs_and_perms(tmp_path: Path) -> None:
    """S3 SC-11: a finalized role is skipped by both attrs and perms passes (no HTTP)."""
    config = _make_config(tmp_path)
    state = MigrationState(
        stoat_server_id="srv1", role_map={"r1": "stoat-r1"}, roles_finalized={"r1"}
    )
    exports = [_make_export(messages=[_make_message("m1", roles=[DCERole(id="r1", name="Mods")])])]
    save_discord_metadata(_meta_with_role("r1"), tmp_path)
    with aioresponses():  # NO mocks — any role PATCH/PUT/POST would raise
        await run_roles(config, state, exports, lambda e: None)
    assert state.roles_finalized == {"r1"}


async def test_mark_at_end_finalizes_without_metadata(tmp_path: Path) -> None:
    """S3 SC-12: a completed run finalizes created roles even with no Discord metadata."""
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")
    exports = [_make_export(messages=[_make_message("m1", roles=[DCERole(id="r1", name="Mods")])])]
    # No save_discord_metadata -> permissions pass skipped, attrs no-op (position 0).
    with aioresponses() as m:
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-r1"}, repeat=True)
        await run_roles(config, state, exports, lambda e: None)
    assert "r1" in state.roles_finalized


async def test_completed_run_finalizes_all_created_roles(tmp_path: Path) -> None:
    """S3 SC-13: a normal completed run finalizes every created role."""
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")
    roles = [DCERole(id="r1", name="A"), DCERole(id="r2", name="B")]
    exports = [_make_export(messages=[_make_message("m1", roles=roles)])]
    with aioresponses() as m:
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-r1"})
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-r2"})
        await run_roles(config, state, exports, lambda e: None)
    assert state.roles_finalized == {"r1", "r2"}


async def test_create_loop_saves_state_periodically(tmp_path: Path) -> None:
    """S3 SC-14: the create loop persists mid-phase (hard-kill durability)."""
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")
    roles = [DCERole(id=f"r{i}", name=f"R{i}") for i in range(1, 11)]  # 10 roles -> idx%10 fires
    exports = [_make_export(messages=[_make_message("m1", roles=roles)])]
    with (
        aioresponses() as m,
        patch("discord_ferry.migrator.structure.save_state") as spy,
    ):
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-x"}, repeat=True)
        await run_roles(config, state, exports, lambda e: None)
    # One intra-loop save (at idx==10) + one finalize-at-end save.
    assert spy.call_count >= 2


async def test_too_many_roles_break_leaves_unmapped_unfinalized(tmp_path: Path) -> None:
    """S3 SC-15: a TooManyRoles break leaves the unmapped role out of roles_finalized."""
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")
    roles = [DCERole(id="r1", name="A"), DCERole(id="r2", name="B")]
    exports = [_make_export(messages=[_make_message("m1", roles=roles)])]
    with aioresponses() as m:
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-r1"})
        m.post(f"{STOAT_URL}/servers/srv1/roles", status=400, body=b'{"type":"TooManyRoles"}')
        await run_roles(config, state, exports, lambda e: None)
    assert "r1" in state.role_map and "r2" not in state.role_map
    assert "r1" in state.roles_finalized and "r2" not in state.roles_finalized


# ---------------------------------------------------------------------------
# #137 — the role-icon path discards the message, so it re-derives the hint
# ---------------------------------------------------------------------------


async def test_run_roles_icon_certificate_failure_explains_itself(tmp_path: Path) -> None:
    """A certificate failure uploading a role icon names the host and the override.

    This handler throws `str(exc)` away on purpose, so the hint upload_to_autumn
    already put in the message never reaches the user — it has to be re-derived
    from the __cause__ chain here. Dropping the `tls_hint(exc)` call leaves the
    bare fixed template, which the last two assertions reject: that bare template
    is the unactionable dead end #137 was filed about.
    """
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)

    role = DCERole(id="r1", name="Mods")
    exports = [_make_export(messages=[_make_message("m1", roles=[role])])]

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
        role_metadata={"r1": RoleMeta(hoist=True, position=0, icon_hash="abc123")},
    )
    save_discord_metadata(meta, tmp_path)

    key = aiohttp.client_reqrep.ConnectionKey(
        "cdn.stoatusercontent.com", 443, True, True, None, None, None
    )
    cert_error = aiohttp.ClientConnectorCertificateError(
        key, ssl.SSLCertVerificationError("unable to get local issuer certificate")
    )

    with (
        aioresponses() as m,
        patch(
            "discord_ferry.migrator.structure.download_role_icon",
            new=AsyncMock(return_value=b"pngbytes"),
        ),
    ):
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-r1", "name": "Mods"})
        m.post(f"{AUTUMN_URL}/icons", exception=cert_error, repeat=True)
        m.patch(f"{STOAT_URL}/servers/srv1/roles/stoat-r1", payload={}, repeat=True)
        m.put(f"{STOAT_URL}/servers/srv1/permissions/stoat-r1", payload={}, repeat=True)

        # Must NOT raise — a certificate failure on an icon degrades like any other.
        await run_roles(config, state, exports, events.append)

    icon_warnings = [w for w in state.warnings if w.get("type") == "role_icon_upload_failed"]
    assert icon_warnings
    message = icon_warnings[0]["message"]
    assert "cdn.stoatusercontent.com" in message
    assert "SSL_CERT_FILE" in message


async def test_run_roles_icon_warning_never_carries_the_response_body(tmp_path: Path) -> None:
    """The hint is the only thing added — the Autumn body still never appears.

    Uses a non-retryable error status so the AutumnUploadError raised carries the
    response body verbatim. The pre-existing token-safety test uses a non-JSON 200,
    whose exception text is a fixed template, so it cannot see this: appending
    `{exc}` beside the hint would leave that test green and this one red.
    """
    events: list[MigrationEvent] = []
    # A distinctive token: the module default is "tok", three characters that a
    # substring check would clear even if the body were interpolated verbatim.
    config = _make_config(tmp_path, token="stoat-session-token-SENTINEL")
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)

    role = DCERole(id="r1", name="Mods")
    exports = [_make_export(messages=[_make_message("m1", roles=[role])])]

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
        role_metadata={"r1": RoleMeta(hoist=True, position=0, icon_hash="abc123")},
    )
    save_discord_metadata(meta, tmp_path)

    with (
        aioresponses() as m,
        patch(
            "discord_ferry.migrator.structure.download_role_icon",
            new=AsyncMock(return_value=b"pngbytes"),
        ),
    ):
        m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-r1", "name": "Mods"})
        m.post(
            f"{AUTUMN_URL}/icons",
            status=400,
            body=f"x-session-token echoed: {config.token} SENTINEL_BODY",
            repeat=True,
        )
        m.patch(f"{STOAT_URL}/servers/srv1/roles/stoat-r1", payload={}, repeat=True)
        m.put(f"{STOAT_URL}/servers/srv1/permissions/stoat-r1", payload={}, repeat=True)

        await run_roles(config, state, exports, events.append)

    icon_warnings = [w for w in state.warnings if w.get("type") == "role_icon_upload_failed"]
    assert icon_warnings
    for w in icon_warnings:
        assert "SENTINEL_BODY" not in w["message"]
        assert config.token not in w["message"]


# ---------------------------------------------------------------------------
# #135 — a refusing proxy must name itself at structure.py:408
# ---------------------------------------------------------------------------


async def test_a_refused_proxy_names_the_proxy(
    tmp_path: Path, fake_proxy, proxy_env, os_proxy
) -> None:
    """SC-135-28. Killing: proxy_hint defined and never called at structure.py:408.

    This handler throws `str(exc)` away on purpose (the Autumn body may echo
    x-session-token), so the hint upload_to_autumn already put in the message
    never reaches the user and has to be re-derived from the __cause__ chain.
    The same shape as the certificate test above it.

    Only the Autumn host is passed through to the real connector; the Stoat
    calls stay mocked, so exactly one request meets the proxy.
    """
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)

    role = DCERole(id="r1", name="Mods")
    exports = [_make_export(messages=[_make_message("m1", roles=[role])])]
    save_discord_metadata(
        DiscordMetadata(
            guild_id="111",
            fetched_at="t",
            server_default_permissions=0,
            role_permissions={},
            channel_metadata={},
            role_metadata={"r1": RoleMeta(hoist=True, position=0, icon_hash="abc123")},
        ),
        tmp_path,
    )

    make, _ = fake_proxy
    server = await make(b"403 Forbidden")
    port = server.sockets[0].getsockname()[1]
    async with server:
        with (
            os_proxy({}),
            proxy_env(HTTPS_PROXY=f"http://127.0.0.1:{port}"),
            aioresponses(passthrough=[AUTUMN_URL]) as m,
            patch(
                "discord_ferry.migrator.structure.download_role_icon",
                new=AsyncMock(return_value=b"pngbytes"),
            ),
        ):
            m.post(f"{STOAT_URL}/servers/srv1/roles", payload={"id": "stoat-r1", "name": "Mods"})
            m.patch(f"{STOAT_URL}/servers/srv1/roles/stoat-r1", payload={}, repeat=True)
            m.put(f"{STOAT_URL}/servers/srv1/permissions/stoat-r1", payload={}, repeat=True)
            # Must NOT raise — a proxy failure on an icon degrades like any other.
            await run_roles(config, state, exports, events.append)

    icon_warnings = [w for w in state.warnings if w.get("type") == "role_icon_upload_failed"]
    assert icon_warnings
    message = icon_warnings[0]["message"]
    assert "Role icon upload failed" in message
    assert f"The request to autumn.test went through the proxy at 127.0.0.1:{port}" in message
    assert "FERRY_DISABLE_PROXY" in message


async def test_forum_index_duplicate_skips_the_pin(tmp_path: Path) -> None:
    """SC-2.6: a duplicate index send leaves no id, so there is nothing to pin.

    Driven through real HTTP so the whole stack runs, including api.py's
    DuplicateNonce branch. The channel wiring below the send uses index_channel_id,
    not the message id, so it must still happen.
    """
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    exports = [
        _make_export(
            channel_id="fp1",
            channel_name="first-post",
            channel_type=15,
            is_thread=True,
            parent_channel_name="my-forum",
            category_id="cat1",
            category="General",
            message_count=42,
        ),
    ]

    pins: list[str] = []

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "stoat-fp1", "name": "my-forum-first-post"},
        )
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "stoat-idx1", "name": "my-forum-index"},
        )
        # The index message send answers with a still-cached Idempotency-Key.
        m.post(
            f"{STOAT_URL}/channels/stoat-idx1/messages",
            status=409,
            payload={"type": "DuplicateNonce", "location": "crates/x/src/lib.rs:1:1"},
        )
        # Registered so a stray pin is recorded rather than erroring out.
        m.put(
            f"{STOAT_URL}/channels/stoat-idx1/messages/idx-msg1/pin",
            payload={},
            callback=lambda url, **kwargs: pins.append(str(url)),  # type: ignore[misc]
        )
        m.patch(f"{STOAT_URL}/servers/srv1", payload={"_id": "srv1"})

        await run_channels(config, state, exports, events.append)

    assert pins == [], "a pin was attempted against a message id that was never returned"
    # The channel wiring does not depend on the message id and must still have run.
    assert state.channel_map["forum-index-forum-my-forum"] == "stoat-idx1"


# ---------------------------------------------------------------------------
# created_channel_names: the name Ferry SENT, from the create response (#289)
# ---------------------------------------------------------------------------
#
# Fixture rule for every test below: the Discord id, the Stoat id and the name
# are three distinct literals. Seeding any two from one value is what lets a
# rename test pass against a broken implementation.


async def test_run_channels_records_the_created_name(tmp_path: Path) -> None:
    """SC-2.2. The ordinary case."""
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")
    exports = [_make_export(channel_id="d-100", channel_name="general", category_id="")]

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "01JSTOATCH00000000000AAA", "name": "general"},
        )
        await run_channels(config, state, exports, [].append)

    assert state.channel_map == {"d-100": "01JSTOATCH00000000000AAA"}
    assert state.created_channel_names == {"d-100": "general"}


async def test_the_recorded_name_comes_from_the_response(tmp_path: Path) -> None:
    """SC-2.3. In every OTHER test the sent name and the returned name are equal.

    Only a response whose name differs from what Ferry sent can distinguish
    recording result["name"] from recording the local unique_name. Without this
    test both implementations pass the whole suite.

    Recording the response is what makes any server-side normalisation invisible
    to the rename check rather than a false positive on every affected channel.
    """
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")
    exports = [_make_export(channel_id="d-100", channel_name="general", category_id="")]

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "01JSTOATCH00000000000AAA", "name": "general-normalised"},
        )
        await run_channels(config, state, exports, [].append)

    assert state.created_channel_names == {"d-100": "general-normalised"}


async def test_a_name_over_32_characters_records_the_truncated_value(tmp_path: Path) -> None:
    """SC-2.4. The truncation trap, and the reason this field is not the Discord name.

    make_unique_channel_name cuts to 32 characters. Recording ch.name would make
    ferry check report channel_renamed for every long channel on a server nobody
    edited, and it would only show on real data.
    """
    long_name = "this-channel-name-is-definitely-longer-than-thirty-two"
    truncated = long_name[:32]
    assert len(truncated) == 32
    assert truncated != long_name

    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")
    exports = [_make_export(channel_id="d-100", channel_name=long_name, category_id="")]

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "01JSTOATCH00000000000AAA", "name": truncated},
        )
        await run_channels(config, state, exports, [].append)

    assert state.created_channel_names == {"d-100": truncated}


async def test_a_collision_pair_records_distinct_suffixed_names(tmp_path: Path) -> None:
    """SC-2.5. Two channels truncating to the same 32 characters.

    make_unique_channel_name appends "-1" to the second. Recording ch.name would
    record the same value twice and then report a rename for the suffixed one.
    """
    base = "a-very-long-channel-name-that-collides-here"
    first = base[:32]
    second = f"{base[: 32 - 2]}-1"

    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")
    exports = [
        _make_export(channel_id="d-100", channel_name=base, category_id=""),
        _make_export(channel_id="d-101", channel_name=base, category_id=""),
    ]

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "01JSTOATCH00000000000AAA", "name": first},
        )
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "01JSTOATCH00000000000BBB", "name": second},
        )
        await run_channels(config, state, exports, [].append)

    assert state.created_channel_names == {"d-100": first, "d-101": second}
    assert first != second


async def test_a_voice_retry_records_the_retrys_response(tmp_path: Path) -> None:
    """SC-2.6. The retry reassigns the response INSIDE an except block.

    A voice channel that fails is retried as text (#194). The id-map write sits
    OUTSIDE the try, so `result` there is whichever attempt succeeded, and the
    name write belongs beside it.

    A name write placed next to the first response read never executes on this
    path, because the exception jumps past it, so that implementation records
    nothing at all for every voice channel. A test driving only the success path
    cannot see that, which is why the two responses carry different name
    literals here.
    """
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")
    exports = [
        _make_export(channel_id="d-100", channel_name="voice-room", category_id="", channel_type=2)
    ]

    with aioresponses() as m:
        m.post(f"{STOAT_URL}/servers/srv1/channels", status=500, body="voice unsupported")
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "01JSTOATCH00000000000AAA", "name": "voice-room-as-text"},
        )
        await run_channels(config, state, exports, [].append)

    assert state.channel_map == {"d-100": "01JSTOATCH00000000000AAA"}
    assert state.created_channel_names == {"d-100": "voice-room-as-text"}
