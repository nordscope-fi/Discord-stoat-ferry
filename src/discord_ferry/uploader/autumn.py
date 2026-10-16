"""Autumn file upload with retry and caching."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING

import aiohttp
from PIL import Image, UnidentifiedImageError

from discord_ferry.core.credential_transport import credential_transport_allowed
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
# so a file in the gap band is uploaded and then rejected with FileTooLarge (HTTP 422).
# Verified 2026-08-02
# against `features.limits.default.file_upload_size_limits` on https://api.stoat.chat/;
# `ferry probe` re-checks it against whatever instance you point it at.
_DEFAULT_TAG_SIZE_LIMITS: Mapping[str, int] = MappingProxyType(
    {
        "attachments": 20_000_000,
        "avatars": 4_000_000,
        "backgrounds": 6_000_000,
        "icons": 2_500_000,
        "banners": 6_000_000,
        "emojis": 500_000,
    }
)
_run_tag_size_limits: ContextVar[Mapping[str, int]] = ContextVar(
    "run_tag_size_limits", default=_DEFAULT_TAG_SIZE_LIMITS
)
_CROCKFORD_VALUES = {char: index for index, char in enumerate("0123456789ABCDEFGHJKMNPQRSTVWXYZ")}


def get_tag_size_limit(tag: str) -> int:
    """Return the active run's limit for an Autumn tag."""
    return _run_tag_size_limits.get()[tag]


class _RuntimeTagSizeLimits(Mapping[str, int]):
    """Read-only mapping that resolves values from the current async run."""

    def __getitem__(self, tag: str) -> int:
        return get_tag_size_limit(tag)

    def __iter__(self) -> Iterator[str]:
        return iter(_DEFAULT_TAG_SIZE_LIMITS)

    def __len__(self) -> int:
        return len(_DEFAULT_TAG_SIZE_LIMITS)

    def __eq__(self, other: object) -> bool:
        return dict(self) == other


TAG_SIZE_LIMITS: Mapping[str, int] = _RuntimeTagSizeLimits()


def _ulid_created_at(user_id: str) -> datetime | None:
    """Decode a Stoat ULID's 48-bit timestamp, or return None when invalid."""
    if len(user_id) != 26 or user_id[0] > "7":
        return None
    value = 0
    try:
        normalized = user_id.upper()
        for char in normalized:
            _CROCKFORD_VALUES[char]
        for char in normalized[:10]:
            value = value * 32 + _CROCKFORD_VALUES[char]
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (KeyError, OverflowError, OSError, ValueError):
        return None


def configure_upload_limits(
    root: object,
    user_id: str | None,
    *,
    now: datetime | None = None,
) -> str:
    """Select and install the upload limits Stoat applies to this account.

    Stoat v0.15.7 selects ``new_user`` when the account ULID is no older than
    ``global.new_user_hours``. If that cannot be established, Ferry selects
    ``default``. Missing or malformed advertised values retain Ferry's current
    limits for those tags.

    Returns the selected tier name.
    """
    tier = "default"
    limits: object = {}
    if isinstance(root, dict):
        features = root.get("features")
        if isinstance(features, dict):
            limits = features.get("limits", {})

    if isinstance(limits, dict) and user_id:
        global_limits = limits.get("global")
        new_user_hours = (
            global_limits.get("new_user_hours") if isinstance(global_limits, dict) else None
        )
        created_at = _ulid_created_at(user_id)
        current_time = now or datetime.now(UTC)
        if (
            isinstance(new_user_hours, int)
            and not isinstance(new_user_hours, bool)
            and new_user_hours >= 0
            and created_at is not None
            and timedelta(0) <= current_time - created_at <= timedelta(hours=new_user_hours)
        ):
            tier = "new_user"

    selected = limits.get(tier) if isinstance(limits, dict) else None
    advertised = selected.get("file_upload_size_limits") if isinstance(selected, dict) else None
    active = dict(_DEFAULT_TAG_SIZE_LIMITS)
    if isinstance(advertised, dict):
        for tag in active:
            value = advertised.get(tag)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                active[tag] = value
    _run_tag_size_limits.set(MappingProxyType(active))
    return tier


# Stoat accepts JPEG XL but Pillow does not decode it. A JPEG XL codestream starts
# with FF 0A; its container begins with the JXL box signature. Leave either form
# for Stoat to validate, including corrupt files bearing a JPEG XL signature.
_JXL_CODESTREAM_SIGNATURE = b"\xff\x0a"
_JXL_CONTAINER_SIGNATURE = b"\x00\x00\x00\x0cJXL \r\n\x87\n"
# Only inspect Stoat image formats, not document formats with external renderers.
_PILLOW_IMAGE_FORMATS = ("AVIF", "BMP", "GIF", "ICO", "JPEG", "PNG", "TIFF", "WEBP")
_IMAGE_SUFFIXES = frozenset(
    {".avif", ".bmp", ".gif", ".ico", ".jpeg", ".jpg", ".jxl", ".png", ".tif", ".tiff", ".webp"}
)


def _is_jpeg_xl(file_path: Path) -> bool:
    """Return whether *file_path* has one of JPEG XL's two file signatures."""
    with file_path.open("rb") as source:
        signature = source.read(len(_JXL_CONTAINER_SIGNATURE))
    return signature.startswith(_JXL_CODESTREAM_SIGNATURE) or signature.startswith(
        _JXL_CONTAINER_SIGNATURE
    )


def _is_image_filename(file_path: Path, filename: str | None) -> bool:
    """Return whether either local or outbound filename identifies an image."""
    return any(
        candidate.lower().endswith(tuple(_IMAGE_SUFFIXES))
        for candidate in (file_path.name, filename or "")
    )


