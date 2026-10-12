"""Migrate page user-fixture tests, replacing the source-string assertions at
test_gui.py:302 and the migrate guard part of test_gui.py:482.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from nicegui import ElementFilter

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from nicegui.testing import User

pytestmark = pytest.mark.nicegui_main_file("tests/nicegui_app.py")


async def test_running_migration_survives_disconnect_and_blocks_second_page(
    user: User,
    create_user: Callable[[], User],
    user_store: dict[str, object],
    tab_store: dict[str, object],
    tmp_path: Path,
) -> None:
    """A disconnected page must not release the process-wide migration guard."""
    user_store["export_dir"] = str(tmp_path)
    user_store["output_dir"] = str(tmp_path)
    user_store["stoat_url"] = "https://example.invalid"
    tab_store["token"] = "stoat-tok"

    started = asyncio.Event()
    release = asyncio.Event()
    stopped = asyncio.Event()
    starts = 0
    active = 0

    async def fake_run_migration(config, *, on_event):  # type: ignore[no-untyped-def]
        nonlocal active, starts
        starts += 1
        active += 1
        started.set()
        try:
            await release.wait()
        finally:
            active -= 1
            stopped.set()

    second_user = create_user()
    try:
        with (
            patch("discord_ferry.gui.run_migration", fake_run_migration),
            patch("discord_ferry.gui.load_state", side_effect=FileNotFoundError),
        ):
            await user.open("/migrate")
            await asyncio.wait_for(started.wait(), timeout=1)

            first_client = user._client  # type: ignore[attr-defined]
            socket_id = next(iter(first_client._socket_to_document_id))
            first_client.handle_disconnect(socket_id)

            await second_user.open("/migrate")
            await second_user.should_see("Migration already running")

            assert starts == 1
            assert active == 1
    finally:
        release.set()
        await asyncio.wait_for(stopped.wait(), timeout=1)
        await second_user.http_client.aclose()


async def test_disconnected_resume_choice_does_not_hold_migration_guard(
    user: User,
    create_user: Callable[[], User],
    user_store: dict[str, object],
    tab_store: dict[str, object],
    tmp_path: Path,
) -> None:
    """A page that never chose Resume or Start Fresh does not own the engine."""
    from discord_ferry.state import MigrationState

    user_store["export_dir"] = str(tmp_path)
    user_store["output_dir"] = str(tmp_path)
    user_store["stoat_url"] = "https://example.invalid"
    tab_store["token"] = "stoat-tok"
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    previous_state = MigrationState(current_phase="messages")

    started = asyncio.Event()
    release = asyncio.Event()
    stopped = asyncio.Event()
    starts = 0

    async def fake_run_migration(config, *, on_event):  # type: ignore[no-untyped-def]
        nonlocal starts
        starts += 1
        started.set()
        try:
            await release.wait()
        finally:
            stopped.set()

    second_user = create_user()
    try:
        with (
            patch("discord_ferry.gui.run_migration", fake_run_migration),
            patch("discord_ferry.gui.load_state", return_value=previous_state),
        ):
            await user.open("/migrate")
            await user.should_see("Previous migration found")

            first_client = user._client  # type: ignore[attr-defined]
            socket_id = next(iter(first_client._socket_to_document_id))
            first_client.handle_disconnect(socket_id)
            await asyncio.sleep(0)

            await second_user.open("/migrate")
            await second_user.should_see("Previous migration found")
            second_user.find("Resume").click()
            await asyncio.wait_for(started.wait(), timeout=1)

            assert starts == 1
    finally:
        release.set()
        if started.is_set():
            await asyncio.wait_for(stopped.wait(), timeout=1)
        await second_user.http_client.aclose()


async def test_two_resume_pages_claim_guard_when_the_engine_starts(
    user: User,
    create_user: Callable[[], User],
    user_store: dict[str, object],
    tab_store: dict[str, object],
    tmp_path: Path,
) -> None:
    """Two waiting pages may open, but only the first choice starts an engine."""
    from discord_ferry.state import MigrationState

    user_store["export_dir"] = str(tmp_path)
    user_store["output_dir"] = str(tmp_path)
    user_store["stoat_url"] = "https://example.invalid"
    tab_store["token"] = "stoat-tok"
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    previous_state = MigrationState(current_phase="messages")

    started = asyncio.Event()
    release = asyncio.Event()
    stopped = asyncio.Event()
    starts = 0

    async def fake_run_migration(config, *, on_event):  # type: ignore[no-untyped-def]
        nonlocal starts
        starts += 1
        started.set()
        try:
            await release.wait()
        finally:
            stopped.set()

    second_user = create_user()
    try:
        with (
            patch("discord_ferry.gui.run_migration", fake_run_migration),
            patch("discord_ferry.gui.load_state", return_value=previous_state),
        ):
            await user.open("/migrate")
            await second_user.open("/migrate")
            await user.should_see("Previous migration found")
            await second_user.should_see("Previous migration found")

            user.find("Resume").click()
            await asyncio.wait_for(started.wait(), timeout=1)
            second_user.find("Resume").click()
            await second_user.should_see("Migration already running")

            assert starts == 1
    finally:
        release.set()
        if started.is_set():
            await asyncio.wait_for(stopped.wait(), timeout=1)
        await second_user.http_client.aclose()


async def test_resume_choice_survives_temporary_socket_disconnect(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
    tmp_path: Path,
) -> None:
    """A socket reconnect before client deletion keeps the waiting page usable."""
    from discord_ferry.state import MigrationState

    user_store["export_dir"] = str(tmp_path)
    user_store["output_dir"] = str(tmp_path)
    user_store["stoat_url"] = "https://example.invalid"
    tab_store["token"] = "stoat-tok"
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    previous_state = MigrationState(current_phase="messages")

    started = asyncio.Event()
    release = asyncio.Event()
    stopped = asyncio.Event()

    async def fake_run_migration(config, *, on_event):  # type: ignore[no-untyped-def]
        started.set()
        try:
            await release.wait()
        finally:
            stopped.set()

    try:
        with (
            patch("discord_ferry.gui.run_migration", fake_run_migration),
            patch("discord_ferry.gui.load_state", return_value=previous_state),
        ):
            await user.open("/migrate")
            await user.should_see("Previous migration found")

            client = user._client  # type: ignore[attr-defined]
            socket_id, document_id = next(iter(client._socket_to_document_id.items()))
            client.handle_disconnect(socket_id)
            await client.handle_handshake(
                "reconnected-socket",
                user.tab_id,
                user.tab_id,
                document_id,
                None,
            )

            user.find("Resume").click()
            await asyncio.wait_for(started.wait(), timeout=1)
    finally:
        release.set()
        if started.is_set():
            await asyncio.wait_for(stopped.wait(), timeout=1)


@pytest.mark.parametrize("outcome", ["complete", "cancel", "failure"])
async def test_migration_guard_releases_after_terminal_outcome(
    user: User,
    create_user: Callable[[], User],
    user_store: dict[str, object],
    tab_store: dict[str, object],
    tmp_path: Path,
    outcome: str,
) -> None:
    """Completion, cancellation, and handled failure all allow the next run."""
    from discord_ferry.errors import MigrationError

    user_store["export_dir"] = str(tmp_path)
    user_store["stoat_url"] = "https://example.invalid"
    tab_store["token"] = "stoat-tok"

    first_returned = asyncio.Event()
    second_started = asyncio.Event()
    release_second = asyncio.Event()
    second_stopped = asyncio.Event()
    starts = 0

    async def fake_run_migration(config, *, on_event):  # type: ignore[no-untyped-def]
        nonlocal starts
        starts += 1
        if starts == 1:
            try:
                if outcome == "cancel":
                    config.cancel_event.set()
                elif outcome == "failure":
                    raise MigrationError("planned failure")
                return
            finally:
                first_returned.set()

        second_started.set()
        try:
            await release_second.wait()
        finally:
            second_stopped.set()

    second_user = create_user()
    try:
        with (
            patch("discord_ferry.gui.run_migration", fake_run_migration),
            patch("discord_ferry.gui.load_state", side_effect=FileNotFoundError),
        ):
            await user.open("/migrate")
            await asyncio.wait_for(first_returned.wait(), timeout=1)

            for _ in range(50):
                if "token" not in tab_store:
                    break
                await asyncio.sleep(0.01)
            assert "token" not in tab_store

            tab_store["token"] = "stoat-tok"
            await asyncio.sleep(0)
            await second_user.open("/migrate")
            await asyncio.wait_for(second_started.wait(), timeout=1)
            await second_user.should_see("Migration Running")

            assert starts == 2
    finally:
        release_second.set()
        if second_started.is_set():
            await asyncio.wait_for(second_stopped.wait(), timeout=1)
        await second_user.http_client.aclose()


async def test_rollback_dialog_appears_on_confirm_rollback_event(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
    tmp_path: Path,
) -> None:
    """SC-3.1: a confirm_rollback event opens the rollback dialog.

    Guards the case arm at gui.py:1648-1652. Mocks run_migration to emit the
    event directly, following the pattern at test_gui.py:729-734.
    """
    from discord_ferry.core.events import MigrationEvent
    from discord_ferry.review import RollbackSummary

    user_store["export_dir"] = str(tmp_path)
    user_store["stoat_url"] = "https://example.invalid"
    tab_store["token"] = "stoat-tok"

    summary = RollbackSummary(
        stoat_server_id="server-1",
        stoat_server_name="Test Server",
        channels_to_delete=[],
        untracked_ferry_suspect=[],
        roles_to_delete=[],
        emoji_to_delete=[],
        categories_to_clean=0,
        autumn_orphan_count=0,
        has_failures_from_prior_run=False,
    )

    migration_started = False

    async def fake_run_migration(config, *, on_event):  # type: ignore[no-untyped-def]
        nonlocal migration_started
        migration_started = True
        on_event(
            MigrationEvent(
                phase="rollback",
                status="confirm_rollback",
                message="Confirm rollback",
                detail={"summary": summary},
            )
        )

    with (
        patch("discord_ferry.gui.run_migration", fake_run_migration),
        # ./ferry-output/state.json may exist from a prior run, which makes
        # the page wait for a resume/fresh choice. Force load_state to fail
        # so the page starts the migration immediately.
        patch("discord_ferry.gui.load_state", side_effect=FileNotFoundError),
    ):
        await user.open("/migrate")
        # Poll until the mock runs.
        for _ in range(50):
            if migration_started:
                break
            await asyncio.sleep(0.1)

        assert migration_started, "fake_run_migration was never called"

    # Search for the rollback dialog button, bypassing the visibility filter.
    with user._client:  # type: ignore[attr-defined]
        rollback_btns = list(ElementFilter(content="Roll back", only_visible=False))
    assert rollback_btns, "Roll back button not found: _show_rollback_dialog did not run"


async def test_missing_tab_token_bounces_from_migrate(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
    tmp_path: Path,
) -> None:
    """SC-3.2: no tab token -> bounce, never show Migration Running.

    Guards the tab-token re-check at gui.py:1228.
    """
    user_store["export_dir"] = str(tmp_path)
    user_store["stoat_url"] = "https://example.invalid"
    tab_store.pop("token", None)

    await user.open("/migrate")
    await user.should_not_see("Migration Running")
