"""Tests for the GUI tool pages and their shared runner (issue #484 and children)."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from nicegui.testing import User

# The User-fixture tests below need the routes re-registered inside the
# simulation; nicegui_app.py reloads both gui and gui_tools for that.
pytestmark = pytest.mark.nicegui_main_file("tests/nicegui_app.py")

# A realistic-length opaque Stoat token, so the redaction test can actually fail
# (a token too short to trigger the mask would pass a broken implementation).
_TOKEN = "01F8MECHZX3TBDFGH4JKLMNPQR_opaque_base64url_like_secret_value"


def test_repair_failure_set_is_shared_and_exact() -> None:
    """SC-1.8: the repair pass-or-fail set is one shared constant, exact."""
    from discord_ferry.migrator.verify import UNREPAIRED_WARNING_TYPES

    assert (
        frozenset({"no_recorded_name", "not_in_export", "forum_index_not_repairable"})
        == UNREPAIRED_WARNING_TYPES
    )


def test_prepare_registers_secret_before_semaphore(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """SC-1.2: the token is registered before anything else the helper does."""
    from discord_ferry import gui_tools

    calls: list[str] = []
    api = gui_tools._api
    monkeypatch.setattr(gui_tools, "register_secret", lambda *a, **k: calls.append("secret"))
    monkeypatch.setattr(api, "init_request_semaphore", lambda *a, **k: calls.append("sem"))
    monkeypatch.setattr(gui_tools, "_semaphore_is_set", lambda: False)
    monkeypatch.setattr(gui_tools, "format_proxy_notices", lambda: calls.append("proxy") or [])

    gui_tools.prepare_tool_call("tok")

    assert calls[0] == "secret"


def test_prepare_inits_semaphore_only_when_unset(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """SC-1.4: a read-only page must not swap the semaphore mid-flight."""
    from discord_ferry import gui_tools

    monkeypatch.setattr(gui_tools, "register_secret", lambda *a, **k: None)
    monkeypatch.setattr(gui_tools, "format_proxy_notices", lambda: [])

    inited: list[int] = []
    api = gui_tools._api
    monkeypatch.setattr(api, "init_request_semaphore", lambda *a, **k: inited.append(1))

    monkeypatch.setattr(gui_tools, "_semaphore_is_set", lambda: True)
    gui_tools.prepare_tool_call("tok")
    assert inited == []  # already set: not re-inited

    monkeypatch.setattr(gui_tools, "_semaphore_is_set", lambda: False)
    gui_tools.prepare_tool_call("tok")
    assert inited == [1]  # unset: inited once


def test_prepare_returns_proxy_notices(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """SC-1.5: the helper surfaces proxy notices for the page to render."""
    from discord_ferry import gui_tools

    monkeypatch.setattr(gui_tools, "register_secret", lambda *a, **k: None)
    monkeypatch.setattr(gui_tools, "_semaphore_is_set", lambda: True)
    monkeypatch.setattr(gui_tools, "format_proxy_notices", lambda: ["proxy: on"])

    assert gui_tools.prepare_tool_call("tok") == ["proxy: on"]


def test_safe_push_masks_token() -> None:
    """SC-1.3: a token in a log-widget line is masked at the push site.

    register_secret alone protects the Python-logging sink; the ui.log widget is
    a separate sink the formatter never sees, and check/probe do no redaction of
    their own. Proven by scratchpad/proto_redaction.py.
    """
    from discord_ferry import gui_tools
    from discord_ferry.core.security import register_secret, reset_secret_registry

    reset_secret_registry()
    register_secret("stoat", _TOKEN)
    pushed: list[str] = []

    gui_tools._safe_push(pushed.append, f"[ERROR] boom: {_TOKEN}")

    assert _TOKEN not in pushed[0]  # masked at the push site
    assert _TOKEN in f"[ERROR] boom: {_TOKEN}"  # control: an unsanitized push would leak it
    reset_secret_registry()


def test_run_tool_error_path_sanitizes_and_reports_none(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """run_tool routes an exception to a sanitized log line and hands the callback None."""
    from discord_ferry import gui_tools
    from discord_ferry.core.security import register_secret, reset_secret_registry

    reset_secret_registry()
    register_secret("stoat", _TOKEN)

    # Run the background coroutine synchronously instead of scheduling it.
    monkeypatch.setattr(gui_tools.background_tasks, "create", lambda coro: asyncio.run(coro))

    pushed: list[str] = []
    done: list[object] = []

    async def _boom() -> None:
        raise RuntimeError(f"failed with {_TOKEN}")

    gui_tools.run_tool(contextlib.nullcontext(), pushed.append, _boom, done.append)

    assert done == [None]  # callback got None on error
    assert pushed and _TOKEN not in pushed[0]  # error line was sanitized
    reset_secret_registry()


async def test_tools_landing_lists_tools(user: User) -> None:
    """SC-1.1: /tools is reachable and lists every tool."""
    await user.open("/tools")
    for name in (
        "Check",
        "Repair",
        "Retry",
        "Probe",
        "Blueprint export",
        "Build",
        "Validate",
        "Stats",
        "TLS check",
    ):
        await user.should_see(name)


def test_status_colour_covers_every_check_status() -> None:
    """SC-1.7: every CheckReport status maps to a colour, none falls through."""
    from discord_ferry import gui_tools
    from discord_ferry.migrator.verify import STATUSES

    for status in STATUSES:
        assert status in gui_tools._STATUS_COLOURS
    # probe uses ok/warn/fail, a subset already covered above
    for status in ("ok", "warn", "fail"):
        assert gui_tools._status_colour(status)
    # an unknown status resolves to a defined default, never a crash
    assert gui_tools._status_colour("nonsense")


def test_check_rows_one_per_result_with_status_and_detail() -> None:
    """SC-2.1: the check table has a row per result carrying status and detail."""
    from discord_ferry import gui_tools
    from discord_ferry.migrator.verify import CheckReport

    report = CheckReport()
    report.add(name="general", status="ok", kind="channel_present", detail="present")
    report.add(name="mods", status="fail", kind="channel_missing", detail="missing on target")

    rows = gui_tools._check_rows(report)

    assert len(rows) == 2
    assert rows[0]["name"] == "general"
    assert rows[0]["status"] == "ok"
    assert rows[0]["detail"] == "present"
    assert rows[1]["colour"] == gui_tools._status_colour("fail")
    assert report.counts()["fail"] == 1


async def test_check_page_bounces_without_token(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
) -> None:
    """SC-1.6: the session-expired guard fires from the page builder."""
    user_store["stoat_url"] = "https://example.invalid"
    tab_store.pop("token", None)

    await user.open("/tools/check")
    await user.should_see("Session expired")
    await user.should_not_see("Run check")


async def test_check_page_streams_banners_and_renders_table(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    """SC-2.2: the two banners reach the log, then the table renders."""
    from discord_ferry.core.events import MigrationEvent
    from discord_ferry.migrator.verify import CheckReport

    user_store["stoat_url"] = "https://example.invalid"
    user_store["output_dir"] = str(tmp_path)
    tab_store["token"] = _TOKEN

    report = CheckReport()
    report.add(name="general", status="ok", kind="channel_present", detail="present")

    async def fake_check(stoat_url, token, state, on_event, *, session=None):  # type: ignore[no-untyped-def]
        on_event(
            MigrationEvent(phase="check", status="started", message="Checking server structure...")
        )
        on_event(
            MigrationEvent(phase="check", status="started", message="Checking each channel...")
        )
        return report

    with (
        patch("discord_ferry.gui_tools.load_state", return_value=object()),
        patch("discord_ferry.gui_tools.run_check", new=fake_check),
    ):
        await user.open("/tools/check")
        user.find("Run check").click()
        await user.should_see("Checking server structure")  # first banner streamed
        await user.should_see("1 ok")  # the report's counts line rendered after the run


def test_check_verdict_matches_has_failures() -> None:
    """SC-2.4 / SC-I1: the GUI verdict tracks report.has_failures, same as the CLI."""
    from discord_ferry import gui_tools
    from discord_ferry.migrator.verify import CheckReport

    clean = CheckReport()
    clean.add(name="general", status="ok", kind="channel_present", detail="present")
    failing = CheckReport()
    failing.add(name="mods", status="fail", kind="channel_missing", detail="missing")

    clean_label, _ = gui_tools._check_verdict(clean)
    fail_label, _ = gui_tools._check_verdict(failing)

    assert clean.has_failures is False
    assert failing.has_failures is True
    assert clean_label != fail_label
    # parity: the verdict is a pure function of has_failures
    assert ("problem" in fail_label.lower()) and ("passed" in clean_label.lower())


async def test_check_page_shows_cannot_check_on_checkerror(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    """SC-2.3: a CheckError (dry-run or server-less state) shows a clear message."""
    from discord_ferry.errors import CheckError

    user_store["stoat_url"] = "https://example.invalid"
    user_store["output_dir"] = str(tmp_path)
    tab_store["token"] = _TOKEN

    async def raises_check_error(*a, **k):  # type: ignore[no-untyped-def]
        raise CheckError("state is a dry run, nothing was migrated")

    with (
        patch("discord_ferry.gui_tools.load_state", return_value=object()),
        patch("discord_ferry.gui_tools.run_check", new=raises_check_error),
    ):
        await user.open("/tools/check")
        user.find("Run check").click()
        await user.should_see("Cannot check this migration")


def test_check_rows_sanitizes_server_controlled_text() -> None:
    """Chunk-2 review: the table is a rendered sink, so name/detail are sanitized.

    A token in a server-controlled field would otherwise reach the table, which
    the logging formatter does not cover (same class as the log-widget sink).
    """
    from discord_ferry import gui_tools
    from discord_ferry.core.security import register_secret, reset_secret_registry
    from discord_ferry.migrator.verify import CheckReport

    reset_secret_registry()
    register_secret("stoat", _TOKEN)
    report = CheckReport()
    report.add(
        name=f"chan-{_TOKEN}",
        status="fail",
        kind="channel_missing",
        detail=f"detail with {_TOKEN} in it",
    )

    rows = gui_tools._check_rows(report)

    assert _TOKEN not in rows[0]["name"]
    assert _TOKEN not in rows[0]["detail"]
    # control: the token really was in the source fields
    assert _TOKEN in report.results[0].detail
    reset_secret_registry()
