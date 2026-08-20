"""Tests for CLI entry point."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import certifi
import pytest
from click.testing import CliRunner

from discord_ferry.cli import main
from discord_ferry.core.http import reset_http_state
from discord_ferry.errors import MigrationError
from discord_ferry.migrator.verify import CheckReport, RepairOutcome
from discord_ferry.state import FailedMessage, MigrationState

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
    # Fixtures include markdown_rendered.json, which needs acknowledging, so the
    # exit code is 1.  We still verify the table and warnings render.  The
    # wording of the closing line is pinned by
    # test_validate_reports_the_consequence_and_still_exits_1, which controls
    # the fixture set and the console width.
    result = runner.invoke(main, ["validate", FIXTURES_DIR])
    assert result.exit_code == 1
    assert "Export Summary" in result.output
    assert "Messages" in result.output


def test_validate_reports_the_consequence_and_still_exits_1(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exit code is a settled decision: a command whose job is to pass or
    fail an export keeps failing, and changing a documented exit code in a patch
    release breaks anything scripted on it. Only the wording changes.

    Rich wraps at 80 columns when not attached to a terminal, so COLUMNS is
    pinned wide enough that the sentence under test stays on one line. Click 8.2
    removed CliRunner(mix_stderr=...).

    "across N export file(s)" is the discriminating fragment: only
    acknowledgement_required counts files, so the per-channel warning row above
    it cannot produce that phrasing. The consequence phrase alone would not
    kill the old code, because the warning row already carries it.

    Killing: a CLI that stops exiting 1, and one that keeps its own inline type
    check instead of the shared classifier.
    """
    monkeypatch.setenv("COLUMNS", "200")
    shutil.copy(Path(FIXTURES_DIR) / "markdown_rendered.json", tmp_path / "rendered.json")

    result = runner.invoke(main, ["validate", str(tmp_path)])

    assert result.exit_code == 1
    assert "1 message(s) across 1 export file(s) have mentions written as plain text." in (
        result.output
    )
    assert "arrive as text" in result.output
    assert "Critical warnings found" not in result.output


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


def test_build_prints_proxy_notice(runner: CliRunner, tmp_path: Path, proxy_env, os_proxy) -> None:
    """Task 13. Killing: a build command that reaches api_create_server's
    network call without ever calling _print_proxy_notices, leaving a proxied
    user with no explanation. build is one of the four notice entry points
    outside run_migration."""
    from discord_ferry.blueprint import ServerBlueprint

    bp = ServerBlueprint(name="Empty")
    p = _write_bp(tmp_path, bp)
    with (
        os_proxy({}),
        proxy_env(ALL_PROXY="socks5://sock:1080"),
        patch("discord_ferry.core.engine.api_create_server", AsyncMock(return_value="srv")),
    ):
        result = runner.invoke(
            main,
            ["build", "--blueprint", str(p), "--stoat-url", "http://x", "--token", "t"],
            catch_exceptions=False,
        )
    assert result.exit_code == 0
    assert "cannot use" in result.output


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


def test_rollback_prints_proxy_notice(
    runner: CliRunner, tmp_path: Path, proxy_env, os_proxy
) -> None:
    """Task 13. Killing: a rollback command that reaches run_rollback's network
    work without ever calling _print_proxy_notices. rollback is one of the four
    notice entry points outside run_migration."""
    _write_rollback_state(tmp_path)
    mock_engine = AsyncMock(return_value=MigrationState(stoat_server_id="srv01"))
    with (
        os_proxy({}),
        proxy_env(ALL_PROXY="socks5://sock:1080"),
        patch("discord_ferry.cli.run_rollback", mock_engine),
    ):
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
    assert "cannot use" in result.output


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


def test_probe_writes_notices_to_stderr_under_json(runner: CliRunner, proxy_env, os_proxy) -> None:
    """Task 13. probe --json prints machine-readable JSON to stdout
    (cli.py:1322-1324), so a notice printed to stdout would corrupt it. Killing:
    a _print_proxy_notices call site that defaults to_stderr, or omits it, and
    lands the notice on stdout instead."""
    from discord_ferry.migrator.probe import ProbeReport

    with (
        os_proxy({}),
        proxy_env(ALL_PROXY="socks5://sock:1080"),
        patch(
            "discord_ferry.migrator.probe.run_probe",
            AsyncMock(return_value=ProbeReport()),
        ),
    ):
        result = runner.invoke(
            main,
            [
                "probe",
                "--json",
                "--stoat-url",
                "https://api.test",
                "--token",
                "t",
                "--test-server-id",
                "srv1",
            ],
            catch_exceptions=False,
        )
    assert result.exit_code == 0
    assert "cannot use" not in result.stdout  # stdout stays machine-readable
    assert "cannot use" in result.stderr  # the human gets told


