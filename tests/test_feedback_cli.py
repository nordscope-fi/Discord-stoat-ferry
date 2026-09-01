"""Tests for the interactive command-line feedback flow."""

from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import aiohttp
from click.testing import CliRunner

from discord_ferry.cli import main
from discord_ferry.errors import MigrationError
from discord_ferry.feedback import (
    Architecture,
    FeedbackDiagnostics,
    FeedbackInterface,
    FeedbackKind,
    FeedbackStage,
    OperatingSystem,
)
from discord_ferry.state import MigrationState

if TYPE_CHECKING:
    from pathlib import Path


class _FakeFeedbackClient:
    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.submissions: list[dict[str, object]] = []

    async def __aenter__(self) -> _FakeFeedbackClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def submit(self, draft: object) -> object:
        self.submissions.append(
            {
                **asdict(draft),
                "public_body": draft.render_public_body(),
            }
        )
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _receipt() -> object:
    return SimpleNamespace(url="https://github.com/nordscope-fi/Discord-stoat-ferry/issues/42")


def _diagnostics() -> FeedbackDiagnostics:
    return FeedbackDiagnostics(
        ferry_version="2.37.3",
        operating_system=OperatingSystem.MACOS,
        architecture=Architecture.ARM64,
        interface=FeedbackInterface.CLI,
        stage=FeedbackStage.SETUP,
        last_error="Original error",
        log_excerpt="Original log line",
    )


def _invoke_feedback(input_text: str, client: _FakeFeedbackClient) -> object:
    with patch("discord_ferry.feedback_cli.FeedbackClient", return_value=client):
        return CliRunner().invoke(main, ["feedback"], input=input_text)


def test_feedback_help_is_listed_at_the_root() -> None:
    result = CliRunner().invoke(main, ["--help"])

    assert result.exit_code == 0
    assert "feedback" in result.output


def test_feedback_help_explains_public_and_private_choices() -> None:
    result = CliRunner().invoke(main, ["feedback", "--help"])

    assert result.exit_code == 0
    for text in (
        "Bug",
        "Idea",
        "General",
        "public on GitHub",
        "diagnostics",
        "anonymous",
    ):
        assert text in result.output


def test_feedback_startup_does_not_build_migration_config() -> None:
    with (
        patch("discord_ferry.cli.run_feedback_cli", new=AsyncMock()) as run_feedback,
        patch("discord_ferry.cli._build_config") as build_config,
    ):
        result = CliRunner().invoke(main, ["feedback"])

    assert result.exit_code == 0
    run_feedback.assert_awaited_once_with()
    build_config.assert_not_called()


def test_feedback_flow_accepts_the_shortest_anonymous_report() -> None:
    client = _FakeFeedbackClient(_receipt())

    result = _invoke_feedback(
        "Bug\nThe window stopped responding\n\n\n\nn\nContinue\ny\ny\n",
        client,
    )

    assert result.exit_code == 0, result.output
    assert "https://github.com/nordscope-fi/Discord-stoat-ferry/issues/42" in result.output
    assert len(client.submissions) == 1
    submission = client.submissions[0]
    assert submission["kind"] is FeedbackKind.BUG
    assert submission["description"] == "The window stopped responding"
    assert submission["expected"] is None
    assert submission["reproduction"] is None
    assert submission["contact_email"] is None
    assert submission["diagnostics"] is None
    assert submission["public_acknowledged"] is True


def test_feedback_flow_reviews_diagnostics_and_all_public_edits() -> None:
    client = _FakeFeedbackClient(_receipt())
    user_input = "\n".join(
        (
            "Idea",
            "Original report",
            "Original expected",
            "Original steps",
            "person@example.com",
            "y",
            "y",
            "Edit",
            "Edited error",
            "",
            "Include",
            "y",
            "Edit",
            "Report",
            "Edited report",
            "Edit",
            "Expected result",
            "Edited expected",
            "Edit",
            "Reproduction steps",
            "Edited steps",
            "Continue",
            "y",
            "y",
            "",
        )
    )

    with (
        patch("discord_ferry.feedback_cli.FeedbackClient", return_value=client),
        patch("discord_ferry.feedback_cli.build_diagnostics", return_value=_diagnostics()),
    ):
        result = CliRunner().invoke(main, ["feedback"], input=user_input)

    assert result.exit_code == 0, result.output
    assert "names or message content may remain" in result.output
    assert "Ferry version: 2.37.3" in result.output
    submission = client.submissions[0]
    assert submission["kind"] is FeedbackKind.IDEA
    assert submission["description"] == "Edited report"
    assert submission["expected"] == "Edited expected"
    assert submission["reproduction"] == "Edited steps"
    assert submission["contact_email"] == "person@example.com"
    assert submission["diagnostics_acknowledged"] is True
    diagnostics = submission["diagnostics"]
    assert diagnostics["last_error"] == "Edited error"
    assert diagnostics["log_excerpt"] is None


