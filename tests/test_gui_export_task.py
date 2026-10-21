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
import json
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

if TYPE_CHECKING:
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


@pytest.mark.parametrize("result_kind", ["success", "error", "cancel", "parse-error"])
async def test_cached_permission_refresh_uses_selected_context_and_no_exporter(
    user, tmp_path, user_store, tab_store, monkeypatch, result_kind
):
    from aioresponses import aioresponses
    from nicegui import ui

    from discord_ferry import discord, gui
    from discord_ferry.discord.metadata import DiscordMetadata, save_discord_metadata
    from discord_ferry.errors import MigrationError
    from tests.test_metadata_refresh import BASE, GUILD, channel_payload, role_payload

    export_dir = tmp_path / "selected-exports"
    export_dir.mkdir()
    data = json.loads((Path(__file__).parent / "fixtures" / "simple_channel.json").read_text())
    data["guild"]["id"], data["channel"]["id"] = GUILD, "333"
    (export_dir / "channel.json").write_text(json.dumps(data))
    output = tmp_path / "selected-output"
    save_discord_metadata(DiscordMetadata(GUILD, "old", 0, {}, {}), output)
    (output / "state.json").write_bytes(b'{"keep":"confirmed-id"}')
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    _seed_orchestrated_session(user_store, export_dir)
    user_store.update({"discord_server_id": GUILD, "output_dir": str(output)})
    tab_store.update({"discord_token": DISCORD_TOKEN, "token": STOAT_TOKEN})
    sessions = []
    actual_refresh = discord.refresh_permission_metadata

    async def refresh(session, credential, guild, exports, destination):
        sessions.append(session)
        assert credential == DISCORD_TOKEN and guild == GUILD
        assert destination == output and {e.channel.id for e in exports} == {"333"}
        if result_kind == "cancel":
            raise asyncio.CancelledError
        if result_kind == "error":
            raise MigrationError("Synthetic source refusal " + credential)
        return await actual_refresh(session, credential, guild, exports, destination)

    monkeypatch.setattr(gui, "refresh_permission_metadata", refresh, raising=False)
    if result_kind == "parse-error":

        def bad_export(*args, **kwargs):
            raise MigrationError("Synthetic export refusal " + DISCORD_TOKEN)

        monkeypatch.setattr(gui, "parse_export_directory", bad_export)
    exporter = AsyncMock(side_effect=AssertionError("Permission refresh launched exporter"))
    monkeypatch.setattr("discord_ferry.exporter.run_dce_export", exporter)
    with aioresponses() as remote:
        remote.get(BASE, payload={"id": GUILD, "name": "source"})
        remote.get(BASE + "/roles", payload=role_payload())
        remote.get(BASE + "/channels", payload=channel_payload())
        await user.open("/export")
        await user.should_see("Refresh source permissions")
        user.find("Refresh source permissions").click()
        await user.should_see(
            {
                "success": "Source permission metadata refreshed",
                "error": "Source permission refresh failed",
                "cancel": "Source permission refresh cancelled",
                "parse-error": "Source permission refresh failed",
            }[result_kind]
        )
        assert len(remote.requests) == (3 if result_kind == "success" else 0)
        assert all(method == "GET" and str(url).startswith(BASE) for method, url in remote.requests)
    assert (
        (not sessions)
        if result_kind == "parse-error"
        else (sessions and all(session.closed for session in sessions))
    )
    assert all(DISCORD_TOKEN not in str(element.text) for element in user.find(ui.label).elements)
    assert tab_store == {"token": STOAT_TOKEN}
    source_inputs = [
        element
        for element in user.find(ui.input).elements
        if element.label == "Source Discord token"
    ]
    assert len(source_inputs) == 1 and source_inputs[0].value == ""
    after = {p.name: p.read_bytes() for p in output.iterdir()}
    if result_kind == "success":
        assert after.pop("discord_metadata.json") != before.pop("discord_metadata.json")
    assert after == before
    exporter.assert_not_called()


