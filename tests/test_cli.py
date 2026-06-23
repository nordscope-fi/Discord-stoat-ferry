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
