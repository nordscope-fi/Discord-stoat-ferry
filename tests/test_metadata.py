"""Tests for Discord metadata persistence."""

import json
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from discord_ferry.discord import fetch_and_translate_guild_metadata
from discord_ferry.discord.metadata import (
    ChannelMeta,
    DiscordMetadata,
    PermissionPair,
    RoleMeta,
    RoleOverride,
    _dict_to_meta,
    _meta_to_dict,
    load_bound_discord_metadata,
    load_discord_metadata,
    load_discord_metadata_for_resume,
    read_cached_metadata,
    save_discord_metadata,
)
from discord_ferry.discord.permissions import ALL_STOAT_PERMISSIONS, translate_permissions
from discord_ferry.errors import MigrationError


def test_metadata_roundtrip_preserves_new_fields(tmp_path: Path) -> None:
    meta = DiscordMetadata(
        guild_id="123",
        fetched_at="2026-06-23T00:00:00+00:00",
        server_default_permissions=7,
        role_permissions={"role-a": PermissionPair(allow=1, deny=2)},
        channel_metadata={"chan-a": ChannelMeta(nsfw=True)},
        role_metadata={"role-a": RoleMeta(hoist=True, position=5)},
        category_positions={"cat-a": 3, "cat-b": 0},
        guild_description="A server",
        guild_nsfw=True,
    )
    save_discord_metadata(meta, tmp_path)
    loaded = load_discord_metadata(tmp_path)
    assert loaded == meta


def test_metadata_legacy_file_loads_with_defaults(tmp_path: Path) -> None:
    # A pre-upgrade discord_metadata.json without the new keys must load.
    legacy = {
        "guild_id": "123",
        "fetched_at": "2026-06-23T00:00:00+00:00",
        "server_default_permissions": 0,
        "role_permissions": {},
        "channel_metadata": {},
    }
    (tmp_path / "discord_metadata.json").write_text(json.dumps(legacy))
    loaded = load_discord_metadata(tmp_path)
    assert loaded is not None
    assert loaded.role_metadata == {}
    assert loaded.category_positions == {}
    assert loaded.guild_description == ""
    assert loaded.guild_nsfw is False


def test_save_load_roundtrip(tmp_path: Path) -> None:
    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="2026-03-01T00:00:00Z",
        server_default_permissions=1_048_576,
        role_permissions={
            "role1": PermissionPair(allow=4_194_304, deny=0),
        },
        channel_metadata={
            "ch1": ChannelMeta(
                nsfw=True,
                default_override=PermissionPair(allow=4_194_304, deny=8_388_608),
                role_overrides=[
                    RoleOverride(discord_role_id="role1", allow=4_194_304, deny=0),
                ],
            ),
        },
    )
    save_discord_metadata(meta, tmp_path)
    loaded = load_discord_metadata(tmp_path)
    assert loaded is not None
    assert loaded.guild_id == "111"
    assert loaded.server_default_permissions == 1_048_576
    assert loaded.role_permissions["role1"].allow == 4_194_304
    assert loaded.channel_metadata["ch1"].nsfw is True
    assert loaded.channel_metadata["ch1"].default_override is not None
    assert loaded.channel_metadata["ch1"].default_override.deny == 8_388_608
    assert len(loaded.channel_metadata["ch1"].role_overrides) == 1
    assert loaded.channel_metadata["ch1"].role_overrides[0].discord_role_id == "role1"


def test_load_missing_returns_none(tmp_path: Path) -> None:
    assert load_discord_metadata(tmp_path) is None


def test_save_creates_directory(tmp_path: Path) -> None:
    nested = tmp_path / "deep" / "dir"
    meta = DiscordMetadata(
        guild_id="x",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
    )
    save_discord_metadata(meta, nested)
    assert (nested / "discord_metadata.json").exists()