def test_feedback_flow_declines_available_diagnostics() -> None:
    client = _FakeFeedbackClient(_receipt())

    with (
        patch("discord_ferry.feedback_cli.FeedbackClient", return_value=client),
        patch("discord_ferry.feedback_cli.build_diagnostics") as build,
    ):
        result = CliRunner().invoke(
            main,
            ["feedback"],
            input="General\nThank you\n\n\n\nn\nContinue\ny\ny\n",
        )

    assert result.exit_code == 0, result.output
    build.assert_not_called()
    assert client.submissions[0]["diagnostics"] is None


def test_feedback_failure_retry_keeps_request_id_and_gets_a_new_attempt() -> None:
    client = _FakeFeedbackClient(aiohttp.ClientConnectionError("offline"), _receipt())

    result = _invoke_feedback(
        "Bug\nRetry this\n\n\n\nn\nContinue\ny\ny\nRetry\n",
        client,
    )

    assert result.exit_code == 0, result.output
    assert len(client.submissions) == 2
    assert client.submissions[0]["request_id"] == client.submissions[1]["request_id"]
    assert "offline" in result.output
    assert "issues/42" in result.output


def test_feedback_failure_prints_the_complete_public_draft_then_cancels() -> None:
    client = _FakeFeedbackClient(aiohttp.ClientConnectionError("offline"))

    result = _invoke_feedback(
        (
            "General\nPrintable report\nExpected text\nSteps text\n\nn\n"
            "Continue\ny\ny\nPrint\nCancel\n"
        ),
        client,
    )

    assert result.exit_code == 0, result.output
    assert "## Report\nPrintable report" in result.output
    assert "## Expected result\nExpected text" in result.output
    assert "## Reproduction steps\nSteps text" in result.output
    assert len(client.submissions) == 1


