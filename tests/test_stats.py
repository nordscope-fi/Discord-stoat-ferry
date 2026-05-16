"""Tests for state-only stats summarizer."""

from __future__ import annotations

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
        server_name="unknown",
        duration_seconds=None,
        duration_state="unknown",
        current_phase="",
    )
    assert summary.server_name == "unknown"
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
