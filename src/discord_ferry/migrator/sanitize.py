"""String sanitization helpers for Stoat API field limits and filesystem safety."""

from __future__ import annotations

import re

# Stoat API enforces maxLength: 32 on most name fields.
_DEFAULT_MAX_LENGTH = 32

# Emoji names must match ^[a-z0-9_]+$ per OpenAPI spec.
_EMOJI_NAME_RE = re.compile(r"[^a-z0-9_]")

# Characters illegal in filenames on Windows (and / on all platforms).
_FS_ILLEGAL_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Win32 reserved device names (case-insensitive, with or without extension).
_WIN32_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def sanitize_filename(name: str, *, max_length: int = 200) -> str:
    """Make a string safe for use as a filename on all platforms.

    Replaces Windows-illegal characters and forward slash with underscores,
    strips leading/trailing dots and spaces (invalid on Windows), avoids
    reserved device names, and truncates to *max_length*.

    Returns ``"_"`` for an empty or all-whitespace input.
    """
    sanitized = _FS_ILLEGAL_RE.sub("_", name)
    sanitized = sanitized.strip(". ")
    if not sanitized:
        return "_"
    stem = sanitized.rsplit(".", 1)[0] if "." in sanitized else sanitized
    if stem.upper() in _WIN32_RESERVED:
        sanitized = f"_{sanitized}"
    return sanitized[:max_length]


def truncate_name(name: str, max_length: int = _DEFAULT_MAX_LENGTH, *, author_id: str = "") -> str:
    """Truncate a name to fit Stoat's field length limits.

    When ``author_id`` is provided and truncation is needed, a discriminator
    suffix ``#XXXX`` (last 4 chars of ``author_id``) is appended so that
    truncated masquerade names remain identifiable.

    Args:
        name: The string to truncate.
        max_length: Maximum allowed length (default 32).
        author_id: Optional author ID; last 4 chars used as discriminator suffix.

    Returns:
        The name, truncated if it exceeds max_length.
    """
    if len(name) <= max_length:
        return name
    if author_id and max_length >= 6:
        suffix = f"#{author_id[-4:]}"
        return name[: max_length - len(suffix)] + suffix
    return name[:max_length]


def sanitize_emoji_name(
    name: str,
    used_names: dict[str, int] | None = None,
) -> str:
    """Sanitize an emoji name to match Stoat's ``^[a-z0-9_]+$`` pattern.

    Lowercases, replaces invalid characters with underscores, strips
    leading/trailing underscores, truncates to 32 chars, and falls back
    to ``"emoji"`` if the result is empty.

    When *used_names* is provided, duplicate sanitized names receive a
    numeric suffix (``_2``, ``_3``, …) to avoid Stoat API collisions.
    The dict is mutated in-place and must be shared across all calls in a
    single migration run.

    Args:
        name: Raw emoji name string.
        used_names: Optional collision-tracking dict mapping sanitized name
            to the count of times it has been seen so far.

    Returns:
        A sanitized emoji name safe for the Stoat API, unique within the
        current run when *used_names* is supplied.
    """
    sanitized = _EMOJI_NAME_RE.sub("_", name.lower())
    sanitized = sanitized.strip("_")
    sanitized = sanitized[:_DEFAULT_MAX_LENGTH]
    sanitized = sanitized if sanitized else "emoji"

    if used_names is not None:
        if sanitized in used_names:
            count = used_names[sanitized] + 1
            suffix = f"_{count}"
            candidate = sanitized[: _DEFAULT_MAX_LENGTH - len(suffix)] + suffix
            # Bump until unique among ALL emitted names (handles a raw name that
            # equals a previously auto-generated suffixed name).
            while candidate in used_names:
                count += 1
                suffix = f"_{count}"
                candidate = sanitized[: _DEFAULT_MAX_LENGTH - len(suffix)] + suffix
            used_names[sanitized] = count  # advance the base counter
            used_names[candidate] = 1  # register the EMITTED name so it can't re-collide
            sanitized = candidate
        else:
            used_names[sanitized] = 1
    return sanitized
