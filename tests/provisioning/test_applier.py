"""Tests for tests/provisioning/_applier.py."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
import pytest
from aioresponses import aioresponses

if TYPE_CHECKING:
    from collections.abc import Generator

from tests.provisioning._applier import (
    ActualChannel,
    ActualEmbed,
    ActualEmbedField,
    ActualMessage,
    ActualState,
    CreateForumChannelOp,
    CreateForumPostOp,
    CreateMessageOp,
    CreateTextChannelOp,
    CreateThreadOp,
    DeleteChannelOp,
    Diff,
    EmbedMismatch,
    Manifest,
    ManifestEmbed,
    ManifestEmbedField,
    ManifestForumChannel,
    ManifestForumPost,
    ManifestMessage,
    ManifestTextChannel,
    ManifestThread,
    fetch_actual_state,
    load_manifest,
)
from tests.provisioning._bot_api import BotApi, ProvisioningError

DISCORD_API = "https://discord.com/api/v10"
TOKEN = "test-bot-token"


def test_public_dataclass_surface_is_importable() -> None:
    """Smoke-test that all public manifest dataclasses are importable.

    Downstream tasks (12-16) import these names; this test gives a clear
    failure signal if a rename or removal breaks the public surface.
    """
    for cls in (
        Manifest,
        ManifestEmbed,
        ManifestEmbedField,
        ManifestForumChannel,
        ManifestForumPost,
        ManifestMessage,
        ManifestTextChannel,
        ManifestThread,
        # ActualState + diff types (Task 12)
        ActualChannel,
        ActualEmbed,
        ActualEmbedField,
        ActualMessage,
        ActualState,
        CreateForumChannelOp,
        CreateForumPostOp,
        CreateMessageOp,
        CreateTextChannelOp,
        CreateThreadOp,
        DeleteChannelOp,
        Diff,
        EmbedMismatch,
    ):
        assert isinstance(cls, type)


def _valid_manifest_dict() -> dict[str, Any]:
    return {
        "version": 1,
        "marker": "[ferry-fixture]",
        "guild_name_for_bootstrap": "Discord Ferry Test Fixture",
        "text_channels": [
            {
                "id": "ch-general",
                "name": "general",
                "topic_suffix": "primary",
                "messages": [
                    {"id": f"msg-{i:03d}", "content": f"Message {i}."} for i in range(1, 10)
                ]
                + [
                    {
                        "id": "msg-embed",
                        "content": "Embed message.",
                        "embed": {
                            "title": "T",
                            "description": "D",
                            "color": 5814783,
                            "fields": [
                                {"name": "i1", "value": "v1", "inline": True},
                                {"name": "i2", "value": "v2", "inline": True},
                                {"name": "i3", "value": "v3", "inline": True},
                                {"name": "n1", "value": "v4", "inline": False},
                                {"name": "n2", "value": "v5", "inline": False},
                            ],
                        },
                    }
                ],
            }
        ],
        "threads": [
            {
                "id": "thread-cool",
                "name": "Cool Thread",
                "parent_channel_id": "ch-general",
                "anchor_message_id": "msg-003",
                "first_message_content": "First reply.",
            }
        ],
        "forum_channels": [
            {
                "id": "fch-feedback",
                "name": "Feedback Forum",
                "topic_suffix": "forum",
                "posts": [
                    {
                        "id": "post-bug",
                        "name": "Bug Report",
                        "first_message_content": "Bug body.",
                    }
                ],
            }
        ],
    }


def test_load_manifest_happy_path(tmp_path: Path) -> None:
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(_valid_manifest_dict()))
    manifest = load_manifest(path)
    assert isinstance(manifest, Manifest)
    assert manifest.version == 1
    assert len(manifest.text_channels[0].messages) == 10


def test_load_manifest_rejects_duplicate_ids(tmp_path: Path) -> None:
    data = _valid_manifest_dict()
    data["text_channels"][0]["messages"][1]["id"] = "msg-001"  # dup
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ProvisioningError, match="duplicate.*msg-001"):
        load_manifest(path)


def test_load_manifest_rejects_dangling_anchor_message_id(tmp_path: Path) -> None:
    data = _valid_manifest_dict()
    data["threads"][0]["anchor_message_id"] = "msg-nonexistent"
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ProvisioningError, match="anchor_message_id.*msg-nonexistent"):
        load_manifest(path)


def test_load_manifest_rejects_wrong_inline_ratio(tmp_path: Path) -> None:
    data = _valid_manifest_dict()
    # Flip one inline field to non-inline → 2 inline / 3 non-inline
    data["text_channels"][0]["messages"][9]["embed"]["fields"][0]["inline"] = False
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ProvisioningError, match="inline"):
        load_manifest(path)


def test_load_manifest_rejects_unsafe_marker(tmp_path: Path) -> None:
    data = _valid_manifest_dict()
    data["marker"] = "<script>alert(1)</script>"
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ProvisioningError, match="marker"):
        load_manifest(path)


def test_load_manifest_rejects_wrong_version(tmp_path: Path) -> None:
    data = _valid_manifest_dict()
    data["version"] = 2
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ProvisioningError, match="version"):
        load_manifest(path)


def test_load_manifest_rejects_zero_embed_messages(tmp_path: Path) -> None:
    """Spec invariant: exactly 1 message per text channel must have an embed."""
    data = _valid_manifest_dict()
    # Remove the embed from the only embed-carrying message
    del data["text_channels"][0]["messages"][9]["embed"]
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ProvisioningError, match="embed"):
        load_manifest(path)


def test_load_manifest_rejects_two_embed_messages(tmp_path: Path) -> None:
    """Spec invariant: exactly 1 (not 2+) message per text channel has an embed."""
    data = _valid_manifest_dict()
    # Add an embed to a second message
    data["text_channels"][0]["messages"][0]["embed"] = {
        "title": "T2",
        "description": "D2",
        "color": 0,
        "fields": [
            {"name": "i1", "value": "v1", "inline": True},
            {"name": "i2", "value": "v2", "inline": True},
            {"name": "i3", "value": "v3", "inline": True},
            {"name": "n1", "value": "v4", "inline": False},
            {"name": "n2", "value": "v5", "inline": False},
        ],
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ProvisioningError, match="embed"):
        load_manifest(path)


def test_committed_fixture_spec_loads() -> None:
    """The committed fixture-spec.json must pass load_manifest invariants."""
    path = Path(__file__).parent / "fixture-spec.json"
    manifest = load_manifest(path)
    assert len(manifest.text_channels) == 1
    assert len(manifest.text_channels[0].messages) == 10
    assert len(manifest.threads) == 1
    assert len(manifest.forum_channels) == 1
    assert len(manifest.forum_channels[0].posts) == 1
    # Verify the embed has exactly 3 inline + 2 non-inline:
    embed_msg = next(m for m in manifest.text_channels[0].messages if m.embed)
    assert embed_msg.embed is not None
    inline = sum(1 for f in embed_msg.embed.fields if f.inline)
    assert inline == 3


def test_actual_state_is_frozen_and_uses_mapping() -> None:
    state = ActualState(
        guild_id="111",
        channels=(),
        messages_by_channel={},
    )
    with pytest.raises(AttributeError):
        state.guild_id = "222"  # type: ignore[misc]


def test_diff_op_dataclasses_are_constructible() -> None:
    """Per-kind dataclasses replace the single DiffOp for mypy --strict."""
    msg = ManifestMessage(id="x", content="y")
    op = CreateMessageOp(
        target=msg,
        parent_manifest_channel_id="ch-1",
        parent_discord_id="100",
        reason="missing",
    )
    assert op.target is msg
    assert op.reason == "missing"
    assert op.parent_manifest_channel_id == "ch-1"


def test_empty_diff_is_no_op() -> None:
    diff = Diff(
        ops=(),
        missing_entities=(),
        extra_marker_entities=(),
        extra_foreign_entities=(),
        mismatched_embeds=(),
    )
    assert len(diff.ops) == 0


@pytest.fixture
def mock_discord_for_state() -> Generator[aioresponses, None, None]:
    with aioresponses() as m:
        yield m


async def test_fetch_actual_state_merges_active_and_archived_threads(
    mock_discord_for_state: aioresponses,
) -> None:
    guild = "111"
    # Channels: one text + one forum
    mock_discord_for_state.get(
        f"{DISCORD_API}/guilds/{guild}/channels",
        payload=[
            {
                "id": "100",
                "name": "general",
                "type": 0,
                "topic": "[ferry-fixture] x",
                "parent_id": None,
            },
            {
                "id": "101",
                "name": "Feedback Forum",
                "type": 15,
                "topic": "[ferry-fixture] y",
                "parent_id": None,
            },
        ],
    )
    # Active threads: one in text channel
    mock_discord_for_state.get(
        f"{DISCORD_API}/guilds/{guild}/threads/active",
        payload={
            "threads": [
                {"id": "t1", "name": "Active Thread", "type": 11, "topic": None, "parent_id": "100"}
            ],
            "members": [],
        },
    )
    # Archived public threads per parent channel
    mock_discord_for_state.get(
        f"{DISCORD_API}/channels/100/threads/archived/public",
        payload={
            "threads": [
                {
                    "id": "t2",
                    "name": "Archived Thread",
                    "type": 11,
                    "topic": None,
                    "parent_id": "100",
                }
            ],
            "members": [],
            "has_more": False,
        },
    )
    mock_discord_for_state.get(
        f"{DISCORD_API}/channels/101/threads/archived/public",
        payload={"threads": [], "members": [], "has_more": False},
    )
    # Messages in each channel (empty for this test)
    mock_discord_for_state.get(
        f"{DISCORD_API}/channels/100/messages?limit=100",
        payload=[],
    )
    mock_discord_for_state.get(
        f"{DISCORD_API}/channels/101/messages?limit=100",
        payload=[],
    )
    mock_discord_for_state.get(
        f"{DISCORD_API}/channels/t1/messages?limit=100",
        payload=[],
    )
    mock_discord_for_state.get(
        f"{DISCORD_API}/channels/t2/messages?limit=100",
        payload=[],
    )

    async with aiohttp.ClientSession() as session:
        api = BotApi(session, TOKEN)
        state = await fetch_actual_state(api, guild)

    # 4 channels total: 1 text + 1 forum + 1 active thread + 1 archived thread
    assert len(state.channels) == 4
    thread_ids = {ch.discord_id for ch in state.channels if ch.type == 11}
    assert thread_ids == {"t1", "t2"}


from tests.provisioning._applier import diff  # noqa: E402


def test_diff_empty_actual_yields_create_ops_for_every_manifest_entity() -> None:
    """Empty guild + manifest → ops to create everything."""
    path = Path(__file__).parent / "fixture-spec.json"
    manifest = load_manifest(path)
    actual = ActualState(guild_id="111", channels=(), messages_by_channel={})
    d = diff(manifest, actual)
    # Should have: 1 text channel + 10 messages + 1 thread + 1 forum channel + 1 forum post
    create_ops = [o for o in d.ops if not isinstance(o, DeleteChannelOp)]
    assert len(create_ops) == 14  # 1 + 10 + 1 + 1 + 1 = 14
    text_ops = [o for o in d.ops if isinstance(o, CreateTextChannelOp)]
    assert len(text_ops) == 1


def test_diff_matching_state_yields_zero_ops() -> None:
    path = Path(__file__).parent / "fixture-spec.json"
    manifest = load_manifest(path)
    actual = _build_fully_matching_state(manifest)
    d = diff(manifest, actual)
    assert d.ops == ()
    assert d.missing_entities == ()
    assert d.extra_marker_entities == ()
    assert d.mismatched_embeds == ()


def test_diff_extra_marker_channel_reported(tmp_path: Path) -> None:
    path = Path(__file__).parent / "fixture-spec.json"
    manifest = load_manifest(path)
    actual = _build_fully_matching_state(manifest)
    # Add an extra marker-carrying channel that isn't in the manifest:
    extra = ActualChannel(
        discord_id="999",
        name="orphan",
        type=0,
        topic="[ferry-fixture] leftover",
        parent_id=None,
    )
    actual = ActualState(
        guild_id=actual.guild_id,
        channels=actual.channels + (extra,),
        messages_by_channel=actual.messages_by_channel,
    )
    d = diff(manifest, actual)
    assert any(ch.discord_id == "999" for ch in d.extra_marker_entities)


def test_diff_extra_foreign_channel_ignored() -> None:
    path = Path(__file__).parent / "fixture-spec.json"
    manifest = load_manifest(path)
    actual = _build_fully_matching_state(manifest)
    foreign = ActualChannel(
        discord_id="888",
        name="dev-chat",
        type=0,
        topic=None,  # no marker
        parent_id=None,
    )
    actual = ActualState(
        guild_id=actual.guild_id,
        channels=actual.channels + (foreign,),
        messages_by_channel=actual.messages_by_channel,
    )
    d = diff(manifest, actual)
    assert d.extra_marker_entities == ()
    assert any(ch.discord_id == "888" for ch in d.extra_foreign_entities)
    assert d.ops == ()


def test_diff_embed_field_text_drift_detected() -> None:
    path = Path(__file__).parent / "fixture-spec.json"
    manifest = load_manifest(path)
    actual = _build_fully_matching_state(manifest)
    # Find the embed message and corrupt its first field name
    text_ch = next(c for c in actual.channels if c.name == "general")
    msgs = list(actual.messages_by_channel[text_ch.discord_id])
    embed_msg_idx = next(i for i, m in enumerate(msgs) if m.embed is not None)
    bad = msgs[embed_msg_idx]
    assert bad.embed is not None
    new_fields = (ActualEmbedField(name="DRIFT", value="Value 1", inline=True),) + bad.embed.fields[
        1:
    ]
    msgs[embed_msg_idx] = ActualMessage(
        discord_id=bad.discord_id,
        channel_discord_id=bad.channel_discord_id,
        content=bad.content,
        embed=ActualEmbed(
            title=bad.embed.title,
            description=bad.embed.description,
            color=bad.embed.color,
            fields=new_fields,
        ),
    )
    new_msg_map = dict(actual.messages_by_channel)
    new_msg_map[text_ch.discord_id] = tuple(msgs)
    actual = ActualState(
        guild_id=actual.guild_id,
        channels=actual.channels,
        messages_by_channel=new_msg_map,
    )
    d = diff(manifest, actual)
    assert len(d.mismatched_embeds) == 1
    assert (
        "DRIFT" in d.mismatched_embeds[0].reason or "field" in d.mismatched_embeds[0].reason.lower()
    )


def _build_fully_matching_state(manifest: Manifest) -> ActualState:
    """Construct an ActualState that perfectly mirrors the manifest."""
    channels: list[ActualChannel] = []
    messages_by_channel: dict[str, tuple[ActualMessage, ...]] = {}
    snowflake = 1000
    for tc in manifest.text_channels:
        ch_id = str(snowflake)
        snowflake += 1
        channels.append(
            ActualChannel(
                discord_id=ch_id,
                name=tc.name,
                type=0,
                topic=f"{manifest.marker} {tc.topic_suffix}",
                parent_id=None,
            )
        )
        msgs: list[ActualMessage] = []
        for m in tc.messages:
            embed: ActualEmbed | None = None
            if m.embed is not None:
                embed = ActualEmbed(
                    title=m.embed.title,
                    description=m.embed.description,
                    color=m.embed.color,
                    fields=tuple(
                        ActualEmbedField(name=f.name, value=f.value, inline=f.inline)
                        for f in m.embed.fields
                    ),
                )
            msg_id = str(snowflake)
            snowflake += 1
            msgs.append(
                ActualMessage(
                    discord_id=msg_id,
                    channel_discord_id=ch_id,
                    content=f"{m.content} [ferry:{m.id}]",
                    embed=embed,
                )
            )
        messages_by_channel[ch_id] = tuple(msgs)
    for t in manifest.threads:
        parent_ch = next(c for c in channels if c.name == _channel_name_for_thread(manifest, t))
        thread_id = str(snowflake)
        snowflake += 1
        channels.append(
            ActualChannel(
                discord_id=thread_id,
                name=t.name,
                type=11,
                topic=None,
                parent_id=parent_ch.discord_id,
            )
        )
        first_id = str(snowflake)
        snowflake += 1
        messages_by_channel[thread_id] = (
            ActualMessage(
                discord_id=first_id,
                channel_discord_id=thread_id,
                content=f"{t.first_message_content} [ferry:{t.id}]",
                embed=None,
            ),
        )
    for fc in manifest.forum_channels:
        forum_id = str(snowflake)
        snowflake += 1
        channels.append(
            ActualChannel(
                discord_id=forum_id,
                name=fc.name,
                type=15,
                topic=f"{manifest.marker} {fc.topic_suffix}",
                parent_id=None,
            )
        )
        for post in fc.posts:
            post_id = str(snowflake)
            snowflake += 1
            channels.append(
                ActualChannel(
                    discord_id=post_id,
                    name=post.name,
                    type=11,
                    topic=None,
                    parent_id=forum_id,
                )
            )
            first_post_msg_id = str(snowflake)
            snowflake += 1
            messages_by_channel[post_id] = (
                ActualMessage(
                    discord_id=first_post_msg_id,
                    channel_discord_id=post_id,
                    content=f"{post.first_message_content} [ferry:{post.id}]",
                    embed=None,
                ),
            )
    return ActualState(
        guild_id="111",
        channels=tuple(channels),
        messages_by_channel=messages_by_channel,
    )


def _channel_name_for_thread(manifest: Manifest, thread: ManifestThread) -> str:
    return next(c.name for c in manifest.text_channels if c.id == thread.parent_channel_id)


import re as re_  # noqa: E402

from tests.provisioning._applier import reconcile_provision  # noqa: E402


async def test_reconcile_provision_on_empty_guild(
    mock_discord_for_state: aioresponses,
) -> None:
    """provision on empty guild → 1 text ch + 10 messages + 1 thread + 1 forum + 1 post."""
    guild = "111"
    path = Path(__file__).parent / "fixture-spec.json"
    manifest = load_manifest(path)
    actual_empty = ActualState(guild_id=guild, channels=(), messages_by_channel={})

    # Mock the text channel creation:
    mock_discord_for_state.post(
        f"{DISCORD_API}/guilds/{guild}/channels",
        payload={"id": "ch_text_id", "name": "general", "type": 0},
    )
    # 10 messages in text channel
    for i in range(10):
        mock_discord_for_state.post(
            f"{DISCORD_API}/channels/ch_text_id/messages",
            payload={"id": f"msg_id_{i}", "channel_id": "ch_text_id"},
        )
    # Thread creation off some message (use regex match for which message)
    mock_discord_for_state.post(
        re_.compile(r".*/channels/ch_text_id/messages/.+/threads$"),
        payload={"id": "thread_id", "name": "Cool Thread", "type": 11, "parent_id": "ch_text_id"},
    )
    # First message in the thread
    mock_discord_for_state.post(
        f"{DISCORD_API}/channels/thread_id/messages",
        payload={"id": "thread_first_msg_id", "channel_id": "thread_id"},
    )
    # Forum channel creation (separate POST to same endpoint as text channel)
    mock_discord_for_state.post(
        f"{DISCORD_API}/guilds/{guild}/channels",
        payload={"id": "forum_id", "name": "Feedback Forum", "type": 15},
    )
    # Forum post
    mock_discord_for_state.post(
        f"{DISCORD_API}/channels/forum_id/threads",
        payload={
            "id": "post_id",
            "name": "Bug Report",
            "message": {"id": "post_first_msg_id"},
        },
    )

    async with aiohttp.ClientSession() as session:
        api = BotApi(session, TOKEN)
        d = diff(manifest, actual_empty)
        result = await reconcile_provision(
            d, api, guild_id=guild, audit_reason="provision (issue #35)"
        )

    assert result.created_count >= 14  # 1 text + 10 msgs + 1 thread + 1 forum + 1 post