def test_empty_metadata_roundtrip(tmp_path: Path) -> None:
    meta = DiscordMetadata(
        guild_id="empty",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
    )
    save_discord_metadata(meta, tmp_path)
    loaded = load_discord_metadata(tmp_path)
    assert loaded is not None
    assert loaded.role_permissions == {}
    assert loaded.channel_metadata == {}


def test_channel_without_overrides(tmp_path: Path) -> None:
    meta = DiscordMetadata(
        guild_id="g",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={
            "ch1": ChannelMeta(nsfw=False),
        },
    )
    save_discord_metadata(meta, tmp_path)
    loaded = load_discord_metadata(tmp_path)
    assert loaded is not None
    assert loaded.channel_metadata["ch1"].default_override is None
    assert loaded.channel_metadata["ch1"].role_overrides == []


_DISCORD_API = "https://discord.com/api/v10"
_GUILD_ID = "999000000000000001"


async def test_everyone_deny_view_channel_produces_stoat_deny_bit() -> None:
    """Discord @everyone VIEW_CHANNEL deny -> Stoat ViewChannel deny bit."""
    from aioresponses import aioresponses

    discord_view_channel = 1 << 10  # Discord VIEW_CHANNEL bit
    stoat_view_channel = 1 << 20  # Stoat ViewChannel bit

    channel_id = "555000000000000001"

    mock_roles = [
        {
            "id": _GUILD_ID,  # @everyone role id == guild_id
            "name": "@everyone",
            "permissions": "0",
            "position": 0,
            "color": 0,
            "hoist": False,
            "managed": False,
        },
    ]

    mock_channels = [
        {
            "id": channel_id,
            "name": "private-channel",
            "type": 0,
            "nsfw": False,
            "permission_overwrites": [
                {
                    "id": _GUILD_ID,  # @everyone override
                    "type": 0,  # role type
                    "allow": "0",
                    "deny": str(discord_view_channel),
                },
            ],
        },
    ]

    mock_guild = {"id": _GUILD_ID, "name": "Test", "banner": None}

    with aioresponses() as m:
        m.get(f"{_DISCORD_API}/guilds/{_GUILD_ID}", payload=mock_guild)
        m.get(f"{_DISCORD_API}/guilds/{_GUILD_ID}/roles", payload=mock_roles)
        m.get(f"{_DISCORD_API}/guilds/{_GUILD_ID}/channels", payload=mock_channels)

        async with aiohttp.ClientSession() as session:
            meta = await fetch_and_translate_guild_metadata(session, "test-token", _GUILD_ID)

    ch_meta = meta.channel_metadata[channel_id]
    assert ch_meta.default_override is not None, "Expected default_override for @everyone deny"
    assert ch_meta.default_override.deny & stoat_view_channel, (
        f"Expected Stoat ViewChannel deny bit (1<<20), got {ch_meta.default_override.deny}"
    )


_STAFF_CHANNEL = "555000000000000002"
_ADMIN_ROLE = "777000000000000001"
_ADMINISTRATOR = 1 << 3  # Discord ADMINISTRATOR


def _role(role_id: str, name: str, permissions: int = 0, position: int = 0) -> dict[str, object]:
    """One Discord role payload, in the shape the roles endpoint returns."""
    return {
        "id": role_id,
        "name": name,
        "permissions": str(permissions),
        "position": position,
        "color": 0,
        "hoist": False,
        "managed": False,
    }


async def _fetch_one_channel(
    overwrites: list[dict[str, object]],
    *,
    roles: list[dict[str, object]] | None = None,
) -> DiscordMetadata:
    """Fetch metadata for a guild with one text channel carrying ``overwrites``."""
    from aioresponses import aioresponses

    channels = [
        {
            "id": _STAFF_CHANNEL,
            "name": "staff",
            "type": 0,
            "nsfw": False,
            "permission_overwrites": overwrites,
        }
    ]
    with aioresponses() as m:
        m.get(
            f"{_DISCORD_API}/guilds/{_GUILD_ID}",
            payload={"id": _GUILD_ID, "name": "Test", "banner": None},
        )
        m.get(
            f"{_DISCORD_API}/guilds/{_GUILD_ID}/roles",
            payload=roles if roles is not None else [_role(_GUILD_ID, "@everyone")],
        )
        m.get(f"{_DISCORD_API}/guilds/{_GUILD_ID}/channels", payload=channels)
        async with aiohttp.ClientSession() as session:
            return await fetch_and_translate_guild_metadata(session, "test-token", _GUILD_ID)


