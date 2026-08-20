"""Tests for server blueprint export/import."""

import importlib.resources
from pathlib import Path

import pytest

from discord_ferry.blueprint import (
    BlueprintCategory,
    BlueprintChannel,
    BlueprintRole,
    ServerBlueprint,
    blueprint_from_exports,
    export_blueprint,
    import_blueprint,
)
from discord_ferry.parser.models import DCEChannel, DCEExport, DCEGuild


def _export(name: str, ch_type: int, category: str = "") -> DCEExport:
    """A minimal DCEExport carrying one channel, for the mapping test."""
    return DCEExport(
        guild=DCEGuild(id="g1", name="My Guild"),
        channel=DCEChannel(id=name, type=ch_type, name=name, category=category),
    )


def test_blueprint_from_exports_maps_channels_and_categories() -> None:
    """The shared mapping: type 4 skipped, type 2 -> Voice, empty category -> uncategorized."""
    exports = [
        _export("general", 0, category="Text Channels"),
        _export("lounge", 2, category="Voice Channels"),
        _export("rules", 0),  # no category -> uncategorized
        _export("a-category", 4, category=""),  # category-type channel, skipped
    ]

    bp = blueprint_from_exports(exports)

    assert bp.name == "My Guild"  # from the first export's guild
    assert {c.name for c in bp.categories} == {"Text Channels", "Voice Channels"}
    voice_cat = next(c for c in bp.categories if c.name == "Voice Channels")
    assert voice_cat.channels[0].type == "Voice"
    assert [c.name for c in bp.uncategorized_channels] == ["rules"]
    all_names = [ch.name for cat in bp.categories for ch in cat.channels] + [
        c.name for c in bp.uncategorized_channels
    ]
    assert "a-category" not in all_names  # the type-4 channel was skipped


def test_blueprint_from_exports_name_override() -> None:
    """An explicit name overrides the guild name."""
    bp = blueprint_from_exports([_export("general", 0)], name="Renamed")
    assert bp.name == "Renamed"


def _make_blueprint() -> ServerBlueprint:
    return ServerBlueprint(
        name="Test Server",
        description="A test server",
        roles=[
            BlueprintRole(name="Admin", colour=16711680, permissions=15, rank=2),
            BlueprintRole(name="Member"),
        ],
        categories=[
            BlueprintCategory(
                name="General",
                channels=[
                    BlueprintChannel(name="general"),
                    BlueprintChannel(name="off-topic"),
                ],
            ),
            BlueprintCategory(
                name="Voice",
                channels=[
                    BlueprintChannel(name="voice-chat", type="Voice"),
                ],
            ),
        ],
        uncategorized_channels=[
            BlueprintChannel(name="rules", nsfw=False),
        ],
    )


def test_export_import_roundtrip(tmp_path: Path) -> None:
    bp = _make_blueprint()
    path = tmp_path / "blueprint.json"
    export_blueprint(bp, path)
    loaded = import_blueprint(path)

    assert loaded.name == "Test Server"
    assert loaded.description == "A test server"
    assert len(loaded.roles) == 2
    assert loaded.roles[0].name == "Admin"
    assert loaded.roles[0].colour == 16711680
    assert loaded.roles[0].permissions == 15
    assert loaded.roles[0].rank == 2
    assert loaded.roles[1].name == "Member"
    assert len(loaded.categories) == 2
    assert loaded.categories[0].name == "General"
    assert len(loaded.categories[0].channels) == 2
    assert loaded.categories[1].channels[0].type == "Voice"
    assert len(loaded.uncategorized_channels) == 1
    assert loaded.uncategorized_channels[0].name == "rules"


def test_export_creates_parent_dirs(tmp_path: Path) -> None:
    bp = ServerBlueprint(name="Test")
    nested = tmp_path / "deep" / "dir" / "blueprint.json"
    export_blueprint(bp, nested)
    assert nested.exists()


