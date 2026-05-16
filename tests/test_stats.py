"""Tests for state-only stats summarizer."""

from __future__ import annotations

from discord_ferry.stats import FidelityBlock, RollbackBlock, StateSummary


def test_fidelity_block_optional_subscores_default_to_none() -> None:
    fb = FidelityBlock(
        overall=97.3, messages=98.0, attachments=95.5,
        embeds=None, replies=None, reactions=None,
    )
    assert fb.overall == 97.3
    assert fb.embeds is None
    assert fb.replies is None
    assert fb.reactions is None


def test_rollback_block_all_int_fields() -> None:
    rb = RollbackBlock(
        channels_deleted=10, roles_deleted=3, emoji_deleted=5,
        categories_cleaned=True, untracked_channels_deleted=2,
        failure_count=0, started_at="2026-05-16T10:00:00",
        completed_at="2026-05-16T10:05:00",
    )
    assert rb.channels_deleted == 10
    assert rb.categories_cleaned is True


def test_state_summary_default_construction_is_explicit() -> None:
    # StateSummary has no defaults; every field must be provided.
    # This test exists to lock that contract in place.
    fb = FidelityBlock(
        overall=100.0, messages=100.0, attachments=100.0,
        embeds=None, replies=None, reactions=None,
    )
    summary = StateSummary(
        channels=0, roles=0, categories=0, emojis=0, messages=0,
        attachments_uploaded=0, attachments_skipped=0,
        pins_applied=0, reactions_applied=0,
        replies_linked=0, replies_total=0,
        embeds_total=0, embeds_dropped=0,
        failed_messages=0, prior_messages_total=0,
        error_count=0, warning_count=0,
        last_error=None, last_warning=None,
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