async def test_everyone_overwrite_allowing_administrator_translates_to_zero() -> None:
    """SC-1.1 (#986): Discord grants nothing by ADMINISTRATOR inside an overwrite."""
    meta = await _fetch_one_channel(
        [{"id": _GUILD_ID, "type": 0, "allow": str(_ADMINISTRATOR), "deny": "0"}]
    )
    override = meta.channel_metadata[_STAFF_CHANNEL].default_override
    assert override is not None
    assert override.allow == 0


async def test_role_overwrite_allowing_administrator_keeps_only_send_messages() -> None:
    """SC-1.2 (#986): the overwrite's other bits survive, the shortcut does not."""
    meta = await _fetch_one_channel(
        [
            {
                "id": _ADMIN_ROLE,
                "type": 0,
                "allow": str(_ADMINISTRATOR | (1 << 11)),  # plus SEND_MESSAGES
                "deny": "0",
            }
        ]
    )
    (override,) = meta.channel_metadata[_STAFF_CHANNEL].role_overrides
    assert override.allow == translate_permissions(1 << 11)
    assert override.allow == 1 << 22  # Stoat SendMessage and nothing else


async def test_everyone_overwrite_denying_administrator_keeps_the_other_deny_bits() -> None:
    """SC-1.4: the deny side behaves exactly as before the fix."""
    meta = await _fetch_one_channel(
        [{"id": _GUILD_ID, "type": 0, "allow": "0", "deny": str(_ADMINISTRATOR | (1 << 10))}]
    )
    override = meta.channel_metadata[_STAFF_CHANNEL].default_override
    assert override is not None
    assert override.deny == 1 << 20  # Stoat ViewChannel only


async def test_role_level_administrator_still_expands_while_an_overwrite_does_not() -> None:
    """SC-1.5: the keyword reaches overwrites only, never role or server-default permissions."""
    meta = await _fetch_one_channel(
        [{"id": _ADMIN_ROLE, "type": 0, "allow": str(_ADMINISTRATOR), "deny": "0"}],
        roles=[
            _role(_GUILD_ID, "@everyone", _ADMINISTRATOR),
            _role(_ADMIN_ROLE, "Admin", _ADMINISTRATOR, position=1),
        ],
    )
    assert meta.server_default_permissions == ALL_STOAT_PERMISSIONS
    assert meta.role_permissions[_ADMIN_ROLE].allow == ALL_STOAT_PERMISSIONS
    (override,) = meta.channel_metadata[_STAFF_CHANNEL].role_overrides
    assert override.allow == 0


def test_user_override_channels_roundtrip(tmp_path: Path) -> None:
    """user_override_channels persists through save/load cycle."""
    overrides = [
        {"channel_id": "ch1", "channel_name": "general", "override_count": 3},
        {"channel_id": "ch2", "channel_name": "mods", "override_count": 1},
    ]
    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="2026-03-01T00:00:00Z",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
        user_override_channels=overrides,
    )
    save_discord_metadata(meta, tmp_path)
    loaded = load_discord_metadata(tmp_path)
    assert loaded is not None
    assert len(loaded.user_override_channels) == 2
    assert loaded.user_override_channels[0]["channel_id"] == "ch1"
    assert loaded.user_override_channels[0]["override_count"] == 3
    assert loaded.user_override_channels[1]["channel_name"] == "mods"


def test_user_override_channels_empty_by_default(tmp_path: Path) -> None:
    """user_override_channels defaults to empty list."""
    meta = DiscordMetadata(
        guild_id="g",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
    )
    assert meta.user_override_channels == []
    save_discord_metadata(meta, tmp_path)
    loaded = load_discord_metadata(tmp_path)
    assert loaded is not None
    assert loaded.user_override_channels == []


