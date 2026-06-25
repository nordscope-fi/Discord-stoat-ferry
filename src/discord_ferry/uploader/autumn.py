"""Autumn file upload with retry and caching."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import aiohttp

from discord_ferry.errors import AutumnUploadError

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
_RETRYABLE_STATUSES = {429, 502, 503, 504}

TAG_SIZE_LIMITS: dict[str, int] = {
    "attachments": 20 * 1024 * 1024,
    "avatars": 4 * 1024 * 1024,
    "backgrounds": 6 * 1024 * 1024,
    "icons": 2_500_000,  # Autumn enforces a flat 2.5MB (stoatchat Revolt.toml), not 2560*1024
    "banners": 6 * 1024 * 1024,
    "emojis": 500 * 1024,
}


async def _retry_after_ms(response: aiohttp.ClientResponse) -> float:
    """429 backoff in ms: body ``retry_after`` -> ``Retry-After`` header (int seconds) -> 1000ms."""
    try:
        body = await response.json(content_type=None)
    except (aiohttp.ClientError, ValueError):
        body = None
    if isinstance(body, dict):
        ra = body.get("retry_after")
        if isinstance(ra, (int, float)):
            return float(ra)
    header = response.headers.get("Retry-After")
    if header is not None:
        stripped = header.strip()
        if stripped.isascii() and stripped.isdigit():
            return float(int(stripped) * 1000)
    return 1000.0


async def upload_to_autumn(
    session: aiohttp.ClientSession,
    autumn_url: str,
    tag: str,
    file_path: Path,
    token: str,
    *,
    verify_size: bool = False,
) -> str:
    """Upload a file to Autumn and return the file ID.

    Args:
        session: An active aiohttp ClientSession to use for the request.
        autumn_url: Autumn server base URL (e.g. "https://autumn.stoat.chat").
        tag: Upload tag determining the bucket (attachments, avatars, icons, banners, emojis, etc.).
        file_path: Local path to the file to upload.
        token: Stoat session token for the x-session-token header.
        verify_size: When True, compare the ``size`` field in the Autumn response (if present)
            against the local file size. On a present-and-mismatched size, raises
            ``AutumnUploadError`` — the upload is treated as failed and is never cached. This is
            best-effort — not all Autumn responses include ``size``.

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
                    try:
                        result = await response.json(content_type=None)
                    except (aiohttp.ClientError, ValueError) as exc:
                        raise AutumnUploadError(
                            f"Autumn returned a non-JSON 200 response for tag '{tag}'."
                        ) from exc
                    if not isinstance(result, dict) or "id" not in result:
                        raise AutumnUploadError(
                            f"Autumn 200 response for tag '{tag}' is missing the 'id' field."
                        )
                    file_id = str(result["id"])

                    # Best-effort: a present-and-mismatched size is a failed upload.
                    if verify_size and "size" in result:
                        server_size = result["size"]
                        if isinstance(server_size, int) and server_size != file_size:
                            logger.warning(
                                "Upload size mismatch for %r: local=%d bytes, server=%d bytes — "
                                "treating as a failed upload.",
                                file_path.name,
                                file_size,
                                server_size,
                            )
                            raise AutumnUploadError(
                                f"Upload size mismatch for '{file_path.name}': "
                                f"local={file_size} bytes, server={server_size} bytes."
                            )

                    return file_id

                if response.status in _RETRYABLE_STATUSES:
                    if attempt == MAX_RETRIES - 1:
                        raise AutumnUploadError(
                            f"Upload failed after {MAX_RETRIES} attempts "
                            f"(last status: {response.status})."
                        )
                    if response.status == 429:
                        await asyncio.sleep(await _retry_after_ms(response) / 1000)
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
    *,
    verify_size: bool = False,
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
        verify_size: When True, pass size verification to the upload call. On a
            present-and-mismatched size the upload raises ``AutumnUploadError`` and is never
            cached. Best-effort — not all Autumn responses include ``size``.

    Returns:
        Autumn file ID string.
    """
    key = str(file_path)
    if key in cache:
        return cache[key]

    await asyncio.sleep(delay)
    file_id = await upload_to_autumn(
        session,
        autumn_url,
        tag,
        file_path,
        token,
        verify_size=verify_size,
    )
    cache[key] = file_id
    return file_id
