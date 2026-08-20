from __future__ import annotations

import struct
from pathlib import Path

import aiohttp
import pytest
from aioresponses import aioresponses

from discord_ferry.migrator.probe import ProbeReport, _make_test_file

BASE_URL = "https://api.test"
AUTUMN_URL = "https://cdn.test"
TOKEN = "test-token"
SERVER_ID = "srv_test"


def _check(report: ProbeReport, name: str) -> str:
    for c in report.checks:
        if c.name == name:
            return f"{c.status}: {c.detail}"
    raise AssertionError(f"no check named {name!r} in {[c.name for c in report.checks]}")


@pytest.fixture
def mock_aiohttp() -> aioresponses:
    with aioresponses() as m:
        yield m


def test_make_test_file_png_exact_size(tmp_path: Path) -> None:
    """Non-attachments tags produce a valid PNG at the exact requested size."""
    target = 1000
    path = _make_test_file(tmp_path, "icons", target)
    assert path.stat().st_size == target
    with open(path, "rb") as f:
        assert f.read(8) == b"\x89PNG\r\n\x1a\n"


def test_make_test_file_attachments_raw_bytes(tmp_path: Path) -> None:
    """Attachments tag produces raw zero bytes, not a PNG."""
    target = 500
    path = _make_test_file(tmp_path, "attachments", target)
    assert path.stat().st_size == target
    with open(path, "rb") as f:
        assert f.read(8) != b"\x89PNG\r\n\x1a\n"


def test_make_test_file_png_various_tags(tmp_path: Path) -> None:
    """All non-attachment tags produce exact-sized valid PNGs."""
    for tag in ("emojis", "avatars", "banners", "backgrounds"):
        target = 2000
        path = _make_test_file(tmp_path, tag, target)
        assert path.stat().st_size == target, f"{tag} size mismatch"
        with open(path, "rb") as f:
            assert f.read(8) == b"\x89PNG\r\n\x1a\n", f"{tag} not valid PNG"
