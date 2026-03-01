"""Tests for Discord metadata persistence."""

from pathlib import Path

from discord_ferry.discord.metadata import (
    ChannelMeta,
    DiscordMetadata,
    PermissionPair,
    RoleOverride,
    load_discord_metadata,
    save_discord_metadata,
)


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
