"""Tests for the renderer fallback and FERRY_NO_NATIVE (issue #128).

The failure being guarded: Ferry's window opens with an engine that cannot run ES
modules, so NiceGUI's interface never starts and the user gets a blank window with no
route past it. Two properties make the fix work, and both are asserted here rather than
assumed, because both were established by measurement against a real window:

  1. Ordinary (non-module) JavaScript still runs in that engine, so the page can report
     itself. The classic script must therefore avoid anything modern -- `fetch` and arrow
     functions defeat the whole point.
  2. A link cannot reach the system browser from inside the dead window; it navigates the
     dead window instead. So Python opens the browser, driven by the report.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from discord_ferry import gui


@pytest.fixture(autouse=True)
def _clean_shared_html(monkeypatch: pytest.MonkeyPatch) -> None:
    """`add_*_html(shared=True)` writes to class attributes that would leak between tests."""
    from nicegui import Client

    monkeypatch.setattr(Client, "shared_head_html", "", raising=False)
    monkeypatch.setattr(Client, "shared_body_html", "", raising=False)
    monkeypatch.setattr(gui, "_browser_opened", False, raising=False)


class TestNativeEnabled:
    def test_native_when_webview_present_and_variable_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gui, "_HAS_WEBVIEW", True)
        monkeypatch.delenv("FERRY_NO_NATIVE", raising=False)
        assert gui._native_enabled() is True

    def test_variable_disables_the_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gui, "_HAS_WEBVIEW", True)
        monkeypatch.setenv("FERRY_NO_NATIVE", "1")
        assert gui._native_enabled() is False

    def test_empty_value_counts_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`FERRY_NO_NATIVE=` should behave like an absent variable, not a surprise."""
        monkeypatch.setattr(gui, "_HAS_WEBVIEW", True)
        monkeypatch.setenv("FERRY_NO_NATIVE", "")
        assert gui._native_enabled() is True

    def test_no_webview_is_still_browser_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gui, "_HAS_WEBVIEW", False)
        monkeypatch.delenv("FERRY_NO_NATIVE", raising=False)
        assert gui._native_enabled() is False


class TestRunGuiHonoursTheVariable:
    @staticmethod
    def _capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        seen: dict[str, Any] = {}
        monkeypatch.setattr(gui.ui, "run", lambda **kwargs: seen.update(kwargs))
        monkeypatch.setattr(gui, "_teardown_native_window", lambda: None)
        monkeypatch.setattr(gui.app, "on_shutdown", MagicMock())
        monkeypatch.setattr(gui, "_install_renderer_fallback", MagicMock())
        return seen

    def test_variable_set_runs_in_browser_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gui, "_HAS_WEBVIEW", True)
        monkeypatch.setenv("FERRY_NO_NATIVE", "1")
        seen = self._capture(monkeypatch)
        gui._run_gui()
        assert seen["native"] is False
        gui.app.on_shutdown.assert_not_called()  # type: ignore[attr-defined]
        gui._install_renderer_fallback.assert_not_called()  # type: ignore[attr-defined]

    def test_variable_unset_runs_native_and_installs_the_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gui, "_HAS_WEBVIEW", True)
        monkeypatch.delenv("FERRY_NO_NATIVE", raising=False)
        seen = self._capture(monkeypatch)
        gui._run_gui()
        assert seen["native"] is True
        gui._install_renderer_fallback.assert_called_once()  # type: ignore[attr-defined]

    def test_the_served_address_matches_the_fallback_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fallback pointing somewhere the server is not would open a dead page."""
        monkeypatch.setattr(gui, "_HAS_WEBVIEW", True)
        monkeypatch.delenv("FERRY_NO_NATIVE", raising=False)
        seen = self._capture(monkeypatch)
        gui._run_gui()
        assert f"http://{seen['host']}:{seen['port']}" == gui._URL


class TestInjectedMarkup:
    """The markup has to survive an engine that predates ES modules."""

    @staticmethod
    def _install(monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
        from nicegui import Client

        monkeypatch.setattr(gui.app, "get", lambda *_a, **_k: lambda fn: fn)
        gui._install_renderer_fallback()
        return Client.shared_head_html, Client.shared_body_html

    def test_the_reporting_script_is_not_a_module(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A module script cannot run in the engine this exists to survive."""
        head, _ = self._install(monkeypatch)
        classic = head.split('<script type="module">')[0]
        assert "XMLHttpRequest" in classic
        assert 'type="module"' not in classic

    def test_the_reporting_script_avoids_modern_javascript(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`fetch` and arrow functions are absent from the engine that fails here."""
        head, _ = self._install(monkeypatch)
        classic = head.split('<script type="module">')[0]
        assert "fetch(" not in classic
        assert "=>" not in classic

    def test_a_working_renderer_cancels_the_report_and_hides_the_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        head, _ = self._install(monkeypatch)
        module = head.split('<script type="module">')[1]
        assert "clearTimeout" in module
        assert "ferry-no-window" in module

    def test_the_message_carries_the_address(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, body = self._install(monkeypatch)
        assert gui._URL in body
        assert "ferry-no-window" in body


class TestRendererDeadRoute:
    """The endpoint the dead page calls.

    Exercised by capturing the handler rather than through a test client: NiceGUI's
    lifespan refuses to start without a real ``ui.run()``.
    """

    @staticmethod
    def _handler(monkeypatch: pytest.MonkeyPatch) -> Any:
        captured: list[Any] = []

        def fake_get(path: str, **_kwargs: Any) -> Any:
            assert path == gui._RENDERER_DEAD_ROUTE

            def decorate(fn: Any) -> Any:
                captured.append(fn)
                return fn

            return decorate

        monkeypatch.setattr(gui.app, "get", fake_get)
        gui._install_renderer_fallback()
        assert captured, "the route was never registered"
        return captured[0]

    def test_it_opens_the_browser_at_the_served_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Assert the CALL, never the absence of a window."""
        opened = MagicMock(return_value=True)
        monkeypatch.setattr(gui.webbrowser, "open", opened)
        handler = self._handler(monkeypatch)
        assert handler() == {"status": "ok"}
        opened.assert_called_once_with(gui._URL)

    def test_a_second_report_does_not_open_a_second_browser(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        opened = MagicMock(return_value=True)
        monkeypatch.setattr(gui.webbrowser, "open", opened)
        handler = self._handler(monkeypatch)
        handler()
        assert handler() == {"status": "already-opened"}
        assert opened.call_count == 1

    def test_a_failed_browser_launch_is_reported_not_raised(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Raising here would replace a dead window with a crash."""
        monkeypatch.setattr(gui.webbrowser, "open", MagicMock(side_effect=OSError("no browser")))
        handler = self._handler(monkeypatch)
        with caplog.at_level("WARNING"):
            assert handler() == {"status": "ok"}
        assert "by hand" in caplog.text

    def test_a_browser_that_reports_failure_is_also_logged(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(gui.webbrowser, "open", MagicMock(return_value=False))
        handler = self._handler(monkeypatch)
        with caplog.at_level("WARNING"):
            handler()
        assert "No browser could be opened" in caplog.text
