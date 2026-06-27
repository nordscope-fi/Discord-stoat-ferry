"""Tests for CLI entry point."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from discord_ferry.cli import main
from discord_ferry.errors import MigrationError
from discord_ferry.state import MigrationState

if TYPE_CHECKING:
    from discord_ferry.config import FerryConfig

FIXTURES_DIR = str(Path(__file__).parent / "fixtures")


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------


def test_cli_help(runner: CliRunner) -> None:
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "Migrate a Discord server" in result.output


def test_migrate_help(runner: CliRunner) -> None:
    result = runner.invoke(main, ["migrate", "--help"])
    assert result.exit_code == 0
    assert "--stoat-url" in result.output


def test_validate_help(runner: CliRunner) -> None:
    result = runner.invoke(main, ["validate", "--help"])
    assert result.exit_code == 0
    assert "EXPORT_DIR" in result.output


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------


def test_validate_basic(runner: CliRunner) -> None:
    # Fixtures include markdown_rendered.json which triggers a critical warning,
    # so exit code is 1.  We still verify the table and warnings render.
    result = runner.invoke(main, ["validate", FIXTURES_DIR])
    assert result.exit_code == 1
    assert "Export Summary" in result.output
    assert "Messages" in result.output
    assert "Critical warnings found" in result.output


def test_validate_empty_dir(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(main, ["validate", str(tmp_path)])
    assert result.exit_code == 1
    assert "No valid DCE JSON files" in result.output


# ---------------------------------------------------------------------------
# Migrate — argument validation
# ---------------------------------------------------------------------------


def test_migrate_missing_url(runner: CliRunner) -> None:
    result = runner.invoke(
        main,
        ["migrate", "--export-dir", FIXTURES_DIR, "--token", "test-token"],
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    assert "--stoat-url is required" in result.output


def test_migrate_missing_token(runner: CliRunner) -> None:
    result = runner.invoke(
        main,
        ["migrate", "--export-dir", FIXTURES_DIR, "--stoat-url", "http://localhost"],
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    assert "--token is required" in result.output


# ---------------------------------------------------------------------------
# Migrate — engine integration
# ---------------------------------------------------------------------------


def _make_mock_engine() -> AsyncMock:
    """Create a mock run_migration that returns a minimal MigrationState."""
    mock = AsyncMock(return_value=MigrationState())
    return mock


def test_migrate_calls_engine(runner: CliRunner) -> None:
    mock_engine = _make_mock_engine()
    with patch("discord_ferry.cli.run_migration", mock_engine):
        result = runner.invoke(
            main,
            [
                "migrate",
                "--export-dir",
                FIXTURES_DIR,
                "--stoat-url",
                "http://localhost",
                "--token",
                "test-token",
            ],
            catch_exceptions=False,
        )
    assert result.exit_code == 0
    mock_engine.assert_called_once()
    config: FerryConfig = mock_engine.call_args[0][0]
    assert config.stoat_url == "http://localhost"
    assert config.token == "test-token"
    assert config.export_dir == Path(FIXTURES_DIR)
    assert config.skip_export is True


def test_migrate_resume_flag(runner: CliRunner) -> None:
    mock_engine = _make_mock_engine()
    with patch("discord_ferry.cli.run_migration", mock_engine):
        result = runner.invoke(
            main,
            [
                "migrate",
                "--export-dir",
                FIXTURES_DIR,
                "--stoat-url",
                "http://localhost",
                "--token",
                "t",
                "--resume",
            ],
            catch_exceptions=False,
        )
    assert result.exit_code == 0
    config: FerryConfig = mock_engine.call_args[0][0]
    assert config.resume is True


def test_migrate_skip_flags(runner: CliRunner) -> None:
    mock_engine = _make_mock_engine()
    with patch("discord_ferry.cli.run_migration", mock_engine):
        result = runner.invoke(
            main,
            [
                "migrate",
                "--export-dir",
                FIXTURES_DIR,
                "--stoat-url",
                "http://localhost",
                "--token",
                "t",
                "--skip-messages",
                "--skip-emoji",
                "--skip-reactions",
                "--skip-threads",
            ],
            catch_exceptions=False,
        )
    assert result.exit_code == 0
    config: FerryConfig = mock_engine.call_args[0][0]
    assert config.skip_messages is True
    assert config.skip_emoji is True
    assert config.skip_reactions is True
    assert config.skip_threads is True


def test_migrate_rate_limit(runner: CliRunner) -> None:
    mock_engine = _make_mock_engine()
    with patch("discord_ferry.cli.run_migration", mock_engine):
        result = runner.invoke(
            main,
            [
                "migrate",
                "--export-dir",
                FIXTURES_DIR,
                "--stoat-url",
                "http://localhost",
                "--token",
                "t",
                "--rate-limit",
                "2.0",
            ],
            catch_exceptions=False,
        )
    assert result.exit_code == 0
    config: FerryConfig = mock_engine.call_args[0][0]
    assert config.message_rate_limit == 2.0


def test_migrate_env_vars(runner: CliRunner) -> None:
    mock_engine = _make_mock_engine()
    env = {"STOAT_URL": "http://env-url", "STOAT_TOKEN": "env-token"}
    with patch("discord_ferry.cli.run_migration", mock_engine):
        result = runner.invoke(
            main,
            ["migrate", "--export-dir", FIXTURES_DIR],
            env=env,
            catch_exceptions=False,
        )
    assert result.exit_code == 0
    config: FerryConfig = mock_engine.call_args[0][0]
    assert config.stoat_url == "http://env-url"
    assert config.token == "env-token"


def test_migrate_engine_error(runner: CliRunner) -> None:
    mock_engine = AsyncMock(side_effect=MigrationError("Phase connect failed: boom"))
    with patch("discord_ferry.cli.run_migration", mock_engine):
        result = runner.invoke(
            main,
            [
                "migrate",
                "--export-dir",
                FIXTURES_DIR,
                "--stoat-url",
                "http://localhost",
                "--token",
                "t",
            ],
        )
    assert result.exit_code == 1
    assert "Migration failed" in result.output


def test_verbose_flag(runner: CliRunner) -> None:
    mock_engine = _make_mock_engine()
    with patch("discord_ferry.cli.run_migration", mock_engine):
        result = runner.invoke(
            main,
            [
                "migrate",
                "--export-dir",
                FIXTURES_DIR,
                "--stoat-url",
                "http://localhost",
                "--token",
                "t",
                "-v",
            ],
            catch_exceptions=False,
        )
    assert result.exit_code == 0
    config: FerryConfig = mock_engine.call_args[0][0]
    assert config.verbose is True


# ---------------------------------------------------------------------------
# Migrate — orchestrated mode
# ---------------------------------------------------------------------------


def test_migrate_orchestrated_mode(runner: CliRunner) -> None:
    """Orchestrated mode: --discord-token + --discord-server sets skip_export=False."""
    mock_engine = _make_mock_engine()
    with patch("discord_ferry.cli.run_migration", mock_engine):
        result = runner.invoke(
            main,
            [
                "migrate",
                "--discord-token",
                "dt",
                "--discord-server",
                "12345",
                "--stoat-url",
                "http://localhost",
                "--token",
                "t",
                "--yes",
            ],
            catch_exceptions=False,
        )
    assert result.exit_code == 0
    config: FerryConfig = mock_engine.call_args[0][0]
    assert config.discord_token == "dt"
    assert config.discord_server_id == "12345"
    assert config.skip_export is False


def test_migrate_mutual_exclusion(runner: CliRunner) -> None:
    """Cannot use both --export-dir and --discord-token."""
    result = runner.invoke(
        main,
        [
            "migrate",
            "--export-dir",
            FIXTURES_DIR,
            "--discord-token",
            "dt",
            "--discord-server",
            "12345",
            "--stoat-url",
            "http://localhost",
            "--token",
            "t",
        ],
    )
    assert result.exit_code == 1
    assert "Cannot use both" in result.output


def test_migrate_neither_mode(runner: CliRunner) -> None:
    """Must provide either --export-dir or --discord-token."""
    result = runner.invoke(
        main,
        [
            "migrate",
            "--stoat-url",
            "http://localhost",
            "--token",
            "t",
        ],
    )
    assert result.exit_code == 1
    assert "Provide either" in result.output


# ---------------------------------------------------------------------------
# Migrate — ToS confirmation
# ---------------------------------------------------------------------------


def test_migrate_orchestrated_prompts_tos(runner: CliRunner) -> None:
    """Orchestrated mode prompts for ToS confirmation; declining exits 1."""
    mock_engine = _make_mock_engine()
    with patch("discord_ferry.cli.run_migration", mock_engine):
        result = runner.invoke(
            main,
            [
                "migrate",
                "--discord-token",
                "dt",
                "--discord-server",
                "12345",
                "--stoat-url",
                "http://localhost",
                "--token",
                "t",
            ],
            input="n\n",
        )
    assert result.exit_code == 1
    assert "Terms of Service" in result.output
    mock_engine.assert_not_called()


def test_migrate_orchestrated_yes_flag_skips_tos(runner: CliRunner) -> None:
    """--yes flag bypasses ToS prompt in orchestrated mode."""
    mock_engine = _make_mock_engine()
    with patch("discord_ferry.cli.run_migration", mock_engine):
        result = runner.invoke(
            main,
            [
                "migrate",
                "--discord-token",
                "dt",
                "--discord-server",
                "12345",
                "--stoat-url",
                "http://localhost",
                "--token",
                "t",
                "--yes",
            ],
            catch_exceptions=False,
        )
    assert result.exit_code == 0
    mock_engine.assert_called_once()


def test_migrate_offline_no_tos_prompt(runner: CliRunner) -> None:
    """Offline mode (--export-dir) does not prompt for ToS."""
    mock_engine = _make_mock_engine()
    with patch("discord_ferry.cli.run_migration", mock_engine):
        result = runner.invoke(
            main,
            [
                "migrate",
                "--export-dir",
                FIXTURES_DIR,
                "--stoat-url",
                "http://localhost",
                "--token",
                "t",
            ],
            catch_exceptions=False,
        )
    assert result.exit_code == 0
    assert "Terms of Service" not in result.output


# ---------------------------------------------------------------------------
# Build command
# ---------------------------------------------------------------------------


def test_build_help(runner: CliRunner) -> None:
    result = runner.invoke(main, ["build", "--help"])
    assert result.exit_code == 0
    assert "--template" in result.output
    assert "--blueprint" in result.output


# ---------------------------------------------------------------------------
# Export-blueprint command
# ---------------------------------------------------------------------------


def test_export_blueprint_help(runner: CliRunner) -> None:
    result = runner.invoke(main, ["export-blueprint", "--help"])
    assert result.exit_code == 0
    assert "--from" in result.output
    assert "--output" in result.output


# ---------------------------------------------------------------------------
# _ProgressTracker — confirm event
# ---------------------------------------------------------------------------


def test_confirm_event_handled() -> None:
    """Sending a confirm event to _ProgressTracker must not raise."""
    from discord_ferry.cli import _ProgressTracker
    from discord_ferry.core.events import MigrationEvent

    tracker = _ProgressTracker(verbose=False)
    event = MigrationEvent(
        phase="VALIDATE",
        status="confirm",
        message="Pre-migration review",
        detail={
            "server_name": "Test Server",
            "roles": 3,
            "categories": 2,
            "channels": 10,
            "emoji": 5,
            "messages": 1000,
            "threads": 1,
            "has_permissions": True,
            "nsfw_channels": 0,
            "warnings": ["Some warning"],
        },
    )
    # Should not raise
    tracker.on_event(event)


# ---------------------------------------------------------------------------
# Rollback subcommand (issue #10)
# ---------------------------------------------------------------------------


def _write_rollback_state(tmp_path: Path) -> None:
    """Copy the rollback_state.json fixture into tmp_path."""
    fixture = Path(__file__).parent / "fixtures" / "rollback_state.json"
    (tmp_path / "state.json").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "message_map.json").write_text("{}", encoding="utf-8")


def test_rollback_help(runner: CliRunner) -> None:
    result = runner.invoke(main, ["rollback", "--help"])
    assert result.exit_code == 0
    assert "--output-dir" in result.output
    assert "--force-unlock" in result.output


def test_rollback_missing_state_file(runner: CliRunner, tmp_path: Path) -> None:
    """SC-32: empty output dir → exit code 2, error message about state.json."""
    result = runner.invoke(
        main,
        [
            "rollback",
            "--output-dir",
            str(tmp_path),
            "--yes",
            "--stoat-url",
            "http://localhost",
            "--token",
            "test-token",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 2
    assert "state.json" in result.output


def test_rollback_missing_url(runner: CliRunner, tmp_path: Path) -> None:
    _write_rollback_state(tmp_path)
    result = runner.invoke(
        main,
        ["rollback", "--output-dir", str(tmp_path), "--token", "test-token"],
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    assert "--stoat-url is required" in result.output


def test_rollback_missing_token(runner: CliRunner, tmp_path: Path) -> None:
    _write_rollback_state(tmp_path)
    result = runner.invoke(
        main,
        ["rollback", "--output-dir", str(tmp_path), "--stoat-url", "http://localhost"],
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    assert "--token is required" in result.output


def test_rollback_calls_engine_yes_flag(runner: CliRunner, tmp_path: Path) -> None:
    """SC-2: CLI happy path with --yes — calls run_rollback with correct config."""
    _write_rollback_state(tmp_path)
    mock_engine = AsyncMock(return_value=MigrationState(stoat_server_id="srv01"))
    with patch("discord_ferry.cli.run_rollback", mock_engine):
        result = runner.invoke(
            main,
            [
                "rollback",
                "--output-dir",
                str(tmp_path),
                "--yes",
                "--stoat-url",
                "http://localhost",
                "--token",
                "test-token",
            ],
            catch_exceptions=False,
        )
    assert result.exit_code == 0
    mock_engine.assert_called_once()
    config = mock_engine.call_args[0][0]
    assert config.stoat_url == "http://localhost"
    assert config.token == "test-token"
    assert config.output_dir == tmp_path
    assert config.server_id == "srv01"  # loaded from state.json
    assert config.skip_export is True
    assert config.force_unlock is False
    assert config.pause_event is not None  # engine needs this for the confirm gate


def test_rollback_force_unlock(runner: CliRunner, tmp_path: Path) -> None:
    """SC-29: --force-unlock propagates to FerryConfig."""
    _write_rollback_state(tmp_path)
    mock_engine = AsyncMock(return_value=MigrationState(stoat_server_id="srv01"))
    with patch("discord_ferry.cli.run_rollback", mock_engine):
        result = runner.invoke(
            main,
            [
                "rollback",
                "--output-dir",
                str(tmp_path),
                "--yes",
                "--force-unlock",
                "--stoat-url",
                "http://localhost",
                "--token",
                "test-token",
            ],
            catch_exceptions=False,
        )
    assert result.exit_code == 0
    config = mock_engine.call_args[0][0]
    assert config.force_unlock is True


def test_rollback_exit_code_on_failures(runner: CliRunner, tmp_path: Path) -> None:
    """Exit code 1 when state.rollback_progress.failures is non-empty."""
    _write_rollback_state(tmp_path)
    from discord_ferry.state import RollbackFailure, RollbackProgress

    async def fake_rollback(config: object, state: MigrationState, **kw: object) -> MigrationState:
        # Simulate engine writing a failure to state.
        state.rollback_progress = RollbackProgress(
            failures=[
                RollbackFailure(entity_type="channel", stoat_id="ch1", error="x", http_status=403)
            ]
        )
        return state

    with patch("discord_ferry.cli.run_rollback", fake_rollback):
        result = runner.invoke(
            main,
            [
                "rollback",
                "--output-dir",
                str(tmp_path),
                "--yes",
                "--stoat-url",
                "http://localhost",
                "--token",
                "test-token",
            ],
            catch_exceptions=False,
        )
    assert result.exit_code == 1


def test_rollback_engine_error(runner: CliRunner, tmp_path: Path) -> None:
    """Engine MigrationError → exit 1 with error message."""
    _write_rollback_state(tmp_path)
    mock_engine = AsyncMock(side_effect=MigrationError("Lock conflict"))
    with patch("discord_ferry.cli.run_rollback", mock_engine):
        result = runner.invoke(
            main,
            [
                "rollback",
                "--output-dir",
                str(tmp_path),
                "--yes",
                "--stoat-url",
                "http://localhost",
                "--token",
                "test-token",
            ],
            catch_exceptions=False,
        )
    assert result.exit_code == 1
    assert "Lock conflict" in result.output


def test_rollback_tracker_renders_suspect_columns() -> None:
    """SC-33: Rich table renders created_at + stoat_id columns for untracked suspects."""
    import asyncio as _asyncio
    import io

    from rich.console import Console as _Console

    from discord_ferry.cli import _RollbackProgressTracker
    from discord_ferry.core.events import MigrationEvent as _Event
    from discord_ferry.review import RollbackSummary, UntrackedSuspectChannel

    # Capture stdout via a fresh Console.
    buf = io.StringIO()
    fake_console = _Console(file=buf, force_terminal=False, no_color=True, width=140)

    suspects = [
        UntrackedSuspectChannel(
            stoat_id="01KPTJT1G00123456789ABCDEF",
            name="orphan-room",
            created_at_iso="2026-04-22T12:32:00+00:00",
        ),
        UntrackedSuspectChannel(
            stoat_id="not-a-ulid-id-here",  # falls back to "unknown"
            name="weird-id",
            created_at_iso=None,
        ),
    ]
    summary = RollbackSummary(
        stoat_server_id="srv01",
        stoat_server_name="Target",
        channels_to_delete=[],
        untracked_ferry_suspect=suspects,
        roles_to_delete=[],
        emoji_to_delete=[],
        categories_to_clean=0,
        autumn_orphan_count=0,
        has_failures_from_prior_run=False,
    )

    pause = _asyncio.Event()
    tracker = _RollbackProgressTracker(pause_event=pause, skip_confirmations=True)
    # Patch the module-level console reference inside _render_summary_and_prompt.
    with patch("discord_ferry.cli.console", fake_console):
        tracker.on_event(
            _Event(
                phase="rollback",
                status="confirm_rollback",
                message="review",
                detail={"summary": summary},
            )
        )

    out = buf.getvalue()
    # Suspect columns: name, created_at, stoat_id.
    assert "orphan-room" in out
    assert "2026-04-22T12:32:00+00:00" in out
    assert "01KPTJT1G00123456789ABCDEF" in out
    # Non-ULID fallback to "unknown".
    assert "unknown" in out
    assert "not-a-ulid-id-here" in out
    # --yes mode releases the gate.
    assert pause.is_set()


# ---------------------------------------------------------------------------
# Probe subcommand (T6) + create-invite flag wiring (T9)
# ---------------------------------------------------------------------------


def test_probe_requires_credentials(runner: CliRunner) -> None:
    result = runner.invoke(main, ["probe", "--test-server-id", "srv1"], catch_exceptions=False)
    assert result.exit_code == 1
    assert "--stoat-url is required" in result.output


def test_probe_requires_test_server_id(runner: CliRunner) -> None:
    result = runner.invoke(
        main, ["probe", "--stoat-url", "https://api.test", "--token", "t"], catch_exceptions=False
    )
    assert result.exit_code != 0  # Click flags the missing required option


def test_no_create_invite_flag_threads_to_config(runner: CliRunner) -> None:
    mock_engine = _make_mock_engine()
    with patch("discord_ferry.cli.run_migration", mock_engine):
        result = runner.invoke(
            main,
            [
                "migrate",
                "--export-dir",
                FIXTURES_DIR,
                "--stoat-url",
                "u",
                "--token",
                "t",
                "--no-create-invite",
                "--yes",
            ],
            catch_exceptions=False,
        )
    assert result.exit_code == 0
    config: FerryConfig = mock_engine.call_args[0][0]
    assert config.create_invite is False


def test_create_invite_default_true(runner: CliRunner) -> None:
    mock_engine = _make_mock_engine()
    with patch("discord_ferry.cli.run_migration", mock_engine):
        runner.invoke(
            main,
            ["migrate", "--export-dir", FIXTURES_DIR, "--stoat-url", "u", "--token", "t", "--yes"],
            catch_exceptions=False,
        )
    config: FerryConfig = mock_engine.call_args[0][0]
    assert config.create_invite is True


# ---------------------------------------------------------------------------
# Batch 5 — S1 markup escape + I2 guard (T1)
# ---------------------------------------------------------------------------


def _b5_console() -> tuple[object, object]:
    """A StringIO-backed Rich Console for capturing rendered tracker output."""
    import io

    from rich.console import Console as _Console

    buf = io.StringIO()
    return buf, _Console(file=buf, force_terminal=False, no_color=True, width=140)


def test_progress_tracker_escapes_hostile_message() -> None:  # SC-1
    from discord_ferry.cli import _ProgressTracker
    from discord_ferry.core.events import MigrationEvent

    buf, fake = _b5_console()
    with patch("discord_ferry.cli.console", fake):
        _ProgressTracker(verbose=False).on_event(
            MigrationEvent(phase="channels", status="started", message="spam[/]")
        )
    assert "spam[/]" in buf.getvalue()  # literal text survived (escaped, not parsed away)


def test_progress_tracker_escapes_channel_name() -> None:  # SC-2
    from discord_ferry.cli import _ProgressTracker
    from discord_ferry.core.events import MigrationEvent

    tracker = _ProgressTracker(verbose=False)
    tracker.on_event(
        MigrationEvent(
            phase="messages",
            status="progress",
            message="m",
            channel_name="news[/]",
            current=1,
            total=10,
        )
    )
    # Rendering the display must not raise on the hostile channel name, and it survives literally.
    buf, fake = _b5_console()
    fake.print(tracker._make_display())
    assert "news[/]" in buf.getvalue()


def _b5_confirm_detail(
    server_name: str = "S", warnings: list[str] | None = None
) -> dict[str, object]:
    return {
        "server_name": server_name,
        "roles": 3,
        "categories": 2,
        "channels": 10,
        "emoji": 5,
        "messages": 1000,
        "threads": 1,
        "has_permissions": True,
        "nsfw_channels": 0,
        "warnings": warnings if warnings is not None else [],
    }


def test_progress_tracker_confirm_escapes_server_name() -> None:  # SC-3
    from discord_ferry.cli import _ProgressTracker
    from discord_ferry.core.events import MigrationEvent

    buf, fake = _b5_console()
    with patch("discord_ferry.cli.console", fake):
        _ProgressTracker(verbose=False).on_event(
            MigrationEvent(
                phase="VALIDATE",
                status="confirm",
                message="r",
                detail=_b5_confirm_detail(server_name="Evil [/] Server"),
            )
        )
    out = buf.getvalue()
    assert "Evil [/] Server" in out
    assert "1,000" in out  # int count rows still render


def test_progress_tracker_confirm_escapes_warning() -> None:  # SC-4
    from discord_ferry.cli import _ProgressTracker
    from discord_ferry.core.events import MigrationEvent

    buf, fake = _b5_console()
    with patch("discord_ferry.cli.console", fake):
        _ProgressTracker(verbose=True).on_event(
            MigrationEvent(
                phase="V",
                status="confirm",
                message="r",
                detail=_b5_confirm_detail(warnings=["danger [/] zone"]),
            )
        )
    assert "danger [/] zone" in buf.getvalue()


def test_rollback_tracker_escapes_message() -> None:  # SC-5
    import asyncio

    from discord_ferry.cli import _RollbackProgressTracker
    from discord_ferry.core.events import MigrationEvent

    buf, fake = _b5_console()
    with patch("discord_ferry.cli.console", fake):
        _RollbackProgressTracker(pause_event=asyncio.Event(), skip_confirmations=True).on_event(
            MigrationEvent(phase="rollback", status="started", message="boom[/]")
        )
    assert "boom[/]" in buf.getvalue()


def test_rollback_tracker_escapes_table_cells() -> None:  # SC-6 (I1)
    import asyncio

    from discord_ferry.cli import _RollbackProgressTracker
    from discord_ferry.core.events import MigrationEvent
    from discord_ferry.review import RollbackSummary, UntrackedSuspectChannel

    summary = RollbackSummary(
        stoat_server_id="srv01",
        stoat_server_name="Srv [/] X",
        channels_to_delete=[],
        roles_to_delete=[],
        emoji_to_delete=[],
        categories_to_clean=0,
        autumn_orphan_count=0,
        has_failures_from_prior_run=False,
        untracked_ferry_suspect=[
            UntrackedSuspectChannel(stoat_id="01ABC", name="orphan[/]", created_at_iso=None)
        ],
    )
    buf, fake = _b5_console()
    with patch("discord_ferry.cli.console", fake):
        _RollbackProgressTracker(pause_event=asyncio.Event(), skip_confirmations=True).on_event(
            MigrationEvent(
                phase="rollback",
                status="confirm_rollback",
                message="r",
                detail={"summary": summary},
            )
        )
    out = buf.getvalue()
    assert "Srv [/] X" in out
    assert "orphan[/]" in out


def test_progress_tracker_guard_catches_missed_escape() -> None:  # SC-7 (I2)
    from discord_ferry.cli import _ProgressTracker
    from discord_ferry.core.events import MigrationEvent

    buf, fake = _b5_console()
    # Simulate a missed/broken escape site: _safe becomes the identity function.
    with (
        patch("discord_ferry.cli.console", fake),
        patch("discord_ferry.cli._safe", lambda v: str(v)),
    ):
        _ProgressTracker(verbose=False).on_event(
            MigrationEvent(phase="channels", status="started", message="bad[/]")
        )
    # No exception propagated (guard caught MarkupError); content surfaced unstyled.
    assert "bad[/]" in buf.getvalue()


def test_rollback_tracker_guard_catches_missed_escape() -> None:  # SC-7b (I2, rollback)
    import asyncio

    from discord_ferry.cli import _RollbackProgressTracker
    from discord_ferry.core.events import MigrationEvent

    buf, fake = _b5_console()
    with (
        patch("discord_ferry.cli.console", fake),
        patch("discord_ferry.cli._safe", lambda v: str(v)),
    ):
        _RollbackProgressTracker(pause_event=asyncio.Event(), skip_confirmations=True).on_event(
            MigrationEvent(phase="rollback", status="started", message="bad[/]")
        )
    assert "bad[/]" in buf.getvalue()


def test_progress_tracker_keeps_styling_for_clean_message() -> None:  # SC-8
    import io

    from rich.console import Console as _Console

    from discord_ferry.cli import _ProgressTracker
    from discord_ferry.core.events import MigrationEvent

    buf = io.StringIO()
    styled = _Console(file=buf, force_terminal=True, width=140)  # render ANSI styles
    with patch("discord_ferry.cli.console", styled):
        _ProgressTracker(verbose=False).on_event(
            MigrationEvent(phase="channels", status="completed", message="all good")
        )
    out = buf.getvalue()
    assert "all good" in out
    assert "[bold green]" not in out  # static tag was applied as style, not shown literally


def test_progress_tracker_only_markup_name() -> None:  # SC-22
    from discord_ferry.cli import _ProgressTracker
    from discord_ferry.core.events import MigrationEvent

    buf, fake = _b5_console()
    with patch("discord_ferry.cli.console", fake):
        _ProgressTracker(verbose=False).on_event(
            MigrationEvent(phase="x", status="started", message="[/]")
        )
    assert "[/]" in buf.getvalue()


def test_cli_exposes_api_helpers_module_level() -> None:  # SC-20 (C1)
    from discord_ferry.cli import api_create_channel, api_edit_role

    assert callable(api_create_channel)
    assert callable(api_edit_role)


# ---------------------------------------------------------------------------
# Batch 5 — S2 build voice->Text fallback (T2)
# ---------------------------------------------------------------------------


def _write_bp(tmp_path: Path, bp: object) -> Path:
    from discord_ferry.blueprint import export_blueprint

    p = tmp_path / "bp.json"
    export_blueprint(bp, p)  # type: ignore[arg-type]
    return p


def test_build_channel_voice_retries_as_text() -> None:  # SC-9
    import asyncio

    from discord_ferry.blueprint import BlueprintChannel
    from discord_ferry.cli import _build_blueprint_channel

    ch = BlueprintChannel(name="VC", type="Voice")
    mock = AsyncMock(side_effect=[MigrationError("voice fail"), {"_id": "ch_text"}])
    with patch("discord_ferry.cli.api_create_channel", mock):
        rid = asyncio.run(_build_blueprint_channel(None, "u", "t", "srv", ch))
    assert rid == "ch_text"
    assert mock.await_count == 2
    assert mock.await_args_list[0].kwargs["channel_type"] == "Voice"
    assert mock.await_args_list[1].kwargs["channel_type"] == "Text"


def test_build_channel_non_voice_propagates() -> None:  # SC-10
    import asyncio

    from discord_ferry.blueprint import BlueprintChannel
    from discord_ferry.cli import _build_blueprint_channel

    ch = BlueprintChannel(name="TC", type="Text")
    mock = AsyncMock(side_effect=MigrationError("text fail"))
    with (
        patch("discord_ferry.cli.api_create_channel", mock),
        pytest.raises(MigrationError),
    ):
        asyncio.run(_build_blueprint_channel(None, "u", "t", "srv", ch))
    assert mock.await_count == 1


def test_build_channel_voice_happy_path() -> None:  # SC-11
    import asyncio

    from discord_ferry.blueprint import BlueprintChannel
    from discord_ferry.cli import _build_blueprint_channel

    ch = BlueprintChannel(name="VC", type="Voice")
    mock = AsyncMock(return_value={"_id": "ch1"})
    with patch("discord_ferry.cli.api_create_channel", mock):
        rid = asyncio.run(_build_blueprint_channel(None, "u", "t", "srv", ch))
    assert rid == "ch1"
    assert mock.await_count == 1


def test_build_categorized_voice_preserves_id(runner: CliRunner, tmp_path: Path) -> None:  # SC-12
    from discord_ferry.blueprint import BlueprintCategory, BlueprintChannel, ServerBlueprint

    bp = ServerBlueprint(
        name="S",
        categories=[
            BlueprintCategory(name="Cat", channels=[BlueprintChannel(name="VC", type="Voice")])
        ],
    )
    p = _write_bp(tmp_path, bp)
    create = AsyncMock(side_effect=[MigrationError("voice fail"), {"_id": "ch_text"}])
    upsert = AsyncMock(return_value={})
    with (
        patch("discord_ferry.cli.api_create_server", AsyncMock(return_value={"_id": "srv"})),
        patch("discord_ferry.cli.api_create_channel", create),
        patch("discord_ferry.cli.api_upsert_categories", upsert),
    ):
        result = runner.invoke(
            main,
            ["build", "--blueprint", str(p), "--stoat-url", "http://x", "--token", "t"],
            catch_exceptions=False,
        )
    assert result.exit_code == 0
    cats = upsert.await_args.args[-1]  # the categories list (5th positional)
    assert any("ch_text" in c["channels"] for c in cats)


def test_build_uncategorized_voice_fallback(runner: CliRunner, tmp_path: Path) -> None:  # SC-13
    from discord_ferry.blueprint import BlueprintChannel, ServerBlueprint

    bp = ServerBlueprint(
        name="S", uncategorized_channels=[BlueprintChannel(name="VC", type="Voice")]
    )
    p = _write_bp(tmp_path, bp)
    create = AsyncMock(side_effect=[MigrationError("voice fail"), {"_id": "ch_text"}])
    with (
        patch("discord_ferry.cli.api_create_server", AsyncMock(return_value={"_id": "srv"})),
        patch("discord_ferry.cli.api_create_channel", create),
    ):
        result = runner.invoke(
            main,
            ["build", "--blueprint", str(p), "--stoat-url", "http://x", "--token", "t"],
            catch_exceptions=False,
        )
    assert result.exit_code == 0
    assert create.await_count == 2


def test_build_non_voice_failure_aborts(runner: CliRunner, tmp_path: Path) -> None:  # SC-19
    from discord_ferry.blueprint import BlueprintChannel, ServerBlueprint

    bp = ServerBlueprint(
        name="S", uncategorized_channels=[BlueprintChannel(name="TC", type="Text")]
    )
    p = _write_bp(tmp_path, bp)
    with (
        patch("discord_ferry.cli.api_create_server", AsyncMock(return_value={"_id": "srv"})),
        patch(
            "discord_ferry.cli.api_create_channel", AsyncMock(side_effect=MigrationError("boom"))
        ),
    ):
        result = runner.invoke(
            main,
            ["build", "--blueprint", str(p), "--stoat-url", "http://x", "--token", "t"],
        )
    assert result.exit_code == 1
    assert "Build failed" in result.output


def test_build_empty_blueprint(runner: CliRunner, tmp_path: Path) -> None:  # SC-21
    from discord_ferry.blueprint import ServerBlueprint

    bp = ServerBlueprint(name="Empty")
    p = _write_bp(tmp_path, bp)
    create = AsyncMock(return_value={"_id": "ch"})
    with (
        patch("discord_ferry.cli.api_create_server", AsyncMock(return_value={"_id": "srv"})),
        patch("discord_ferry.cli.api_create_channel", create),
    ):
        result = runner.invoke(
            main,
            ["build", "--blueprint", str(p), "--stoat-url", "http://x", "--token", "t"],
            catch_exceptions=False,
        )
    assert result.exit_code == 0
    assert create.await_count == 0


# ---------------------------------------------------------------------------
# Batch 5 — S4 build applies role rank (T3)
# ---------------------------------------------------------------------------


def test_build_applies_distinct_ranks(runner: CliRunner, tmp_path: Path) -> None:  # SC-15
    from discord_ferry.blueprint import BlueprintRole, ServerBlueprint

    bp = ServerBlueprint(
        name="S",
        roles=[
            BlueprintRole(name="Admin", colour=0xFF0000, rank=3),
            BlueprintRole(name="Mod", rank=2),
        ],
    )
    p = _write_bp(tmp_path, bp)
    edit = AsyncMock(return_value={})
    with (
        patch("discord_ferry.cli.api_create_server", AsyncMock(return_value={"_id": "srv"})),
        patch("discord_ferry.cli.api_create_role", AsyncMock(return_value={"id": "r"})),
        patch("discord_ferry.cli.api_edit_role", edit),
        patch("discord_ferry.cli.api_set_role_permissions", AsyncMock(return_value={})),
    ):
        result = runner.invoke(
            main,
            ["build", "--blueprint", str(p), "--stoat-url", "http://x", "--token", "t"],
            catch_exceptions=False,
        )
    assert result.exit_code == 0
    ranks = {c.kwargs.get("rank") for c in edit.await_args_list}
    assert {2, 3} <= ranks


def test_build_skips_rank_zero(runner: CliRunner, tmp_path: Path) -> None:  # SC-16
    from discord_ferry.blueprint import BlueprintRole, ServerBlueprint

    bp = ServerBlueprint(name="S", roles=[BlueprintRole(name="Plain", rank=0)])
    p = _write_bp(tmp_path, bp)
    edit = AsyncMock(return_value={})
    with (
        patch("discord_ferry.cli.api_create_server", AsyncMock(return_value={"_id": "srv"})),
        patch("discord_ferry.cli.api_create_role", AsyncMock(return_value={"id": "r"})),
        patch("discord_ferry.cli.api_edit_role", edit),
        patch("discord_ferry.cli.api_set_role_permissions", AsyncMock(return_value={})),
    ):
        runner.invoke(
            main,
            ["build", "--blueprint", str(p), "--stoat-url", "http://x", "--token", "t"],
            catch_exceptions=False,
        )
    assert edit.await_count == 0  # no colour, no rank -> no edit call


def test_build_folds_colour_and_rank_one_patch(runner: CliRunner, tmp_path: Path) -> None:  # SC-17
    from discord_ferry.blueprint import BlueprintRole, ServerBlueprint

    bp = ServerBlueprint(name="S", roles=[BlueprintRole(name="A", colour=0x00FF00, rank=5)])
    p = _write_bp(tmp_path, bp)
    edit = AsyncMock(return_value={})
    with (
        patch("discord_ferry.cli.api_create_server", AsyncMock(return_value={"_id": "srv"})),
        patch("discord_ferry.cli.api_create_role", AsyncMock(return_value={"id": "r"})),
        patch("discord_ferry.cli.api_edit_role", edit),
        patch("discord_ferry.cli.api_set_role_permissions", AsyncMock(return_value={})),
    ):
        runner.invoke(
            main,
            ["build", "--blueprint", str(p), "--stoat-url", "http://x", "--token", "t"],
            catch_exceptions=False,
        )
    assert edit.await_count == 1
    kw = edit.await_args.kwargs
    assert kw.get("colour") == 0x00FF00
    assert kw.get("rank") == 5


def test_build_preserves_permissions(runner: CliRunner, tmp_path: Path) -> None:  # SC-18
    from discord_ferry.blueprint import BlueprintRole, ServerBlueprint

    bp = ServerBlueprint(name="S", roles=[BlueprintRole(name="A", permissions=15, rank=2)])
    p = _write_bp(tmp_path, bp)
    perms = AsyncMock(return_value={})
    with (
        patch("discord_ferry.cli.api_create_server", AsyncMock(return_value={"_id": "srv"})),
        patch("discord_ferry.cli.api_create_role", AsyncMock(return_value={"id": "r"})),
        patch("discord_ferry.cli.api_edit_role", AsyncMock(return_value={})),
        patch("discord_ferry.cli.api_set_role_permissions", perms),
    ):
        runner.invoke(
            main,
            ["build", "--blueprint", str(p), "--stoat-url", "http://x", "--token", "t"],
            catch_exceptions=False,
        )
    assert perms.await_count == 1
    assert perms.await_args.kwargs.get("allow") == 15


# ---------------------------------------------------------------------------
# Batch 5 — S3 export-blueprint keeps voice type, builds via S2 (T4, A1)
# ---------------------------------------------------------------------------


def _write_dce_channel(dir_path: Path, fname: str, ch_id: str, ch_type: int, ch_name: str) -> None:
    import json

    doc = {
        "guild": {"id": "111", "name": "Test", "iconUrl": ""},
        "channel": {
            "id": ch_id,
            "type": ch_type,
            "categoryId": "999",
            "category": "Cat",
            "name": ch_name,
            "topic": "",
        },
        "dateRange": {"after": None, "before": None},
        "exportedAt": "2024-06-15T10:30:00+00:00",
        "messages": [],
        "messageCount": 0,
    }
    (dir_path / fname).write_text(json.dumps(doc), encoding="utf-8")


def test_export_blueprint_keeps_voice_type(runner: CliRunner, tmp_path: Path) -> None:  # SC-14
    import json

    src = tmp_path / "exp"
    src.mkdir()
    _write_dce_channel(src, "voice.json", "201", 2, "General Voice")
    _write_dce_channel(src, "text.json", "202", 0, "general")
    out = tmp_path / "bp.json"
    result = runner.invoke(
        main,
        ["export-blueprint", "--from", str(src), "-o", str(out)],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    all_ch = [c for cat in data.get("categories", []) for c in cat["channels"]] + data.get(
        "uncategorized_channels", []
    )
    types = {c["name"]: c["type"] for c in all_ch}
    assert types.get("General Voice") == "Voice"  # A1: export unchanged
    assert types.get("general") == "Text"
