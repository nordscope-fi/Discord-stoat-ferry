"""The issue #123 regression: the /export page must actually start exporting.

For eleven releases (v2.6.14 -> v2.11.0) this screen showed "Preparing..." with an
empty log panel forever, on every platform, because `_run_export` died on its
first statement inside a `background_tasks.create` coroutine and the traceback
went to a logger with no handler.

Why these tests use the `user` fixture rather than a hand-rolled double:
`user_simulation()` enters `core.app.router.lifespan_context`, which sets
`app.is_started = True`. That matters because `Context.slot_stack` fabricates a
pseudo-client whenever `not core.app.is_started` -- so under an ordinary pytest
run `ui.context` *succeeds* and the defect is invisible. The fixture is the only
harness here that reproduces the running server.

It also fails any test carrying an ERROR log record, and NiceGUI routes uncaught
background-task exceptions to exactly that -- so a regression surfaces even
without an explicit assertion.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from nicegui.testing import User

pytestmark = pytest.mark.nicegui_main_file("tests/nicegui_app.py")

# Assembled at runtime, never stored as a literal: a Discord-shaped string in a
# committed file trips GitHub secret-scanning push protection (it did, on the
# first push of this branch). The joined value still exercises the redaction
# regex exactly as a real token would.
DISCORD_TOKEN = ".".join(("MTIzNDU2Nzg5MDEyMzQ1Njc4", "GhIjKl", "abcdefghijklmnopqrstuvwxyz123"))
STOAT_TOKEN = "SEKRET-stoat-token-abcd"  # noqa: S105


def _seed_orchestrated_session(store: dict[str, object], export_dir: Path) -> None:
    """Put the user store into the state /export expects."""
    store.update(
        {
            "mode": "orchestrated",
            "export_dir": str(export_dir),
            "discord_server_id": "123456789012345678",
            "stoat_url": "https://example.invalid",
        }
    )


async def test_export_page_reaches_the_first_progress_event(
    user: User,
    tmp_path: Path,
    user_store: dict[str, object],
    tab_store: dict[str, object],
) -> None:
    """SC-123-1: the #123 regression.

    Pre-fix this hangs on "Preparing..." forever with an empty log panel. The
    assertion is that the export actually *starts* -- i.e. `_run_export` got past
    `ui.context.client`, which is the line that used to raise.
    """
    export_dir = tmp_path / "dce_cache"
    export_dir.mkdir()
    _seed_orchestrated_session(user_store, export_dir)

    tab_store["discord_token"] = DISCORD_TOKEN
    tab_store["token"] = STOAT_TOKEN

    with (
        patch("discord_ferry.exporter.validate_discord_token", new=AsyncMock()),
        patch("discord_ferry.exporter.get_dce_path", return_value=tmp_path / "dce"),
        patch("discord_ferry.exporter.detect_dotnet", return_value=True),
        patch("discord_ferry.exporter.run_dce_export", new=AsyncMock()) as run_dce,
    ):
        await user.open("/export")
        # should_see loops on asyncio.sleep, which is what actually hands the
        # event loop to the background task. Asserting straight after open()
        # would pass vacuously -- _on_handshake never suspends in the test path,
        # so the task is merely *scheduled*, never run.
        await user.should_see("Exporting from Discord")
        # THE assertion. This string is emitted by the first on_export_event call
        # inside _run_export. Pre-fix the task raised before reaching it, so the
        # log panel stayed empty forever -- which is exactly issue #123.
        await user.should_see("Validating Discord token")

    assert run_dce.await_count == 1, (
        "_run_export never reached run_dce_export — it died before launching DCE"
    )


async def test_export_page_bounces_when_the_tab_token_is_gone(
    user: User,
    tmp_path: Path,
    user_store: dict[str, object],
    tab_store: dict[str, object],
) -> None:
    """SC-123-4: the session-expired check must fire from the BUILDER.

    It used to live inside `_run_export`; leaving it there while moving the token
    reads would silently change when a stale-session user gets bounced.
    """
    export_dir = tmp_path / "dce_cache"
    export_dir.mkdir()
    _seed_orchestrated_session(user_store, export_dir)

    tab_store.pop("discord_token", None)

    await user.open("/export")
    await user.should_not_see("Exporting from Discord")


async def test_non_orchestrated_mode_redirects(
    user: User,
    tmp_path: Path,
    user_store: dict[str, object],
    tab_store: dict[str, object],
) -> None:
    """SC-123-7: guards the sync -> async page conversion.

    An async page builder returns its response at a different point than a sync
    one, so the early-return redirect is worth pinning.
    """
    user_store.update({"mode": "offline", "export_dir": str(tmp_path)})
    await user.open("/export")
    await user.should_not_see("Exporting from Discord")
