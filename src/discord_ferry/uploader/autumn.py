"""Autumn file upload with retry and caching."""

from __future__ import annotations

import asyncio
import logging
import math
from typing import TYPE_CHECKING

import aiohttp

from discord_ferry.core.http import proxy_hint, tls_hint
from discord_ferry.errors import AutumnUploadError

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
_MAX_RETRY_DELAY_MS = 60_000.0  # Mirrors migrator.api._MAX_RETRY_DELAY_SECONDS.
_DEFAULT_RETRY_DELAY_MS = 1000.0
_RETRYABLE_STATUSES = {429, 502, 503, 504}

# Autumn's limits are DECIMAL megabytes, as written in stoatchat's Revolt.toml -- never
# N * 1024 * 1024. Getting that wrong makes our pre-upload guard LOOSER than the server's,
# so a file in the gap band is uploaded and then rejected with a 413. Verified 2026-08-02
# against `features.limits.default.file_upload_size_limits` on https://api.stoat.chat/;
# `ferry probe` re-checks it against whatever instance you point it at.
TAG_SIZE_LIMITS: dict[str, int] = {
    "attachments": 20_000_000,
    "avatars": 4_000_000,
    "backgrounds": 6_000_000,
    "icons": 2_500_000,
    "banners": 6_000_000,
    "emojis": 500_000,
}

# Single-flight registry: coalesces concurrent first-uploads of the same cache key
# (keyed by str(file_path), the same key upload_with_cache caches under) so two
# parallel channel workers requesting the same physical file upload it only once.
# Self-cleaning — every entry is popped on completion (success or failure).
_inflight_uploads: dict[str, asyncio.Future[str]] = {}


async def _advertised_retry_after_ms(response: aiohttp.ClientResponse) -> float:
    """The delay Autumn advertises for a 429, in MILLISECONDS, before clamping.

    Precedence, most specific first:

    1. body ``retry_after`` -- milliseconds. Stoat's rate-limit middleware writes the
       same ``ratelimiter.reset`` value into the body that it puts in the header.
    2. ``Retry-After`` header -- RFC 9110 delta-SECONDS, hence x1000. Autumn does not
       send it, but a CDN or proxy in front of Autumn may, and its view of when it will
       let us back in outranks the origin's.
    3. ``X-RateLimit-Reset-After`` header -- MILLISECONDS. What Autumn actually sends.

    UNIT HAZARD -- do NOT unify this with ``discord.client._retry_after_seconds``. Autumn
    sits behind the same middleware as the Stoat API, so its ``X-RateLimit-Reset-After``
    is milliseconds, matching ``migrator.api._stoat_rate_delay_seconds``; Discord's
    identically-named header is delta-seconds. See that function's docstring for the full
    rules and the test pinning the Discord side.
    """
    try:
        body = await response.json(content_type=None)
    except (aiohttp.ClientError, ValueError):
        body = None
    if isinstance(body, dict):
        ra = body.get("retry_after")
        # bool is a subclass of int, and a JSON `true` would otherwise become a 1 ms
        # delay -- under the clamp's floor, so effectively no backoff at all.
        if isinstance(ra, (int, float)) and not isinstance(ra, bool):
            return float(ra)

    header = response.headers.get("Retry-After")
    if header is not None:
        stripped = header.strip()
        if stripped.isascii() and stripped.isdigit():
            return float(int(stripped) * 1000)

    reset_after = response.headers.get("X-RateLimit-Reset-After")
    if reset_after:
        try:
            return float(reset_after)
        except ValueError:
            pass  # Unparseable -- fall through to the default.

    return _DEFAULT_RETRY_DELAY_MS