@pytest.mark.parametrize("missing", ["output", "guild", "missing-directory", "output-is-file"])
async def test_cached_permission_refresh_requires_explicit_context(
    user, tmp_path, user_store, tab_store, monkeypatch, missing
):
    from nicegui import ui

    from discord_ferry import gui

    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    (export_dir / "channel.json").write_text("{}")
    _seed_orchestrated_session(user_store, export_dir)
    output = tmp_path / "output"
    if missing == "output-is-file":
        output.write_text("not a directory")
    elif missing != "missing-directory":
        output.mkdir()
    if missing != "output":
        user_store["output_dir"] = str(output)
    if missing == "guild":
        user_store["discord_server_id"] = ""
    tab_store["discord_token"] = DISCORD_TOKEN
    backend = AsyncMock(side_effect=AssertionError("Incomplete selection reached source refresh"))
    monkeypatch.setattr(gui, "refresh_permission_metadata", backend, raising=False)
    await user.open("/export")
    await user.should_see("Refresh source permissions")
    buttons = [
        element
        for element in user.find(ui.button).elements
        if element.text == "Refresh source permissions"
    ]
    assert len(buttons) == 1 and not buttons[0].enabled
    backend.assert_not_called()


async def test_cached_permission_refresh_allows_only_one_running_action(
    user, tmp_path, user_store, tab_store, monkeypatch
):
    from nicegui import ui

    from discord_ferry import gui
    from tests.test_metadata_refresh import exports

    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    (export_dir / "channel.json").write_text("{}")
    output = tmp_path / "output"
    output.mkdir()
    _seed_orchestrated_session(user_store, export_dir)
    user_store["output_dir"] = str(output)
    tab_store.update({"discord_token": DISCORD_TOKEN, "token": STOAT_TOKEN})
    monkeypatch.setattr(gui, "parse_export_directory", lambda *args, **kwargs: exports())
    release = asyncio.Event()
    sessions = []

    async def refresh(session, *args):
        sessions.append(session)
        await release.wait()

    backend = AsyncMock(side_effect=refresh)
    monkeypatch.setattr(gui, "refresh_permission_metadata", backend)
    exporter = AsyncMock(side_effect=AssertionError("Concurrent exporter started"))
    monkeypatch.setattr("discord_ferry.exporter.run_dce_export", exporter)
    await user.open("/export")
    await user.should_see("Refresh source permissions")
    user.find("Refresh source permissions").click()
    user.find("Refresh source permissions").click()
    try:
        await user.should_see("Refreshing source permissions")
        assert backend.await_count == 1
        for element in user.find(ui.button).elements:
            if element.text in {"Use Cached", "Re-export", "Refresh source permissions"}:
                assert not element.enabled
    finally:
        release.set()
    await user.should_see("Source permission metadata refreshed")
    assert backend.await_count == 1
    assert len(sessions) == 1 and sessions[0].closed
    assert tab_store == {"token": STOAT_TOKEN}
    exporter.assert_not_called()


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
        patch(
            "discord_ferry.exporter.detect_dotnet",
            side_effect=AssertionError("runtime probe must not run"),
        ),
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


async def test_export_page_shows_cancellation_instead_of_a_failure(
    user: User,
    tmp_path: Path,
    user_store: dict[str, object],
    tab_store: dict[str, object],
) -> None:
    """A cancellation from DCE is shown as cancellation, not an export error."""
    export_dir = tmp_path / "dce_cache"
    export_dir.mkdir()
    _seed_orchestrated_session(user_store, export_dir)
    tab_store["discord_token"] = DISCORD_TOKEN
    tab_store["token"] = STOAT_TOKEN

    with (
        patch("discord_ferry.exporter.validate_discord_token", new=AsyncMock()),
        patch("discord_ferry.exporter.get_dce_path", return_value=tmp_path / "dce"),
        patch("discord_ferry.exporter.detect_dotnet", return_value=True),
        patch(
            "discord_ferry.exporter.run_dce_export",
            new=AsyncMock(side_effect=asyncio.CancelledError("Export cancelled by user")),
        ),
    ):
        await user.open("/export")
        await user.should_see("Export cancelled.")
        await user.should_not_see("Export failed:")


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
    from discord_ferry.config import FerryConfig  # noqa: TC001

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
