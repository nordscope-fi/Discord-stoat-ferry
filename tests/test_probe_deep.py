from __future__ import annotations

from typing import TYPE_CHECKING

import aiohttp
import pytest
from aioresponses import aioresponses

if TYPE_CHECKING:
    from pathlib import Path

from discord_ferry.migrator.probe import (
    ProbeReport,
    _check_autumn,
    _check_deep_uploads,
    _make_test_file,
    _probe_one_tag,
    _raw_autumn_upload,
)
from discord_ferry.uploader.autumn import TAG_SIZE_LIMITS

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


async def test_check_autumn_returns_autumn_url(mock_aiohttp: aioresponses) -> None:
    """_check_autumn returns the discovered autumn_url for deep probe reuse."""
    mock_aiohttp.get(
        f"{BASE_URL}/",
        payload={"features": {"autumn": {"url": AUTUMN_URL}}},
    )
    mock_aiohttp.get(f"{AUTUMN_URL}/", payload={"autumn": "0.5.0", "version": "0.5.0"})
    report = ProbeReport()
    async with aiohttp.ClientSession() as sess:
        result = await _check_autumn(sess, BASE_URL, report)
    assert result == AUTUMN_URL


async def test_check_autumn_returns_none_on_failure(mock_aiohttp: aioresponses) -> None:
    """_check_autumn returns None when the root is unreachable."""
    mock_aiohttp.get(f"{BASE_URL}/", exception=aiohttp.ClientError("conn refused"))
    report = ProbeReport()
    async with aiohttp.ClientSession() as sess:
        result = await _check_autumn(sess, BASE_URL, report)
    assert result is None


async def test_raw_upload_returns_status_without_raising(
    mock_aiohttp: aioresponses, tmp_path: Path
) -> None:
    """Over-limit upload returns the HTTP status, never raises."""
    mock_aiohttp.post(f"{AUTUMN_URL}/attachments", status=413)
    test_file = tmp_path / "over.bin"
    test_file.write_bytes(b"\x00" * 100)
    async with aiohttp.ClientSession() as sess:
        status = await _raw_autumn_upload(sess, AUTUMN_URL, "attachments", test_file, TOKEN)
    assert status == 413


async def test_raw_upload_returns_200_on_success(
    mock_aiohttp: aioresponses, tmp_path: Path
) -> None:
    """Successful upload returns 200."""
    mock_aiohttp.post(f"{AUTUMN_URL}/icons", status=200, payload={"id": "abc123"})
    test_file = tmp_path / "ok.png"
    test_file.write_bytes(b"\x89PNG" + b"\x00" * 96)
    async with aiohttp.ClientSession() as sess:
        status = await _raw_autumn_upload(sess, AUTUMN_URL, "icons", test_file, TOKEN)
    assert status == 200


# ---------------------------------------------------------------------------
# _check_deep_uploads
# ---------------------------------------------------------------------------


def _mock_at_limit_accept(m: aioresponses, tag: str) -> None:
    """Mock the at-limit upload as accepted (200)."""
    m.post(f"{AUTUMN_URL}/{tag}", status=200, payload={"id": f"file_{tag}"})


def _mock_over_limit_reject(m: aioresponses, tag: str) -> None:
    """Mock the over-limit upload as rejected (413)."""
    m.post(f"{AUTUMN_URL}/{tag}", status=413)


def _mock_over_limit_accept(m: aioresponses, tag: str) -> None:
    """Mock the over-limit upload as accepted (200, limit NOT enforced)."""
    m.post(f"{AUTUMN_URL}/{tag}", status=200, payload={"id": f"over_{tag}"})


def _mock_all_tags_normal(m: aioresponses) -> None:
    """Mock every tag: at-limit accepted, over-limit rejected, teardown succeeds."""
    for tag in TAG_SIZE_LIMITS:
        _mock_at_limit_accept(m, tag)
        _mock_over_limit_reject(m, tag)
    # Teardown mocks for entity-backed tags.
    m.post(f"{BASE_URL}/servers/{SERVER_ID}/channels", payload={"_id": "ch_probe"})
    m.delete(f"{BASE_URL}/channels/ch_probe", status=204)
    m.put(f"{AUTUMN_URL.replace('cdn', 'api')}/custom/emoji/file_emojis", status=200, payload={})
    m.put(f"{BASE_URL}/custom/emoji/file_emojis", status=200, payload={"_id": "file_emojis"})
    m.delete(f"{BASE_URL}/custom/emoji/file_emojis", status=204)
    m.patch(f"{BASE_URL}/servers/{SERVER_ID}", status=200, payload={}, repeat=True)


