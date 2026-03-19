"""Tests for migration report generator."""

import json
from pathlib import Path

from discord_ferry.config import FerryConfig
from discord_ferry.discord.metadata import (
    DiscordMetadata,
    PermissionPair,
    save_discord_metadata,
)
from discord_ferry.parser.models import DCEChannel, DCEExport, DCEGuild
from discord_ferry.reporter import compute_fidelity_score, generate_markdown_report, generate_report
from discord_ferry.state import FailedMessage, MigrationState


def _make_config(tmp_path: Path) -> FerryConfig:
    return FerryConfig(
        export_dir=tmp_path,
        stoat_url="https://api.test",
        token="test-token",
        output_dir=tmp_path,
    )


def _make_export(
    guild_id: str = "111",
    guild_name: str = "Test Guild",
    channel_id: str = "222",
    channel_name: str = "general",
    message_count: int = 0,
    is_thread: bool = False,
) -> DCEExport:
    guild = DCEGuild(id=guild_id, name=guild_name)
    channel = DCEChannel(id=channel_id, type=0, name=channel_name)
    return DCEExport(
        guild=guild,
        channel=channel,
        messages=[],
        message_count=message_count,
        is_thread=is_thread,
    )


def test_generate_report_structure(tmp_path: Path) -> None:
    """Report contains all required top-level keys."""
    config = _make_config(tmp_path)
    state = MigrationState()
    exports = [_make_export()]

    report = generate_report(config, state, exports)

    assert "started_at" in report
    assert "completed_at" in report
    assert "duration_seconds" in report
    assert "source_guild" in report
    assert "target_server_id" in report
    assert "summary" in report
    assert "warnings" in report
    assert "errors" in report
    assert "maps" in report


def test_generate_report_summary_keys(tmp_path: Path) -> None:
    """Summary dict contains all required keys."""
    config = _make_config(tmp_path)
    state = MigrationState()
    exports = [_make_export()]

    report = generate_report(config, state, exports)
    summary = report["summary"]

    assert isinstance(summary, dict)
    assert "channels_created" in summary
    assert "roles_created" in summary
    assert "categories_created" in summary
    assert "messages_imported" in summary
    assert "messages_skipped" in summary
    assert "attachments_uploaded" in summary
    assert "attachments_skipped" in summary
    assert "emoji_created" in summary
    assert "reactions_added" in summary
    assert "pins_restored" in summary
    assert "threads_flattened" in summary
    assert "errors" in summary
    assert "warnings" in summary


def test_generate_report_counts(tmp_path: Path) -> None:
    """Summary counts match state data."""
    config = _make_config(tmp_path)
    state = MigrationState(
        channel_map={"d1": "s1", "d2": "s2"},
        role_map={"r1": "sr1"},
        category_map={"c1": "sc1", "c2": "sc2", "c3": "sc3"},
        message_map={"m1": "sm1", "m2": "sm2", "m3": "sm3"},
        upload_cache={"file1": "autumn1"},
        attachments_uploaded=1,
        emoji_map={"e1": "se1"},
        pending_pins=[("ch1", "msg1"), ("ch2", "msg2")],
        reactions_applied=5,
        pins_applied=2,
        errors=[{"phase": "messages", "context": "x", "message": "err"}],
        warnings=[{"phase": "structure", "context": "y", "message": "warn"}],
    )
    # total messages = 10, imported = 3, so skipped = 7
    exports = [_make_export(message_count=10)]

    report = generate_report(config, state, exports)
    summary = report["summary"]

    assert summary["channels_created"] == 2
    assert summary["roles_created"] == 1
    assert summary["categories_created"] == 3
    assert summary["messages_imported"] == 3
    assert summary["messages_skipped"] == 7
    assert summary["attachments_uploaded"] == 1
    assert summary["emoji_created"] == 1
    assert summary["reactions_added"] == 5
    assert summary["pins_restored"] == 2
    assert summary["errors"] == 1
    assert summary["warnings"] == 1


