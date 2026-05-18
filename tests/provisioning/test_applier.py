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
