"""The acknowledgement gate on /validate (issue #143).

An earlier draft of this design claimed a NiceGUI page closure cannot be
reached from a test, and made this a manual check. That was wrong:
conftest.py registers nicegui.testing.user_plugin and nicegui_app.py
re-registers the routes. Getting that wrong would have left the entire
user-visible fix uncovered, which is how the one-click export in #123 shipped
dead for eleven releases.

Both states are asserted, disabled before the tick and enabled after, because
`bind_enabled_from` propagates at bind time. A binding that does not work
leaves Start Migration disabled forever, which is #143 reproduced by the fix
for #143, and only the second half of that pair can see it.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from nicegui.testing import User

pytestmark = pytest.mark.nicegui_main_file("tests/nicegui_app.py")

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _seed_export(store: dict[str, object], tmp_path: Path, fixture: str) -> Path:
    """Copy one DCE fixture into its own directory and point the session at it.

    A dedicated subdirectory, never tmp_path itself: the autouse logging fixture
    puts ferry.log there, and an export directory should hold export files only.
    """
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    shutil.copy(FIXTURES_DIR / fixture, export_dir / "channel.json")
    store["export_dir"] = str(export_dir)
    store["stoat_url"] = "https://example.invalid"
    return export_dir


async def test_start_migration_is_blocked_until_acknowledged(
    user: User, user_store: dict[str, object], tmp_path: Path
) -> None:
    """Killing: a checkbox that is never created, a binding that is never made
    (which leaves the button dead forever, reproducing #143 with the fix for
    #143), a binding wired backwards, and a gui.py that keeps its own inline
    type check instead of calling the shared classifier.
    """
    _seed_export(user_store, tmp_path, "markdown_rendered.json")

    await user.open("/validate")

    # The classifier's own sentence must be on screen. This is what pins the
    # GUI to acknowledgement_required rather than to a second inline check.
    await user.should_see("will arrive as text")
    # The consequence phrase alone is not enough: the per-file warning row
    # inside the Warnings expansion shares it, so that assertion passes even
    # with no reason label at all. Only acknowledgement_required's return value
    # counts the files, so this fragment is the one that pins the shared
    # classifier to the screen.
    await user.should_see("export file(s) have mentions")

    button = user.find("Start Migration").elements.pop()
    assert button.enabled is False, (
        "Start Migration should start disabled while the warning is unacknowledged"
    )

    user.find("I understand").click()
    assert button.enabled is True, (
        "ticking the acknowledgement must enable Start Migration; a binding that "
        "does not propagate leaves the user in the #143 dead end"
    )


async def test_a_clean_export_needs_no_acknowledgement(
    user: User, user_store: dict[str, object], tmp_path: Path
) -> None:
    """Killing: a checkbox created unconditionally, which would make every user
    tick a box to migrate a perfectly good export.
    """
    _seed_export(user_store, tmp_path, "simple_channel.json")

    await user.open("/validate")

    assert user.find("Start Migration").elements.pop().enabled is True
    await user.should_not_see("I understand")


async def test_missing_export_dir_bounces_from_validate(
    user: User,
    user_store: dict[str, object],
) -> None:
    """SC-4.1: no export_dir in user_store -> bounce, no export summary shown.

    Guards the validate guard at gui.py:1111. The old source-string assertion
    at test_gui.py:482 checked that the guard reads export_dir/stoat_url, not
    storage.get('token'). This test exercises the real guard.
    """
    # user_store is empty: no export_dir, no stoat_url
    await user.open("/validate")
    await user.should_not_see("Export:")