async def test_deep_skipped_when_autumn_url_none(mock_aiohttp: aioresponses) -> None:
    """Returns a single fail row when autumn_url is None."""
    report = ProbeReport()
    async with aiohttp.ClientSession() as sess:
        await _check_deep_uploads(sess, BASE_URL, None, TOKEN, SERVER_ID, report)
    assert len(report.checks) == 1
    assert _check(report, "deep_probe").startswith("fail:")


ICON_LIMIT = TAG_SIZE_LIMITS["icons"]
ATTACH_LIMIT = TAG_SIZE_LIMITS["attachments"]
EMOJI_LIMIT = TAG_SIZE_LIMITS["emojis"]
BANNER_LIMIT = TAG_SIZE_LIMITS["banners"]


async def test_deep_at_limit_accepted_reports_ok(
    mock_aiohttp: aioresponses, tmp_path: Path
) -> None:
    """An at-limit upload that the server accepts is 'ok'."""
    _mock_at_limit_accept(mock_aiohttp, "icons")
    _mock_over_limit_reject(mock_aiohttp, "icons")
    mock_aiohttp.patch(f"{BASE_URL}/servers/{SERVER_ID}", status=200, payload={}, repeat=True)
    report = ProbeReport()
    async with aiohttp.ClientSession() as sess:
        await _probe_one_tag(
            sess, BASE_URL, AUTUMN_URL, TOKEN, SERVER_ID, "icons", ICON_LIMIT, tmp_path, report
        )
    result = _check(report, "deep_icons_at_limit")
    assert result.startswith("ok:")


async def test_deep_over_limit_rejected_reports_ok(
    mock_aiohttp: aioresponses, tmp_path: Path
) -> None:
    """An over-limit upload rejected by the server is 'ok'."""
    _mock_at_limit_accept(mock_aiohttp, "icons")
    _mock_over_limit_reject(mock_aiohttp, "icons")
    mock_aiohttp.patch(f"{BASE_URL}/servers/{SERVER_ID}", status=200, payload={}, repeat=True)
    report = ProbeReport()
    async with aiohttp.ClientSession() as sess:
        await _probe_one_tag(
            sess, BASE_URL, AUTUMN_URL, TOKEN, SERVER_ID, "icons", ICON_LIMIT, tmp_path, report
        )
    result = _check(report, "deep_icons_over_limit")
    assert result.startswith("ok:")


async def test_deep_over_limit_accepted_reports_warn(
    mock_aiohttp: aioresponses, tmp_path: Path
) -> None:
    """An over-limit upload accepted by the server is 'warn' (limit NOT enforced)."""
    _mock_at_limit_accept(mock_aiohttp, "icons")
    _mock_over_limit_accept(mock_aiohttp, "icons")
    mock_aiohttp.patch(f"{BASE_URL}/servers/{SERVER_ID}", status=200, payload={}, repeat=True)
    report = ProbeReport()
    async with aiohttp.ClientSession() as sess:
        await _probe_one_tag(
            sess, BASE_URL, AUTUMN_URL, TOKEN, SERVER_ID, "icons", ICON_LIMIT, tmp_path, report
        )
    result = _check(report, "deep_icons_over_limit")
    assert result.startswith("warn:")
    assert "limit NOT enforced" in result


async def test_deep_attachments_channel_deleted_on_success(
    mock_aiohttp: aioresponses, tmp_path: Path
) -> None:
    """Attachment probe creates and deletes a channel for teardown."""
    _mock_at_limit_accept(mock_aiohttp, "attachments")
    _mock_over_limit_reject(mock_aiohttp, "attachments")
    mock_aiohttp.post(f"{BASE_URL}/servers/{SERVER_ID}/channels", payload={"_id": "ch_probe"})
    delete_called = {"n": 0}
    mock_aiohttp.delete(
        f"{BASE_URL}/channels/ch_probe",
        callback=lambda u, **k: delete_called.__setitem__("n", 1),
        status=204,
    )
    report = ProbeReport()
    async with aiohttp.ClientSession() as sess:
        await _probe_one_tag(
            sess,
            BASE_URL,
            AUTUMN_URL,
            TOKEN,
            SERVER_ID,
            "attachments",
            ATTACH_LIMIT,
            tmp_path,
            report,
        )
    assert delete_called["n"] == 1