def test_user_override_channels_backward_compat(tmp_path: Path) -> None:
    """Loading old metadata without user_override_channels field returns empty list."""
    import json

    old_data = {
        "guild_id": "111",
        "fetched_at": "t",
        "server_default_permissions": 0,
        "role_permissions": {},
        "channel_metadata": {},
        # No user_override_channels key — old format
    }
    (tmp_path / "discord_metadata.json").write_text(json.dumps(old_data), encoding="utf-8")
    loaded = load_discord_metadata(tmp_path)
    assert loaded is not None
    assert loaded.user_override_channels == []


async def test_user_overrides_counted_in_fetch() -> None:
    """fetch_and_translate_guild_metadata counts user overrides per channel."""
    from aioresponses import aioresponses

    guild_id = "999000000000000001"

    mock_roles = [
        {
            "id": guild_id,
            "name": "@everyone",
            "permissions": "0",
            "position": 0,
            "color": 0,
            "hoist": False,
            "managed": False,
        },
    ]

    mock_channels = [
        {
            "id": "ch1",
            "name": "general",
            "type": 0,
            "nsfw": False,
            "permission_overwrites": [
                {"id": "user1", "type": 1, "allow": "0", "deny": "1024"},
                {"id": "user2", "type": 1, "allow": "0", "deny": "1024"},
                {"id": guild_id, "type": 0, "allow": "0", "deny": "0"},
            ],
        },
        {
            "id": "ch2",
            "name": "no-overrides",
            "type": 0,
            "nsfw": False,
            "permission_overwrites": [],
        },
    ]

    mock_guild = {"id": guild_id, "name": "Test", "banner": None}

    with aioresponses() as m:
        m.get(
            f"https://discord.com/api/v10/guilds/{guild_id}",
            payload=mock_guild,
        )
        m.get(
            f"https://discord.com/api/v10/guilds/{guild_id}/roles",
            payload=mock_roles,
        )
        m.get(
            f"https://discord.com/api/v10/guilds/{guild_id}/channels",
            payload=mock_channels,
        )

        async with aiohttp.ClientSession() as session:
            meta = await fetch_and_translate_guild_metadata(session, "test-token", guild_id)

    assert len(meta.user_override_channels) == 1
    assert meta.user_override_channels[0]["channel_id"] == "ch1"
    assert meta.user_override_channels[0]["channel_name"] == "general"
    assert meta.user_override_channels[0]["override_count"] == 2


async def test_banner_hash_extracted() -> None:
    """fetch_and_translate_guild_metadata extracts banner hash from guild data."""
    from aioresponses import aioresponses

    guild_id = "999000000000000001"

    mock_guild = {
        "id": guild_id,
        "name": "Test Guild",
        "banner": "abc123",
    }

    mock_roles = [
        {
            "id": guild_id,
            "name": "@everyone",
            "permissions": "0",
            "position": 0,
            "color": 0,
            "hoist": False,
            "managed": False,
        },
    ]

    mock_channels: list[dict[str, object]] = []

    with aioresponses() as m:
        m.get(f"{_DISCORD_API}/guilds/{guild_id}", payload=mock_guild)
        m.get(f"{_DISCORD_API}/guilds/{guild_id}/roles", payload=mock_roles)
        m.get(f"{_DISCORD_API}/guilds/{guild_id}/channels", payload=mock_channels)

        async with aiohttp.ClientSession() as session:
            meta = await fetch_and_translate_guild_metadata(session, "test-token", guild_id)

    assert meta.banner_hash == "abc123"