def test_probe_json_stdout_survives_a_realistic_payload(runner: CliRunner) -> None:
    """Issue #145. `--json` promises machine-readable output, so stdout must parse.

    The sibling test above passes an empty ProbeReport, whose payload is far under
    80 columns and so never wrapped. That is exactly why this shipped unnoticed.
    Killing: printing the payload through the module-level Rich console, which has
    soft_wrap=False and falls back to 80 columns off a terminal, inserting a real
    newline wherever the wrap lands -- often inside a `", "` separator.
    """
    import json as json_mod

    from discord_ferry.migrator.probe import ProbeCheck, ProbeReport

    report = ProbeReport(
        checks=[
            ProbeCheck(
                name="autumn_limits",
                status="ok",
                detail="attachments 20000000, avatars 4000000, banners 6000000 from limits",
            ),
            ProbeCheck(
                name="rate_window",
                status="ok",
                detail="X-RateLimit-Reset-After reported in milliseconds, bucket /servers, limit 5",
            ),
            ProbeCheck(
                name="voice_channel",
                status="warn",
                detail="voice channel creation returned 200 but the channel type came back as Text",
            ),
            ProbeCheck(
                name="webhooks",
                status="ok",
                detail="webhook created, executed and deleted against the throwaway server",
            ),
        ]
    )

    with patch(
        "discord_ferry.migrator.probe.run_probe",
        AsyncMock(return_value=report),
    ):
        result = runner.invoke(
            main,
            [
                "probe",
                "--json",
                "--stoat-url",
                "https://api.test",
                "--token",
                "t",
                "--test-server-id",
                "srv1",
            ],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    payload = json_mod.loads(result.stdout)
    assert payload["voice_channel"]["status"] == "warn"
    assert payload["autumn_limits"]["detail"].startswith("attachments 20000000")


def test_probe_table_escapes_server_controlled_markup(runner: CliRunner) -> None:
    """Issue #384. A closing Rich tag in a server-supplied detail string raises
    MarkupError and kills all probe output. Killing: a code path that passes
    c.name or c.detail to Rich without escape()."""
    from discord_ferry.migrator.probe import ProbeCheck, ProbeReport

    report = ProbeReport(
        checks=[
            ProbeCheck(
                name="rate_limits[/bold]",
                status="ok",
                detail="bucket [/red] reset_after 10000ms",
            ),
        ]
    )

    with patch(
        "discord_ferry.migrator.probe.run_probe",
        AsyncMock(return_value=report),
    ):
        result = runner.invoke(
            main,
            [
                "probe",
                "--stoat-url",
                "https://api.test",
                "--token",
                "t",
                "--test-server-id",
                "srv1",
            ],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert "rate_limits[/bold]" in result.output
    assert "[/red]" in result.output


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


def test_engine_exposes_build_api_helpers_module_level() -> None:  # SC-20 (C1)
    # The build sequence moved from cli to engine.run_build (#491), so the api
    # helpers it patches are now module-level in engine, not cli. This guards
    # that patchability at its new home.
    from discord_ferry.core.engine import api_create_channel, api_edit_role

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
    from discord_ferry.core.engine import _build_blueprint_channel

    ch = BlueprintChannel(name="VC", type="Voice")
    mock = AsyncMock(side_effect=[MigrationError("voice fail"), {"_id": "ch_text"}])
    with patch("discord_ferry.core.engine.api_create_channel", mock):
        rid = asyncio.run(_build_blueprint_channel(None, "u", "t", "srv", ch, lambda _e: None))
    assert rid == "ch_text"
    assert mock.await_count == 2
    assert mock.await_args_list[0].kwargs["channel_type"] == "Voice"
    assert mock.await_args_list[1].kwargs["channel_type"] == "Text"


def test_build_channel_non_voice_propagates() -> None:  # SC-10
    import asyncio

    from discord_ferry.blueprint import BlueprintChannel
    from discord_ferry.core.engine import _build_blueprint_channel

    ch = BlueprintChannel(name="TC", type="Text")
    mock = AsyncMock(side_effect=MigrationError("text fail"))
    with (
        patch("discord_ferry.core.engine.api_create_channel", mock),
        pytest.raises(MigrationError),
    ):
        asyncio.run(_build_blueprint_channel(None, "u", "t", "srv", ch, lambda _e: None))
    assert mock.await_count == 1


def test_build_channel_voice_happy_path() -> None:  # SC-11
    import asyncio

    from discord_ferry.blueprint import BlueprintChannel
    from discord_ferry.core.engine import _build_blueprint_channel

    ch = BlueprintChannel(name="VC", type="Voice")
    mock = AsyncMock(return_value={"_id": "ch1"})
    with patch("discord_ferry.core.engine.api_create_channel", mock):
        rid = asyncio.run(_build_blueprint_channel(None, "u", "t", "srv", ch, lambda _e: None))
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
        patch("discord_ferry.core.engine.api_create_server", AsyncMock(return_value="srv")),
        patch("discord_ferry.core.engine.api_create_channel", create),
        patch("discord_ferry.core.engine.api_upsert_categories", upsert),
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
        patch("discord_ferry.core.engine.api_create_server", AsyncMock(return_value="srv")),
        patch("discord_ferry.core.engine.api_create_channel", create),
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
        patch("discord_ferry.core.engine.api_create_server", AsyncMock(return_value="srv")),
        patch(
            "discord_ferry.core.engine.api_create_channel",
            AsyncMock(side_effect=MigrationError("boom")),
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
        patch("discord_ferry.core.engine.api_create_server", AsyncMock(return_value="srv")),
        patch("discord_ferry.core.engine.api_create_channel", create),
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
            BlueprintRole(name="Plain", rank=0),
        ],
    )
    p = _write_bp(tmp_path, bp)
    create_role = AsyncMock(side_effect=[{"id": "r-admin"}, {"id": "r-mod"}, {"id": "r-plain"}])
    ranks_mock = AsyncMock(return_value={})
    edit = AsyncMock(return_value={})
    with (
        patch("discord_ferry.core.engine.api_create_server", AsyncMock(return_value="srv")),
        patch("discord_ferry.core.engine.api_create_role", create_role),
        patch("discord_ferry.core.engine.api_edit_role", edit),
        patch("discord_ferry.core.engine.api_edit_role_ranks", ranks_mock),
        patch("discord_ferry.core.engine.api_set_role_permissions", AsyncMock(return_value={})),
    ):
        result = runner.invoke(
            main,
            ["build", "--blueprint", str(p), "--stoat-url", "http://x", "--token", "t"],
            catch_exceptions=False,
        )
    assert result.exit_code == 0
    ranks_mock.assert_awaited_once()
    ordered = ranks_mock.await_args.args[4]
    assert ordered == ["r-admin", "r-mod", "r-plain"]


def test_build_skips_rank_zero(runner: CliRunner, tmp_path: Path) -> None:  # SC-16
    from discord_ferry.blueprint import BlueprintRole, ServerBlueprint

    bp = ServerBlueprint(name="S", roles=[BlueprintRole(name="Plain", rank=0)])
    p = _write_bp(tmp_path, bp)
    ranks_mock = AsyncMock(return_value={})
    with (
        patch("discord_ferry.core.engine.api_create_server", AsyncMock(return_value="srv")),
        patch("discord_ferry.core.engine.api_create_role", AsyncMock(return_value={"id": "r"})),
        patch("discord_ferry.core.engine.api_edit_role", AsyncMock(return_value={})),
        patch("discord_ferry.core.engine.api_edit_role_ranks", ranks_mock),
        patch("discord_ferry.core.engine.api_set_role_permissions", AsyncMock(return_value={})),
    ):
        runner.invoke(
            main,
            ["build", "--blueprint", str(p), "--stoat-url", "http://x", "--token", "t"],
            catch_exceptions=False,
        )
    ranks_mock.assert_not_awaited()


def test_build_separates_colour_and_rank(runner: CliRunner, tmp_path: Path) -> None:  # SC-17
    from discord_ferry.blueprint import BlueprintRole, ServerBlueprint

    bp = ServerBlueprint(name="S", roles=[BlueprintRole(name="A", colour=0x00FF00, rank=5)])
    p = _write_bp(tmp_path, bp)
    edit = AsyncMock(return_value={})
    ranks_mock = AsyncMock(return_value={})
    with (
        patch("discord_ferry.core.engine.api_create_server", AsyncMock(return_value="srv")),
        patch("discord_ferry.core.engine.api_create_role", AsyncMock(return_value={"id": "r-a"})),
        patch("discord_ferry.core.engine.api_edit_role", edit),
        patch("discord_ferry.core.engine.api_edit_role_ranks", ranks_mock),
        patch("discord_ferry.core.engine.api_set_role_permissions", AsyncMock(return_value={})),
    ):
        runner.invoke(
            main,
            ["build", "--blueprint", str(p), "--stoat-url", "http://x", "--token", "t"],
            catch_exceptions=False,
        )
    assert edit.await_count == 1
    kw = edit.await_args.kwargs
    assert kw.get("colour") == 0x00FF00
    assert "rank" not in kw
    ranks_mock.assert_awaited_once()
    assert ranks_mock.await_args.args[4] == ["r-a"]


def test_build_ordering_failure_degrades(runner: CliRunner, tmp_path: Path) -> None:
    from discord_ferry.blueprint import BlueprintRole, ServerBlueprint

    bp = ServerBlueprint(name="S", roles=[BlueprintRole(name="A", rank=2)])
    p = _write_bp(tmp_path, bp)
    ranks_mock = AsyncMock(side_effect=MigrationError("test ordering failure"))
    with (
        patch("discord_ferry.core.engine.api_create_server", AsyncMock(return_value="srv")),
        patch("discord_ferry.core.engine.api_create_role", AsyncMock(return_value={"id": "r-a"})),
        patch("discord_ferry.core.engine.api_edit_role", AsyncMock(return_value={})),
        patch("discord_ferry.core.engine.api_edit_role_ranks", ranks_mock),
        patch("discord_ferry.core.engine.api_set_role_permissions", AsyncMock(return_value={})),
    ):
        result = runner.invoke(
            main,
            ["build", "--blueprint", str(p), "--stoat-url", "http://x", "--token", "t"],
            catch_exceptions=False,
        )
    assert result.exit_code == 0
    assert "Warning" in result.output
    assert "Done!" in result.output


def test_build_preserves_permissions(runner: CliRunner, tmp_path: Path) -> None:  # SC-18
    from discord_ferry.blueprint import BlueprintRole, ServerBlueprint

    bp = ServerBlueprint(name="S", roles=[BlueprintRole(name="A", permissions=15, rank=2)])
    p = _write_bp(tmp_path, bp)
    perms = AsyncMock(return_value={})
    with (
        patch("discord_ferry.core.engine.api_create_server", AsyncMock(return_value="srv")),
        patch("discord_ferry.core.engine.api_create_role", AsyncMock(return_value={"id": "r"})),
        patch("discord_ferry.core.engine.api_edit_role", AsyncMock(return_value={})),
        patch("discord_ferry.core.engine.api_edit_role_ranks", AsyncMock(return_value={})),
        patch("discord_ferry.core.engine.api_set_role_permissions", perms),
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


# ---------------------------------------------------------------------------
# Issue #99 — seven exposed settings
# ---------------------------------------------------------------------------

_BASE_MIGRATE_ARGS = [
    "migrate",
    "--export-dir",
    FIXTURES_DIR,
    "--stoat-url",
    "http://localhost",
    "--token",
    "t",
]

_EXPOSED_FIELDS = (
    "reaction_mode",
    "min_thread_messages",
    "checkpoint_interval",
    "max_concurrent_channels",
    "max_concurrent_requests",
    "skip_avatars",
    "validate_after",
)


def test_migrate_exposed_settings_land_on_config(runner: CliRunner) -> None:
    """SC-1: every new flag reaches FerryConfig."""
    mock_engine = _make_mock_engine()
    with patch("discord_ferry.cli.run_migration", mock_engine):
        result = runner.invoke(
            main,
            [
                *_BASE_MIGRATE_ARGS,
                "--reaction-mode",
                "native",
                "--min-thread-messages",
                "5",
                "--checkpoint-interval",
                "100",
                "--max-concurrent-channels",
                "6",
                "--max-concurrent-requests",
                "12",
                "--skip-avatars",
                "--validate-after",
            ],
            catch_exceptions=False,
        )
    assert result.exit_code == 0
    config: FerryConfig = mock_engine.call_args[0][0]
    assert config.reaction_mode == "native"
    assert config.min_thread_messages == 5
    assert config.checkpoint_interval == 100
    assert config.max_concurrent_channels == 6
    assert config.max_concurrent_requests == 12
    assert config.skip_avatars is True
    assert config.validate_after is True


def test_migrate_no_flags_preserves_defaults(runner: CliRunner) -> None:
    """SC-2: omitting all seven flags produces dataclass-default values."""
    from discord_ferry.config import FerryConfig as RuntimeFerryConfig

    mock_engine = _make_mock_engine()
    with patch("discord_ferry.cli.run_migration", mock_engine):
        result = runner.invoke(main, _BASE_MIGRATE_ARGS, catch_exceptions=False)
    assert result.exit_code == 0
    config: FerryConfig = mock_engine.call_args[0][0]
    defaults = RuntimeFerryConfig(export_dir=Path(FIXTURES_DIR), stoat_url="x", token="x")
    for field in _EXPOSED_FIELDS:
        assert getattr(config, field) == getattr(defaults, field), field


def test_migrate_help_documents_exposed_settings(runner: CliRunner) -> None:
    """SC-3: --help lists every new flag."""
    result = runner.invoke(main, ["migrate", "--help"])
    assert result.exit_code == 0
    for flag in (
        "--reaction-mode",
        "--min-thread-messages",
        "--checkpoint-interval",
        "--max-concurrent-channels",
        "--max-concurrent-requests",
        "--skip-avatars",
        "--validate-after",
    ):
        assert flag in result.output, flag


def test_other_commands_unchanged_by_exposed_settings(runner: CliRunner) -> None:
    """SC-8: the seven flags are migrate-only; rollback keeps its own concurrency flag."""
    for command in ("rollback", "build", "probe", "validate"):
        result = runner.invoke(main, [command, "--help"])
        assert result.exit_code == 0
        for flag in (
            "--reaction-mode",
            "--min-thread-messages",
            "--checkpoint-interval",
            "--max-concurrent-channels",
            "--skip-avatars",
            "--validate-after",
        ):
            assert flag not in result.output, f"{flag} leaked into {command}"
    rollback_help = runner.invoke(main, ["rollback", "--help"]).output
    assert "--max-concurrent-requests" in rollback_help  # its own, unrelated flag


def test_migrate_rejects_out_of_range_settings(runner: CliRunner) -> None:
    """SC-10: Choice/IntRange reject bad values at parse time; engine never invoked."""
    mock_engine = _make_mock_engine()
    bad_args = [
        ["--reaction-mode", "emoji"],
        ["--min-thread-messages", "-1"],
        ["--checkpoint-interval", "0"],
        ["--max-concurrent-channels", "0"],
        ["--max-concurrent-requests", "0"],
    ]
    for extra in bad_args:
        with patch("discord_ferry.cli.run_migration", mock_engine):
            result = runner.invoke(main, [*_BASE_MIGRATE_ARGS, *extra])
        assert result.exit_code != 0, extra
        mock_engine.assert_not_called()


def test_migrate_warns_on_official_service_concurrency(runner: CliRunner) -> None:
    """SC-11: warning fires only for official host + raised concurrency; never blocks."""
    cases = [
        # (stoat_url, extra_args, expect_warning)
        ("https://api.stoat.chat", ["--max-concurrent-channels", "6"], True),
        ("https://api.stoat.chat", ["--max-concurrent-requests", "12"], True),
        ("https://api.stoat.chat", [], False),
        ("https://stoat.example.com", ["--max-concurrent-channels", "6"], False),
    ]
    for stoat_url, extra, expect in cases:
        mock_engine = _make_mock_engine()
        with patch("discord_ferry.cli.run_migration", mock_engine):
            result = runner.invoke(
                main,
                [
                    "migrate",
                    "--export-dir",
                    FIXTURES_DIR,
                    "--stoat-url",
                    stoat_url,
                    "--token",
                    "t",
                    *extra,
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 0, (stoat_url, extra)  # informational — never blocks
        mock_engine.assert_called_once()
        has_warning = "self-hosted" in result.output
        assert has_warning is expect, (stoat_url, extra, result.output)


def test_state_roundtrip_excludes_exposed_settings(tmp_path: Path) -> None:
    """SC-12: the seven settings are per-run config, never persisted state."""
    import json

    from discord_ferry.state import save_state

    state = MigrationState()
    save_state(state, tmp_path)
    raw = json.loads((tmp_path / "state.json").read_text())
    for key in _EXPOSED_FIELDS:
        assert key not in raw


def test_version_flag_matches_package_version() -> None:
    """--version must not drift from __init__.py.

    ferry.spec regex-reads that file at build time, so it is the single source
    of truth. Package metadata is unreliable inside a PyInstaller bundle.
    """
    from discord_ferry import __version__

    result = CliRunner().invoke(main, ["--version"])

    assert result.exit_code == 0
    assert __version__ in result.output


def test_version_is_read_from_module_attribute_not_package_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The explicit version argument to click.version_option must stay.

    Dropping it makes Click fall back to importlib.metadata. In an editable
    install that metadata equals __init__.py, so the assertion above keeps
    passing while a PyInstaller bundle reports the wrong version or fails
    outright -- metadata is unreliable there, which is why ferry.spec
    regex-reads __init__.py instead.

    Patching the attribute and reloading proves the value reaching --version
    travels through discord_ferry.__version__, not through package metadata.
    """
    import importlib

    import discord_ferry

    # Hold the submodule by reference, not via the `discord_ferry.cli` package
    # attribute. A NiceGUI page-test module collected earlier leaves
    # `discord_ferry` re-imported as a fresh package whose `cli` attribute is
    # never re-set: the submodule is still in sys.modules (from this file's
    # module-level `from discord_ferry.cli import main`), so a later
    # `import discord_ferry.cli` finds it there and skips the parent-attribute
    # assignment, making `discord_ferry.cli` raise AttributeError. sys.modules
    # always holds the submodule regardless of collection order, so it is the
    # order-independent handle for reload. (Issue #156.)
    cli_module = importlib.import_module("discord_ferry.cli")

    sentinel = "9.9.9-not-a-real-version"
    monkeypatch.setattr(discord_ferry, "__version__", sentinel)
    try:
        reloaded = importlib.reload(cli_module)
        result = CliRunner().invoke(reloaded.main, ["--version"])

        assert result.exit_code == 0
        assert sentinel in result.output
    finally:
        # Restore the attribute first: the reload re-runs `from discord_ferry
        # import __version__` at cli.py:24 and would otherwise bake the
        # sentinel into the module every later test imports.
        monkeypatch.undo()
        importlib.reload(cli_module)


# ---------------------------------------------------------------------------
# tls-check
# ---------------------------------------------------------------------------


def test_tls_check_prints_the_four_pinned_keys() -> None:
    """SC-134-16. These key names are a contract with release.yml's parser."""
    result = CliRunner().invoke(main, ["tls-check"])
    assert result.exit_code == 0
    for key in ("ca-bundle:", "ca-bundle-readable:", "trust-source:", "ca-visible:"):
        assert key in result.output


def test_tls_check_reports_the_branch_actually_taken(tmp_path: Path) -> None:
    """SC-134-17.

    Reporting only that certifi exists would prove packaging while leaving the
    silent fallback invisible, which is the failure this check exists to catch.

    `describe_trust()` reads the cached SSL context, matching what sessions
    actually use, so this test resets it itself between invocations -- the
    same thing `tests/test_http.py` already does around every case that must
    observe a fresh build, on top of the autouse fixture that only resets
    between test FUNCTIONS.
    """
    assert "trust-source: union" in CliRunner().invoke(main, ["tls-check"]).output

    missing = tmp_path / "nope.pem"
    reset_http_state()
    with patch.object(certifi, "where", return_value=str(missing)):
        out = CliRunner().invoke(main, ["tls-check"]).output
    assert "trust-source: fallback" in out

    # A missing bundle only proves certifi.where() failed to resolve a path.
    # The check this command exists for is a bundle that RESOLVES and READS
    # but does not PARSE: load_verify_locations() raises ssl.SSLError (an
    # OSError subclass) on malformed PEM content, which must still flip the
    # flag to fallback even though the file itself is perfectly readable.
    bad = tmp_path / "garbage.pem"
    bad.write_text("not a certificate")
    reset_http_state()
    with patch.object(certifi, "where", return_value=str(bad)):
        out = CliRunner().invoke(main, ["tls-check"]).output
    assert "ca-bundle-readable: true" in out  # the file is fine
    assert "trust-source: fallback" in out  # the trust is not


# ---------------------------------------------------------------------------
# tls-check — proxy state (Task 11)
# ---------------------------------------------------------------------------


def test_tls_check_reports_the_proxy_keys(proxy_env, os_proxy) -> None:
    """SC-135-42. Killing: a diagnostic that reports configuration rather than
    resolution, which is what makes a diagnostic lie."""
    with os_proxy({}), proxy_env(HTTPS_PROXY="http://corp:8080"):
        out = CliRunner().invoke(main, ["tls-check"]).output
    for key in ("proxy-https:", "proxy-source:", "proxy-disabled:"):
        assert key in out


def test_tls_check_keeps_the_original_four_keys() -> None:
    """SC-135-43. Killing: breaking release.yml's Windows regex or macOS glob,
    and with them the #134 gate.

    A regression guard: describe_trust already prints all four today. It goes
    red only if a later change drops or renames one of them.
    """
    out = CliRunner().invoke(main, ["tls-check"]).output
    for key in ("ca-bundle:", "ca-bundle-readable:", "trust-source:", "ca-visible:"):
        assert key in out


def test_tls_check_never_prints_userinfo(proxy_env, os_proxy) -> None:
    """SC-135-44. Killing: rendering the raw URL. Measured: str(URL), repr(URL)
    and URL.human_repr() all leak userinfo."""
    with os_proxy({}), proxy_env(HTTPS_PROXY="http://user:secret@corp:8080"):
        out = CliRunner().invoke(main, ["tls-check"]).output
    assert "corp" in out
    assert "secret" not in out
    assert "user" not in out


def test_tls_check_reports_proxy_disabled_true_when_the_switch_is_set(proxy_env, os_proxy) -> None:
    """Task 11 review fix. Killing: dropping describe_proxy's own
    FERRY_DISABLE_PROXY early return. resolve_proxy's own kill-switch check
    still makes proxy-http/proxy-https read 'none' either way, so those two
    keys cannot catch a diagnostic that reports proxy-disabled: false while
    everything is in fact suppressed, which is a diagnostic lying about its
    own state.
    """
    with os_proxy({}), proxy_env(FERRY_DISABLE_PROXY="1"):
        out = CliRunner().invoke(main, ["tls-check"]).output
    assert "proxy-disabled: true" in out


# ---------------------------------------------------------------------------
# notice status
# ---------------------------------------------------------------------------


def test_a_notice_prints_without_verbose() -> None:
    """SC-135-38. Killing: emitting at status='warning'. Measured: cli.py:361-363
    gates that behind `if self.verbose`, default off, so the user would see only
    'N warning(s) suppressed' AFTER the run finished. A test asserting only that
    the event was emitted would pass while the user saw nothing."""
    from discord_ferry.cli import _ProgressTracker
    from discord_ferry.core.events import MigrationEvent

    buf, fake = _b5_console()
    with patch("discord_ferry.cli.console", fake):
        _ProgressTracker(verbose=False).on_event(
            MigrationEvent(phase="preflight", status="notice", message="PROXY-NOTICE")
        )
    assert "PROXY-NOTICE" in buf.getvalue()


def test_a_notice_does_not_inflate_the_warning_count() -> None:
    """Killing: reusing 'error' or 'warning', which would make the final
    'Warnings: N' line wrong for a configuration notice."""
    from discord_ferry.cli import _ProgressTracker
    from discord_ferry.core.events import MigrationEvent

    buf, fake = _b5_console()
    with patch("discord_ferry.cli.console", fake):
        tracker = _ProgressTracker(verbose=False)
        tracker.on_event(MigrationEvent(phase="preflight", status="notice", message="x"))
    assert tracker.warning_count == 0
    assert tracker.error_count == 0


def test_the_rollback_tracker_also_handles_notice() -> None:
    """SC-135-39. Killing: adding an arm to one match and not the other. Neither
    has a `case _`, so an unhandled status prints nothing at all, which is worse
    than the gated 'warning' behaviour it replaced. rollback is one of the four
    notice entry points."""
    import asyncio

    from discord_ferry.cli import _RollbackProgressTracker
    from discord_ferry.core.events import MigrationEvent

    buf, fake = _b5_console()
    with patch("discord_ferry.cli.console", fake):
        _RollbackProgressTracker(pause_event=asyncio.Event(), skip_confirmations=True).on_event(
            MigrationEvent(phase="preflight", status="notice", message="ROLLBACK-NOTICE")
        )
    assert "ROLLBACK-NOTICE" in buf.getvalue()


def test_tls_check_reports_unreadable_without_crashing(proxy_env) -> None:
    """The library call being guarded is not the same as the command surviving.

    Killing: a boundary that covers describe_proxy but lets the CLI wrapper
    traceback. The command must exit zero and still print the four describe_trust
    keys release.yml pins, or the #134 gate breaks.
    """
    with (
        proxy_env(),
        patch("discord_ferry.core.http._os_proxies", side_effect=KeyError("boom")),
        patch("discord_ferry.core.http._os_proxy_bypass", return_value=False),
    ):
        result = CliRunner().invoke(main, ["tls-check"])

    assert result.exit_code == 0
    assert "proxy-source: unreadable" in result.output
    for key in ("ca-bundle:", "ca-bundle-readable:", "trust-source:", "ca-visible:"):
        assert key in result.output


def test_tls_check_says_nothing_about_unreadable_on_a_healthy_machine(proxy_env) -> None:
    """A diagnostic that cries wolf is the same defect in the other direction.

    test_tls_check_reports_the_proxy_keys calls reporting configuration rather
    than resolution "what makes a diagnostic lie". Reporting a failure that did
    not happen lies just as loudly.
    """
    with (
        proxy_env(HTTPS_PROXY="http://corp:8080"),
        patch("discord_ferry.core.http._os_proxies", return_value={}),
        patch("discord_ferry.core.http._os_proxy_bypass", return_value=False),
    ):
        out = CliRunner().invoke(main, ["tls-check"]).output

    assert "proxy-source: env" in out
    assert "proxy-https: corp:8080" in out
    assert "unreadable" not in out


def test_tls_check_never_prints_userinfo_on_the_failure_path(proxy_env) -> None:
    """A new code path is a new chance to leak.

    test_tls_check_never_prints_userinfo measured that str(URL), repr(URL) and
    URL.human_repr() all render userinfo. This drives the same command down the
    branch that did not exist when that test was written.
    """
    with (
        proxy_env(HTTPS_PROXY="http://user:secret@corp:8080"),
        patch("discord_ferry.core.http._os_proxies", return_value={"http": "http://os:3128"}),
        patch("discord_ferry.core.http._os_proxy_bypass", side_effect=OSError("registry")),
    ):
        out = CliRunner().invoke(main, ["tls-check"]).output

    assert "secret" not in out
    assert "user" not in out


# ---------------------------------------------------------------------------
# ferry check (#107 batch 9, tasks #261 and #262)
# ---------------------------------------------------------------------------


def _check_report(*results: tuple[str, str]) -> CheckReport:
    report = CheckReport()
    for status, kind in results:
        report.add(name=f"x:{kind}", status=status, kind=kind, detail="detail")  # type: ignore[arg-type]
    return report


def _patched(order: list[str], report: CheckReport | None = None) -> Any:
    """Patch the three things check_cmd must do, recording call order."""
    return (
        patch(
            "discord_ferry.cli.register_secret",
            side_effect=lambda *_a: order.append("register_secret"),
        ),
        patch(
            "discord_ferry.cli.init_request_semaphore",
            side_effect=lambda *_a: order.append("semaphore"),
        ),
        patch(
            "discord_ferry.cli.load_state",
            return_value=MigrationState(stoat_server_id="srv1"),
        ),
        patch(
            "discord_ferry.migrator.verify.run_check",
            new=AsyncMock(
                side_effect=lambda *a, **k: (
                    order.append("request") or (report if report is not None else _check_report())
                )
            ),
        ),
    )


def test_check_registers_the_stoat_token_before_any_request(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Asserts the CALL, deliberately, and never the absence of a token in a
    sample string.

    An absence assertion passes against a value that simply did not appear in
    that particular string. Ferry shipped unmasked Stoat tokens to its log file
    for two releases behind exactly that shape of check.

    probe_cmd's own comment records why the call is needed at all: a command
    that bypasses FerryConfig never fires the engine's store hook, and the regex
    backstop deliberately cannot match Stoat's opaque base64url values, so
    without this line the whole command has no coverage.
    """
    order: list[str] = []
    a, b, c, d = _patched(order)
    with a, b, c, d:
        runner.invoke(
            main,
            ["check", str(tmp_path), "--stoat-url", "https://api.test", "--token", "t"],
        )
    assert "register_secret" in order, "never registered for masking"
    assert order.index("register_secret") < order.index("request")


def test_check_initialises_the_request_semaphore_before_any_request(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Omitting this gives UNBOUNDED concurrency and no error at all.

    _request_semaphore starts None and _api_request treats that as no limit, so
    nothing anywhere reports the omission. On a 200-channel server that is 200
    simultaneous sockets. Asserted as a call, for the same reason as above.
    """
    order: list[str] = []
    a, b, c, d = _patched(order)
    with a, b, c, d:
        runner.invoke(
            main,
            ["check", str(tmp_path), "--stoat-url", "https://api.test", "--token", "t"],
        )
    assert "semaphore" in order, "the concurrency semaphore was never initialised"
    assert order.index("semaphore") < order.index("request")


def test_validate_still_resolves_to_the_export_validator(runner: CliRunner) -> None:
    """`ferry validate` is TAKEN and means something else: parse and validate an
    export, with no API calls. The new command had to be named `check`.

    Kills registering the new command under the existing name, which would
    silently replace a released one.
    """
    result = runner.invoke(main, ["validate", "--help"])
    assert result.exit_code == 0
    assert "export" in result.output.lower()
    assert "--stoat-url" not in result.output


def test_check_requires_a_url(runner: CliRunner, tmp_path: Path) -> None:
    """Matches probe_cmd: a clear message rather than a traceback."""
    result = runner.invoke(main, ["check", str(tmp_path), "--token", "t"], env={"STOAT_URL": ""})
    assert result.exit_code != 0
    assert "stoat-url" in result.output.lower()


def test_check_exits_zero_when_everything_passed(runner: CliRunner, tmp_path: Path) -> None:
    """SC-5.4."""
    order: list[str] = []
    a, b, c, d = _patched(order, _check_report(("ok", "tail_present")))
    with a, b, c, d:
        result = runner.invoke(
            main,
            ["check", str(tmp_path), "--stoat-url", "https://api.test", "--token", "t"],
        )
    assert result.exit_code == 0


def test_check_exits_non_zero_on_any_failure(runner: CliRunner, tmp_path: Path) -> None:
    """SC-5.5. The exit code IS the machine-readable interface, which is why no
    --json is needed to ship."""
    order: list[str] = []
    report = _check_report(("ok", "tail_present"), ("fail", "channel_missing"))
    a, b, c, d = _patched(order, report)
    with a, b, c, d:
        result = runner.invoke(
            main,
            ["check", str(tmp_path), "--stoat-url", "https://api.test", "--token", "t"],
        )
    assert result.exit_code != 0


def test_a_warning_alone_still_exits_zero(runner: CliRunner, tmp_path: Path) -> None:
    """SC-5.7. A renamed category is cosmetic: the entity exists and its content
    is intact. Exiting non-zero for it would fail a migration that is fine."""
    order: list[str] = []
    a, b, c, d = _patched(order, _check_report(("warn", "category_title_mismatch")))
    with a, b, c, d:
        result = runner.invoke(
            main,
            ["check", str(tmp_path), "--stoat-url", "https://api.test", "--token", "t"],
        )
    assert result.exit_code == 0


def test_a_report_of_only_unverifiable_exits_zero_and_says_so(
    runner: CliRunner, tmp_path: Path
) -> None:
    """SC-5.6. Exit 0, AND the summary must not read as a clean pass.

    The exit code alone cannot separate "all fine" from "could not check any of
    it", because both exit 0. On a merge migration most tail results are
    unverifiable, so the count has to be visible or the tool looks like it
    approved something it never examined.
    """
    order: list[str] = []
    report = _check_report(
        ("unverifiable", "tail_not_recorded"), ("unverifiable", "channel_not_visible")
    )
    a, b, c, d = _patched(order, report)
    with a, b, c, d:
        result = runner.invoke(
            main,
            ["check", str(tmp_path), "--stoat-url", "https://api.test", "--token", "t"],
        )
    assert result.exit_code == 0
    assert "2 unverifiable" in result.output
    assert "could not be verified" in result.output


def test_the_summary_names_every_count_and_the_exit_code(runner: CliRunner, tmp_path: Path) -> None:
    """SC-4.6. Counts lead, and the exit code is stated rather than inferred."""
    order: list[str] = []
    report = _check_report(
        ("ok", "tail_present"),
        ("warn", "category_title_mismatch"),
        ("fail", "channel_missing"),
        ("unverifiable", "tail_not_recorded"),
    )
    a, b, c, d = _patched(order, report)
    with a, b, c, d:
        result = runner.invoke(
            main,
            ["check", str(tmp_path), "--stoat-url", "https://api.test", "--token", "t"],
        )
    for fragment in ("1 ok", "1 failed", "1 unverifiable", "1 warning"):
        assert fragment in result.output, f"summary omitted {fragment!r}"


# ---------------------------------------------------------------------------
# ferry build, driven through the real api_create_server wrapper
# ---------------------------------------------------------------------------


def test_build_parses_the_real_create_response(runner: CliRunner, tmp_path: Path) -> None:
    """SC-3.1. Drive ferry build through the real wrapper, not a patched one.

    Every other build test patches api_create_server out, so none of them touches the
    response parsing. This one serves the wrapped body over aioresponses instead, which
    is what spec story S3 actually asks for.
    """
    from aioresponses import aioresponses

    from discord_ferry.blueprint import BlueprintRole, ServerBlueprint

    bp = ServerBlueprint(name="Rebuilt", roles=[BlueprintRole(name="Mod")])
    p = _write_bp(tmp_path, bp)

    with aioresponses() as m:
        # Register the whole chain. Leaving the roles route out would make aioresponses
        # raise a connection error, which _api_request retries three times with backoff:
        # seconds of runtime, ending in a MigrationError resembling the bug under test.
        m.post(
            "http://x/servers/create",
            payload={"server": {"_id": "srv1", "name": "Rebuilt"}, "channels": []},
            repeat=True,
        )
        m.post(
            "http://x/servers/srv1/roles",
            payload={"id": "role1", "role": {"name": "Mod"}},
            repeat=True,
        )
        result = runner.invoke(
            main,
            ["build", "--blueprint", str(p), "--stoat-url", "http://x", "--token", "t"],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert "Created role" in result.output


def test_build_reports_an_unrecognised_create_response(runner: CliRunner, tmp_path: Path) -> None:
    """SC-3.2. This path prints the error with no redaction available.

    cli.py catches MigrationError and prints it through _safe, which is Rich markup
    escaping rather than redaction, and build registers no secret. The message has to be
    safe on its own, so it carries key names and never a value.
    """
    from aioresponses import aioresponses

    from discord_ferry.blueprint import ServerBlueprint

    bp = ServerBlueprint(name="Rebuilt")
    p = _write_bp(tmp_path, bp)

    with aioresponses() as m:
        m.post("http://x/servers/create", payload={"id": "srv1"}, repeat=True)
        result = runner.invoke(
            main,
            ["build", "--blueprint", str(p), "--stoat-url", "http://x", "--token", "t"],
            catch_exceptions=False,
        )

    assert result.exit_code == 1
    assert "Build failed:" in result.output
    # Assert the diagnostic reached the user, not only that the value did not. Without
    # this the test passes against MigrationError(""), which is the shape this project
    # keeps shipping: a condition that cannot fail. Match a short fragment, because the
    # module-level Rich Console wraps at 80 columns off a TTY and would split a longer one.
    assert "'id'" in result.output
    assert "srv1" not in result.output


def test_check_summary_does_not_offer_merge_on_a_flatten_migration(
    runner: CliRunner, tmp_path: Path
) -> None:
    """#267's problem sentence, and it lives here rather than in verify.py.

    The summary told every user an unverifiable result is "expected when
    --thread-strategy=merge was used, after a duplicate send, or for a channel
    this token cannot read". On a flatten migration the first of those three is
    false, and Ferry now records which strategy actually ran.

    verify.py's per-result detail was fixed in #293. That is what --json
    serialises and what the repair tool reads in-process. This is what a human
    reads, and fixing one without the other leaves half the audience wrong.
    """
    order: list[str] = []
    state = MigrationState(stoat_server_id="srv1")
    state.thread_strategy = "flatten"
    report = _check_report(("unverifiable", "tail_not_recorded"))

    a, _b, _c, d = _patched(order, report)
    with a, _b, patch("discord_ferry.cli.load_state", return_value=state), d:
        result = runner.invoke(
            main, ["check", str(tmp_path), "--stoat-url", "https://api.test", "--token", "t"]
        )

    assert result.exit_code == 0
    assert "could not be verified" in result.output
    assert "merge" not in result.output


def test_check_summary_keeps_the_old_wording_when_no_strategy_was_recorded(
    runner: CliRunner, tmp_path: Path
) -> None:
    """A state.json written before 2.17.0 records no strategy.

    It must keep the v2.16.0 sentence listing the possibilities, because that is
    genuinely all Ferry knows about that migration. Naming an unknown strategy
    would read as a defect rather than as an old file.
    """
    order: list[str] = []
    state = MigrationState(stoat_server_id="srv1")
    assert state.thread_strategy == ""
    report = _check_report(("unverifiable", "tail_not_recorded"))

    a, _b, _c, d = _patched(order, report)
    with a, _b, patch("discord_ferry.cli.load_state", return_value=state), d:
        result = runner.invoke(
            main, ["check", str(tmp_path), "--stoat-url", "https://api.test", "--token", "t"]
        )

    assert result.exit_code == 0
    assert "merge" in result.output
    assert "duplicate send" in result.output


# ---------------------------------------------------------------------------
# ferry check --json (#300, SC-4.1 to SC-4.7)
# ---------------------------------------------------------------------------


def _invoke_check(runner: CliRunner, tmp_path: Path, report: CheckReport, *extra: str) -> Any:
    """Drive check_cmd with a given report, optionally with extra flags."""
    order: list[str] = []
    a, b, c, d = _patched(order, report)
    with a, b, c, d:
        return runner.invoke(
            main,
            ["check", str(tmp_path), "--stoat-url", "https://api.test", "--token", "t", *extra],
        )


def test_check_json_output_parses(runner: CliRunner, tmp_path: Path) -> None:
    """SC-4.1. Asserted BY PARSING, never by inspecting the string.

    A substring assertion passes against output that is not valid JSON at all,
    which is the whole failure mode issue #145 records: the module-level Console
    falls back to 80 columns off a terminal and inserts a real newline wherever
    the wrap lands, including inside a string value.
    """
    report = _check_report(("ok", "channel_present"), ("warn", "channel_renamed"))
    result = _invoke_check(runner, tmp_path, report, "--json")

    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert isinstance(parsed, dict)


def test_check_json_mirrors_the_report(runner: CliRunner, tmp_path: Path) -> None:
    """SC-4.2. Every CheckResult field, plus the counts summary."""
    report = CheckReport()
    report.add(
        name="channel:d-100",
        status="warn",
        kind="channel_renamed",
        detail="renamed on the server",
        discord_id="d-100",
        stoat_id="01JSTOATCH00000000000AAA",
        expected="general",
        found="renamed-here",
    )
    result = _invoke_check(runner, tmp_path, report, "--json")

    parsed = json.loads(result.output)
    assert parsed["counts"]["warn"] == 1
    (row,) = parsed["results"]
    assert row == {
        "name": "channel:d-100",
        "status": "warn",
        "kind": "channel_renamed",
        "detail": "renamed on the server",
        "discord_id": "d-100",
        "stoat_id": "01JSTOATCH00000000000AAA",
        "expected": "general",
        "found": "renamed-here",
    }


def test_check_json_survives_a_control_character_in_a_detail(
    runner: CliRunner, tmp_path: Path
) -> None:
    """SC-4.3. The discriminator for this story.

    CheckResult.detail can embed a server-supplied error body. The protection
    that exists on the Rich path is incidental: Rich interleaves its own style
    codes between an ESC and the following '[', so the sequence never reaches the
    terminal contiguously. click.echo does no such thing and would print the ESC
    verbatim.
    """
    report = CheckReport()
    report.add(
        name="channel:d-100",
        status="fail",
        kind="check_error",
        detail="server said: \x1b[2J\x07 wiped\nyour screen\r",
    )
    result = _invoke_check(runner, tmp_path, report, "--json")

    parsed = json.loads(result.output)
    detail = parsed["results"][0]["detail"]
    assert "\x1b" not in detail
    assert "\x07" not in detail
    assert "\r" not in detail
    assert "wiped" in detail and "your screen" in detail


def test_check_json_suppresses_the_table(runner: CliRunner, tmp_path: Path) -> None:
    """SC-4.4. Stdout is one JSON document and nothing else."""
    report = _check_report(("ok", "channel_present"))
    result = _invoke_check(runner, tmp_path, report, "--json")

    assert "Migration Check" not in result.output
    assert "ok ·" not in result.output
    json.loads(result.output)


def test_the_rich_path_is_unchanged_by_the_stripping(runner: CliRunner, tmp_path: Path) -> None:
    """SC-4.5. Stripping at the wrong layer would alter shipped behaviour.

    Applying it in CheckResult or in report.add would change what the Rich table
    prints, which is output v2.16.0 already produces. Only this test catches
    that, because every other assertion here reads the JSON path.
    """
    report = CheckReport()
    report.add(
        name="channel:d-100",
        status="fail",
        kind="check_error",
        detail="server said: \x1b[2J oops",
    )
    result = _invoke_check(runner, tmp_path, report)

    assert result.exit_code == 1
    assert "Migration Check" in result.output
    # The dataclass itself must still hold the raw text: the stripping belongs
    # to the JSON path alone.
    assert report.results[0].detail == "server said: \x1b[2J oops"


def test_check_json_does_not_change_the_exit_code(runner: CliRunner, tmp_path: Path) -> None:
    """SC-4.6. The v2.16.0 contract: non-zero only when something failed."""
    failing = _check_report(("fail", "channel_missing"))
    passing = _check_report(("warn", "channel_renamed"), ("unverifiable", "tail_not_recorded"))

    assert _invoke_check(runner, tmp_path, failing, "--json").exit_code == 1
    assert _invoke_check(runner, tmp_path, failing).exit_code == 1
    assert _invoke_check(runner, tmp_path, passing, "--json").exit_code == 0
    assert _invoke_check(runner, tmp_path, passing).exit_code == 0


def test_check_json_still_registers_the_token_and_the_semaphore(
    runner: CliRunner, tmp_path: Path
) -> None:
    """SC-4.7. Asserts the CALLS, never an absence.

    A test asserting a token is missing from some sample string passes against a
    token that simply did not appear there, and Ferry shipped unmasked Stoat
    tokens to its log file for two releases behind exactly that shape of check.
    Both calls must still happen before the request on the --json path.
    """
    order: list[str] = []
    a, b, c, d = _patched(order, _check_report(("ok", "channel_present")))
    with a, b, c, d:
        runner.invoke(
            main,
            [
                "check",
                str(tmp_path),
                "--stoat-url",
                "https://api.test",
                "--token",
                "t",
                "--json",
            ],
        )

    assert "register_secret" in order
    assert "semaphore" in order
    assert order.index("register_secret") < order.index("request")
    assert order.index("semaphore") < order.index("request")


def test_check_json_does_not_wrap_a_long_detail(runner: CliRunner, tmp_path: Path) -> None:
    """The test that actually enforces issue #145, and the first seven did not.

    Every other --json test here uses a short payload, and a short payload fits
    inside 80 columns, so all seven passed against console.print as readily as
    against click.echo. The rule was documented in a comment and guarded by
    nothing.

    Measured: a 275-character JSON line printed through the module-level Console
    comes back with five newlines and fails to parse, because Console has
    soft_wrap=False and falls back to 80 columns off a terminal. The wrap lands
    inside a string value.

    So the detail here is deliberately long enough to force that. If this test
    is ever shortened it stops testing anything.
    """
    report = CheckReport()
    report.add(
        name="channel:d-100",
        status="fail",
        kind="check_error",
        detail="could not read this channel's messages: " + ("verylongword" * 20),
    )
    result = _invoke_check(runner, tmp_path, report, "--json")

    assert len(result.output) > 200, "payload too short to force a wrap; this test is inert"
    parsed = json.loads(result.output)
    assert parsed["results"][0]["detail"].endswith("verylongword")


def test_check_json_strips_c1_controls_and_the_synthetic_id(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Two gaps the first version of the stripping had, both measured.

    "It parses" was never the right test. json.dumps escapes a control character
    in the JSON TEXT, so the document always parses and then hands the raw byte
    back to whoever parses it. The threat is downstream of parsing, which is why
    these assertions read the PARSED values.

    C1: \\x9b is CSI, the single-byte equivalent of ESC[. Its ordinal is 155, so
    an `ord(ch) >= 32` filter passes it straight through. The first version of
    _strip_control used exactly that filter.

    discord_id: not always a Discord snowflake. The forum index writer stores a
    synthetic `forum-index-forum-{parent_channel_name}`, and that name comes from
    the export, so a forum named with an escape byte puts it into this key. It
    was not stripped at all.
    """
    report = CheckReport()
    report.add(
        name="channel:forum-index-forum-my\x1b[2Jforum",
        status="ok",
        kind="channel_present",
        detail="fine\x9b2J here",
        discord_id="forum-index-forum-my\x1b[2Jforum",
        stoat_id="01JSTOAT\x9bCH000000000AAA",
    )
    result = _invoke_check(runner, tmp_path, report, "--json")

    row = json.loads(result.output)["results"][0]
    for field in ("name", "detail", "discord_id", "stoat_id"):
        assert "\x1b" not in row[field], f"{field} still carries an ESC after parsing"
        assert "\x9b" not in row[field], f"{field} still carries a C1 CSI after parsing"
    # The surrounding text survives, so this is stripping and not blanking.
    assert row["discord_id"] == "forum-index-forum-my[2Jforum"
    assert row["detail"] == "fine2J here"


def test_check_json_keeps_a_tab_and_ordinary_unicode(runner: CliRunner, tmp_path: Path) -> None:
    """The strip must not be a blunt ASCII filter.

    A tab is deliberately kept, and U+00A0 is category Zs rather than Cc, so a
    non-breaking space and any ordinary non-ASCII text must survive. A filter
    written as `ch.isprintable()` would drop both.
    """
    report = CheckReport()
    report.add(
        name="channel:d-100",
        status="ok",
        kind="channel_present",
        detail="col\tumn   café 日本語",
    )
    result = _invoke_check(runner, tmp_path, report, "--json")

    detail = json.loads(result.output)["results"][0]["detail"]
    assert detail == "col\tumn   café 日本語"


def test_the_summary_does_not_claim_an_exclusive_cause(runner: CliRunner, tmp_path: Path) -> None:
    """`unverifiable` has FIVE producers, and the summary must not imply fewer.

    channel_not_visible, category_title_unknown, check_error, tail_not_recorded
    and tail_window_exhausted all produce it. An earlier draft of this sentence
    said a non-merge migration "means a duplicate send, or a channel this token
    cannot read", which claimed exclusivity and omitted three. The whole-branch
    review caught it.

    Each branch now points at the per-result detail instead of enumerating, so
    this asserts the pointer is there rather than trying to list five causes in
    a summary line.
    """
    for strategy in ("flatten", "merge", "archive"):
        state = MigrationState(stoat_server_id="srv1")
        state.thread_strategy = strategy
        report = _check_report(("unverifiable", "tail_window_exhausted"))
        order: list[str] = []
        a, b, _c, d = _patched(order, report)
        with a, b, patch("discord_ferry.cli.load_state", return_value=state), d:
            result = runner.invoke(
                main, ["check", str(tmp_path), "--stoat-url", "https://api.test", "--token", "t"]
            )
        normalised = " ".join(result.output.split())
        assert "Each result above says which cause applies" in normalised, (
            f"the {strategy} summary does not point at the per-result detail"
        )


# ---------------------------------------------------------------------------
# ferry retry (#107 batch 10, task #323)
# ---------------------------------------------------------------------------

D_MSG = "100000000000000001"
S_CHANNEL = "01JSTOATCHN000000000OLD"


def _write_minimal_export(export_dir: Path, channel_id: str = "800000000000000001") -> Path:
    """A DCE export directory holding exactly one message.

    Written by hand rather than reusing a fixture, so the message id is the
    literal the retry state names. Discord and Stoat ids stay visibly different
    throughout: a fixture that seeds both sides from one variable lets a test
    pass by comparing a value with itself.
    """
    export_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "guild": {"id": "900000000000000001", "name": "Test Guild", "iconUrl": ""},
        "channel": {
            "id": channel_id,
            "type": 0,
            "name": "general",
            "categoryId": "",
            "category": "",
            "topic": "",
        },
        "dateRange": {"after": None, "before": None},
        "exportedAt": "2026-01-01T00:00:00+00:00",
        "messageCount": 1,
        "messages": [
            {
                "id": D_MSG,
                "type": "Default",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "timestampEdited": None,
                "content": "hello",
                "author": {
                    "id": "700000000000000001",
                    "name": "author",
                    "nickname": "author",
                    "isBot": False,
                    "avatarUrl": "",
                },
                "attachments": [],
                "embeds": [],
                "stickers": [],
                "reactions": [],
                "mentions": [],
            }
        ],
    }
    (export_dir / "general.json").write_text(json.dumps(payload), encoding="utf-8")
    return export_dir


def _write_state_with_one_failure(out_dir: Path) -> Path:
    """An output directory whose state.json carries one failed message."""
    from discord_ferry.state import FailedMessage, save_state

    out_dir.mkdir(parents=True, exist_ok=True)
    state = MigrationState(
        stoat_server_id="01JSTOATSRV000000000AAA",
        channel_map={"800000000000000001": S_CHANNEL},
        failed_messages=[
            FailedMessage(discord_msg_id=D_MSG, stoat_channel_id=S_CHANNEL, error="timeout")
        ],
    )
    save_state(state, out_dir)
    return out_dir


def _retry_argv(out_dir: Path, export_dir: Path) -> list[str]:
    return [
        "retry",
        str(out_dir),
        "--export-dir",
        str(export_dir),
        "--stoat-url",
        "https://api.test",
        "--token",
        "t",
    ]


def test_retry_passes_a_real_export_to_the_coroutine(runner: CliRunner, tmp_path: Path) -> None:
    """SC-1.7. Asserts the parse happened AND that exports is non-empty.

    This is the highest-value assertion in the command. run_retry_failed needs
    both a real config.export_dir and a NON-EMPTY exports list, which it scans
    to resolve each FailedMessage back to a DCEMessage. Given an empty list
    every message takes the "not found in exports, skipping" branch, stays
    failed, and the command still reports a plausible result.

    A test asserting only the exit code passes against exactly that. Neither
    obvious template wires it: rollback_cmd sets export_dir to a value it never
    reads and passes exports=[], and check_cmd builds no FerryConfig at all.
    """
    export_dir = _write_minimal_export(tmp_path / "export")
    out_dir = _write_state_with_one_failure(tmp_path / "out")
    seen: dict[str, Any] = {}

    async def _capture(config: Any, state: Any, exports: Any, on_event: Any) -> None:
        seen["exports"] = exports
        seen["export_dir"] = config.export_dir
        # Stand in for a retry that succeeded, so the exit code reflects the
        # command's contract rather than this stub doing nothing.
        state.failed_messages.clear()

    with patch("discord_ferry.cli.run_retry_failed", new=_capture):
        result = runner.invoke(main, _retry_argv(out_dir, export_dir))

    assert result.exit_code == 0, result.output
    assert seen["export_dir"] == export_dir
    assert len(seen["exports"]) > 0, (
        "an empty exports list makes every retry a silent no-op that still reports success"
    )
    assert seen["exports"][0].channel.id == "800000000000000001"


def test_retry_registers_the_token_and_the_semaphore_before_any_request(
    runner: CliRunner, tmp_path: Path
) -> None:
    """SC-1.2 and SC-1.3. Asserts the CALLS and their ORDER.

    Never the absence of a token from output: an absence assertion passes
    against a token that simply did not appear in that string.
    """
    export_dir = _write_minimal_export(tmp_path / "export")
    out_dir = _write_state_with_one_failure(tmp_path / "out")
    order: list[str] = []

    async def _noop_coro(*_a: Any, **_k: Any) -> None:
        order.append("request")

    with (
        patch(
            "discord_ferry.cli.register_secret",
            side_effect=lambda *_a: order.append("register_secret"),
        ),
        patch(
            "discord_ferry.cli.init_request_semaphore",
            side_effect=lambda *_a: order.append("semaphore"),
        ),
        patch("discord_ferry.cli.run_retry_failed", new=_noop_coro),
    ):
        runner.invoke(main, _retry_argv(out_dir, export_dir))

    assert "register_secret" in order, "the Stoat token was never registered for masking"
    assert "semaphore" in order, "the concurrency semaphore was never initialised"
    assert order.index("register_secret") < order.index("request")
    assert order.index("semaphore") < order.index("request")


def test_retry_with_nothing_failed_exits_zero_in_the_coroutines_own_words(
    runner: CliRunner, tmp_path: Path
) -> None:
    """SC-1.5. The wording comes from the engine event, not from the shell.

    A separate CLI sentence would drift from the engine's the first time either
    changed, and the engine is the one that knows.
    """
    export_dir = _write_minimal_export(tmp_path / "export")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    from discord_ferry.state import save_state

    save_state(MigrationState(stoat_server_id="01JSTOATSRV000000000AAA"), out_dir)

    result = runner.invoke(main, _retry_argv(out_dir, export_dir))
    assert result.exit_code == 0, result.output
    assert "No failed messages to retry." in result.output


def test_retry_exits_non_zero_when_a_message_is_still_failed(
    runner: CliRunner, tmp_path: Path
) -> None:
    """SC-1.6. A retry that fixed nothing must not look like success."""
    export_dir = _write_minimal_export(tmp_path / "export")
    out_dir = _write_state_with_one_failure(tmp_path / "out")

    async def _leaves_it_failed(config: Any, state: Any, exports: Any, on_event: Any) -> None:
        state.failed_messages[0].retry_count += 1

    with patch("discord_ferry.cli.run_retry_failed", new=_leaves_it_failed):
        result = runner.invoke(main, _retry_argv(out_dir, export_dir))
    assert result.exit_code == 1, result.output


def test_retry_with_a_missing_export_directory_exits_two_and_makes_no_request(
    runner: CliRunner, tmp_path: Path
) -> None:
    """SC-1.8. Asserts the request COUNT, not the message.

    A command that reached the network and then failed would pass an assertion
    that only reads stdout.
    """
    out_dir = _write_state_with_one_failure(tmp_path / "out")
    missing = tmp_path / "not-here"
    calls: list[str] = []

    async def _record(*_a: Any, **_k: Any) -> None:
        calls.append("request")

    with patch("discord_ferry.cli.run_retry_failed", new=_record):
        result = runner.invoke(main, _retry_argv(out_dir, missing))

    assert result.exit_code == 2, result.output
    assert str(missing) in result.output
    assert calls == [], "the command reached the coroutine despite a missing export directory"


def test_retry_with_an_unreadable_state_exits_two(runner: CliRunner, tmp_path: Path) -> None:
    """SC-1.1's error half: a corrupt state file is exit 2, matching rollback.

    The exit code alone is NOT enough here, and the first draft of this test
    proved it: Click returns 2 for "No such command 'retry'" too, so a bare
    `exit_code == 2` assertion passed before the command existed at all. It
    could not distinguish the case under test from the command being absent.

    So it asserts the message names the state file, and that Click did not
    reject the invocation itself.
    """
    export_dir = _write_minimal_export(tmp_path / "export")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "state.json").write_text("{not json", encoding="utf-8")

    result = runner.invoke(main, _retry_argv(out_dir, export_dir))
    assert result.exit_code == 2, result.output
    assert "No such command" not in result.output, "the command does not exist yet"
    assert "state.json" in result.output


# ---------------------------------------------------------------------------
# ferry repair (#107 batch 10, task #340)
# ---------------------------------------------------------------------------


def _repair_argv(out_dir: Path, export_dir: Path, *extra: str) -> list[str]:
    return [
        "repair",
        str(out_dir),
        "--export-dir",
        str(export_dir),
        "--stoat-url",
        "https://api.test",
        "--token",
        "t",
        *extra,
    ]


def test_repair_exits_zero_when_nothing_is_left_failing(runner: CliRunner, tmp_path: Path) -> None:
    """The contract a script reads, alongside re-running ferry check --json."""
    export_dir = _write_minimal_export(tmp_path / "export")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    from discord_ferry.state import save_state

    save_state(MigrationState(stoat_server_id="01JSTOATSRV000000000AAA"), out_dir)

    async def _noop(*_a: Any, **_k: Any) -> RepairOutcome:
        return RepairOutcome()

    with patch("discord_ferry.cli.run_repair", new=_noop):
        result = runner.invoke(main, _repair_argv(out_dir, export_dir))
    assert result.exit_code == 0, result.output


def test_repair_exits_non_zero_when_a_message_is_still_failed(
    runner: CliRunner, tmp_path: Path
) -> None:
    """A repair that could not finish must not look like one that did."""
    export_dir = _write_minimal_export(tmp_path / "export")
    out_dir = _write_state_with_one_failure(tmp_path / "out")

    async def _leaves_it(config: Any, state: Any, exports: Any, on_event: Any) -> RepairOutcome:
        return RepairOutcome()

    with patch("discord_ferry.cli.run_repair", new=_leaves_it):
        result = runner.invoke(main, _repair_argv(out_dir, export_dir))
    assert result.exit_code == 1, result.output


def test_a_repair_dry_run_always_exits_zero(runner: CliRunner, tmp_path: Path) -> None:
    """A preview reports on a plan, not an outcome.

    Exiting non-zero here would make --dry-run unusable in a script that treats
    a non-zero code as "there is work to do, act now": the preview would look
    exactly like a failed repair.
    """
    export_dir = _write_minimal_export(tmp_path / "export")
    out_dir = _write_state_with_one_failure(tmp_path / "out")
    seen: dict[str, Any] = {}

    async def _capture(config: Any, state: Any, exports: Any, on_event: Any) -> None:
        seen["dry_run"] = config.dry_run

    with patch("discord_ferry.cli.run_repair", new=_capture):
        result = runner.invoke(main, _repair_argv(out_dir, export_dir, "--dry-run"))

    assert seen["dry_run"] is True, "the --dry-run flag never reached the config"
    assert result.exit_code == 0, (
        f"a dry run exited {result.exit_code}, which a script cannot tell from real work"
    )


def test_repair_passes_a_real_export_to_the_coroutine(runner: CliRunner, tmp_path: Path) -> None:
    """The same hole ferry retry had: an empty exports list is a silent no-op.

    A recreated channel's resend and a tail repair both resolve their messages
    out of `exports`. Given an empty list they restore nothing and report
    success.
    """
    export_dir = _write_minimal_export(tmp_path / "export")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    from discord_ferry.state import save_state

    save_state(MigrationState(stoat_server_id="01JSTOATSRV000000000AAA"), out_dir)
    seen: dict[str, Any] = {}

    async def _capture(config: Any, state: Any, exports: Any, on_event: Any) -> RepairOutcome:
        seen["exports"] = exports
        return RepairOutcome()

    with patch("discord_ferry.cli.run_repair", new=_capture):
        runner.invoke(main, _repair_argv(out_dir, export_dir))

    assert len(seen["exports"]) > 0, "an empty exports list restores nothing"


def test_repair_registers_the_token_and_the_semaphore_before_any_request(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Asserted as CALLS, never as the absence of a token from output."""
    export_dir = _write_minimal_export(tmp_path / "export")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    from discord_ferry.state import save_state

    save_state(MigrationState(stoat_server_id="01JSTOATSRV000000000AAA"), out_dir)
    order: list[str] = []

    async def _noop(*_a: Any, **_k: Any) -> RepairOutcome:
        order.append("request")
        return RepairOutcome()

    with (
        patch(
            "discord_ferry.cli.register_secret",
            side_effect=lambda *_a: order.append("register_secret"),
        ),
        patch(
            "discord_ferry.cli.init_request_semaphore",
            side_effect=lambda *_a: order.append("semaphore"),
        ),
        patch("discord_ferry.cli.run_repair", new=_noop),
    ):
        runner.invoke(main, _repair_argv(out_dir, export_dir))

    assert "register_secret" in order and "semaphore" in order
    assert order.index("register_secret") < order.index("request")
    assert order.index("semaphore") < order.index("request")


def test_repair_reports_a_refusal_and_exits_one(runner: CliRunner, tmp_path: Path) -> None:
    """CheckError reaches the operator as a sentence and a code, not a traceback.

    run_repair raises it for a dry-run state and for a state recording no
    server, and deliberately does not catch it.
    """
    export_dir = _write_minimal_export(tmp_path / "export")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    from discord_ferry.state import save_state

    save_state(MigrationState(stoat_server_id="01JSTOATSRV000000000AAA"), out_dir)

    async def _raises(*_a: Any, **_k: Any) -> None:
        from discord_ferry.errors import CheckError

        raise CheckError("cannot check a dry-run state")

    with patch("discord_ferry.cli.run_repair", new=_raises):
        result = runner.invoke(main, _repair_argv(out_dir, export_dir))

    assert result.exit_code == 1, result.output
    assert "dry-run state" in result.output


def test_repair_with_a_missing_export_directory_exits_two(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Exit 2, the path intact, and the coroutine never reached."""
    out_dir = _write_state_with_one_failure(tmp_path / "out")
    missing = tmp_path / "not-here"
    calls: list[str] = []

    async def _record(*_a: Any, **_k: Any) -> None:
        calls.append("ran")

    with patch("discord_ferry.cli.run_repair", new=_record):
        result = runner.invoke(main, _repair_argv(out_dir, missing))

    assert result.exit_code == 2, result.output
    assert str(missing) in result.output, "the path was wrapped or lost"
    assert calls == [], "repair ran despite a missing export directory"


def test_repair_exits_non_zero_when_it_declined_a_repair(runner: CliRunner, tmp_path: Path) -> None:
    """A defect repair could not fix REMAINS, so the code must say so.

    A channel with no recorded name, one missing from the export, and a forum
    index are all cases repair declines. None of them leaves a FailedMessage, so
    an exit code reading only that queue would report 0 and tell a script the
    server is whole when it is not.
    """
    export_dir = _write_minimal_export(tmp_path / "export")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    from discord_ferry.state import save_state

    save_state(MigrationState(stoat_server_id="01JSTOATSRV000000000AAA"), out_dir)

    async def _declines(config: Any, state: Any, exports: Any, on_event: Any) -> RepairOutcome:
        decline = {
            "phase": "repair",
            "type": "no_recorded_name",
            "message": "Cannot recreate channel 800000000000000001",
        }
        state.warnings.append(decline)
        return RepairOutcome(declined=[{"type": "no_recorded_name", "message": decline["message"]}])

    with patch("discord_ferry.cli.run_repair", new=_declines):
        result = runner.invoke(main, _repair_argv(out_dir, export_dir))
    assert result.exit_code == 1, (
        f"a declined repair exited {result.exit_code}, which reads as success"
    )


def test_repair_exit_code_ignores_a_stale_decline_from_an_earlier_run(
    runner: CliRunner, tmp_path: Path
) -> None:
    """#308 whole-branch review. The exit code reflects THIS run, not history.

    state.warnings is never cleared, so a not_in_export from an earlier run
    persists on disk. If the exit code rescanned that list, it would report 1
    forever, while the --json document's `declined` (scoped to this run) reports
    []. The two must agree. This run declines nothing new and fails nothing, so
    it must exit 0 and its document must show declined == [].
    """
    state = MigrationState(stoat_server_id="srv1")
    state.warnings.append({"phase": "repair", "type": "not_in_export", "message": "old run"})
    result = _invoke_repair(runner, tmp_path, RepairOutcome(), "--json", state=state)
    doc = json.loads(result.stdout)
    assert doc["declined"] == []
    assert result.exit_code == 0, (
        f"a stale decline from an earlier run made this run exit {result.exit_code}"
    )


def test_repair_still_exits_zero_on_a_documented_partial_restore(
    runner: CliRunner, tmp_path: Path
) -> None:
    """The other half, so the assertion above cannot be met by failing everything.

    merge_thread_content_not_restored names a partial restore of something
    repair DID fix, and no_discord_metadata is a degradation rather than an
    unrepaired defect. Exiting 1 on either would make the code useless for the
    strategy where it always fires.
    """
    export_dir = _write_minimal_export(tmp_path / "export")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    from discord_ferry.state import save_state

    save_state(MigrationState(stoat_server_id="01JSTOATSRV000000000AAA"), out_dir)

    async def _partial(config: Any, state: Any, exports: Any, on_event: Any) -> RepairOutcome:
        decline = {
            "phase": "repair",
            "type": "merge_thread_content_not_restored",
            "message": "thread content not restored",
        }
        state.warnings.append(decline)
        return RepairOutcome(declined=[{"type": decline["type"], "message": decline["message"]}])

    with patch("discord_ferry.cli.run_repair", new=_partial):
        result = runner.invoke(main, _repair_argv(out_dir, export_dir))
    assert result.exit_code == 0, (
        f"a documented partial restore exited {result.exit_code}, which fails every merge repair"
    )


# ---------------------------------------------------------------------------
# ferry repair --json (#308)
# ---------------------------------------------------------------------------


def _invoke_repair(
    runner: CliRunner,
    tmp_path: Path,
    outcome: RepairOutcome,
    *extra: str,
    state: MigrationState | None = None,
    post_check: CheckReport | None = None,
    post_check_error: Exception | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> Any:
    """Drive repair_cmd with a mocked run_repair outcome and post-repair check."""
    st = state if state is not None else MigrationState(stoat_server_id="srv1")
    check_mock = (
        AsyncMock(side_effect=post_check_error)
        if post_check_error is not None
        else AsyncMock(return_value=post_check if post_check is not None else CheckReport())
    )
    with (
        patch("discord_ferry.cli.register_secret", lambda *_a: None),
        patch("discord_ferry.cli.init_request_semaphore", lambda *_a: None),
        patch("discord_ferry.cli.load_state", return_value=st),
        patch("discord_ferry.cli.parse_export_directory", return_value=[]),
        patch("discord_ferry.cli.run_repair", new=AsyncMock(return_value=outcome)),
        patch("discord_ferry.migrator.verify.run_check", new=check_mock),
    ):
        return runner.invoke(
            main,
            [
                "repair",
                str(tmp_path),
                "--export-dir",
                str(tmp_path),
                "--stoat-url",
                "https://api.test",
                "--token",
                "t",
                *extra,
            ],
        )


def test_repair_json_prints_one_parseable_document(runner: CliRunner, tmp_path: Path) -> None:
    """SC-1.1, SC-4.1. Asserted by parsing, never by inspecting the string (#145)."""
    outcome = RepairOutcome(
        recreated_channels=[
            {"discord_id": "d1", "stoat_id": "s1", "name": "general", "resent_count": 2}
        ]
    )
    result = _invoke_repair(runner, tmp_path, outcome, "--json")
    doc = json.loads(result.stdout)
    assert set(doc) >= {"dry_run", "actions", "declined", "failed_messages", "check"}
    assert doc["actions"]["recreated_channels"][0]["name"] == "general"
    assert doc["check"] is not None


def test_repair_json_keeps_human_output_off_stdout(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SC-1.2. The banner and proxy notices go to stderr; stdout is pure JSON."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:8080")
    monkeypatch.setenv("COLUMNS", "200")
    outcome = RepairOutcome()
    result = _invoke_repair(runner, tmp_path, outcome, "--json")
    json.loads(result.stdout)  # stdout parses as JSON
    assert "Discord Ferry" not in result.stdout
    assert "Discord Ferry" in result.stderr


def test_repair_without_json_prints_the_banner_on_stdout(runner: CliRunner, tmp_path: Path) -> None:
    """SC-1.3. Without --json the human banner is unchanged on stdout."""
    result = _invoke_repair(runner, tmp_path, RepairOutcome())
    assert "Discord Ferry" in result.output


def test_repair_dry_run_json_has_null_check_and_exits_zero(
    runner: CliRunner, tmp_path: Path
) -> None:
    """SC-4.3. --dry-run --json emits a valid document, check null, exit 0."""
    result = _invoke_repair(runner, tmp_path, RepairOutcome(dry_run=True), "--json", "--dry-run")
    doc = json.loads(result.stdout)
    assert doc["dry_run"] is True
    assert doc["check"] is None
    assert result.exit_code == 0


def test_repair_json_survives_a_failing_post_check(runner: CliRunner, tmp_path: Path) -> None:
    """SC-4.4. A failing post-repair check yields check null + check_error, no crash."""
    result = _invoke_repair(
        runner,
        tmp_path,
        RepairOutcome(),
        "--json",
        post_check_error=MigrationError("boom"),
    )
    doc = json.loads(result.stdout)
    assert doc["check"] is None
    assert "check_error" in doc


def test_repair_exit_code_identical_with_and_without_json(
    runner: CliRunner, tmp_path: Path
) -> None:
    """SC-1.4. The --json flag never changes the exit code."""
    clean = MigrationState(stoat_server_id="srv1")
    failing = MigrationState(stoat_server_id="srv1")
    failing.failed_messages = [FailedMessage(discord_msg_id="m1", stoat_channel_id="c1", error="x")]

    for state, expected in ((clean, 0), (failing, 1)):
        plain = _invoke_repair(runner, tmp_path, RepairOutcome(), state=state)
        js = _invoke_repair(runner, tmp_path, RepairOutcome(), "--json", state=state)
        assert plain.exit_code == expected
        assert js.exit_code == expected


def test_repair_json_empty_sections_are_lists(runner: CliRunner, tmp_path: Path) -> None:
    """SC-3.3. A clean run serialises declined and failed_messages as []."""
    result = _invoke_repair(runner, tmp_path, RepairOutcome(), "--json")
    doc = json.loads(result.stdout)
    assert doc["declined"] == []
    assert doc["failed_messages"] == []


def test_repair_json_failed_messages_carry_ids_only(runner: CliRunner, tmp_path: Path) -> None:
    """SC-3.2. Residual failures expose identifying fields, never message content."""
    outcome = RepairOutcome(failed_messages=[{"discord_msg_id": "m1", "stoat_channel_id": "c1"}])
    result = _invoke_repair(runner, tmp_path, outcome, "--json")
    doc = json.loads(result.stdout)
    assert doc["failed_messages"] == [{"discord_msg_id": "m1", "stoat_channel_id": "c1"}]
    assert "content_preview" not in doc["failed_messages"][0]


def test_repair_json_stdout_survives_a_realistic_payload(runner: CliRunner, tmp_path: Path) -> None:
    """SC-5.2. Modelled on the probe #145 test: force a payload past 200 chars.

    The empty-payload tests above never wrap, which is exactly how #145 shipped
    unnoticed. This one asserts its own length so shortening it later fails loud.
    """
    long_msg = (
        "Role 'moderators' was recreated with its name and permissions. Its colour, "
        "rank, hoist setting and icon are not restored: set them by hand (see #344)."
    )
    outcome = RepairOutcome(
        recreated_channels=[
            {
                "discord_id": "d1",
                "stoat_id": "s1",
                "name": "announcements-general",
                "resent_count": 42,
            }
        ],
        declined=[{"type": "role_attributes_not_restored", "message": long_msg}],
    )
    result = _invoke_repair(runner, tmp_path, outcome, "--json")
    doc = json.loads(result.stdout)
    assert len(result.stdout) > 200
    assert doc["declined"][0]["message"] == long_msg
    assert long_msg in result.stdout  # intact on one line, no injected newline


def test_repair_json_strips_control_chars_from_stdout(runner: CliRunner, tmp_path: Path) -> None:
    """SC-5.3. A control char in a recorded name never reaches stdout."""
    outcome = RepairOutcome(
        recreated_channels=[
            {"discord_id": "d1", "stoat_id": "s1", "name": "gen\x07eral", "resent_count": 0}
        ]
    )
    result = _invoke_repair(runner, tmp_path, outcome, "--json")
    assert "\x07" not in result.stdout
    doc = json.loads(result.stdout)
    assert doc["actions"]["recreated_channels"][0]["name"] == "general"


def test_repair_json_mixed_run_is_consistent(runner: CliRunner, tmp_path: Path) -> None:
    """SC-I1. Every section populated; exit code equals the non-json run's."""
    state = MigrationState(stoat_server_id="srv1")
    state.failed_messages = [FailedMessage(discord_msg_id="m1", stoat_channel_id="c1", error="x")]
    outcome = RepairOutcome(
        recreated_channels=[
            {"discord_id": "d1", "stoat_id": "s1", "name": "general", "resent_count": 5}
        ],
        restored_tails=[{"discord_id": "d2", "stoat_id": "s2", "name": "notices"}],
        dead_letter={"drained": 2, "remaining": 1},
        declined=[{"type": "role_attributes_not_restored", "message": "colour not restored"}],
        failed_messages=[{"discord_msg_id": "m1", "stoat_channel_id": "c1"}],
    )
    post = CheckReport()
    post.add(name="c", status="fail", kind="channel_missing", detail="still gone", discord_id="d3")

    js = _invoke_repair(runner, tmp_path, outcome, "--json", state=state, post_check=post)
    plain = _invoke_repair(runner, tmp_path, outcome, state=state, post_check=post)
    doc = json.loads(js.stdout)

    assert doc["actions"]["recreated_channels"] and doc["actions"]["restored_tails"]
    assert doc["actions"]["dead_letter"] == {"drained": 2, "remaining": 1}
    assert doc["declined"] and doc["failed_messages"]
    assert doc["check"]["counts"]["fail"] == 1
    assert js.exit_code == plain.exit_code == 1


# ---------------------------------------------------------------------------
# ferry backfill-roles (#388, #482)
# ---------------------------------------------------------------------------


def _backfill_argv(out_dir: Path, export_dir: Path, *extra: str) -> list[str]:
    return [
        "backfill-roles",
        str(out_dir),
        "--export-dir",
        str(export_dir),
        "--stoat-url",
        "https://api.test",
        "--token",
        "t",
        *extra,
    ]


def _backfill_state(tmp_path: Path) -> tuple[Path, Path]:
    """A completed state plus a minimal export, the inputs backfill reads."""
    export_dir = _write_minimal_export(tmp_path / "export")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    from discord_ferry.state import save_state

    save_state(MigrationState(stoat_server_id="01JSTOATSRV000000000AAA"), out_dir)
    return out_dir, export_dir


def test_backfill_help_lists_options(runner: CliRunner) -> None:
    """SC-1.1."""
    result = runner.invoke(main, ["backfill-roles", "--help"])
    assert result.exit_code == 0
    for opt in ("--export-dir", "--stoat-url", "--token", "--dry-run"):
        assert opt in result.output


def test_backfill_missing_url_errors(runner: CliRunner, tmp_path: Path) -> None:
    """SC-1.3, no URL."""
    out_dir, export_dir = _backfill_state(tmp_path)
    result = runner.invoke(
        main,
        ["backfill-roles", str(out_dir), "--export-dir", str(export_dir), "--token", "t"],
        env={"STOAT_URL": "", "STOAT_TOKEN": ""},
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    assert "--stoat-url is required" in result.output


def test_backfill_missing_token_errors(runner: CliRunner, tmp_path: Path) -> None:
    """SC-1.3, no token."""
    out_dir, export_dir = _backfill_state(tmp_path)
    result = runner.invoke(
        main,
        [
            "backfill-roles",
            str(out_dir),
            "--export-dir",
            str(export_dir),
            "--stoat-url",
            "https://api.test",
        ],
        env={"STOAT_URL": "", "STOAT_TOKEN": ""},
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    assert "--token is required" in result.output


def test_backfill_exits_2_on_unreadable_state(runner: CliRunner, tmp_path: Path) -> None:
    """SC-1.2. No state.json in the output dir."""
    export_dir = _write_minimal_export(tmp_path / "export")
    out_dir = tmp_path / "out"
    out_dir.mkdir()  # exists, but holds no state.json
    result = runner.invoke(main, _backfill_argv(out_dir, export_dir))
    assert result.exit_code == 2, result.output


def test_backfill_exits_2_on_unreadable_export(runner: CliRunner, tmp_path: Path) -> None:
    """SC-4.4."""
    out_dir, _ = _backfill_state(tmp_path)
    missing = tmp_path / "nope"
    result = runner.invoke(main, _backfill_argv(out_dir, missing))
    assert result.exit_code == 2, result.output


def test_backfill_dry_run_exits_0_and_passes_flag(runner: CliRunner, tmp_path: Path) -> None:
    """SC-1.4."""
    out_dir, export_dir = _backfill_state(tmp_path)
    seen: dict[str, Any] = {}

    async def _capture(config: Any, state: Any, exports: Any, on_event: Any) -> None:
        seen["dry_run"] = config.dry_run

    with patch("discord_ferry.cli.run_role_backfill", new=_capture):
        result = runner.invoke(main, _backfill_argv(out_dir, export_dir, "--dry-run"))
    assert seen["dry_run"] is True
    assert result.exit_code == 0, result.output


def test_backfill_exits_0_when_clean(runner: CliRunner, tmp_path: Path) -> None:
    """SC-4.1."""
    out_dir, export_dir = _backfill_state(tmp_path)

    async def _noop(config: Any, state: Any, exports: Any, on_event: Any) -> None:
        return None

    with patch("discord_ferry.cli.run_role_backfill", new=_noop):
        result = runner.invoke(main, _backfill_argv(out_dir, export_dir))
    assert result.exit_code == 0, result.output


def test_backfill_exits_1_on_permission_refusal(runner: CliRunner, tmp_path: Path) -> None:
    """SC-4.2."""
    out_dir, export_dir = _backfill_state(tmp_path)

    async def _refused(config: Any, state: Any, exports: Any, on_event: Any) -> None:
        state.warnings.append(
            {"phase": "roles", "type": "role_ordering_not_permitted", "message": "x"}
        )

    with patch("discord_ferry.cli.run_role_backfill", new=_refused):
        result = runner.invoke(main, _backfill_argv(out_dir, export_dir))
    assert result.exit_code == 1, result.output


def test_backfill_exits_1_on_generic_failure(runner: CliRunner, tmp_path: Path) -> None:
    """SC-4.3. A failed read-back (role_ordering_failed) must not exit 0."""
    out_dir, export_dir = _backfill_state(tmp_path)

    async def _failed(config: Any, state: Any, exports: Any, on_event: Any) -> None:
        state.warnings.append({"phase": "roles", "type": "role_ordering_failed", "message": "x"})

    with patch("discord_ferry.cli.run_role_backfill", new=_failed):
        result = runner.invoke(main, _backfill_argv(out_dir, export_dir))
    assert result.exit_code == 1, result.output


def test_backfill_summary_reports_already_correct(runner: CliRunner, tmp_path: Path) -> None:
    """SC-5.2. A no-op run tells the operator the order was already correct."""
    out_dir, export_dir = _backfill_state(tmp_path)

    async def _noop(config: Any, state: Any, exports: Any, on_event: Any) -> None:
        return None

    with patch("discord_ferry.cli.run_role_backfill", new=_noop):
        result = runner.invoke(main, _backfill_argv(out_dir, export_dir))
    assert result.exit_code == 0
    assert "already correct" in result.output


def test_backfill_summary_reports_a_reorder(runner: CliRunner, tmp_path: Path) -> None:
    """SC-5.1. A reorder surfaces the ordering event and not the no-op line."""
    out_dir, export_dir = _backfill_state(tmp_path)

    async def _reorder(config: Any, state: Any, exports: Any, on_event: Any) -> None:
        from discord_ferry.core.events import MigrationEvent

        on_event(
            MigrationEvent(
                phase="roles", status="progress", message="Applied role ordering to 3 roles"
            )
        )

    with patch("discord_ferry.cli.run_role_backfill", new=_reorder):
        result = runner.invoke(main, _backfill_argv(out_dir, export_dir))
    assert result.exit_code == 0
    assert "Applied role ordering to 3 roles" in result.output
    assert "already correct" not in result.output


def test_backfill_stale_warning_from_prior_run_does_not_cause_false_failure(
    runner: CliRunner, tmp_path: Path
) -> None:
    """A role_ordering warning left in state.json by the ORIGINAL migration must
    not make a successful backfill re-run exit 1. That is the exact scenario the
    command exists for. #388 whole-branch review."""
    from discord_ferry.state import load_state, save_state

    out_dir, export_dir = _backfill_state(tmp_path)
    state = load_state(out_dir)
    state.warnings.append(
        {"phase": "roles", "type": "role_ordering_not_permitted", "message": "stale"}
    )
    save_state(state, out_dir)

    async def _noop(config: Any, state: Any, exports: Any, on_event: Any) -> None:
        return None  # this run succeeds

    with patch("discord_ferry.cli.run_role_backfill", new=_noop):
        result = runner.invoke(main, _backfill_argv(out_dir, export_dir))
    assert result.exit_code == 0, result.output
