"""Tests for src/discord_ferry/gui._open_path platform dispatch."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

if TYPE_CHECKING:
    import pytest

from discord_ferry.gui import _open_path


class TestOpenPathPlatformDispatch:
    def test_open_path_uses_startfile_on_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.platform", "win32")
        mock_startfile = MagicMock()
        # raising=False: os.startfile doesn't exist on macOS/Linux test hosts
        monkeypatch.setattr("os.startfile", mock_startfile, raising=False)
        p = Path("C:/Users/Pete/report.html")
        _open_path(p)
        mock_startfile.assert_called_once_with(p)  # Path object, not str

    def test_open_path_uses_open_on_macos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.platform", "darwin")
        mock_popen = MagicMock()
        monkeypatch.setattr("subprocess.Popen", mock_popen)
        p = Path("/Users/Pete/report.html")
        _open_path(p)
        mock_popen.assert_called_once_with(["open", p])  # Path, not str

    def test_open_path_uses_xdg_open_on_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.platform", "linux")
        mock_popen = MagicMock()
        monkeypatch.setattr("subprocess.Popen", mock_popen)
        p = Path("/home/pete/report.html")
        _open_path(p)
        mock_popen.assert_called_once_with(["xdg-open", p])
