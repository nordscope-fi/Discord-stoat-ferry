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

from discord_ferry.reporter import calculate_duration, compute_fidelity_score  # noqa: F401

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
    server_name: str
    duration_seconds: float | None
    duration_state: DurationState
    current_phase: str


def summarize_state(state: MigrationState) -> StateSummary:
    """Stub. Real implementation arrives in Task 3."""
    raise NotImplementedError
