"""Credential transport coverage for authenticated probe requests."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aioresponses import aioresponses
from yarl import URL

from discord_ferry.errors import AutumnUploadError
from discord_ferry.migrator.probe import _raw_autumn_upload

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

    assert status == 413
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
