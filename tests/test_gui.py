"""Tests for GUI helper functions and pause/cancel engine integration."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from discord_ferry.config import FerryConfig
from discord_ferry.gui import _compute_summary, _format_eta, _msgs_per_hour
from discord_ferry.parser.dce_parser import parse_export_directory

if TYPE_CHECKING:
    from discord_ferry.core.engine import PhaseFunction
    from discord_ferry.core.events import MigrationEvent
    from discord_ferry.state import MigrationState

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


def test_format_eta_zero_messages() -> None:
    assert _format_eta(0, 1.0) == "~0m"


def test_format_eta_small() -> None:
    assert _format_eta(300, 1.0) == "~5m"


def test_format_eta_large() -> None:
    result = _format_eta(12483, 1.0)
    assert result.startswith("~3h")


def test_format_eta_with_rate() -> None:
    # 1000 messages at 2.0s/msg = 2000s = ~33m
    result = _format_eta(1000, 2.0)
    assert "33m" in result


def test_msgs_per_hour_default() -> None:
    assert _msgs_per_hour(1.0) == 3600


def test_msgs_per_hour_fast() -> None:
    assert _msgs_per_hour(0.5) == 7200


def test_msgs_per_hour_zero() -> None:
    assert _msgs_per_hour(0) == 0


def test_step_labels_include_export() -> None:
    """Step labels include the Export step."""
    from discord_ferry.gui import _STEP_LABELS

    assert "Export" in _STEP_LABELS


def test_phase_labels_include_export() -> None:
    """Phase labels include the export phase."""
    from discord_ferry.gui import _PHASE_LABELS

    assert "export" in _PHASE_LABELS


def test_status_colour_confirm() -> None:
    """_STATUS_COLOUR includes a colour for the confirm status."""
    from discord_ferry.gui import _STATUS_COLOUR

    assert _STATUS_COLOUR["confirm"] == "amber"


def test_phase_labels_complete() -> None:
    """_PHASE_LABELS has entries for all phases in PHASE_ORDER."""
    from discord_ferry.core.engine import PHASE_ORDER
    from discord_ferry.gui import _PHASE_LABELS

    for phase in PHASE_ORDER:
        assert phase in _PHASE_LABELS, f"Missing label for phase: {phase}"


def test_compute_summary_with_fixtures() -> None:
    exports = parse_export_directory(FIXTURES_DIR)
    summary = _compute_summary(exports)
    assert summary["channels"] == len(exports)
    assert summary["messages"] > 0
    assert isinstance(summary["categories"], int)
    assert isinstance(summary["roles"], int)
    assert isinstance(summary["threads"], int)


# ---------------------------------------------------------------------------
# Pause/cancel engine integration tests
# ---------------------------------------------------------------------------


async def _noop_phase(
    config: FerryConfig,
    state: MigrationState,
    exports: list[object],
    on_event: object,
) -> None:
    pass


async def _slow_phase(
    config: FerryConfig,
    state: MigrationState,
    exports: list[object],
    on_event: object,
) -> None:
    """A phase that takes a little time, giving cancel a window."""
    await asyncio.sleep(0.05)


def test_detect_cached_exports_with_files(tmp_path: Path) -> None:
    """_detect_cached_exports returns summary when JSON files exist."""
    from discord_ferry.gui import _detect_cached_exports

    (tmp_path / "guild - general [123].json").write_text('{"messageCount": 50}')
    (tmp_path / "guild - memes [456].json").write_text('{"messageCount": 100}')

    result = _detect_cached_exports(tmp_path)
    assert result is not None
    assert result["file_count"] == 2
    assert result["total_size"] > 0


def test_detect_cached_exports_empty_dir(tmp_path: Path) -> None:
    """_detect_cached_exports returns None when no JSON files exist."""
    from discord_ferry.gui import _detect_cached_exports

    result = _detect_cached_exports(tmp_path)
    assert result is None


@pytest.mark.asyncio
async def test_cancel_event_stops_migration() -> None:
    """When cancel_event is set, the engine should return early (not raise)."""
    from discord_ferry.core.engine import run_migration

    cancel = asyncio.Event()
    cancel.set()  # pre-cancelled

    config = FerryConfig(
        export_dir=FIXTURES_DIR,
        stoat_url="http://localhost",
        token="test",
        cancel_event=cancel,
        skip_export=True,
    )

    events: list[MigrationEvent] = []
    noop: PhaseFunction = _noop_phase  # type: ignore[assignment]
    overrides = {
        "connect": noop,
        "server": noop,
        "roles": noop,
        "categories": noop,
        "channels": noop,
        "emoji": noop,
        "messages": noop,
        "reactions": noop,
        "pins": noop,
    }

    state = await run_migration(config, on_event=events.append, phase_overrides=overrides)

    # Should return without error (cancelled gracefully)
    assert state is not None
    # At least one phase should have been skipped due to cancel
    cancelled_events = [e for e in events if "cancelled" in e.message.lower()]
    assert len(cancelled_events) > 0


@pytest.mark.asyncio
async def test_cancel_saves_state(tmp_path: Path) -> None:
    """Cancel during a phase should save state before returning."""
    from discord_ferry.core.engine import run_migration

    cancel = asyncio.Event()

    async def _cancel_during_phase(
        config: FerryConfig,
        state: MigrationState,
        exports: list[object],
        on_event: object,
    ) -> None:
        cancel.set()  # signal cancel during this phase
        await asyncio.sleep(0.01)
        raise asyncio.CancelledError("test cancel")

    config = FerryConfig(
        export_dir=FIXTURES_DIR,
        stoat_url="http://localhost",
        token="test",
        cancel_event=cancel,
        output_dir=tmp_path,
        skip_export=True,
    )

    noop: PhaseFunction = _noop_phase  # type: ignore[assignment]
    overrides = {
        "connect": _cancel_during_phase,  # type: ignore[dict-item]
        "server": noop,
        "roles": noop,
        "categories": noop,
        "channels": noop,
        "emoji": noop,
        "messages": noop,
        "reactions": noop,
        "pins": noop,
    }

    state = await run_migration(config, on_event=lambda e: None, phase_overrides=overrides)

    # State file should have been saved
    assert (tmp_path / "state.json").exists()
    assert state is not None


@pytest.mark.asyncio
async def test_pause_event_blocks_message_rate_limit() -> None:
    """Verify _rate_limit_with_pause waits when pause_event is cleared."""
    from discord_ferry.migrator.messages import _rate_limit_with_pause

    pause = asyncio.Event()
    pause.clear()  # paused

    config = FerryConfig(
        export_dir=FIXTURES_DIR,
        stoat_url="http://localhost",
        token="test",
        message_rate_limit=0.01,
        pause_event=pause,
    )

    # Start the rate limit in a task — it should block on pause
    task = asyncio.create_task(_rate_limit_with_pause(config))
    await asyncio.sleep(0.05)
    assert not task.done(), "Task should be blocked waiting for pause_event"

    # Unpause — task should complete
    pause.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()


# ---------------------------------------------------------------------------
# Rollback button + dialog (issue #10, SC-31 regression guard)
# ---------------------------------------------------------------------------


def test_gui_handles_confirm_rollback_event() -> None:
    """SC-31 regression guard: gui.py's match block dispatches confirm_rollback.

    The migrate_page() closure builds an on_event handler with a match block
    over event.status. Adding a "confirm_rollback" status without a matching
    case arm makes the GUI silently ignore the confirmation event — the
    engine waits forever on pause_event. This test reads the source to
    assert the arm + dispatch function exist.
    """
    import inspect

    import discord_ferry.gui as gui_mod

    source = inspect.getsource(gui_mod)
    # The match arm must exist alongside the other case clauses.
    assert 'case "confirm_rollback":' in source, (
        'GUI match block must include a `case "confirm_rollback":` arm — '
        "without it, run_rollback hangs forever on pause_event"
    )
    # The dispatch target must also exist and reference the suspect channels.
    assert "_show_rollback_dialog" in source
    assert "untracked_ferry_suspect" in source


def test_gui_imports_run_rollback() -> None:
    """The GUI module must import run_rollback so the report-page button works."""
    import discord_ferry.gui as gui_mod

    assert hasattr(gui_mod, "run_rollback")
