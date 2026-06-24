"""Tests for state-only stats summarizer."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from discord_ferry.cli import (
    _build_channels_table,
    _build_rollback_table,
    _build_stats_table,
    main,
)
from discord_ferry.state import MigrationState, RollbackProgress
from discord_ferry.stats import FidelityBlock, RollbackBlock, StateSummary, summarize_state


def test_fidelity_block_optional_subscores_default_to_none() -> None:
    fb = FidelityBlock(
        overall=97.3,
        messages=98.0,
        attachments=95.5,
        embeds=None,
        replies=None,
        reactions=None,
    )
    assert fb.overall == 97.3
    assert fb.embeds is None
    assert fb.replies is None
    assert fb.reactions is None


def test_rollback_block_all_int_fields() -> None:
    rb = RollbackBlock(
        channels_deleted=10,
        roles_deleted=3,
        emoji_deleted=5,
        categories_cleaned=True,
        untracked_channels_deleted=2,
        failure_count=0,
        started_at="2026-05-16T10:00:00",
        completed_at="2026-05-16T10:05:00",
    )
    assert rb.channels_deleted == 10
    assert rb.categories_cleaned is True


def test_state_summary_default_construction_is_explicit() -> None:
    # StateSummary has no defaults; every field must be provided.
    # This test exists to lock that contract in place.
    fb = FidelityBlock(
        overall=100.0,
        messages=100.0,
        attachments=100.0,
        embeds=None,
        replies=None,
        reactions=None,
    )
    summary = StateSummary(
        channels=0,
        roles=0,
        categories=0,
        emojis=0,
        messages=0,
        attachments_uploaded=0,
        attachments_skipped=0,
        pins_applied=0,
        reactions_applied=0,
        replies_linked=0,
        replies_total=0,
        embeds_total=0,
        embeds_dropped=0,
        failed_messages=0,
        prior_messages_total=0,
        error_count=0,
        warning_count=0,
        last_error=None,
        last_warning=None,
        fidelity=fb,
        rollback=None,
        channel_breakdown={},
        is_dry_run=False,
        stoat_server_id="unknown",
        duration_seconds=None,
        duration_state="unknown",
        current_phase="",
    )
    assert summary.stoat_server_id == "unknown"
    assert summary.rollback is None
    assert summary.channel_breakdown == {}


def _full_state() -> MigrationState:
    """Build a realistic MigrationState for happy-path tests."""
    state = MigrationState()
    state.role_map = {f"r{i}": f"sr{i}" for i in range(4)}
    state.channel_map = {f"c{i}": f"sc{i}" for i in range(7)}
    state.category_map = {"cat1": "scat1", "cat2": "scat2"}
    state.emoji_map = {f"e{i}": f"se{i}" for i in range(12)}
    state.message_map = {f"m{i}": f"sm{i}" for i in range(1000)}
    state.attachments_uploaded = 85
    state.attachments_skipped = 5
    state.pins_applied = 12
    state.reactions_applied = 200
    state.pending_reactions = []  # all applied → reactions_total = 200
    state.replies_linked = 48
    state.replies_total = 50
    state.embeds_total = 30
    state.embeds_dropped = 1
    state.failed_messages = []
    state.prior_messages_total = 1000
    state.source_messages_total = 1000
    state.errors = [{"phase": "MESSAGES", "message": "rate limited at message 42"}]
    state.warnings = [{"phase": "STRUCTURE", "message": "emoji slot near cap"}]
    state.channel_message_counts = {"c0": 600, "c1": 300, "c2": 100}
    state.is_dry_run = False
    state.stoat_server_id = "01HXYZSERVER"
    state.started_at = "2026-05-16T10:00:00"
    state.completed_at = "2026-05-16T10:30:00"
    state.current_phase = "REPORT"
    return state


def test_summarize_state_entity_counts() -> None:
    summary = summarize_state(_full_state())
    assert summary.channels == 7
    assert summary.roles == 4
    assert summary.categories == 2
    assert summary.emojis == 12
    assert summary.messages == 1000


def test_summarize_state_message_counters() -> None:
    summary = summarize_state(_full_state())
    assert summary.attachments_uploaded == 85
    assert summary.attachments_skipped == 5
    assert summary.pins_applied == 12
    assert summary.reactions_applied == 200
    assert summary.replies_linked == 48
    assert summary.replies_total == 50
    assert summary.embeds_total == 30
    assert summary.embeds_dropped == 1
    assert summary.failed_messages == 0
    assert summary.prior_messages_total == 1000


def test_summarize_state_error_warning_preview() -> None:
    summary = summarize_state(_full_state())
    assert summary.error_count == 1
    assert summary.warning_count == 1
    assert summary.last_error == "rate limited at message 42"
    assert summary.last_warning == "emoji slot near cap"


def test_summarize_state_fidelity_no_zero_denoms() -> None:
    summary = summarize_state(_full_state())
    # Non-zero denominators → real percentages (compute_fidelity_score's math)
    assert summary.fidelity.embeds is not None
    assert summary.fidelity.replies is not None
    assert summary.fidelity.reactions is not None
    assert summary.fidelity.overall > 0


def test_summarize_state_fidelity_zero_embeds_renders_none() -> None:
    state = _full_state()
    state.embeds_total = 0
    state.embeds_dropped = 0
    summary = summarize_state(state)
    assert summary.fidelity.embeds is None


def test_summarize_state_fidelity_zero_reactions_renders_none() -> None:
    state = _full_state()
    state.reactions_applied = 0
    state.pending_reactions = []  # derived total = 0
    summary = summarize_state(state)
    assert summary.fidelity.reactions is None


def test_summarize_state_reactions_total_derives_from_applied_plus_pending() -> None:
    state = _full_state()
    state.reactions_applied = 10
    state.pending_reactions = [{"x": 1}, {"x": 2}, {"x": 3}]
    summary = summarize_state(state)
    # Derived total = 13. ratio = 10/13 ≈ 76.9%. Should be a real float, not None.
    assert summary.fidelity.reactions is not None
    assert 76.0 < summary.fidelity.reactions < 77.5


def test_summarize_state_duration_complete() -> None:
    summary = summarize_state(_full_state())
    assert summary.duration_state == "complete"
    assert summary.duration_seconds == 1800.0  # 30 min


def test_summarize_state_duration_in_progress() -> None:
    state = _full_state()
    state.completed_at = ""
    summary = summarize_state(state)
    assert summary.duration_state == "in_progress"
    assert summary.duration_seconds is None


def test_summarize_state_duration_unknown() -> None:
    state = _full_state()
    state.started_at = ""
    state.completed_at = ""
    summary = summarize_state(state)
    assert summary.duration_state == "unknown"
    assert summary.duration_seconds is None


def test_summarize_state_duration_complete_but_malformed_returns_none() -> None:
    """Malformed ISO timestamps with both fields populated → duration_seconds is None.

    Without this guard, calculate_duration's 0.0 parse-error sentinel would
    produce "complete migration, zero seconds" — a silent misread.
    """
    state = _full_state()
    state.started_at = "not-an-iso-timestamp"
    state.completed_at = "also-bogus"
    summary = summarize_state(state)
    # We keep duration_state == "complete" because both fields are present;
    # the parse-failure surfaces via duration_seconds being None.
    assert summary.duration_state == "complete"
    assert summary.duration_seconds is None


def test_summarize_state_dry_run_flag() -> None:
    state = _full_state()
    state.is_dry_run = True
    summary = summarize_state(state)
    assert summary.is_dry_run is True


def test_summarize_state_channel_breakdown_passes_through() -> None:
    summary = summarize_state(_full_state())
    assert summary.channel_breakdown == {"c0": 600, "c1": 300, "c2": 100}


def test_summarize_state_rollback_none_when_progress_unset() -> None:
    summary = summarize_state(_full_state())
    assert summary.rollback is None


def test_summarize_state_rollback_populated_when_progress_set() -> None:
    state = _full_state()
    state.rollback_progress = RollbackProgress(
        channels_deleted=7,
        roles_deleted=4,
        emoji_deleted=12,
        categories_cleaned=True,
        untracked_channels_deleted=1,
        started_at="2026-05-16T11:00:00",
        completed_at="2026-05-16T11:02:00",
    )
    summary = summarize_state(state)
    assert summary.rollback is not None
    assert summary.rollback.channels_deleted == 7
    assert summary.rollback.failure_count == 0
    assert summary.rollback.started_at == "2026-05-16T11:00:00"


def test_summarize_state_no_errors_no_warnings_renders_none() -> None:
    state = _full_state()
    state.errors = []
    state.warnings = []
    summary = summarize_state(state)
    assert summary.error_count == 0
    assert summary.last_error is None
    assert summary.last_warning is None


def _summary_with_overrides(**overrides: object) -> StateSummary:
    base = summarize_state(_full_state())
    # Replace named fields without subclassing
    from dataclasses import replace

    return replace(base, **overrides)


def test_build_stats_table_title_includes_stoat_server_id() -> None:
    summary = summarize_state(_full_state())
    table = _build_stats_table(summary)
    assert table.title is not None
    assert "01HXYZSERVER" in table.title


def test_build_stats_table_dry_run_badge_in_title() -> None:
    summary = _summary_with_overrides(is_dry_run=True)
    table = _build_stats_table(summary)
    assert table.title is not None
    assert "[DRY-RUN]" in table.title


def test_build_stats_table_no_dry_run_badge_when_false() -> None:
    summary = summarize_state(_full_state())
    table = _build_stats_table(summary)
    assert table.title is not None
    assert "[DRY-RUN]" not in table.title


def test_build_channels_table_returns_none_when_breakdown_empty() -> None:
    summary = _summary_with_overrides(channel_breakdown={})
    assert _build_channels_table(summary) is None


def test_build_channels_table_returns_table_when_populated() -> None:
    summary = summarize_state(_full_state())
    table = _build_channels_table(summary)
    assert table is not None
    assert table.row_count >= 1


def test_build_rollback_table_returns_none_when_absent() -> None:
    summary = summarize_state(_full_state())  # _full_state has no rollback
    assert _build_rollback_table(summary) is None


def test_build_rollback_table_returns_table_when_present() -> None:
    state = _full_state()
    state.rollback_progress = RollbackProgress(
        channels_deleted=5,
        roles_deleted=2,
        emoji_deleted=8,
        categories_cleaned=True,
        untracked_channels_deleted=0,
        started_at="2026-05-16T11:00:00",
        completed_at="2026-05-16T11:01:00",
    )
    summary = summarize_state(state)
    table = _build_rollback_table(summary)
    assert table is not None
    assert table.row_count >= 1


def test_build_stats_table_truncates_long_error() -> None:
    long_msg = "x" * 200
    state = _full_state()
    state.errors = [{"phase": "MESSAGES", "message": long_msg}]
    summary = summarize_state(state)
    table = _build_stats_table(summary)
    # Render to string via Rich's Console capture
    from rich.console import Console

    console = Console(width=120, record=True)
    console.print(table)
    rendered = console.export_text()
    # The 200-char string should not appear in full; should be truncated to ~80.
    assert long_msg not in rendered
    assert "x" * 80 in rendered or "…" in rendered


def _write_state_json(tmp_path: object, state: MigrationState) -> object:
    """Persist a state to an output dir for CliRunner tests."""
    from pathlib import Path

    from discord_ferry.state import save_state

    out = Path(str(tmp_path)) / "ferry-out"  # type: ignore[arg-type]  # pytest tmp_path is LocalPath at runtime; str() narrows for Path()
    out.mkdir(parents=True, exist_ok=True)
    save_state(state, out)
    return out


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def test_stats_help_lists_subcommand(runner: CliRunner) -> None:
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "stats" in result.output


def test_stats_command_help_prints(runner: CliRunner) -> None:
    result = runner.invoke(main, ["stats", "--help"])
    assert result.exit_code == 0
    assert "OUTPUT_DIR" in result.output


def test_stats_happy_path_exits_zero(runner: CliRunner, tmp_path: object) -> None:
    out = _write_state_json(tmp_path, _full_state())
    result = runner.invoke(main, ["stats", str(out)])
    assert result.exit_code == 0
    assert "Migration Stats" in result.output
    assert "Fidelity" in result.output
    assert "01HXYZSERVER" in result.output


def test_stats_missing_dir_exits_two_no_traceback(runner: CliRunner) -> None:
    # Click's `Path(exists=True)` validation rejects missing paths with exit
    # code 2 (UsageError) before the function body runs. Asserting the
    # specific code catches regressions if Click's behaviour changes.
    result = runner.invoke(main, ["stats", "/nonexistent/path/to/nowhere"])
    assert result.exit_code == 2
    assert "Traceback" not in result.output


def test_stats_corrupt_state_exits_one_no_traceback(runner: CliRunner, tmp_path: object) -> None:
    from pathlib import Path

    out = Path(str(tmp_path)) / "ferry-out"  # type: ignore[arg-type]  # pytest tmp_path is LocalPath at runtime; str() narrows for Path()
    out.mkdir(parents=True, exist_ok=True)
    (out / "state.json").write_text("not valid json {{{", encoding="utf-8")
    result = runner.invoke(main, ["stats", str(out)])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "Error" in result.output


def test_stats_dry_run_badge_appears_in_output(runner: CliRunner, tmp_path: object) -> None:
    state = _full_state()
    state.is_dry_run = True
    out = _write_state_json(tmp_path, state)
    result = runner.invoke(main, ["stats", str(out)])
    assert result.exit_code == 0
    assert "[DRY-RUN]" in result.output


def test_stats_rollback_section_appears_when_present(runner: CliRunner, tmp_path: object) -> None:
    state = _full_state()
    state.rollback_progress = RollbackProgress(
        channels_deleted=3,
        roles_deleted=1,
        emoji_deleted=2,
        categories_cleaned=True,
        untracked_channels_deleted=0,
        started_at="2026-05-16T11:00:00",
        completed_at="2026-05-16T11:01:00",
    )
    out = _write_state_json(tmp_path, state)
    result = runner.invoke(main, ["stats", str(out)])
    assert result.exit_code == 0
    assert "Rollback" in result.output


def test_stats_channels_section_appears_when_populated(runner: CliRunner, tmp_path: object) -> None:
    out = _write_state_json(tmp_path, _full_state())
    result = runner.invoke(main, ["stats", str(out)])
    assert result.exit_code == 0
    assert "Per-Channel Messages" in result.output


def test_stats_channels_section_omitted_when_empty(runner: CliRunner, tmp_path: object) -> None:
    state = _full_state()
    state.channel_message_counts = {}
    out = _write_state_json(tmp_path, state)
    result = runner.invoke(main, ["stats", str(out)])
    assert result.exit_code == 0
    assert "Per-Channel Messages" not in result.output


def test_summarize_state_messages_fidelity_uses_source_total() -> None:
    """A fresh run (prior_messages_total=0) still reports correct Messages fidelity."""
    state = MigrationState()
    state.message_map = {f"m{i}": f"sm{i}" for i in range(100)}
    state.failed_messages = []
    state.source_messages_total = 100  # set by engine, even on a fresh run
    state.prior_messages_total = 0  # incremental-only; must NOT be the denominator
    summary = summarize_state(state)
    assert summary.fidelity.messages == 100.0


def test_summarize_state_messages_fidelity_legacy_fallback() -> None:
    """Legacy state (source_messages_total absent/0) falls back to map+failed, not 0%."""
    state = MigrationState()
    state.message_map = {f"m{i}": f"sm{i}" for i in range(50)}
    state.failed_messages = []
    state.source_messages_total = 0  # legacy
    summary = summarize_state(state)
    assert summary.fidelity.messages == 100.0  # 50/50, not 0/0