async def test_no_banner_hash() -> None:
    """fetch_and_translate_guild_metadata returns empty banner_hash when guild has no banner."""
    from aioresponses import aioresponses

    guild_id = "999000000000000001"

    mock_guild = {
        "id": guild_id,
        "name": "Test Guild",
        "banner": None,
    }

    mock_roles = [
        {
            "id": guild_id,
            "name": "@everyone",
            "permissions": "0",
            "position": 0,
            "color": 0,
            "hoist": False,
            "managed": False,
        },
    ]

    mock_channels: list[dict[str, object]] = []

    with aioresponses() as m:
        m.get(f"{_DISCORD_API}/guilds/{guild_id}", payload=mock_guild)
        m.get(f"{_DISCORD_API}/guilds/{guild_id}/roles", payload=mock_roles)
        m.get(f"{_DISCORD_API}/guilds/{guild_id}/channels", payload=mock_channels)

        async with aiohttp.ClientSession() as session:
            meta = await fetch_and_translate_guild_metadata(session, "test-token", guild_id)

    assert meta.banner_hash == ""


def test_batch2_fields_round_trip():
    meta = DiscordMetadata(
        guild_id="g1",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={
            "c1": ChannelMeta(nsfw=False, slowmode=30, user_limit=5),
        },
        role_metadata={"r1": RoleMeta(hoist=True, position=3, icon_hash="abc", unicode_emoji="")},
    )
    restored = _dict_to_meta(_meta_to_dict(meta))
    assert restored.channel_metadata["c1"].slowmode == 30
    assert restored.channel_metadata["c1"].user_limit == 5
    assert restored.role_metadata["r1"].icon_hash == "abc"
    assert restored.role_metadata["r1"].unicode_emoji == ""


def test_batch2_fields_default_on_legacy_json():
    # Legacy metadata without the new keys loads with zero/empty defaults.
    legacy = {
        "guild_id": "g",
        "fetched_at": "t",
        "server_default_permissions": 0,
        "role_permissions": {},
        "channel_metadata": {"c1": {"nsfw": False}},
        "role_metadata": {"r1": {"hoist": False, "position": 0}},
    }
    restored = _dict_to_meta(legacy)
    assert restored.channel_metadata["c1"].slowmode == 0
    assert restored.channel_metadata["c1"].user_limit == 0
    assert restored.role_metadata["r1"].icon_hash == ""


def test_rolemeta_name_color_round_trip():
    from discord_ferry.discord.metadata import (
        DiscordMetadata,
        RoleMeta,
        _dict_to_meta,
        _meta_to_dict,
    )

    meta = DiscordMetadata(
        guild_id="g",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
        role_metadata={"r1": RoleMeta(hoist=True, position=3, name="Mod", color="#ff0000")},
    )
    restored = _dict_to_meta(_meta_to_dict(meta))
    assert restored.role_metadata["r1"].name == "Mod"
    assert restored.role_metadata["r1"].color == "#ff0000"


def test_rolemeta_legacy_json_defaults_name_color():
    from discord_ferry.discord.metadata import _dict_to_meta

    legacy = {
        "guild_id": "g",
        "fetched_at": "t",
        "server_default_permissions": 0,
        "role_permissions": {},
        "channel_metadata": {},
        "role_metadata": {"r1": {"hoist": False, "position": 0}},  # no name/color keys
    }
    restored = _dict_to_meta(legacy)
    assert restored.role_metadata["r1"].name == ""
    assert restored.role_metadata["r1"].color == ""


def test_save_metadata_overwrites_existing_file_on_windows(
    windows_filesystem: None, tmp_path: Path
) -> None:
    """Issue #172, the third swap site."""
    first = DiscordMetadata(
        guild_id="123",
        fetched_at="2026-06-23T00:00:00+00:00",
        server_default_permissions=7,
        role_permissions={"role-a": PermissionPair(allow=1, deny=2)},
        channel_metadata={"chan-a": ChannelMeta(nsfw=True)},
    )
    second = DiscordMetadata(
        guild_id="456",
        fetched_at="2026-06-24T00:00:00+00:00",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
    )

    save_discord_metadata(first, tmp_path)
    save_discord_metadata(second, tmp_path)

    # Full-object equality, matching test_metadata_roundtrip_preserves_new_fields.
    # Asserting guild_id alone would pass even if the second write landed only
    # partially, because every other field would still read back as `first`'s.
    assert load_discord_metadata(tmp_path) == second