def _is_jpeg_xl_filename(file_path: Path, filename: str | None) -> bool:
    """Return whether either local or outbound filename identifies JPEG XL."""
    return any(candidate.lower().endswith(".jxl") for candidate in (file_path.name, filename or ""))


def _validate_image(file_path: Path, tag: str, filename: str | None) -> None:
    """Reject unsupported or corrupt image data before sending an Autumn request."""
    if _is_jpeg_xl(file_path):
        return

    identified = False
    try:
        # Image.open identifies the format lazily. load() reads its data too, so a
        # truncated or otherwise corrupt image is rejected before the HTTP request.
        with Image.open(file_path, formats=_PILLOW_IMAGE_FORMATS) as image:
            identified = True
            image.verify()
        with Image.open(file_path, formats=_PILLOW_IMAGE_FORMATS) as image:
            for frame in range(getattr(image, "n_frames", 1)):
                image.seek(frame)
                image.load()
    except (
        EOFError,
        Image.DecompressionBombError,
        OSError,
        SyntaxError,
        UnidentifiedImageError,
        ValueError,
    ) as exc:
        # A filename hint must not override a format Pillow already identified.
        if not identified and _is_jpeg_xl_filename(file_path, filename):
            return
        if tag == "attachments" and not identified and not _is_image_filename(file_path, filename):
            return
        raise AutumnUploadError(
            f"File '{file_path.name}' is not a valid image for the {tag} tag."
        ) from exc


# Single-flight registry: coalesces concurrent first-uploads of the same cache key
# (``upload_cache_key(tag, file_path)``, the same key upload_with_cache caches under) so
# two parallel channel workers requesting the same physical file under the same tag
# upload it only once.
# Self-cleaning — every entry is popped on completion (success or failure).
_inflight_uploads: dict[str, asyncio.Future[str]] = {}


def upload_cache_key(tag: str, file_path: Path) -> str:
    """Key for ``MigrationState.upload_cache`` and the in-flight map: ``"<tag>:<path>"``.

    An Autumn file id belongs to the tag it was uploaded under (avatars, emojis, icons,
    banners, attachments), so the same local file under two tags must not share an entry
    (#984). Tags are fixed lowercase words with no colon, so the first colon always ends
    the tag.

    Entries written before #984 are keyed by the bare path. They carry no tag, so nothing
    can prove which one the id belongs to, and they never match this format: a resume
    treats them as a miss and uploads the file once more. They stay in the saved dict
    unread.
    """
    return f"{tag}:{file_path}"


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
    filename: str | None = None,
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
        filename: Optional name to send in multipart metadata. When omitted, use the local
            filename.
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

    url = f"{autumn_url.rstrip('/')}/{tag}"
    if not credential_transport_allowed(url):
        raise AutumnUploadError(
            "Credential-bearing Autumn requests require HTTPS except for localhost and private "
            "or loopback IP addresses."
        )

    if not file_path.exists():
        raise AutumnUploadError(f"File not found: {file_path}")

    if not file_path.is_file():
        raise AutumnUploadError(f"Not a regular file: {file_path}")

    file_size = file_path.stat().st_size
    if file_size == 0:
        raise AutumnUploadError(f"File '{file_path.name}' is empty.")

    limit = TAG_SIZE_LIMITS[tag]
    if file_size > limit:
        raise AutumnUploadError(
            f"File '{file_path.name}' is {file_size} bytes, "
            f"which exceeds the {tag} limit of {limit} bytes."
        )

    await asyncio.to_thread(_validate_image, file_path, tag, filename)

    headers = {"x-session-token": token}

    for attempt in range(MAX_RETRIES):
        # Autumn stores the multipart filename verbatim. aiohttp's default quotes Unicode as
        # percent escapes, so use raw UTF-8 to preserve names from Discord exports.
        form = aiohttp.FormData(quote_fields=False)
        fh = file_path.open("rb")
        try:
            multipart_filename = filename if filename is not None else file_path.name
            form.add_field("file", fh, filename=multipart_filename)

            async with session.post(
                url, data=form, headers=headers, allow_redirects=False
            ) as response:
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

                error: object | None = None
                if response.status == 422:
                    with contextlib.suppress(aiohttp.ClientError, ValueError):
                        error = await response.json(content_type=None)

                if response.status == 413 or (
                    response.status == 422
                    and isinstance(error, dict)
                    and error.get("type") == "FileTooLarge"
                ):
                    limit = TAG_SIZE_LIMITS.get(tag, 0)
                    if isinstance(error, dict):
                        server_limit = error.get("max")
                        if (
                            isinstance(server_limit, int)
                            and not isinstance(server_limit, bool)
                            and server_limit >= 0
                        ):
                            limit = server_limit
                    status = " (HTTP 422)" if response.status == 422 else ""
                    raise AutumnUploadError(
                        f"File too large{status}: {file_path.name} "
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
    filename: str | None = None,
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
        cache: Mutable dict mapping ``upload_cache_key(tag, file_path)`` -> Autumn file ID.
        delay: Seconds to sleep before uploading (rate-limit courtesy). Default 0.5s.
        filename: Optional name to send in multipart metadata. When omitted, use the local
            filename.
        verify_size: When True, pass size verification to the upload call. On a
            present-and-mismatched size the upload raises ``AutumnUploadError`` and is never
            cached. Best-effort — not all Autumn responses include ``size``.
        skip_cache: When True, bypass the cache lookup and in-flight coalescing so a fresh
            upload always runs. Use for consumers that may have attached an earlier id.

    Returns:
        Autumn file ID string.
    """
    key = upload_cache_key(tag, file_path)
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
            filename=filename,
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