def test_generate_report_writes_file(tmp_path: Path) -> None:
    """Report file is written to output_dir."""
    config = _make_config(tmp_path)
    state = MigrationState()
    exports = [_make_export()]

    report = generate_report(config, state, exports)

    report_path = tmp_path / "migration_report.json"
    assert report_path.exists()

    with report_path.open(encoding="utf-8") as f:
        on_disk = json.load(f)

    assert on_disk["started_at"] == report["started_at"]
    assert on_disk["completed_at"] == report["completed_at"]
    assert on_disk["summary"] == report["summary"]


def test_generate_report_duration(tmp_path: Path) -> None:
    """Duration calculated from started_at and completed_at."""
    config = _make_config(tmp_path)
    state = MigrationState(
        started_at="2024-01-01T10:00:00",
        completed_at="2024-01-01T10:01:00",
    )
    exports = [_make_export()]

    report = generate_report(config, state, exports)

    assert report["duration_seconds"] == 60


def test_generate_report_duration_zero_when_missing(tmp_path: Path) -> None:
    """Duration is 0 when started_at or completed_at is empty."""
    config = _make_config(tmp_path)
    state = MigrationState(started_at="", completed_at="")
    exports = [_make_export()]

    report = generate_report(config, state, exports)

    assert report["duration_seconds"] == 0


def test_generate_report_duration_zero_when_only_started(tmp_path: Path) -> None:
    """Duration is 0 when only started_at is set."""
    config = _make_config(tmp_path)
    state = MigrationState(started_at="2024-01-01T10:00:00", completed_at="")
    exports = [_make_export()]

    report = generate_report(config, state, exports)

    assert report["duration_seconds"] == 0


def test_generate_report_empty_exports(tmp_path: Path) -> None:
    """Report handles empty exports list gracefully."""
    config = _make_config(tmp_path)
    state = MigrationState()
    exports: list[DCEExport] = []

    report = generate_report(config, state, exports)

    assert report["source_guild"] == {"id": "", "name": ""}


def test_generate_report_source_guild_from_first_export(tmp_path: Path) -> None:
    """source_guild reflects the first export's guild metadata."""
    config = _make_config(tmp_path)
    state = MigrationState()
    exports = [
        _make_export(guild_id="999", guild_name="My Server"),
        _make_export(guild_id="888", guild_name="Other Server"),
    ]

    report = generate_report(config, state, exports)

    assert report["source_guild"] == {"id": "999", "name": "My Server"}


def test_generate_report_threads_counted(tmp_path: Path) -> None:
    """threads_flattened counts exports with is_thread=True."""
    config = _make_config(tmp_path)
    state = MigrationState()
    exports = [
        _make_export(channel_id="1", is_thread=False),
        _make_export(channel_id="2", is_thread=True),
        _make_export(channel_id="3", is_thread=False),
    ]

    report = generate_report(config, state, exports)

    assert report["summary"]["threads_flattened"] == 1


def test_generate_report_messages_skipped_multiple_exports(tmp_path: Path) -> None:
    """messages_skipped sums message_count across all exports."""
    config = _make_config(tmp_path)
    state = MigrationState(
        message_map={"m1": "sm1", "m2": "sm2"},
    )
    exports = [
        _make_export(channel_id="1", message_count=5),
        _make_export(channel_id="2", message_count=8),
    ]

    report = generate_report(config, state, exports)

    # total = 13, imported = 2, skipped = 11
    assert report["summary"]["messages_skipped"] == 11


def test_generate_report_maps_structure(tmp_path: Path) -> None:
    """maps dict contains channels, roles, and emoji keys."""
    config = _make_config(tmp_path)
    state = MigrationState(
        channel_map={"d1": "s1"},
        role_map={"r1": "sr1"},
        emoji_map={"e1": "se1"},
    )
    exports = [_make_export()]

    report = generate_report(config, state, exports)
    maps = report["maps"]

    assert isinstance(maps, dict)
    assert maps["channels"] == {"d1": "s1"}
    assert maps["roles"] == {"r1": "sr1"}
    assert maps["emoji"] == {"e1": "se1"}


def test_generate_report_target_server_id(tmp_path: Path) -> None:
    """target_server_id comes from state.stoat_server_id."""
    config = _make_config(tmp_path)
    state = MigrationState(stoat_server_id="stoat-abc-123")
    exports = [_make_export()]

    report = generate_report(config, state, exports)

    assert report["target_server_id"] == "stoat-abc-123"