# --- Resume checks (#971) ----------------------------------------------------

_GUILD = "111111111111111111"
_ROLE = "222222222222222222"
_CHAN = "333333333333333333"


def _good_dict() -> dict[str, object]:
    return {
        "guild_id": _GUILD,
        "fetched_at": "2026-06-23T00:00:00+00:00",
        "server_default_permissions": 1 << 22,
        "role_permissions": {_ROLE: {"allow": 1 << 22, "deny": 0}},
        "channel_metadata": {
            _CHAN: {
                "nsfw": False,
                "default_override": {"allow": 0, "deny": 1 << 20},
                "role_overrides": [{"discord_role_id": _ROLE, "allow": 1 << 22, "deny": 0}],
            }
        },
    }


def _write(tmp_path: Path, data: dict[str, object]) -> None:
    (tmp_path / "discord_metadata.json").write_text(json.dumps(data))


def test_resume_load_returns_none_without_a_file(tmp_path: Path) -> None:
    assert load_discord_metadata_for_resume(tmp_path, configured_guild_id=_GUILD) is None


def test_resume_load_keeps_valid_metadata_exactly(tmp_path: Path) -> None:
    _write(tmp_path, _good_dict())
    loaded = load_discord_metadata_for_resume(tmp_path, configured_guild_id=_GUILD)
    assert loaded is not None
    assert loaded == load_discord_metadata(tmp_path)
    assert loaded.server_default_permissions == 1 << 22
    assert loaded.role_permissions[_ROLE] == PermissionPair(allow=1 << 22, deny=0)


def test_resume_load_without_a_configured_guild_skips_the_guild_comparison(
    tmp_path: Path,
) -> None:
    _write(tmp_path, _good_dict())
    assert load_discord_metadata_for_resume(tmp_path) is not None


def test_resume_load_rejects_a_different_configured_guild(tmp_path: Path) -> None:
    _write(tmp_path, _good_dict())
    with pytest.raises(MigrationError) as err:
        load_discord_metadata_for_resume(tmp_path, configured_guild_id="999999999999999999")
    message = str(err.value)
    assert _GUILD in message
    assert "999999999999999999" in message
    assert "discord_metadata.json" in message


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda d: d.update(guild_id="not-a-guild"), id="guild-id-letters"),
        pytest.param(lambda d: d.update(guild_id=123), id="guild-id-int"),
        pytest.param(lambda d: d.update(guild_id=""), id="guild-id-empty"),
        pytest.param(lambda d: d.update(guild_id="1" * 21), id="guild-id-too-long"),
        pytest.param(lambda d: d.update(guild_id="１２３"), id="guild-id-unicode-digits"),
        pytest.param(
            lambda d: d.update(role_permissions={"role-a": {"allow": 0, "deny": 0}}),
            id="role-key",
        ),
        pytest.param(
            lambda d: d.update(channel_metadata={"chan-a": {"nsfw": False}}), id="channel-key"
        ),
        pytest.param(
            lambda d: d["channel_metadata"][_CHAN]["role_overrides"][0].update(  # type: ignore[index,union-attr]
                discord_role_id="x"
            ),
            id="override-role-id",
        ),
        pytest.param(lambda d: d.update(role_metadata={"r": {}}), id="role-metadata-key"),
        pytest.param(lambda d: d.update(category_positions={"c": 1}), id="category-key"),
    ],
)
def test_resume_load_rejects_malformed_identifiers(tmp_path: Path, mutate: Any) -> None:
    data = _good_dict()
    mutate(data)
    _write(tmp_path, data)
    with pytest.raises(MigrationError, match="not a Discord id"):
        load_discord_metadata_for_resume(tmp_path)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda d: d.update(server_default_permissions=True), id="default-bool"),
        pytest.param(lambda d: d.update(server_default_permissions=-1), id="default-negative"),
        pytest.param(lambda d: d.update(server_default_permissions="5"), id="default-string"),
        pytest.param(lambda d: d.update(server_default_permissions=1.5), id="default-float"),
        pytest.param(
            lambda d: d.update(role_permissions={_ROLE: {"allow": True, "deny": 0}}),
            id="role-allow-bool",
        ),
        pytest.param(
            lambda d: d.update(role_permissions={_ROLE: {"allow": 0, "deny": -4}}),
            id="role-deny-negative",
        ),
        pytest.param(
            lambda d: d["channel_metadata"][_CHAN]["default_override"].update(allow=False),  # type: ignore[index,union-attr]
            id="channel-default-bool",
        ),
        pytest.param(
            lambda d: d["channel_metadata"][_CHAN]["role_overrides"][0].update(deny=-1),  # type: ignore[index,union-attr]
            id="channel-override-negative",
        ),
    ],
)
def test_resume_load_rejects_bool_negative_and_non_int_permissions(
    tmp_path: Path, mutate: Any
) -> None:
    data = _good_dict()
    mutate(data)
    _write(tmp_path, data)
    with pytest.raises(MigrationError, match="non-negative whole number"):
        load_discord_metadata_for_resume(tmp_path)


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        "[]",
        '{"fetched_at": "x"}',
        '{"guild_id": "1", "fetched_at": "x", "role_permissions": {"1": 5}}',
    ],
    ids=["bad-json", "not-an-object", "missing-guild-id", "pair-not-an-object"],
)
def test_resume_load_rejects_a_file_that_is_not_ferry_metadata(tmp_path: Path, text: str) -> None:
    (tmp_path / "discord_metadata.json").write_text(text)
    with pytest.raises(MigrationError, match="not a valid Ferry metadata file"):
        load_discord_metadata_for_resume(tmp_path)