def test_feedback_failure_saves_only_after_a_path_is_chosen(
    tmp_path: Path,
) -> None:
    client = _FakeFeedbackClient(aiohttp.ClientConnectionError("offline"))
    path = tmp_path / "my feedback.md"

    result = _invoke_feedback(
        (f"Bug\nSave this\n\n\nprivate@example.com\nn\nContinue\ny\ny\nSave\n{path}\nn\nCancel\n"),
        client,
    )

    assert result.exit_code == 0, result.output
    assert path.read_text() == client.submissions[0]["public_body"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert "private@example.com" not in path.read_text()


def test_feedback_failure_edit_returns_to_review_before_resubmitting() -> None:
    client = _FakeFeedbackClient(aiohttp.ClientConnectionError("offline"), _receipt())

    result = _invoke_feedback(
        (
            "Bug\nBefore failure\n\n\n\nn\nContinue\ny\ny\nEdit\nReport\n"
            "After failure\nContinue\ny\ny\n"
        ),
        client,
    )

    assert result.exit_code == 0, result.output
    assert len(client.submissions) == 2
    assert client.submissions[0]["description"] == "Before failure"
    assert client.submissions[1]["description"] == "After failure"
    assert client.submissions[0]["request_id"] == client.submissions[1]["request_id"]


def test_feedback_cancel_stops_before_a_client_or_request() -> None:
    with patch("discord_ferry.feedback_cli.FeedbackClient") as client_factory:
        result = CliRunner().invoke(
            main,
            ["feedback"],
            input="Bug\nDo not send\n\n\n\nn\nCancel\n",
        )

    assert result.exit_code == 0, result.output
    client_factory.assert_not_called()


def _assert_failure_hint(result: object, after: str) -> None:
    assert result.exit_code != 0 or after == "Probe failed"
    assert result.output.count("ferry feedback") == 1
    assert result.output.index(after) < result.output.index("ferry feedback")


def test_migration_failure_hint_does_not_start_feedback(tmp_path: Path) -> None:
    with (
        patch(
            "discord_ferry.cli.run_migration",
            new=AsyncMock(side_effect=MigrationError("Migration stopped")),
        ),
        patch("discord_ferry.cli.run_feedback_cli", new=AsyncMock()) as feedback,
    ):
        result = CliRunner().invoke(
            main,
            [
                "migrate",
                "--stoat-url",
                "https://api.test",
                "--token",
                "token",
                "--export-dir",
                str(tmp_path),
            ],
        )

    _assert_failure_hint(result, "Migration stopped")
    feedback.assert_not_awaited()


def test_validation_failure_hint_follows_the_original_error(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["validate", str(tmp_path)])

    _assert_failure_hint(result, "No valid DCE JSON files found")


def test_rollback_failure_hint_follows_the_original_error(tmp_path: Path) -> None:
    state = MigrationState(stoat_server_id="01JSTOATSRV000000000AAA")
    with (
        patch("discord_ferry.cli.load_state", return_value=state),
        patch(
            "discord_ferry.cli.run_rollback",
            new=AsyncMock(side_effect=MigrationError("Rollback stopped")),
        ),
    ):
        result = CliRunner().invoke(
            main,
            [
                "rollback",
                "--output-dir",
                str(tmp_path),
                "--stoat-url",
                "https://api.test",
                "--token",
                "token",
                "--yes",
            ],
        )

    _assert_failure_hint(result, "Rollback stopped")


def test_probe_failure_hint_follows_the_failed_check(tmp_path: Path) -> None:
    from discord_ferry.migrator.probe import ProbeCheck, ProbeReport

    report = ProbeReport([ProbeCheck("connection", "fail", "Probe failed")])
    with patch(
        "discord_ferry.migrator.probe.run_probe",
        new=AsyncMock(return_value=report),
    ):
        result = CliRunner().invoke(
            main,
            [
                "probe",
                "--stoat-url",
                "https://api.test",
                "--token",
                "token",
                "--test-server-id",
                "server",
            ],
        )

    _assert_failure_hint(result, "Probe failed")


def test_check_failure_hint_follows_the_failed_report(tmp_path: Path) -> None:
    from discord_ferry.migrator.verify import CheckReport

    report = CheckReport()
    report.add(
        name="channel",
        status="fail",
        kind="channel_missing",
        detail="Check failed",
    )
    with (
        patch("discord_ferry.cli.load_state", return_value=MigrationState()),
        patch(
            "discord_ferry.migrator.verify.run_check",
            new=AsyncMock(return_value=report),
        ),
    ):
        result = CliRunner().invoke(
            main,
            [
                "check",
                str(tmp_path),
                "--stoat-url",
                "https://api.test",
                "--token",
                "token",
            ],
        )

    _assert_failure_hint(result, "Check failed")


def test_retry_failure_hint_follows_the_failed_result(tmp_path: Path) -> None:
    state = MigrationState(stoat_server_id="01JSTOATSRV000000000AAA")
    state.failed_messages = [SimpleNamespace()]

    async def leave_failed(*args: object) -> None:
        args[-1](SimpleNamespace(status="error", message="Retry failed"))

    with (
        patch("discord_ferry.cli.load_state", return_value=state),
        patch("discord_ferry.cli.parse_export_directory", return_value=[object()]),
        patch("discord_ferry.cli.run_retry_failed", new=leave_failed),
    ):
        result = CliRunner().invoke(
            main,
            [
                "retry",
                str(tmp_path),
                "--export-dir",
                str(tmp_path),
                "--stoat-url",
                "https://api.test",
                "--token",
                "token",
            ],
        )

    _assert_failure_hint(result, "Retry failed")


def test_repair_failure_hint_follows_the_failed_result(tmp_path: Path) -> None:
    from discord_ferry.migrator.verify import RepairOutcome

    state = MigrationState(stoat_server_id="01JSTOATSRV000000000AAA")
    state.failed_messages = [SimpleNamespace()]

    async def leave_failed(*args: object) -> RepairOutcome:
        args[-1](SimpleNamespace(status="error", message="Repair failed"))
        return RepairOutcome()

    with (
        patch("discord_ferry.cli.load_state", return_value=state),
        patch("discord_ferry.cli.parse_export_directory", return_value=[object()]),
        patch("discord_ferry.cli.run_repair", new=leave_failed),
    ):
        result = CliRunner().invoke(
            main,
            [
                "repair",
                str(tmp_path),
                "--export-dir",
                str(tmp_path),
                "--stoat-url",
                "https://api.test",
                "--token",
                "token",
            ],
        )

    _assert_failure_hint(result, "Repair failed")


def test_build_failure_hint_follows_the_original_error() -> None:
    with patch(
        "discord_ferry.core.engine.run_build",
        new=AsyncMock(side_effect=MigrationError("Build stopped")),
    ):
        result = CliRunner().invoke(
            main,
            [
                "build",
                "--template",
                "gaming",
                "--stoat-url",
                "https://api.test",
                "--token",
                "token",
            ],
        )

    _assert_failure_hint(result, "Build stopped")


def test_failure_hint_is_absent_from_help_and_usage_errors(tmp_path: Path) -> None:
    help_result = CliRunner().invoke(main, ["validate", "--help"])
    usage_result = CliRunner().invoke(main, ["check", str(tmp_path)])

    assert "ferry feedback" not in help_result.output
    assert "ferry feedback" not in usage_result.output
