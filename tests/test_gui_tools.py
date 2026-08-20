"""Tests for the GUI tool pages and their shared runner (issue #484 and children)."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

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