def test_generate_report_creates_output_dir(tmp_path: Path) -> None:
    """output_dir is created if it does not exist."""
    nested = tmp_path / "nested" / "output"
    config = FerryConfig(
        export_dir=tmp_path,
        stoat_url="https://api.test",
        token="test-token",
        output_dir=nested,
    )
    state = MigrationState()
    exports = [_make_export()]

    generate_report(config, state, exports)

    assert (nested / "migration_report.json").exists()


def test_generate_report_reactions_added(tmp_path: Path) -> None:
    """reactions_added equals state.reactions_applied counter."""
    config = _make_config(tmp_path)
    state = MigrationState(reactions_applied=3)
    exports = [_make_export()]

    report = generate_report(config, state, exports)

    assert report["summary"]["reactions_added"] == 3


def test_generate_report_attachments_uploaded_uses_counter(tmp_path: Path) -> None:
    """attachments_uploaded uses state.attachments_uploaded counter, not upload_cache length."""
    config = _make_config(tmp_path)
    state = MigrationState(
        attachments_uploaded=10,
        upload_cache={"file1": "autumn1", "file2": "autumn2"},
    )
    exports = [_make_export()]

    report = generate_report(config, state, exports)

    # Should use the counter, not len(upload_cache)
    assert report["summary"]["attachments_uploaded"] == 10


def test_generate_report_has_nonzero_duration(tmp_path: Path) -> None:
    """Report has positive duration when completed_at is set before generate_report."""
    config = _make_config(tmp_path)
    state = MigrationState(
        started_at="2024-01-01T10:00:00+00:00",
        completed_at="2024-01-01T10:05:00+00:00",
    )
    exports = [_make_export()]

    report = generate_report(config, state, exports)

    assert report["completed_at"] != ""
    assert report["duration_seconds"] == 300


def test_checklist_with_permissions(tmp_path: Path) -> None:
    """Checklist includes permission review items when discord_metadata.json is present."""
    config = _make_config(tmp_path)
    state = MigrationState()
    exports = [_make_export()]

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="2024-01-01T00:00:00",
        server_default_permissions=0,
        role_permissions={"r1": PermissionPair(allow=0, deny=0)},
        channel_metadata={},
    )
    save_discord_metadata(meta, tmp_path)

    report = generate_report(config, state, exports)

    checklist = report["checklist"]
    assert isinstance(checklist, list)
    tasks = [item["task"] for item in checklist]  # type: ignore[index]
    assert any("Review migrated role permissions" in t for t in tasks)
    assert any("Verify channel permission overrides" in t for t in tasks)
    assert not any("Set up role permissions manually" in t for t in tasks)


def test_checklist_without_permissions(tmp_path: Path) -> None:
    """Checklist includes manual permission setup item when discord_metadata.json is absent."""
    config = _make_config(tmp_path)
    state = MigrationState()
    exports = [_make_export()]

    report = generate_report(config, state, exports)

    checklist = report["checklist"]
    assert isinstance(checklist, list)
    tasks = [item["task"] for item in checklist]  # type: ignore[index]
    assert any("Set up role permissions manually" in t for t in tasks)
    assert not any("Review migrated role permissions" in t for t in tasks)


def test_checklist_with_warnings(tmp_path: Path) -> None:
    """Checklist includes a warnings review item when state.warnings is non-empty."""
    config = _make_config(tmp_path)
    state = MigrationState(
        warnings=[
            {"phase": "messages", "context": "ch1", "message": "warn1"},
            {"phase": "messages", "context": "ch2", "message": "warn2"},
        ]
    )
    exports = [_make_export()]

    report = generate_report(config, state, exports)

    checklist = report["checklist"]
    assert isinstance(checklist, list)
    tasks = [item["task"] for item in checklist]  # type: ignore[index]
    assert any("Review 2 warning(s)" in t for t in tasks)


