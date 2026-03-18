"""Autumn file upload with retry and caching."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import aiohttp

from discord_ferry.errors import AutumnUploadError

if TYPE_CHECKING:
    from pathlib import Path

MAX_RETRIES = 3
_RETRYABLE_STATUSES = {429, 502, 503, 504}

TAG_SIZE_LIMITS: dict[str, int] = {
    "attachments": 20 * 1024 * 1024,
    "avatars": 4 * 1024 * 1024,
    "backgrounds": 6 * 1024 * 1024,
    "icons": 2560 * 1024,
    "banners": 6 * 1024 * 1024,
    "emojis": 500 * 1024,
}


async def upload_to_autumn(
    session: aiohttp.ClientSession,
    autumn_url: str,
    tag: str,
    file_path: Path,
    token: str,
) -> str:
    """Upload a file to Autumn and return the file ID.

    Args:
        session: An active aiohttp ClientSession to use for the request.
        autumn_url: Autumn server base URL (e.g. "https://autumn.stoat.chat").
        tag: Upload tag determining the bucket (attachments, avatars, icons, banners, emojis, etc.).
        file_path: Local path to the file to upload.
        token: Stoat session token for the x-session-token header.

    Returns:
        Autumn file ID string returned by the server.

    Raises:
        AutumnUploadError: If the tag is unknown, the file is missing, the file exceeds the size
            limit, all retries are exhausted, or the server returns a non-retryable error.
    """
    if tag not in TAG_SIZE_LIMITS:
        raise AutumnUploadError(f"Unknown Autumn tag '{tag}'. Valid tags: {list(TAG_SIZE_LIMITS)}")

    if not file_path.exists():
        raise AutumnUploadError(f"File not found: {file_path}")

    file_size = file_path.stat().st_size
    limit = TAG_SIZE_LIMITS[tag]
    if file_size > limit:
        raise AutumnUploadError(
            f"File '{file_path.name}' is {file_size} bytes, "
            f"which exceeds the {tag} limit of {limit} bytes."
        )

    url = f"{autumn_url.rstrip('/')}/{tag}"
    headers = {"x-session-token": token}

    for attempt in range(MAX_RETRIES):
        form = aiohttp.FormData()
        fh = file_path.open("rb")
        try:
            form.add_field("file", fh, filename=file_path.name)

            async with session.post(url, data=form, headers=headers) as response:
                if response.status == 200:
                    result: dict[str, str] = await response.json()
                    return result["id"]

                if response.status in _RETRYABLE_STATUSES:
                    if attempt == MAX_RETRIES - 1:
                        raise AutumnUploadError(
                            f"Upload failed after {MAX_RETRIES} attempts "
                            f"(last status: {response.status})."
                        )
                    if response.status == 429:
                        body: dict[str, float] = await response.json()
                        retry_after_ms = body.get("retry_after", 1000)
                        await asyncio.sleep(retry_after_ms / 1000)
                    else:
                        await asyncio.sleep(2)
                    continue

                if response.status == 413:
                    limit = TAG_SIZE_LIMITS.get(tag, 0)
                    raise AutumnUploadError(
                        f"File too large: {file_path.name} "
                        f"({file_path.stat().st_size / 1_048_576:.1f} MB, "
                        f"limit: {limit / 1_048_576:.1f} MB)"
                    )

                text = await response.text()
                raise AutumnUploadError(f"Upload failed with status {response.status}: {text}")
        finally:
            fh.close()

    # Should be unreachable, but satisfies mypy.
    raise AutumnUploadError(f"Upload failed after {MAX_RETRIES} attempts.")


async def upload_with_cache(
    session: aiohttp.ClientSession,
    autumn_url: str,
    tag: str,
    file_path: Path,
    token: str,
    cache: dict[str, str],
    delay: float = 0.5,
) -> str:
    """Upload a file to Autumn, returning a cached ID if the file was already uploaded.

    Args:
        session: An active aiohttp ClientSession.
        autumn_url: Autumn server base URL.
        tag: Upload tag/bucket name.
        file_path: Local path to the file.
        token: Stoat session token.
        cache: Mutable dict mapping str(file_path) -> Autumn file ID.
        delay: Seconds to sleep before uploading (rate-limit courtesy). Default 0.5s.

    Returns:
        Autumn file ID string.
    """
    key = str(file_path)
    if key in cache:
        return cache[key]

    await asyncio.sleep(delay)
    file_id = await upload_to_autumn(session, autumn_url, tag, file_path, token)
    cache[key] = file_id
    return file_id
