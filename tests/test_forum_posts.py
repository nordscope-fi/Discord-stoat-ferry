"""#1217: recognise real forum posts from Discord metadata."""

from __future__ import annotations

from discord_ferry.discord import client
from discord_ferry.discord.metadata import (
    DiscordMetadata,
    capture_permission_source,
    forum_parent_ids,
    is_forum_post,
)
from discord_ferry.parser.models import DCEChannel, DCEExport, DCEGuild


def _meta(*channels: tuple[str, int]) -> DiscordMetadata:
    parsed = [
        client._parse_channel({"id": cid, "name": "c", "type": ctype, "guild_id": "111"})
        for cid, ctype in channels
    ]
    return DiscordMetadata(
        "111", "now", 0, {}, {}, permission_source=capture_permission_source("111", [], parsed)
    )


def _thread(
    category_id: str, *, channel_type: int = 11, parent: str = "feedback-forum"
) -> DCEExport:
    return DCEExport(
        DCEGuild("111", "source"),
        DCEChannel("222", channel_type, "Bug Report", category_id=category_id, category=parent),
        is_thread=True,
        parent_channel_name=parent,
    )


def test_forum_parent_ids_lists_forum_and_media_channels() -> None:
    meta = _meta(("300", 15), ("400", 16), ("500", 0))
    assert forum_parent_ids(meta) == frozenset({"300", "400"})


def test_forum_parent_ids_is_empty_without_metadata() -> None:
    assert forum_parent_ids(None) == frozenset()
    assert forum_parent_ids(DiscordMetadata("111", "now", 0, {}, {})) == frozenset()


def test_is_forum_post_accepts_real_and_legacy_shapes_only() -> None:
    forums = frozenset({"300"})
    assert is_forum_post(_thread("300"), forums)
    assert not is_forum_post(_thread("500"), forums)
    assert is_forum_post(_thread("", channel_type=15))
    assert is_forum_post(_thread("", channel_type=16))
    channel = DCEExport(
        DCEGuild("111", "source"), DCEChannel("222", 0, "general", category_id="300")
    )
    assert not is_forum_post(channel, forums)
    assert not is_forum_post(_thread("300", parent=""), forums)
