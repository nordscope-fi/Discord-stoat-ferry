"""Tests for structure phases: SERVER (3), ROLES (4), CATEGORIES (5), CHANNELS (6)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aioresponses import aioresponses

from discord_ferry.config import FerryConfig
from discord_ferry.migrator.structure import (
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
        m.post(f"{STOAT_URL}/servers/create", payload={"_id": "srv1", "name": "Test"})

        await run_server(config, state, exports, events.append)

    assert state.stoat_server_id == "srv1"
    messages = _collect_events(events)
    assert any("srv1" in msg for msg in messages)


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
        m.post(f"{STOAT_URL}/servers/create", payload={"_id": "srv1", "name": "Test"})
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
        m.post(f"{STOAT_URL}/servers/create", payload={"_id": "srv1", "name": "Test"})
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

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/categories",
            payload={"id": "stoat-cat1", "title": "General"},
        )
        m.post(
            f"{STOAT_URL}/servers/srv1/categories",
            payload={"id": "stoat-cat2", "title": "Off-Topic"},
        )

        await run_categories(config, state, exports, events.append)

    assert state.category_map == {"cat1": "stoat-cat1", "cat2": "stoat-cat2"}


async def test_run_categories_deduplicates(tmp_path: Path) -> None:
    """CATEGORIES phase creates each category only once even if multiple channels share it."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="srv1")

    exports = [
        _make_export(channel_id="ch1", category_id="cat1", category="General"),
        _make_export(channel_id="ch2", category_id="cat1", category="General"),
    ]

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/categories",
            payload={"id": "stoat-cat1", "title": "General"},
        )

        await run_categories(config, state, exports, events.append)

    assert len(state.category_map) == 1
    assert state.category_map["cat1"] == "stoat-cat1"


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
    """CHANNELS phase calls api_edit_category with the stoat channel IDs."""
    events: list[MigrationEvent] = []
    config = _make_config(tmp_path)
    # Pre-populate category_map as if run_categories already ran.
    state = MigrationState(stoat_server_id="srv1", category_map={"cat1": "stoat-cat1"})

    exports = [_make_export(channel_id="ch1", channel_name="general", category_id="cat1")]

    patch_body: dict[str, object] = {}

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "stoat-ch1", "name": "general"},
        )
        m.patch(
            f"{STOAT_URL}/servers/srv1/categories/stoat-cat1",
            payload={},
            callback=lambda url, **kwargs: patch_body.update(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )

        await run_channels(config, state, exports, events.append)

    assert state.channel_map["ch1"] == "stoat-ch1"
    assert patch_body.get("channels") == ["stoat-ch1"]


async def test_run_channels_thread_flattening(tmp_path: Path) -> None:
    """CHANNELS phase prepends parent channel name to thread channel names."""
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
            payload={"_id": "stoat-th1", "name": "general-my-thread"},
            callback=lambda url, **kwargs: created_names.append(  # type: ignore[misc]
                (kwargs.get("json") or {}).get("name", "")
            ),
        )

        await run_channels(config, state, exports, events.append)

    assert state.channel_map["th1"] == "stoat-th1"
    assert created_names[0] == "general-my-thread"


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
    """Names longer than 64 characters are truncated."""
    long_name = "a" * 100
    existing: set[str] = set()
    result = make_unique_channel_name(long_name, existing)
    assert len(result) == 64
    assert result == "a" * 64


def test_make_unique_channel_name_truncated_collision() -> None:
    """Collision with suffix stays within 64 chars."""
    base = "a" * 64
    existing: set[str] = {base}
    long_name = "a" * 100  # truncates to same base
    result = make_unique_channel_name(long_name, existing)
    assert len(result) <= 64
    assert result == "a" * 62 + "-1"


# ---------------------------------------------------------------------------
# Bug 1: skip_threads in channels phase
# ---------------------------------------------------------------------------


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
