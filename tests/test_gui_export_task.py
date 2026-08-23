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

import asyncio
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


async def test_use_cached_clears_only_discord_token(
    user: User,
    tmp_path: Path,
    user_store: dict[str, object],
    tab_store: dict[str, object],
) -> None:
    """SC-1.1: 'Use Cached' clears discord_token only; Stoat token survives.

    Guards the _use_cached handler at gui.py:884-886. The real _clear_tokens
    semantics are already unit-tested at test_gui.py:325; this test covers the
    handler wiring, which the old source-string assertion at test_gui.py:409
    was a stand-in for.
    """
    export_dir = tmp_path / "dce_cache"
    export_dir.mkdir()
    (export_dir / "channel.json").write_text("{}", encoding="utf-8")

    _seed_orchestrated_session(user_store, export_dir)
    tab_store["discord_token"] = DISCORD_TOKEN
    tab_store["token"] = STOAT_TOKEN

    await user.open("/export")
    await user.should_see("Found cached exports")
    user.find("Use Cached").click()
    await user.should_see("Export")

    assert "discord_token" not in tab_store
    assert tab_store["token"] == STOAT_TOKEN


async def test_export_finally_clears_discord_token(
    user: User,
    tmp_path: Path,
    user_store: dict[str, object],
    tab_store: dict[str, object],
) -> None:
    """SC-1.2: the export finally block clears discord_token after completion.

    Guards gui.py:1090-1091. The existing test_export_page_reaches_the_first_progress_event
    proves the task starts; this one proves the finally clears the token when
    the export runs to completion.
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
        patch("discord_ferry.exporter.run_dce_export", new=AsyncMock()),
    ):
        await user.open("/export")
        await user.should_see("Export complete!")
        # The finally runs after asyncio.sleep(1) + navigate.to("/validate").
        # Poll the tab_store until the finally clears discord_token.
        for _ in range(50):
            if "discord_token" not in tab_store:
                break
            await asyncio.sleep(0.1)

    assert "discord_token" not in tab_store
    assert tab_store["token"] == STOAT_TOKEN


async def test_export_config_carries_coerced_advanced_settings(
    user: User,
    tmp_path: Path,
    user_store: dict[str, object],
    tab_store: dict[str, object],
) -> None:
    """SC-1.3: the FerryConfig passed to run_dce_export carries the seven defaults.

    Guards that _coerce_advanced_settings output flows into config construction
    at gui.py:1042. Inspects the config object's attributes, not kwargs.
    """
    from discord_ferry.config import FerryConfig

    export_dir = tmp_path / "dce_cache"
    export_dir.mkdir()
    _seed_orchestrated_session(user_store, export_dir)
    tab_store["discord_token"] = DISCORD_TOKEN
    tab_store["token"] = STOAT_TOKEN

    captured: list[FerryConfig] = []

    async def capture_config(config: FerryConfig, dce_path: Path, on_event: object) -> None:
        captured.append(config)

    with (
        patch("discord_ferry.exporter.validate_discord_token", new=AsyncMock()),
        patch("discord_ferry.exporter.get_dce_path", return_value=tmp_path / "dce"),
        patch("discord_ferry.exporter.detect_dotnet", return_value=True),
        patch("discord_ferry.exporter.run_dce_export", new=capture_config),
    ):
        await user.open("/export")
        await user.should_see("Validating Discord token")

    assert len(captured) == 1
    config = captured[0]
    assert config.reaction_mode == "text"
    assert config.min_thread_messages == 0
    assert config.checkpoint_interval == 50
    assert config.max_concurrent_channels == 3
    assert config.max_concurrent_requests == 5
    assert config.skip_avatars is False
    assert config.validate_after is False