def test_unknown_permission_bits_are_dropped_like_the_fetch_path_drops_them(
    tmp_path: Path,
) -> None:
    """Bits Stoat does not define (5, 14-19, 41+) cannot come from a fetch, so they go."""
    unknown = (1 << 5) | (1 << 14) | (1 << 41) | (1 << 60)
    known = 1 << 22  # SendMessage
    data = _good_dict()
    data["server_default_permissions"] = known | unknown
    data["role_permissions"] = {_ROLE: {"allow": known | unknown, "deny": unknown}}
    data["channel_metadata"] = {
        _CHAN: {
            "nsfw": False,
            "default_override": {"allow": unknown, "deny": known | unknown},
            "role_overrides": [{"discord_role_id": _ROLE, "allow": known | unknown, "deny": 0}],
        }
    }
    _write(tmp_path, data)
    loaded = load_discord_metadata_for_resume(tmp_path, configured_guild_id=_GUILD)
    assert loaded is not None
    assert loaded.server_default_permissions == known
    assert loaded.role_permissions[_ROLE] == PermissionPair(allow=known, deny=0)
    override = loaded.channel_metadata[_CHAN]
    assert override.default_override == PermissionPair(allow=0, deny=known)
    assert override.role_overrides == [RoleOverride(discord_role_id=_ROLE, allow=known, deny=0)]
    # Masking only ever removes bits: nothing outside the defined set survives, and
    # nothing is added.
    for value in (known | unknown, unknown):
        assert (value & ALL_STOAT_PERMISSIONS) & ~value == 0


def test_every_defined_stoat_bit_survives_the_load(tmp_path: Path) -> None:
    data = _good_dict()
    data["server_default_permissions"] = ALL_STOAT_PERMISSIONS
    _write(tmp_path, data)
    loaded = load_discord_metadata_for_resume(tmp_path)
    assert loaded is not None
    assert loaded.server_default_permissions == ALL_STOAT_PERMISSIONS


def test_read_cached_metadata_accepts_a_listed_export_guild(tmp_path: Path) -> None:
    _write(tmp_path, _good_dict())
    cached = read_cached_metadata(tmp_path, export_guild_ids={_GUILD, "555555555555555555"})
    assert cached.problem is None
    assert cached.meta is not None


