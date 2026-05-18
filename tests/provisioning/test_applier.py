"""Tests for tests/provisioning/_applier.py."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from tests.provisioning._applier import (
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

if TYPE_CHECKING:
    from pathlib import Path


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
