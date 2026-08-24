"""Tests for GUI helper functions and pause/cancel engine integration."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from discord_ferry.config import FerryConfig
from discord_ferry.gui import (
    _SESSION_TOKEN_KEYS,
    _clear_tokens,
    _compute_summary,
    _format_eta,
    _msgs_per_hour,
)
from discord_ferry.parser.dce_parser import parse_export_directory

if TYPE_CHECKING:
    from nicegui.testing import User

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


def test_phase_progress_pipeline_phases() -> None:
    """Pipeline phases map to a fraction in (0, 1]."""
    from discord_ferry.core.engine import PHASE_ORDER
    from discord_ferry.gui import _phase_progress

    assert _phase_progress("export") == pytest.approx(1 / len(PHASE_ORDER))
    assert _phase_progress("report") == pytest.approx(1.0)


def test_phase_progress_post_pipeline_phase_is_none() -> None:
    """Terminal phases outside PHASE_ORDER must return None, not crash .index().

    Regression: the engine emits phase="validate_migration" events, and the GUI's
    progress handler called PHASE_ORDER.index(event.phase) -> ValueError.
    """
    from discord_ferry.gui import _phase_progress

    assert _phase_progress("validate_migration") is None


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


def test_gui_imports_run_rollback() -> None:
    """The GUI module must import run_rollback so the report-page button works."""
    import discord_ferry.gui as gui_mod

    assert hasattr(gui_mod, "run_rollback")


# ---------------------------------------------------------------------------
# _clear_tokens + _SESSION_TOKEN_KEYS unit tests
# ---------------------------------------------------------------------------


def test_clear_tokens_discord_only_leaves_stoat() -> None:
    storage: dict[str, Any] = {
        "token": "stoat-tok",
        "discord_token": "disc-tok",
        "stoat_url": "https://stoat.example",
    }
    _clear_tokens(storage, ("discord_token",))
    assert "discord_token" not in storage
    assert storage["token"] == "stoat-tok"
    assert storage["stoat_url"] == "https://stoat.example"


def test_clear_tokens_session_keys_clears_both() -> None:
    storage: dict[str, Any] = {
        "token": "stoat-tok",
        "discord_token": "disc-tok",
        "stoat_url": "https://stoat.example",
    }
    _clear_tokens(storage, _SESSION_TOKEN_KEYS)
    assert "token" not in storage
    assert "discord_token" not in storage
    assert storage["stoat_url"] == "https://stoat.example"


def test_clear_tokens_missing_key_is_noop() -> None:
    storage: dict[str, Any] = {}
    _clear_tokens(storage, _SESSION_TOKEN_KEYS)  # must not raise
    assert storage == {}


def test_session_token_keys_pinned() -> None:
    # Constant-pin: the security-critical key list must never silently shrink.
    # This is the load-bearing CI guard against the recurring token-leak vector.
    assert _SESSION_TOKEN_KEYS == ("token", "discord_token")


def test_stash_output_dir_writes_string_path() -> None:
    """SC-3.1/3.2 (storage half): the output dir is stashed as a string (#518)."""
    from discord_ferry.gui import _stash_output_dir

    storage: dict[str, Any] = {}
    _stash_output_dir(storage, Path("./ferry-output"))
    assert storage["output_dir"] == "ferry-output"


# ---------------------------------------------------------------------------
# Batch 8 — S1 cached-export gating
# ---------------------------------------------------------------------------


def test_should_auto_export_none_is_true() -> None:
    """SC-1: no cached export -> auto-launch (orchestrated happy path, non-regression)."""
    from discord_ferry.gui import _should_auto_export

    assert _should_auto_export(None) is True


def test_should_auto_export_with_cache_is_false() -> None:
    """SC-2: cached export present -> do NOT auto-run (buttons drive; cached JSON preserved)."""
    from discord_ferry.gui import _should_auto_export

    assert _should_auto_export({"file_count": 3, "total_size": 1_000_000}) is False


def test_store_session_tokens_writes_exactly_two_keys() -> None:
    """SC-7: _store_session_tokens writes exactly token + discord_token, nothing else."""
    from discord_ferry.gui import _store_session_tokens

    d: dict[str, Any] = {}
    _store_session_tokens(d, stoat="s", discord="dt")
    assert d == {"token": "s", "discord_token": "dt"}


def test_store_session_tokens_roundtrip_then_clear() -> None:
    """SC-8: written tokens are readable (data-level handoff); terminal clear wipes both."""
    from discord_ferry.gui import _store_session_tokens

    d: dict[str, Any] = {}
    _store_session_tokens(d, stoat="s", discord="dt")
    assert d["token"] == "s"
    assert d["discord_token"] == "dt"
    _clear_tokens(d, _SESSION_TOKEN_KEYS)
    assert "token" not in d
    assert "discord_token" not in d


def test_legacy_scrub_removes_tokens_keeps_nonsecrets() -> None:
    """SC-9: the setup-load scrub removes on-disk tokens, leaves non-secret keys."""
    user: dict[str, Any] = {
        "token": "old",
        "discord_token": "old",
        "export_dir": "/p",
        "stoat_url": "https://x",
    }
    _clear_tokens(user, _SESSION_TOKEN_KEYS)  # the setup-load scrub
    assert "token" not in user
    assert "discord_token" not in user
    assert user["export_dir"] == "/p"
    assert user["stoat_url"] == "https://x"


# ---------------------------------------------------------------------------
# Issue #99 — advanced-settings coercion helper
# ---------------------------------------------------------------------------

_EXPOSED_DEFAULTS = {
    "reaction_mode": "text",
    "min_thread_messages": 0,
    "checkpoint_interval": 50,
    "max_concurrent_channels": 3,
    "max_concurrent_requests": 5,
    "skip_avatars": False,
    "validate_after": False,
    "incremental": False,
}


def test_coerce_advanced_settings_empty_storage_yields_defaults() -> None:
    """SC-4a: untouched storage produces exact FerryConfig defaults."""
    from discord_ferry.gui import _coerce_advanced_settings

    assert _coerce_advanced_settings({}) == _EXPOSED_DEFAULTS


def test_coerce_advanced_settings_passes_valid_values() -> None:
    """SC-4b: valid stored values flow through unchanged (ui.number floats included)."""
    from discord_ferry.gui import _coerce_advanced_settings

    result = _coerce_advanced_settings(
        {
            "reaction_mode": "native",
            "min_thread_messages": 5,
            "checkpoint_interval": 100.0,  # ui.number stores floats
            "max_concurrent_channels": 6,
            "max_concurrent_requests": 12,
            "skip_avatars": True,
            "validate_after": True,
        }
    )
    assert result["reaction_mode"] == "native"
    assert result["min_thread_messages"] == 5
    assert result["checkpoint_interval"] == 100
    assert result["max_concurrent_channels"] == 6
    assert result["max_concurrent_requests"] == 12
    assert result["skip_avatars"] is True
    assert result["validate_after"] is True


def test_coerce_advanced_settings_clamps_stale_out_of_range() -> None:
    """SC-5: disk-backed storage can hold values the controls never produced."""
    from discord_ferry.gui import _coerce_advanced_settings

    result = _coerce_advanced_settings(
        {
            "max_concurrent_channels": 0,  # would deadlock Semaphore(0)
            "max_concurrent_requests": -3,
            "checkpoint_interval": 0,
            "min_thread_messages": -1,
        }
    )
    assert result["max_concurrent_channels"] == 1
    assert result["max_concurrent_requests"] == 1
    assert result["checkpoint_interval"] == 1
    assert result["min_thread_messages"] == 0


def test_coerce_advanced_settings_handles_junk() -> None:
    """SC-5b/SC-6: None (cleared ui.number), non-numeric strings, invalid mode → defaults."""
    from discord_ferry.gui import _coerce_advanced_settings

    result = _coerce_advanced_settings(
        {
            "reaction_mode": "emoji",
            "min_thread_messages": None,
            "checkpoint_interval": "fifty",
            "max_concurrent_channels": None,
            "max_concurrent_requests": "many",
        }
    )
    assert result["reaction_mode"] == "text"
    assert result["min_thread_messages"] == 0
    assert result["checkpoint_interval"] == 50
    assert result["max_concurrent_channels"] == 3
    assert result["max_concurrent_requests"] == 5


# ---------------------------------------------------------------------------
# Task 13: proxy notices on the export screen
# ---------------------------------------------------------------------------


def test_proxy_notice_lines_formats_for_the_log_panel(proxy_env, os_proxy) -> None:
    """Task 13. _run_export is a nested closure inside export_page, unreachable
    from a unit test (see gui.py:893's docstring), the same blind spot that let
    the one-click export ship dead for eleven releases. _proxy_notice_lines is
    module level so its formatting is testable even though the call site is not.
    Killing: a formatter that drops the "[notice] " prefix _run_export's push
    relies on, or that fails to surface a real notice at all."""
    from discord_ferry.gui import _proxy_notice_lines

    with os_proxy({}), proxy_env(ALL_PROXY="socks5://sock:1080"):
        lines = _proxy_notice_lines()
    assert lines == [
        "[notice] Proxy configuration Ferry cannot use: socks5://sock:1080 (all). "
        "Connected direct. SOCKS is not supported (see issue #141)."
    ]


def test_proxy_notice_lines_is_empty_on_a_clean_configuration(proxy_env, os_proxy) -> None:
    """A clean machine must produce no line, not a stray '[notice] ' row in the
    log panel."""
    from discord_ferry.gui import _proxy_notice_lines

    with os_proxy({}), proxy_env():
        assert _proxy_notice_lines() == []


# ---------------------------------------------------------------------------
# Issue #143: the validate screen's status
# ---------------------------------------------------------------------------


def test_validate_status_three_states() -> None:
    """The banner logic lived inline in validate_page, where nothing could reach
    it. Module level, following _proxy_notice_lines.

    Killing: a helper that colours every warning red, one with no clean state,
    and one whose chip text and reason are the same object, which would make a
    positional swap between them undetectable."""
    from discord_ferry.gui import _validate_status

    red = _validate_status([{"type": "rendered_markdown", "count": "2", "message": "x"}])
    assert red.colour == "red"
    assert red.reason is not None
    assert red.text != red.reason
    assert len(red.text) <= 45  # the shipped chip is 39; the longest other is 41
    assert "fix" not in red.text.lower()  # the old wording named a fix that may not exist

    amber = _validate_status([{"type": "http_attachment", "message": "x"}])
    assert amber.colour == "amber"
    assert amber.reason is None

    assert _validate_status([]).colour == "green"


def test_incremental_coerces_into_config() -> None:
    """SC-5.1 / SC-I3: the incremental checkbox flows into FerryConfig.incremental."""
    from pathlib import Path

    from discord_ferry.config import FerryConfig
    from discord_ferry.gui import _coerce_advanced_settings

    coerced = _coerce_advanced_settings({"incremental": True})
    assert coerced["incremental"] is True
    config = FerryConfig(export_dir=Path("x"), stoat_url="https://s", token="t", **coerced)
    assert config.incremental is True
    assert config.resume is False  # SC-I3: incremental, not resume


def test_incremental_defaults_off() -> None:
    """SC-5.3: incremental defaults off."""
    from discord_ferry.gui import _coerce_advanced_settings

    assert _coerce_advanced_settings({})["incremental"] is False


def test_resume_and_incremental_conflict_detected() -> None:
    """SC-5.2: the guard catches both set, before the engine's mutual-exclusion raise."""
    from discord_ferry.gui import _resume_incremental_conflict

    assert _resume_incremental_conflict({"resume": True, "incremental": True}) is True
    assert _resume_incremental_conflict({"resume": True, "incremental": False}) is False
    assert _resume_incremental_conflict({"resume": False, "incremental": True}) is False
    assert _resume_incremental_conflict({}) is False


# ---------------------------------------------------------------------------
# #518 — completion-card deep-links to Verify and Repair
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("button", "landing_title"),
    [
        ("Verify", "Verify a finished migration"),  # -> /tools/check
        ("Repair", "Verify and fix a migration"),  # -> /tools/repair
    ],
)
@pytest.mark.nicegui_main_file("tests/nicegui_app.py")
async def test_completion_card_deep_links_to_tool_page(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    button: str,
    landing_title: str,
) -> None:
    """SC-3.1/3.2: the completion card's button stashes the dir and opens the page (#518)."""
    from discord_ferry.core.events import MigrationEvent

    user_store["export_dir"] = str(tmp_path)
    user_store["stoat_url"] = "https://example.invalid"
    tab_store["token"] = "stoat-tok"

    async def fake_run_migration(config, *, on_event):  # type: ignore[no-untyped-def]
        # Emit the terminal event so _on_migration_complete reveals the card,
        # without running a real migration.
        on_event(MigrationEvent(phase="report", status="completed", message="done"))

    monkeypatch.setattr("discord_ferry.gui.run_migration", fake_run_migration)

    await user.open("/migrate")
    await user.should_see(button)

    user.find(button).click()
    # The User sim performs the navigation for real, so the target tool page renders.
    await user.should_see(landing_title)
    # The output dir was stashed for the tool page to default from.
    assert user_store["output_dir"] == "ferry-output"  # config.output_dir is ./ferry-output