def test_read_cached_metadata_rejects_another_export_guild(tmp_path: Path) -> None:
    _write(tmp_path, _good_dict())
    cached = read_cached_metadata(tmp_path, export_guild_ids={"555555555555555555"})
    assert cached.meta is None
    assert cached.problem is not None
    assert _GUILD in cached.problem
    assert "555555555555555555" in cached.problem


def test_read_cached_metadata_never_raises_on_a_bad_file(tmp_path: Path) -> None:
    (tmp_path / "discord_metadata.json").write_text("{not json")
    cached = read_cached_metadata(tmp_path)
    assert cached.meta is None
    assert cached.problem is not None
    assert "not a valid Ferry metadata file" in cached.problem


def test_read_cached_metadata_reports_a_missing_file_as_absent(tmp_path: Path) -> None:
    cached = read_cached_metadata(tmp_path, export_guild_ids={_GUILD})
    assert (cached.meta, cached.problem, cached.dropped_unknown_bits) == (None, None, False)


def test_read_cached_metadata_reports_dropped_unknown_bits_only_when_there_are_some(
    tmp_path: Path,
) -> None:
    data = _good_dict()
    _write(tmp_path, data)
    assert read_cached_metadata(tmp_path).dropped_unknown_bits is False
    data["role_permissions"] = {_ROLE: {"allow": (1 << 22) | (1 << 41), "deny": 0}}
    _write(tmp_path, data)
    cached = read_cached_metadata(tmp_path)
    assert cached.dropped_unknown_bits is True
    assert cached.meta is not None
    assert cached.meta.role_permissions[_ROLE].allow == 1 << 22


def test_a_bool_is_not_counted_as_dropped_bits(tmp_path: Path) -> None:
    """A bool is rejected as a problem, not silently masked."""
    data = _good_dict()
    data["server_default_permissions"] = True
    _write(tmp_path, data)
    cached = read_cached_metadata(tmp_path)
    assert cached.dropped_unknown_bits is False
    assert cached.problem is not None


def test_load_bound_discord_metadata_uses_the_configured_guild_and_exports(
    tmp_path: Path,
) -> None:
    from discord_ferry.config import FerryConfig
    from discord_ferry.parser.models import DCEChannel, DCEExport, DCEGuild

    _write(tmp_path, _good_dict())

    def export(guild_id: str) -> DCEExport:
        return DCEExport(
            guild=DCEGuild(id=guild_id, name="g"),
            channel=DCEChannel(id=_CHAN, type=0, name="c"),
        )

    def config(server: str | None) -> FerryConfig:
        return FerryConfig(
            export_dir=tmp_path,
            stoat_url="https://api.test",
            token="t",
            output_dir=tmp_path,
            discord_server_id=server,
        )

    assert load_bound_discord_metadata(config(None)) is not None
    assert load_bound_discord_metadata(config(_GUILD), [export(_GUILD)]) is not None
    assert load_bound_discord_metadata(config(None), [export("555555555555555555")]) is None
    assert load_bound_discord_metadata(config("555555555555555555"), [export(_GUILD)]) is None


@pytest.mark.parametrize(
    "entry",
    [
        {},
        {"channel_id": "222222222222222222", "override_count": 1},
        {"channel_id": "222222222222222222", "channel_name": "c", "override_count": "1"},
        "not a dict",
    ],
)
def test_read_cached_metadata_rejects_a_malformed_user_override_entry(
    tmp_path: Path, entry: object
) -> None:
    """The review step and the report read channel_name and override_count from each
    entry, so a malformed one used to raise KeyError there (#971 review)."""
    data = _good_dict()
    data["user_override_channels"] = [entry]
    _write(tmp_path, data)
    cached = read_cached_metadata(tmp_path)
    assert cached.meta is None
    assert cached.problem is not None
    assert "user_override_channels" in cached.problem
