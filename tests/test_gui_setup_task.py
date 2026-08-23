"""Setup page user-fixture tests, replacing the source-string assertions at
test_gui.py:465 and test_gui.py:597.

The setup page had no user-fixture coverage. These tests open /, fill the form,
click Continue, and assert on store state, which the old inspect.getsource
assertions could never verify.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from nicegui.elements.input import Input
from nicegui.elements.number import Number

if TYPE_CHECKING:
    from pathlib import Path

    from nicegui.testing import User

pytestmark = pytest.mark.nicegui_main_file("tests/nicegui_app.py")

_EXPOSED_DEFAULTS = {
    "reaction_mode": "text",
    "min_thread_messages": 0,
    "checkpoint_interval": 50,
    "max_concurrent_channels": 3,
    "max_concurrent_requests": 5,
    "skip_avatars": False,
    "validate_after": False,
    "incremental": False,
}


def _find_input(user: User, label: str) -> Input | Number:
    """Find an Input or Number by label, filtering out non-input matches (e.g. Labels)."""
    for el in user.find(label).elements:
        if isinstance(el, (Input, Number)):
            return el  # type: ignore[return-value]
    raise AssertionError(f"no Input/Number found with label {label!r}")


async def test_tokens_go_to_tab_store_not_user_store(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
) -> None:
    """SC-2.1: tokens land in the memory-only tab store, never the disk-backed user store."""
    await user.open("/")

    _find_input(user, "Stoat user token").set_value("stoat-tok")
    _find_input(user, "Discord token").set_value("discord-tok")
    _find_input(user, "Discord server ID").set_value("123456789012345678")
    user.find("I acknowledge").click()
    user.find("Continue").click()

    await user.should_see("Exporting from Discord")

    assert tab_store["token"] == "stoat-tok"
    assert tab_store["discord_token"] == "discord-tok"
    assert "token" not in user_store
    assert "discord_token" not in user_store


async def test_exposed_settings_persisted_at_continue_click(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
) -> None:
    """SC-2.2: the seven exposed settings keys are written to user_store at Continue."""
    await user.open("/")

    _find_input(user, "Stoat user token").set_value("stoat-tok")
    _find_input(user, "Discord token").set_value("discord-tok")
    _find_input(user, "Discord server ID").set_value("123456789012345678")
    user.find("I acknowledge").click()
    user.find("Continue").click()

    await user.should_see("Exporting from Discord")

    for key in _EXPOSED_DEFAULTS:
        assert key in user_store, f"missing key: {key}"


async def test_token_inputs_do_not_pre_fill_from_storage(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
) -> None:
    """SC-2.3: token fields are empty on page load, never pre-filled from disk."""
    user_store["token"] = "old-stoat-tok"
    user_store["discord_token"] = "old-discord-tok"

    await user.open("/")

    stoat_input = _find_input(user, "Stoat user token")
    discord_input = _find_input(user, "Discord token")
    assert stoat_input.value == ""
    assert discord_input.value == ""


async def test_official_service_warning_fires_for_high_concurrency(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
    tmp_path: Path,
) -> None:
    """SC-2.4: high concurrency on the official Stoat service triggers a warning.

    Guards gui.py:799-808. Uses offline mode so no Discord token or ToS is
    needed; the warning fires at Continue-click before any navigation.
    """
    # Start in offline mode so the export-folder input is visible immediately.
    user_store["mode"] = "offline"
    # Pre-seed high concurrency so the warning fires at Continue-click.
    # The advanced-options expansion is collapsed by default; the warning
    # reads from storage via _coerce_advanced_settings, not the UI widgets.
    user_store["max_concurrent_channels"] = 6

    await user.open("/")

    _find_input(user, "Stoat user token").set_value("stoat-tok")
    _find_input(user, "Export folder path").set_value(str(tmp_path / "exports"))

    user.find("Continue").click()

    await user.should_see("Raising concurrency on the official Stoat service")