def test_checklist_with_emoji(tmp_path: Path) -> None:
    """Checklist includes an emoji verification item when state.emoji_map is non-empty."""
    config = _make_config(tmp_path)
    state = MigrationState(emoji_map={"e1": "se1", "e2": "se2"})
    exports = [_make_export()]

    report = generate_report(config, state, exports)

    checklist = report["checklist"]
    assert isinstance(checklist, list)
    tasks = [item["task"] for item in checklist]  # type: ignore[index]
    assert any("Verify custom emoji" in t for t in tasks)


def test_report_includes_user_override_channels(tmp_path: Path) -> None:
    """Report includes user_override_channels when warnings exist."""
    from discord_ferry.discord.metadata import ChannelMeta

    config = _make_config(tmp_path)
    state = MigrationState(
        warnings=[
            {
                "phase": "review",
                "type": "user_override_skipped",
                "message": "Channel general has 3 user-specific permission overrides",
            },
        ]
    )
    exports = [_make_export()]

    # Save metadata with user overrides so report can read it
    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={"ch1": ChannelMeta(nsfw=False)},
        user_override_channels=[
            {"channel_id": "ch1", "channel_name": "general", "override_count": 3},
        ],
    )
    save_discord_metadata(meta, tmp_path)

    report = generate_report(config, state, exports)

    checklist = report["checklist"]
    assert isinstance(checklist, list)
    tasks = [item["task"] for item in checklist]  # type: ignore[index]
    assert any("user-specific permission" in t.lower() for t in tasks)


def test_report_no_user_override_checklist_when_none(tmp_path: Path) -> None:
    """Report does not include user override checklist item when no overrides."""
    config = _make_config(tmp_path)
    state = MigrationState()
    exports = [_make_export()]

    report = generate_report(config, state, exports)

    checklist = report["checklist"]
    assert isinstance(checklist, list)
    tasks = [item["task"] for item in checklist]  # type: ignore[index]
    assert not any("user-specific permission" in t.lower() for t in tasks)


# ---------------------------------------------------------------------------
# Orphan upload tracking (S5)
# ---------------------------------------------------------------------------


def test_report_zero_orphans_no_warning(tmp_path: Path) -> None:
    """When all uploads are referenced, report shows orphaned_uploads=0, no orphaned_ids."""
    config = _make_config(tmp_path)
    state = MigrationState(
        autumn_uploads={"autumn1": "att1", "autumn2": "att2"},
        referenced_autumn_ids={"autumn1", "autumn2"},
    )
    exports = [_make_export()]

    report = generate_report(config, state, exports)

    assert report["orphaned_uploads"] == 0
    assert "orphaned_ids" not in report


def test_report_lists_orphaned_ids(tmp_path: Path) -> None:
    """When uploads are not referenced, report includes orphaned_uploads count and IDs."""
    config = _make_config(tmp_path)
    state = MigrationState(
        autumn_uploads={"autumn1": "att1", "autumn2": "att2", "autumn3": "att3"},
        referenced_autumn_ids={"autumn1"},  # only autumn1 was used
    )
    exports = [_make_export()]

    report = generate_report(config, state, exports)

    assert report["orphaned_uploads"] == 2
    assert set(report["orphaned_ids"]) == {"autumn2", "autumn3"}  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Failed message reporting (S1)
# ---------------------------------------------------------------------------


def test_report_includes_failed_message_count_and_ids(tmp_path: Path) -> None:
    """Report includes failed_messages count and failed_message_ids list."""
    config = _make_config(tmp_path)
    state = MigrationState(
        failed_messages=[
            FailedMessage(
                discord_msg_id="msg1",
                stoat_channel_id="ch1",
                error="API timeout",
            ),
            FailedMessage(
                discord_msg_id="msg2",
                stoat_channel_id="ch2",
                error="Rate limited",
                content_preview="hello",
            ),
        ],
    )
    exports = [_make_export()]

    report = generate_report(config, state, exports)

    assert report["failed_messages"] == 2
    assert set(report["failed_message_ids"]) == {"msg1", "msg2"}  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Post-migration validation results (S7)
# ---------------------------------------------------------------------------


