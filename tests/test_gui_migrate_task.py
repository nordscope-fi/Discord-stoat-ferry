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
    from pathlib import Path

    from nicegui.testing import User

pytestmark = pytest.mark.nicegui_main_file("tests/nicegui_app.py")


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
