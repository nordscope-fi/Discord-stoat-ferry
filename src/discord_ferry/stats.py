"""State-only migration stats summarizer.

This module exists to give ``ferry stats <output-dir>`` everything it needs
without re-parsing DCE exports or reconstructing FerryConfig. Contract:
``summarize_state`` takes a ``MigrationState`` and returns a typed
``StateSummary`` — no I/O, no external dependencies beyond reporter's
``compute_fidelity_score`` and ``calculate_duration``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from discord_ferry.reporter import calculate_duration, compute_fidelity_score

if TYPE_CHECKING:
    from discord_ferry.state import MigrationState

DurationState = Literal["complete", "in_progress", "unknown"]


@dataclass
class FidelityBlock:
    """Fidelity sub-scores. ``embeds``/``replies``/``reactions`` are ``None``
    when the corresponding denominator was zero — distinct from a real 0% or
    a hand-waving 100% sentinel."""

    overall: float
    messages: float
    attachments: float
    embeds: float | None
    replies: float | None
    reactions: float | None


@dataclass
class RollbackBlock:
    """Rollback counters mirrored from ``MigrationState.rollback_progress``."""

    channels_deleted: int
    roles_deleted: int
    emoji_deleted: int
    categories_cleaned: bool
    untracked_channels_deleted: int
    failure_count: int
    started_at: str
    completed_at: str


@dataclass
class StateSummary:
    """Everything ``ferry stats`` needs to render. Field-for-field renderable."""

    channels: int
    roles: int
    categories: int
    emojis: int
    messages: int

    attachments_uploaded: int
    attachments_skipped: int
    pins_applied: int
    reactions_applied: int
    replies_linked: int
    replies_total: int
    embeds_total: int
    embeds_dropped: int
    failed_messages: int
    prior_messages_total: int

    error_count: int
    warning_count: int
    last_error: str | None
    last_warning: str | None

    fidelity: FidelityBlock
    rollback: RollbackBlock | None
    channel_breakdown: dict[str, int]
    is_dry_run: bool
    stoat_server_id: str
    duration_seconds: float | None
    duration_state: DurationState
    current_phase: str


def summarize_state(state: MigrationState) -> StateSummary:
    """Build a state-only summary suitable for ``ferry stats`` rendering.

    Pure function. No I/O. No external dependencies beyond
    ``compute_fidelity_score`` and ``calculate_duration``.

    Reactions: the ``compute_fidelity_score`` function takes
    ``reactions_total`` but ``MigrationState`` does not carry it. We derive
    it: ``reactions_total = reactions_applied + len(pending_reactions)``.
    In completed migrations ``pending_reactions`` is empty, so the derived
    value equals ``reactions_applied`` and the sub-score becomes 100%. In
    partial migrations the derived value reflects "attempted so far",
    giving a meaningful denominator.

    Note: ``reporter.generate_report`` passes ``len(pending_reactions)``
    alone — a latent overestimate in partial state. The two surfaces
    diverge intentionally; aligning ``reporter.py`` is tracked separately.
    """
    reactions_total = state.reactions_applied + len(state.pending_reactions)

    score_dict = compute_fidelity_score(
        total_messages=state.prior_messages_total,
        failed_count=len(state.failed_messages),
        attachments_uploaded=state.attachments_uploaded,
        attachments_skipped=state.attachments_skipped,
        embeds_total=state.embeds_total,
        embeds_dropped=state.embeds_dropped,
        replies_linked=state.replies_linked,
        replies_total=state.replies_total,
        reactions_applied=state.reactions_applied,
        reactions_total=reactions_total,
    )

    fidelity = FidelityBlock(
        overall=score_dict["overall"],
        messages=score_dict["messages"],
        attachments=score_dict["attachments"],
        embeds=score_dict["embeds"] if state.embeds_total else None,
        replies=score_dict["replies"] if state.replies_total else None,
        reactions=score_dict["reactions"] if reactions_total else None,
    )

    if state.rollback_progress is not None:
        rp = state.rollback_progress
        rollback: RollbackBlock | None = RollbackBlock(
            channels_deleted=rp.channels_deleted,
            roles_deleted=rp.roles_deleted,
            emoji_deleted=rp.emoji_deleted,
            categories_cleaned=rp.categories_cleaned,
            untracked_channels_deleted=rp.untracked_channels_deleted,
            failure_count=len(rp.failures),
            started_at=rp.started_at,
            completed_at=rp.completed_at,
        )
    else:
        rollback = None

    last_error = state.errors[-1].get("message") if state.errors else None
    last_warning = state.warnings[-1].get("message") if state.warnings else None

    if state.started_at and state.completed_at:
        duration_state: DurationState = "complete"
        # Collapse calculate_duration's 0.0 parse-error sentinel to None so
        # callers don't see "complete migration, zero seconds" for malformed
        # timestamps in state.json.
        duration_seconds: float | None = (
            calculate_duration(state.started_at, state.completed_at) or None
        )
    elif state.started_at:
        duration_state = "in_progress"
        duration_seconds = None
    else:
        duration_state = "unknown"
        duration_seconds = None

    return StateSummary(
        channels=len(state.channel_map),
        roles=len(state.role_map),
        categories=len(state.category_map),
        emojis=len(state.emoji_map),
        messages=len(state.message_map),
        attachments_uploaded=state.attachments_uploaded,
        attachments_skipped=state.attachments_skipped,
        pins_applied=state.pins_applied,
        reactions_applied=state.reactions_applied,
        replies_linked=state.replies_linked,
        replies_total=state.replies_total,
        embeds_total=state.embeds_total,
        embeds_dropped=state.embeds_dropped,
        failed_messages=len(state.failed_messages),
        prior_messages_total=state.prior_messages_total,
        error_count=len(state.errors),
        warning_count=len(state.warnings),
        last_error=last_error,
        last_warning=last_warning,
        fidelity=fidelity,
        rollback=rollback,
        channel_breakdown=dict(state.channel_message_counts),
        is_dry_run=state.is_dry_run,
        stoat_server_id=state.stoat_server_id or "unknown",
        duration_seconds=duration_seconds,
        duration_state=duration_state,
        current_phase=state.current_phase,
    )