def test_import_minimal_blueprint(tmp_path: Path) -> None:
    path = tmp_path / "minimal.json"
    path.write_text('{"name": "Minimal"}', encoding="utf-8")
    loaded = import_blueprint(path)
    assert loaded.name == "Minimal"
    assert loaded.roles == []
    assert loaded.categories == []
    assert loaded.uncategorized_channels == []


def test_export_empty_blueprint(tmp_path: Path) -> None:
    bp = ServerBlueprint(name="Empty")
    path = tmp_path / "empty.json"
    export_blueprint(bp, path)
    loaded = import_blueprint(path)
    assert loaded.name == "Empty"
    assert loaded.description == ""


def test_nsfw_channel_roundtrip(tmp_path: Path) -> None:
    bp = ServerBlueprint(
        name="NSFW Test",
        categories=[
            BlueprintCategory(
                name="Adults",
                channels=[BlueprintChannel(name="nsfw-ch", nsfw=True)],
            ),
        ],
    )
    path = tmp_path / "nsfw.json"
    export_blueprint(bp, path)
    loaded = import_blueprint(path)
    assert loaded.categories[0].channels[0].nsfw is True


def test_role_defaults(tmp_path: Path) -> None:
    bp = ServerBlueprint(
        name="Defaults",
        roles=[BlueprintRole(name="Basic")],
    )
    path = tmp_path / "defaults.json"
    export_blueprint(bp, path)
    loaded = import_blueprint(path)
    assert loaded.roles[0].colour == 0
    assert loaded.roles[0].permissions == 0
    assert loaded.roles[0].rank == 0


def test_gaming_template_parses() -> None:
    """Gaming preset template loads as a valid ServerBlueprint."""
    templates_dir = importlib.resources.files("discord_ferry.templates")
    gaming_path = templates_dir / "gaming.json"
    bp = import_blueprint(Path(str(gaming_path)))
    assert bp.name == "Gaming Server"
    assert len(bp.roles) >= 2
    assert len(bp.categories) >= 3
    # Verify at least one voice channel
    voice_channels = [ch for cat in bp.categories for ch in cat.channels if ch.type == "Voice"]
    assert len(voice_channels) >= 1


def test_community_template_parses() -> None:
    """Community preset template loads as a valid ServerBlueprint."""
    templates_dir = importlib.resources.files("discord_ferry.templates")
    community_path = templates_dir / "community.json"
    bp = import_blueprint(Path(str(community_path)))
    assert bp.name == "Community Server"
    assert len(bp.roles) >= 2
    assert len(bp.categories) >= 3


def test_education_template_parses() -> None:
    """Education preset template loads as a valid ServerBlueprint."""
    templates_dir = importlib.resources.files("discord_ferry.templates")
    education_path = templates_dir / "education.json"
    bp = import_blueprint(Path(str(education_path)))
    assert bp.name == "Education Server"
    assert len(bp.roles) >= 2
    assert any("course" in cat.name.lower() for cat in bp.categories)


def test_a_failed_export_leaves_the_previous_blueprint_importable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #175, the case that made this worth fixing.

    ``export_blueprint`` and ``import_blueprint`` are a matched pair, so a
    truncated file is read back by a later run. Before the atomic write, the
    direct ``write_text`` truncated the target as soon as it opened it, which
    destroyed a good blueprint the moment a second write failed partway through.
    A user who then lost the source server had no way to tell the file was
    incomplete until the import failed or built a partial server.
    """
    target = tmp_path / "server.json"
    export_blueprint(_make_blueprint(), target)
    real_write_text = Path.write_text

    def half_then_fail(self: Path, data: str, *args: object, **kwargs: object) -> None:
        # Half the content, then fail. A failure that writes nothing leaves the
        # target intact even without the temp file, so it would not detect the bug.
        real_write_text(self, data[: len(data) // 2], encoding="utf-8")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_text", half_then_fail)
    with pytest.raises(OSError):
        export_blueprint(ServerBlueprint(name="Replacement"), target)
    monkeypatch.undo()

    recovered = import_blueprint(target)
    assert recovered.name == "Test Server"
    assert [r.name for r in recovered.roles] == ["Admin", "Member"]