async def _retry_after_ms(response: aiohttp.ClientResponse) -> float:
    """429 backoff in MILLISECONDS, clamped to [0, ``_MAX_RETRY_DELAY_MS``].

    The advertised value is remote-supplied, so it is bounded at both ends: a negative
    delay would make ``asyncio.sleep`` return immediately and turn the retry loop hot,
    burning all three attempts inside one closed window, while an unbounded one would
    present as a hang. Mirrors the cap ``migrator.api._MAX_RETRY_DELAY_SECONDS`` already
    applies on the Stoat side.

    Non-finite values are rejected outright rather than clamped, because the clamp alone
    cannot catch NaN: every comparison against it is False, so it survives whichever of
    ``min``/``max`` receives it first. ``asyncio.sleep(nan)`` then **never returns** --
    verified, and unlike ``time.sleep(nan)``, which raises ``ValueError`` (CPython
    #105331) -- so a single such response hangs the upload forever.

    Rejecting is deliberate rather than reordering the clamp to ``max(0.0, min(...))``,
    which would also contain NaN but resolve it to a 0 ms delay: that is the no-backoff
    failure this function exists to prevent. Falling back to the default preserves real
    backoff.

    Reachable from a merely sloppy server, not just a hostile one: Python's ``json``
    module parses bare ``NaN``/``Infinity`` by default, so a body of
    ``{"retry_after": NaN}`` arrives here as a float.
    """
    advertised = await _advertised_retry_after_ms(response)
    if not math.isfinite(advertised):
        return _DEFAULT_RETRY_DELAY_MS
    return min(max(advertised, 0.0), _MAX_RETRY_DELAY_MS)


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
        autumn_url: Autumn server base URL (e.g. "https://cdn.stoatusercontent.com" —
            the old autumn.stoat.chat host 301-redirects there; we discover it at runtime
            from the Stoat root's features.autumn.url).
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
                        # Decimal MB, matching TAG_SIZE_LIMITS -- dividing by 1_048_576
                        # would render the 20_000_000 cap as "limit: 19.1 MB".
                        f"({file_path.stat().st_size / 1_000_000:.1f} MB, "
                        f"limit: {limit / 1_000_000:.1f} MB)"
                    )

                text = await response.text()
                raise AutumnUploadError(f"Upload failed with status {response.status}: {text}")
        except aiohttp.ClientError as exc:
            # No permanence gate here, and that is not an oversight: BOTH arms
            # of this handler raise, so there is no retry for a gate to
            # preserve.
            #
            # Every proxy failure is therefore CONVERTED, a 502 as much as a
            # 407 -- proxy_hint has no status filter. Do not "restore" a raw
            # ClientHttpProxyError here.
            #
            # This paragraph used to say structure.py:403 DEPENDED on this
            # conversion, since its (AutumnUploadError, OSError) handler would
            # let a raw ClientHttpProxyError abort the roles phase. Task 8
            # (2026-08-09) widened that handler to also catch
            # aiohttp.ClientError, so that dependency no longer holds; the
            # conversion stays anyway, for the hint text it attaches.
            #
            # Proxy first and never both, for the reason in api.py.
            hint = proxy_hint(exc, target=url) or tls_hint(exc)
            if hint is None:
                raise
            # Autumn is the one host v2.13.0's error work did not reach: no caller
            # of this function has a handler that could explain a certificate
            # failure, and structure.py's role-icon path discards the text entirely.
            # Converting here covers every caller at once.
            #
            # Interpolating `exc` is safe in THIS branch and nowhere else in this
            # function. A connection-level aiohttp error carries a host, a port and
            # an OpenSSL reason; the response body -- which may echo
            # x-session-token, hence the fixed template at structure.py's catch
            # site -- only exists once a request completed, which a certificate
            # failure guarantees it did not.
            #
            # The same holds for the proxy arm: ClientHttpProxyError's text is
            # the status line of the CONNECT response, written by the PROXY, and
            # the tunnel to Autumn was never established, so no Autumn body
            # exists to echo the token back.
            #
            # Raised rather than retried: neither a certificate failure nor a
            # proxy that refuses outright can succeed on a second attempt, so the
            # caller should not pay two sleeps to learn that.
            raise AutumnUploadError(f"Upload to Autumn failed: {exc}{hint}") from exc
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
    skip_cache: bool = False,
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
        skip_cache: When True, bypass the cache lookup and in-flight coalescing so a fresh
            upload always runs. Required for attachment-tagged uploads because Autumn file
            ids are single-use.

    Returns:
        Autumn file ID string.
    """
    key = str(file_path)
    if not skip_cache:
        if key in cache:
            return cache[key]

        inflight = _inflight_uploads.get(key)
        if inflight is not None:
            return await inflight

    future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    _inflight_uploads[key] = future
    try:
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
        future.set_result(file_id)
        return file_id
    except BaseException as exc:
        future.set_exception(exc)
        future.exception()  # mark retrieved -> silence "exception never retrieved"
        raise
    finally:
        _inflight_uploads.pop(key, None)
