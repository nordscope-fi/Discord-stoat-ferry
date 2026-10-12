"""Credential transport coverage for authenticated probe requests."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aioresponses import aioresponses
from yarl import URL

from discord_ferry.errors import AutumnUploadError
from discord_ferry.migrator.probe import ProbeReport, _check_rate_limit, _raw_autumn_upload

if TYPE_CHECKING:
    from pathlib import Path


async def test_public_http_raw_upload_is_rejected_before_file_or_request_work(
    tmp_path: Path,
) -> None:
    session = AsyncMock(spec=aiohttp.ClientSession)
    missing = tmp_path / "missing.bin"

    with pytest.raises(
        AutumnUploadError,
        match="Credential-bearing Autumn requests require HTTPS",
    ):
        await _raw_autumn_upload(
            session,
            "http://public.example",
            "attachments",
            missing,
            "session-token",
        )

    session.post.assert_not_called()


async def test_private_http_raw_upload_remains_available(tmp_path: Path) -> None:
    file_path = tmp_path / "upload.bin"
    file_path.write_bytes(b"upload")
    url = "http://127.0.0.1:8765/attachments"

    with aioresponses() as mocked:
        mocked.post(url, status=413)
        async with aiohttp.ClientSession() as session:
            status = await _raw_autumn_upload(
                session,
                "http://127.0.0.1:8765",
                "attachments",
                file_path,
                "session-token",
            )

    assert status == (413, None)
    assert len(mocked.requests[("POST", URL(url))]) == 1


async def test_raw_upload_redirect_is_an_error(tmp_path: Path) -> None:
    file_path = tmp_path / "upload.bin"
    file_path.write_bytes(b"upload")
    url = "https://autumn.test/attachments"

    with aioresponses() as mocked:
        mocked.post(url, status=302, headers={"Location": "http://public.example/leak"})
        async with aiohttp.ClientSession() as session:
            with pytest.raises(
                AutumnUploadError,
                match="Deep-probe Autumn request refused a redirect",
            ):
                await _raw_autumn_upload(
                    session,
                    "https://autumn.test",
                    "attachments",
                    file_path,
                    "session-token",
                )

    assert len(mocked.requests[("POST", URL(url))]) == 1


async def test_raw_upload_explicitly_disables_redirects(tmp_path: Path) -> None:
    file_path = tmp_path / "upload.bin"
    file_path.write_bytes(b"upload")
    url = "https://autumn.test/attachments"

    with aioresponses() as mocked:
        mocked.post(url, status=413)
        async with aiohttp.ClientSession() as session:
            await _raw_autumn_upload(
                session,
                "https://autumn.test",
                "attachments",
                file_path,
                "session-token",
            )

    request = mocked.requests[("POST", URL(url))][0]
    assert request.kwargs["allow_redirects"] is False


async def test_public_http_rate_limit_check_records_a_failure_before_request() -> None:
    session = AsyncMock(spec=aiohttp.ClientSession)
    report = ProbeReport()

    await _check_rate_limit(session, "http://public.example", "session-token", "server-1", report)

    session.get.assert_not_called()
    assert report.checks[0].name == "rate_limit"
    assert report.checks[0].status == "fail"
    assert "Credential-bearing API requests require HTTPS" in report.checks[0].detail


async def test_private_http_rate_limit_check_remains_available() -> None:
    url = "http://127.0.0.1:8765/servers/server-1"
    report = ProbeReport()

    with aioresponses() as mocked:
        mocked.get(url, headers={"X-RateLimit-Limit": "5"})
        async with aiohttp.ClientSession() as session:
            await _check_rate_limit(
                session,
                "http://127.0.0.1:8765",
                "session-token",
                "server-1",
                report,
            )

    assert report.checks[0].status == "ok"
    assert len(mocked.requests[("GET", URL(url))]) == 1


async def test_rate_limit_redirect_records_a_failure() -> None:
    url = "https://api.test/servers/server-1"
    report = ProbeReport()

    with aioresponses() as mocked:
        mocked.get(url, status=302, headers={"Location": "http://public.example/leak"})
        async with aiohttp.ClientSession() as session:
            await _check_rate_limit(
                session, "https://api.test", "session-token", "server-1", report
            )

    assert report.checks[0].status == "fail"
    assert "refused a redirect" in report.checks[0].detail
    assert len(mocked.requests[("GET", URL(url))]) == 1


async def test_rate_limit_check_explicitly_disables_redirects() -> None:
    url = "https://api.test/servers/server-1"
    report = ProbeReport()

    with aioresponses() as mocked:
        mocked.get(url, headers={"X-RateLimit-Limit": "5"})
        async with aiohttp.ClientSession() as session:
            await _check_rate_limit(
                session, "https://api.test", "session-token", "server-1", report
            )

    request = mocked.requests[("GET", URL(url))][0]
    assert request.kwargs["allow_redirects"] is False
