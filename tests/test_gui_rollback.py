"""#1678: the GUI rollback button reports a server id that differs from the saved one.

The page's config carries whatever is in the "Existing server ID" field. Rollback used
to ignore it and act on the saved server; the engine now refuses, and the page must show
that refusal rather than crash or stay silent.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from tests.stoat_ids import stoat_id

if TYPE_CHECKING:
    from pathlib import Path

    from nicegui.testing import User

pytestmark = pytest.mark.nicegui_main_file("tests/nicegui_app.py")

SAVED_SERVER = stoat_id(7801)
OTHER_SERVER = stoat_id(7802)


def _open_migrate_page_with_saved_state(
    tmp_path: Path,
    user_store: dict[str, object],
    tab_store: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    *,
    typed_server: str,
) -> None:
    from discord_ferry import gui
    from discord_ferry.state import MigrationState, save_state

    save_state(MigrationState(stoat_server_id=SAVED_SERVER), tmp_path)
    user_store.update(
        {
            "export_dir": str(tmp_path),
            "output_dir": str(tmp_path),
            "stoat_url": "https://api.test",
            "server_id": typed_server,
        }
    )
    tab_store.update({"token": "synthetic-target-token"})
    never = asyncio.Event()

    async def migration_that_waits(config: Any, *, on_event: Any) -> Any:
        await never.wait()

    monkeypatch.setattr(gui, "run_migration", migration_that_waits)


async def _click_rollback(user: User) -> None:
    from nicegui import ui

    # The page awaits the client connection before it renders; the resume card shows
    # it is ready, and its presence also means no migration has started.
    await user.should_see("Previous migration found")
    button = user.find(kind=ui.button, content="Rollback this migration")
    assert len(button.elements) == 1
    button.click()
    for _ in range(50):
        await asyncio.sleep(0.02)


async def test_a_different_server_id_shows_the_refusal(
    user: User,
    tmp_path: Path,
    user_store: dict[str, object],
    tab_store: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _open_migrate_page_with_saved_state(
        tmp_path, user_store, tab_store, monkeypatch, typed_server=OTHER_SERVER
    )
    await user.open("/migrate")
    await _click_rollback(user)
    await user.should_see("Rollback failed")
    await user.should_see("belongs to Stoat server")


async def test_the_saved_server_id_starts_the_rollback(
    user: User,
    tmp_path: Path,
    user_store: dict[str, object],
    tab_store: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: the same click reaches the engine with the saved server.

    Without it, the refusal test could pass because the button never fired.
    """
    from discord_ferry import gui

    _open_migrate_page_with_saved_state(
        tmp_path, user_store, tab_store, monkeypatch, typed_server=SAVED_SERVER
    )
    started: list[str | None] = []

    async def recording_rollback(config: Any, state: Any, **_kwargs: Any) -> Any:
        started.append(config.server_id)
        return state

    monkeypatch.setattr(gui, "run_rollback", recording_rollback)
    await user.open("/migrate")
    await _click_rollback(user)
    assert started == [SAVED_SERVER]
    await user.should_not_see("Rollback failed")
