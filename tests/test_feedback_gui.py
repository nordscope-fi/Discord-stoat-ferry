"""Simulated-user tests for the in-app feedback flow."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

import aiohttp
import pytest

from discord_ferry.errors import MigrationError
from discord_ferry.state import MigrationState

if TYPE_CHECKING:
    from nicegui.testing import User

pytestmark = pytest.mark.nicegui_main_file("tests/nicegui_app.py")

FIXTURES_DIR = Path(__file__).parent / "fixtures"


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
    return SimpleNamespace(url="https://github.com/nordscope-fi/Discord-stoat-ferry/issues/84")


def _seed_validate_page(store: dict[str, object], tmp_path: Path) -> None:
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    shutil.copy(FIXTURES_DIR / "simple_channel.json", export_dir / "channel.json")
    store["export_dir"] = str(export_dir)
    store["stoat_url"] = "https://example.invalid"


def _seed_migrate_page(
    user_store: dict[str, object],
    tab_store: dict[str, object],
    tmp_path: Path,
) -> None:
    user_store["export_dir"] = str(tmp_path)
    user_store["stoat_url"] = "https://example.invalid"
    tab_store["token"] = "stoat-token"


async def _assert_one_visible_feedback_action(user: User, route: str) -> None:
    await user.should_see("Feedback")
    actions = user.find("Feedback").elements
    assert len(actions) == 1
    assert actions.pop().enabled is True
    user.find("Feedback").click()
    await user.should_see("Share feedback")
    assert user.back_history[-1] == route


async def test_feedback_action_visible_on_setup(user: User) -> None:
    await user.open("/")

    await _assert_one_visible_feedback_action(user, "/")


async def test_feedback_action_visible_on_validation_review(
    user: User,
    user_store: dict[str, object],
    tmp_path: Path,
) -> None:
    _seed_validate_page(user_store, tmp_path)
    await user.open("/validate")

    await _assert_one_visible_feedback_action(user, "/validate")


async def test_feedback_action_preserves_active_migration_state(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
    tmp_path: Path,
) -> None:
    from discord_ferry.core.events import MigrationEvent

    _seed_migrate_page(user_store, tab_store, tmp_path)
    state = MigrationState(current_phase="messages")
    release = asyncio.Event()
    captured: dict[str, object] = {"calls": 0}

    async def fake_run_migration(config: object, *, on_event: object) -> None:
        captured["calls"] = int(captured["calls"]) + 1
        captured["task"] = asyncio.current_task()
        captured["state"] = state
        on_event(
            MigrationEvent(
                phase="messages",
                status="progress",
                message="Sending messages",
                current=3,
                total=10,
            )
        )
        await release.wait()

    try:
        with (
            patch("discord_ferry.gui.run_migration", fake_run_migration),
            patch("discord_ferry.gui.load_state", side_effect=FileNotFoundError),
        ):
            await user.open("/migrate")
            await user.should_see("Messages: 3")
            task = captured["task"]
            state_identity = id(captured["state"])

            await _assert_one_visible_feedback_action(user, "/migrate")

            assert captured["calls"] == 1
            assert captured["task"] is task
            assert id(captured["state"]) == state_identity
            assert state.current_phase == "messages"
            await user.should_see("Messages: 3")
    finally:
        release.set()
        await asyncio.sleep(0)


async def test_feedback_action_visible_after_migration_completion(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
    tmp_path: Path,
) -> None:
    from discord_ferry.core.events import MigrationEvent

    _seed_migrate_page(user_store, tab_store, tmp_path)

    async def fake_run_migration(config: object, *, on_event: object) -> None:
        on_event(MigrationEvent(phase="report", status="completed", message="Done"))

    with (
        patch("discord_ferry.gui.run_migration", fake_run_migration),
        patch("discord_ferry.gui.load_state", side_effect=FileNotFoundError),
    ):
        await user.open("/migrate")
        await user.should_see("Migration Complete")

        await _assert_one_visible_feedback_action(user, "/migrate")


async def test_feedback_action_visible_after_handled_migration_failure(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
    tmp_path: Path,
) -> None:
    _seed_migrate_page(user_store, tab_store, tmp_path)

    async def fake_run_migration(config: object, *, on_event: object) -> None:
        raise MigrationError("Visible failure")

    with (
        patch("discord_ferry.gui.run_migration", fake_run_migration),
        patch("discord_ferry.gui.load_state", side_effect=FileNotFoundError),
    ):
        await user.open("/migrate")
        await user.should_see("Migration failed: Visible failure")

        await _assert_one_visible_feedback_action(user, "/migrate")


def _set_value(user: User, label: str, value: object) -> None:
    user.find(label).elements.pop().set_value(value)


async def _open_feedback_report(user: User) -> None:
    user.find("Feedback").click()
    await user.should_see("1. Report")


async def _fill_short_report(
    user: User,
    *,
    description: str = "A short report",
    contact: str = "",
) -> None:
    _set_value(user, "Description", description)
    _set_value(user, "Private contact email", contact)
    user.find("Continue to diagnostics").click()
    await user.should_see("2. Diagnostics")
    user.find("Continue to public review").click()
    await user.should_see("3. Public review")


async def _reconnect(user: User) -> None:
    client = user.client
    assert client is not None
    socket_id, document_id = next(iter(client._socket_to_document_id.items()))
    client.handle_disconnect(socket_id)
    await asyncio.sleep(0)
    client.handle_handshake("test-reconnect", document_id, next_message_id=None)
    await asyncio.sleep(0)


async def test_feedback_dialog_reviews_all_fields_and_returns_the_public_url(
    user: User,
) -> None:
    client = _FakeFeedbackClient(_receipt())
    with patch("discord_ferry.feedback_gui.FeedbackClient", return_value=client):
        await user.open("/")
        await _open_feedback_report(user)

        _set_value(user, "Type", "idea")
        _set_value(user, "Description", "Original idea")
        _set_value(user, "Expected result", "Expected result text")
        _set_value(user, "Reproduction steps", "Reproduction text")
        _set_value(user, "Private contact email", "person@example.com")
        user.find("Continue to diagnostics").click()
        await user.should_see("2. Diagnostics")

        _set_value(user, "Include diagnostics", True)
        _set_value(user, "Last error", "Edited diagnostic error")
        _set_value(user, "Recent log text", "Edited log line")
        await user.should_see("names or message content may remain")
        _set_value(user, "I understand the diagnostics will be public", True)
        user.find("Continue to public review").click()
        await user.should_see("3. Public review")
        await user.should_see("Original idea")
        await user.should_see("Edited diagnostic error")
        await user.should_see("Edited log line")

        user.find("Edit report").click()
        await user.should_see("1. Report")
        _set_value(user, "Description", "Edited idea")
        user.find("Continue to diagnostics").click()
        await user.should_see("2. Diagnostics")
        _set_value(user, "I understand the diagnostics will be public", True)
        user.find("Continue to public review").click()
        await user.should_see("Edited idea")

        _set_value(user, "I understand this report will be public on GitHub", True)
        user.find("Send feedback").click()
        await user.should_see("4. Result")
        await user.should_see("Feedback shared")
        await user.should_see("https://github.com/nordscope-fi/Discord-stoat-ferry/issues/84")

    assert len(client.submissions) == 1
    submission = client.submissions[0]
    assert submission["description"] == "Edited idea"
    assert submission["expected"] == "Expected result text"
    assert submission["reproduction"] == "Reproduction text"
    assert submission["contact_email"] == "person@example.com"
    assert submission["public_acknowledged"] is True
    assert submission["diagnostics_acknowledged"] is True


async def test_feedback_dialog_declines_diagnostics_and_sends_none(user: User) -> None:
    client = _FakeFeedbackClient(_receipt())
    with patch("discord_ferry.feedback_gui.FeedbackClient", return_value=client):
        await user.open("/")
        await _open_feedback_report(user)
        await _fill_short_report(user)
        _set_value(user, "I understand this report will be public on GitHub", True)
        user.find("Send feedback").click()
        await user.should_see("Feedback shared")

    assert client.submissions[0]["diagnostics"] is None
    assert client.submissions[0]["diagnostics_acknowledged"] is False


async def test_feedback_dialog_prefills_handled_error_only_in_bug_diagnostics(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
    tmp_path: Path,
) -> None:
    _seed_migrate_page(user_store, tab_store, tmp_path)

    async def fake_run_migration(config: object, *, on_event: object) -> None:
        raise MigrationError("Visible failure")

    with (
        patch("discord_ferry.gui.run_migration", fake_run_migration),
        patch("discord_ferry.gui.load_state", side_effect=FileNotFoundError),
    ):
        await user.open("/migrate")
        await user.should_see("Migration failed: Visible failure")
        await _open_feedback_report(user)

        assert user.find("Type").elements.pop().value == "bug"
        assert user.find("Description").elements.pop().value == ""
        _set_value(user, "Description", "The migration stopped")
        user.find("Continue to diagnostics").click()
        await user.should_see("2. Diagnostics")
        assert user.find("Include diagnostics").elements.pop().value is True
        await user.should_see("Stage: setup")
        await user.should_see("Visible failure")


async def test_feedback_recovery_copies_saves_and_retries_only_when_chosen(
    user: User,
    tmp_path: Path,
) -> None:
    client = _FakeFeedbackClient(aiohttp.ClientConnectionError("offline"), _receipt())
    copied: list[str] = []
    save_path = tmp_path / "saved feedback.md"
    with (
        patch("discord_ferry.feedback_gui.FeedbackClient", return_value=client),
        patch("discord_ferry.feedback_gui.ui.clipboard.write", copied.append),
        patch("discord_ferry.feedback_gui._pick_save_path", return_value=save_path),
    ):
        await user.open("/")
        await _open_feedback_report(user)
        await _fill_short_report(
            user,
            description="Keep this draft",
            contact="private@example.com",
        )
        _set_value(user, "I understand this report will be public on GitHub", True)
        user.find("Send feedback").click()
        await user.should_see("Feedback could not be sent")
        assert len(client.submissions) == 1

        user.find("Copy public draft").click()
        user.find("Save draft").click()
        await user.should_see("Draft saved")
        assert len(client.submissions) == 1
        assert copied == [client.submissions[0]["public_body"]]
        assert save_path.read_text() == client.submissions[0]["public_body"]
        assert "private@example.com" not in save_path.read_text()
        assert save_path.stat().st_mode & 0o777 == 0o600

        user.find("Retry").click()
        await user.should_see("Feedback shared")

    assert len(client.submissions) == 2
    assert client.submissions[0]["request_id"] == client.submissions[1]["request_id"]


async def test_feedback_cancel_closes_dialog_and_cancels_inflight_send(user: User) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class BlockingClient:
        async def __aenter__(self) -> BlockingClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def submit(self, draft: object) -> object:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    with patch("discord_ferry.feedback_gui.FeedbackClient", return_value=BlockingClient()):
        await user.open("/")
        await _open_feedback_report(user)
        await _fill_short_report(user)
        _set_value(user, "I understand this report will be public on GitHub", True)
        user.find("Send feedback").click()
        await asyncio.wait_for(started.wait(), timeout=1)
        await user.should_see("Sending feedback")
        user.find("Close feedback").click()
        await asyncio.wait_for(cancelled.wait(), timeout=1)
        await user.should_not_see("Sending feedback")


async def test_feedback_reconnect_preserves_editing_only_for_current_page_client(
    user: User,
    user_store: dict[str, object],
) -> None:
    await user.open("/")
    await _open_feedback_report(user)
    _set_value(user, "Description", "Draft before reconnect")
    _set_value(user, "Private contact email", "private@example.com")

    await _reconnect(user)

    assert user.find("Description").elements.pop().value == "Draft before reconnect"
    assert user.find("Private contact email").elements.pop().value == "private@example.com"
    assert "private@example.com" not in repr(user_store)

    await user.open("/")
    await _open_feedback_report(user)
    assert user.find("Description").elements.pop().value == ""
    assert user.find("Private contact email").elements.pop().value == ""


async def test_feedback_reconnect_preserves_public_review_without_storage(
    user: User,
    user_store: dict[str, object],
) -> None:
    await user.open("/")
    await _open_feedback_report(user)
    await _fill_short_report(
        user,
        description="Review survives reconnect",
        contact="review@example.com",
    )

    await _reconnect(user)

    await user.should_see("Review survives reconnect")
    assert "review@example.com" not in repr(user_store)
    user.find("Edit report").click()
    assert user.find("Private contact email").elements.pop().value == "review@example.com"


async def test_feedback_reconnect_cancels_inflight_send_and_requires_retry(
    user: User,
) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class ReconnectingClient(_FakeFeedbackClient):
        async def submit(self, draft: object) -> object:
            self.submissions.append(
                {
                    **asdict(draft),
                    "public_body": draft.render_public_body(),
                }
            )
            if len(self.submissions) == 1:
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            return _receipt()

    client = ReconnectingClient()
    with patch("discord_ferry.feedback_gui.FeedbackClient", return_value=client):
        await user.open("/")
        await _open_feedback_report(user)
        await _fill_short_report(user, description="Inflight reconnect draft")
        _set_value(user, "I understand this report will be public on GitHub", True)
        user.find("Send feedback").click()
        await asyncio.wait_for(started.wait(), timeout=1)

        await _reconnect(user)

        await asyncio.wait_for(cancelled.wait(), timeout=1)
        await user.should_see("Feedback could not be sent")
        await user.should_see("## Report\nInflight reconnect draft")
        await asyncio.sleep(0.1)
        assert len(client.submissions) == 1

        user.find("Retry").click()
        await user.should_see("Feedback shared")

    assert len(client.submissions) == 2
    assert client.submissions[0]["request_id"] == client.submissions[1]["request_id"]


async def test_feedback_reconnect_keeps_failed_draft_without_automatic_retry(
    user: User,
) -> None:
    client = _FakeFeedbackClient(aiohttp.ClientConnectionError("offline"), _receipt())
    with patch("discord_ferry.feedback_gui.FeedbackClient", return_value=client):
        await user.open("/")
        await _open_feedback_report(user)
        await _fill_short_report(user, description="Failed reconnect draft")
        _set_value(user, "I understand this report will be public on GitHub", True)
        user.find("Send feedback").click()
        await user.should_see("Feedback could not be sent")

        await _reconnect(user)

        await user.should_see("## Report\nFailed reconnect draft")
        await user.should_see("Copy public draft")
        await user.should_see("Save draft")
        await asyncio.sleep(0.1)
        assert len(client.submissions) == 1

        user.find("Retry").click()
        await user.should_see("Feedback shared")

    assert client.submissions[0]["request_id"] == client.submissions[1]["request_id"]
