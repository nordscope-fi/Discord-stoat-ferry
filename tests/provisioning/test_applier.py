"""Tests for tests/provisioning/_applier.py."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

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
    load_manifest,
)
from tests.provisioning._bot_api import ProvisioningError


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
