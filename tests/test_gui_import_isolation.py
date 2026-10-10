"""Keep collected Ferry modules patchable after browser-page tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from discord_ferry.uploader import autumn

if TYPE_CHECKING:
    from nicegui.testing import User


pytestmark = pytest.mark.nicegui_main_file("tests/nicegui_app.py")


async def test_a_browser_page_test_runs_before_dotted_patch(user: User) -> None:
    """Run the cleanup that previously detached collected child modules."""
    await user.open("/tools")
    await user.should_see("Check")


def test_b_collected_uploader_stays_patchable_by_dotted_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resident Autumn module remains reachable from its package path."""

    async def fake_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("discord_ferry.uploader.autumn.asyncio.sleep", fake_sleep)

    assert autumn.__dict__["asyncio"].sleep is fake_sleep
