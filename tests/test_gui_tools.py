"""Tests for the GUI tool pages and their shared runner (issue #484 and children)."""

from __future__ import annotations


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