def test_report_includes_validation_results(tmp_path: Path) -> None:
    """Report includes 'validation' key when state has validation_results."""
    config = _make_config(tmp_path)
    state = MigrationState(
        validation_results={
            "channels_expected": 5,
            "channels_found": 5,
            "roles_expected": 2,
            "roles_found": 2,
            "failed_messages": 0,
            "passed": True,
        }
    )
    exports = [_make_export()]

    report = generate_report(config, state, exports)

    assert "validation" in report
    assert report["validation"]["passed"] is True
    assert report["validation"]["channels_expected"] == 5
    assert report["validation"]["channels_found"] == 5


def test_report_no_validation_when_empty(tmp_path: Path) -> None:
    """Report does not include 'validation' key when validation_results is empty."""
    config = _make_config(tmp_path)
    state = MigrationState()  # validation_results defaults to {}
    exports = [_make_export()]

    report = generate_report(config, state, exports)

    assert "validation" not in report


# ---------------------------------------------------------------------------
# Markdown migration report (S6)
# ---------------------------------------------------------------------------


def test_markdown_report_file_created(tmp_path: Path) -> None:
    """generate_markdown_report writes migration_report.md to output_dir."""
    config = _make_config(tmp_path)
    state = MigrationState()
    exports = [_make_export()]

    generate_markdown_report(config, state, exports)

    assert (tmp_path / "migration_report.md").exists()


def test_markdown_report_contains_summary(tmp_path: Path) -> None:
    """Markdown report contains key summary table rows."""
    config = _make_config(tmp_path)
    state = MigrationState(
        channel_map={"d1": "s1", "d2": "s2"},
        message_map={"m1": "sm1"},
    )
    exports = [_make_export()]

    generate_markdown_report(config, state, exports)

    content = (tmp_path / "migration_report.md").read_text(encoding="utf-8")
    assert "Channels created" in content
    assert "Messages imported" in content
    assert "| 2 |" in content  # 2 channels
    assert "| 1 |" in content  # 1 message


def test_markdown_report_lists_failed_messages(tmp_path: Path) -> None:
    """Markdown report lists each failed message ID and error."""
    config = _make_config(tmp_path)
    state = MigrationState(
        failed_messages=[
            FailedMessage(discord_msg_id="msg_aaa", stoat_channel_id="ch1", error="Timeout"),
            FailedMessage(discord_msg_id="msg_bbb", stoat_channel_id="ch2", error="Rate limited"),
        ],
    )
    exports = [_make_export()]

    generate_markdown_report(config, state, exports)

    content = (tmp_path / "migration_report.md").read_text(encoding="utf-8")
    assert "msg_aaa" in content
    assert "msg_bbb" in content
    assert "Timeout" in content
    assert "Rate limited" in content


def test_markdown_report_empty_state(tmp_path: Path) -> None:
    """Markdown report handles empty state gracefully with 'No errors.' text."""
    config = _make_config(tmp_path)
    state = MigrationState()
    exports = [_make_export()]

    generate_markdown_report(config, state, exports)

    content = (tmp_path / "migration_report.md").read_text(encoding="utf-8")
    assert "No errors." in content
    assert "No warnings." in content


# ---------------------------------------------------------------------------
# S18: Migration fidelity scoring
# ---------------------------------------------------------------------------


def test_fidelity_perfect_score() -> None:
    """Perfect migration: all messages imported, no attachments skipped."""
    score = compute_fidelity_score(
        total_messages=100,
        failed_count=0,
        attachments_uploaded=50,
        attachments_skipped=0,
    )
    # All categories at 100% (embed/reply/reaction default to 1.0 when totals are 0).
    assert score["overall"] == 100.0
    assert score["messages"] == 100.0
    assert score["attachments"] == 100.0
    assert score["embeds"] == 100.0
    assert score["replies"] == 100.0
    assert score["reactions"] == 100.0


def test_fidelity_zero_messages() -> None:
    """Zero total messages and zero attachments: msg and att ratios are 0.0."""
    score = compute_fidelity_score(
        total_messages=0,
        failed_count=0,
        attachments_uploaded=0,
        attachments_skipped=0,
    )
    # msg_ratio = 0.0, att_ratio = 0.0; embed/reply/reaction default to 1.0.
    # overall = 0*0.40 + 0*0.25 + 1.0*0.15 + 1.0*0.10 + 1.0*0.10 = 0.35
    assert score["messages"] == 0.0
    assert score["attachments"] == 0.0
    assert score["overall"] == 35.0