async def test_deep_emoji_deleted_after_probe(mock_aiohttp: aioresponses, tmp_path: Path) -> None:
    """Emoji probe creates and deletes the emoji."""
    _mock_at_limit_accept(mock_aiohttp, "emojis")
    _mock_over_limit_reject(mock_aiohttp, "emojis")
    mock_aiohttp.put(
        f"{BASE_URL}/custom/emoji/file_emojis", status=200, payload={"_id": "file_emojis"}
    )
    delete_called = {"n": 0}
    mock_aiohttp.delete(
        f"{BASE_URL}/custom/emoji/file_emojis",
        callback=lambda u, **k: delete_called.__setitem__("n", 1),
        status=204,
    )
    report = ProbeReport()
    async with aiohttp.ClientSession() as sess:
        await _probe_one_tag(
            sess, BASE_URL, AUTUMN_URL, TOKEN, SERVER_ID, "emojis", EMOJI_LIMIT, tmp_path, report
        )
    assert delete_called["n"] == 1


async def test_deep_icon_reset_uses_remove(mock_aiohttp: aioresponses, tmp_path: Path) -> None:
    """Icon probe resets the server icon via remove=['Icon']."""
    _mock_at_limit_accept(mock_aiohttp, "icons")
    _mock_over_limit_reject(mock_aiohttp, "icons")
    patch_calls: list[dict[str, object]] = []

    def capture_patch(url: object, **kwargs: object) -> None:
        patch_calls.append(kwargs.get("json", {}))  # type: ignore[arg-type]

    mock_aiohttp.patch(
        f"{BASE_URL}/servers/{SERVER_ID}",
        callback=capture_patch,
        status=200,
        repeat=True,
    )
    report = ProbeReport()
    async with aiohttp.ClientSession() as sess:
        await _probe_one_tag(
            sess, BASE_URL, AUTUMN_URL, TOKEN, SERVER_ID, "icons", ICON_LIMIT, tmp_path, report
        )
    removes = [c for c in patch_calls if "remove" in c]
    assert any("Icon" in c.get("remove", []) for c in removes)  # type: ignore[union-attr]


async def test_deep_banner_reset_uses_remove(mock_aiohttp: aioresponses, tmp_path: Path) -> None:
    """Banner probe resets via remove=['Banner']."""
    _mock_at_limit_accept(mock_aiohttp, "banners")
    _mock_over_limit_reject(mock_aiohttp, "banners")
    patch_calls: list[dict[str, object]] = []

    def capture_patch(url: object, **kwargs: object) -> None:
        patch_calls.append(kwargs.get("json", {}))  # type: ignore[arg-type]

    mock_aiohttp.patch(
        f"{BASE_URL}/servers/{SERVER_ID}",
        callback=capture_patch,
        status=200,
        repeat=True,
    )
    report = ProbeReport()
    async with aiohttp.ClientSession() as sess:
        await _probe_one_tag(
            sess, BASE_URL, AUTUMN_URL, TOKEN, SERVER_ID, "banners", BANNER_LIMIT, tmp_path, report
        )
    removes = [c for c in patch_calls if "remove" in c]
    assert any("Banner" in c.get("remove", []) for c in removes)  # type: ignore[union-attr]


async def test_deep_teardown_suppresses_exceptions(
    mock_aiohttp: aioresponses, tmp_path: Path
) -> None:
    """Teardown failures are suppressed and do not block other tags."""
    _mock_at_limit_accept(mock_aiohttp, "attachments")
    _mock_over_limit_reject(mock_aiohttp, "attachments")
    mock_aiohttp.post(f"{BASE_URL}/servers/{SERVER_ID}/channels", payload={"_id": "ch_probe"})
    mock_aiohttp.delete(f"{BASE_URL}/channels/ch_probe", status=500)
    report = ProbeReport()
    async with aiohttp.ClientSession() as sess:
        await _probe_one_tag(
            sess,
            BASE_URL,
            AUTUMN_URL,
            TOKEN,
            SERVER_ID,
            "attachments",
            ATTACH_LIMIT,
            tmp_path,
            report,
        )
    assert _check(report, "deep_attachments_at_limit").startswith("ok:")
