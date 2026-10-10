"""Focused path validation tests for the Autumn upload boundary."""

from pathlib import Path
from unittest.mock import AsyncMock

import aiohttp
import pytest

from discord_ferry.errors import AutumnUploadError
from discord_ferry.uploader.autumn import upload_to_autumn


async def test_upload_to_autumn_rejects_directory_before_http(tmp_path: Path) -> None:
    session = AsyncMock(spec=aiohttp.ClientSession)

    with pytest.raises(AutumnUploadError, match="Not a regular file"):
        await upload_to_autumn(
            session,
            "https://autumn.test",
            "attachments",
            tmp_path,
            "test-token",
        )

    session.post.assert_not_called()