def test_fidelity_partial_messages_no_attachments() -> None:
    """50% message success, no attachments: embed/reply/react default to 1.0."""
    score = compute_fidelity_score(
        total_messages=100,
        failed_count=50,
        attachments_uploaded=0,
        attachments_skipped=0,
    )
    # msg_ratio=0.5, att_ratio=0.0, embed/reply/react=1.0
    # overall = 0.5*0.40 + 0*0.25 + 1.0*0.15 + 1.0*0.10 + 1.0*0.10 = 0.55
    assert score["messages"] == 50.0
    assert score["attachments"] == 0.0
    assert score["overall"] == 55.0


def test_fidelity_partial_attachments() -> None:
    """All messages imported, 50% attachments uploaded."""
    score = compute_fidelity_score(
        total_messages=100,
        failed_count=0,
        attachments_uploaded=5,
        attachments_skipped=5,
    )
    # msg_ratio=1.0, att_ratio=0.5, embed/reply/react=1.0
    # overall = 1.0*0.40 + 0.5*0.25 + 1.0*0.15 + 1.0*0.10 + 1.0*0.10 = 0.875
    assert score["messages"] == 100.0
    assert score["attachments"] == 50.0
    assert score["overall"] == 87.5


def test_fidelity_worst_case() -> None:
    """All messages failed, no attachments uploaded: msg and att scores are zero."""
    score = compute_fidelity_score(
        total_messages=100,
        failed_count=100,
        attachments_uploaded=0,
        attachments_skipped=10,
    )
    # msg_ratio=0.0, att_ratio=0.0, embed/reply/react=1.0
    # overall = 0*0.40 + 0*0.25 + 1.0*0.15 + 1.0*0.10 + 1.0*0.10 = 0.35
    assert score["messages"] == 0.0
    assert score["attachments"] == 0.0
    assert score["overall"] == 35.0


def test_fidelity_included_in_json_report(tmp_path: Path) -> None:
    """generate_report includes 'fidelity' key with all 5 category scores."""
    config = _make_config(tmp_path)
    state = MigrationState(
        message_map={"m1": "sm1", "m2": "sm2"},  # 2 imported
        attachments_uploaded=3,
        attachments_skipped=1,
    )
    exports = [_make_export(message_count=4)]

    report = generate_report(config, state, exports)

    assert "fidelity" in report
    fidelity = report["fidelity"]
    assert isinstance(fidelity, dict)
    assert "overall" in fidelity
    assert "messages" in fidelity
    assert "attachments" in fidelity
    assert "embeds" in fidelity
    assert "replies" in fidelity
    assert "reactions" in fidelity


def test_fidelity_included_in_markdown_report(tmp_path: Path) -> None:
    """generate_markdown_report includes fidelity section."""
    config = _make_config(tmp_path)
    state = MigrationState(attachments_uploaded=10, attachments_skipped=0)
    exports = [_make_export(message_count=0)]

    generate_markdown_report(config, state, exports)

    content = (tmp_path / "migration_report.md").read_text(encoding="utf-8")
    assert "Fidelity Score" in content
    assert "Overall:" in content
    assert "Messages:" in content
    assert "Attachments:" in content


def test_fidelity_rounding() -> None:
    """Fidelity scores are rounded to one decimal place."""
    score = compute_fidelity_score(
        total_messages=3,
        failed_count=1,
        attachments_uploaded=2,
        attachments_skipped=1,
    )
    # msg_ratio = 2/3, att_ratio = 2/3, embed/reply/react default to 1.0
    # overall = (2/3)*0.40 + (2/3)*0.25 + 1.0*0.15 + 1.0*0.10 + 1.0*0.10
    #         = (2/3)*0.65 + 0.35 ≈ 0.7833 → 78.3
    assert score["overall"] == 78.3
    assert score["messages"] == round((2 / 3) * 100, 1)
