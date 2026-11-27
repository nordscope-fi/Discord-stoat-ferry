"""Phase 7.5: AVATARS, resolve each author's picture link once, before messages."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from yarl import URL

from discord_ferry.core.events import MigrationEvent
from discord_ferry.core.http import new_session, remote_media_refusal
from discord_ferry.discord.client import fetch_user
from discord_ferry.errors import DiscordAuthError, DiscordForbiddenError, MigrationError
from discord_ferry.parser.dce_parser import stream_messages
from discord_ferry.state import save_state

if TYPE_CHECKING:
    import aiohttp

    from discord_ferry.config import FerryConfig
    from discord_ferry.core.events import EventCallback
    from discord_ferry.parser.models import DCEAuthor, DCEExport
    from discord_ferry.state import MigrationState

logger = logging.getLogger(__name__)

#: Longest masquerade avatar link Stoat accepts (``#[validate(length(min = 1, max = 256))]``).
AVATAR_LINK_MAX_LENGTH = 256

_DISCORD_CDN = "https://cdn.discordapp.com"
_SNOWFLAKE = r"[0-9]{1,20}"
_HASH = r"(?:a_)?[0-9a-f]+"
_IMAGE_SUFFIX = r"\.(?:png|jpe?g|webp|gif)"
_USER_ID = re.compile(_SNOWFLAKE)
_AVATAR_HASH = re.compile(_HASH)
#: The documented picture paths on Discord's CDN: a user picture, a default picture and a
#: per-server picture. Matched against the raw path, so an encoded separator never matches.
_PICTURE_PATH = re.compile(
    "|".join(
        (
            rf"/avatars/{_SNOWFLAKE}/{_HASH}{_IMAGE_SUFFIX}",
            r"/embed/avatars/[0-9]\.png",
            rf"/guilds/{_SNOWFLAKE}/users/{_SNOWFLAKE}/avatars/{_HASH}{_IMAGE_SUFFIX}",
        )
    )
)

ExportLinkKind = Literal["link", "unusable", "skip"]

#: Reasons that depend on the token, so a resumed run asks again. Every other reason is a
#: property of the author or the export and comes out the same on a second attempt.
_TOKEN_REASONS = frozenset({"no_discord_token", "token_rejected", "lookup_not_allowed"})

#: Wall-clock gap between mid-phase state saves, so a hard kill loses a few seconds of lookups.
_SAVE_INTERVAL_SECONDS = 5.0


def classify_export_link(url: str) -> tuple[ExportLinkKind, str]:
    """Decide whether an export ``avatarUrl`` can be used as a picture link.

    Returns ``("link", cleaned_link)`` for an https link on Discord's own picture host
    (``remote_media_refusal`` accepts it) whose path is a documented picture path, with the
    query string and fragment removed. Returns ``("unusable", "")`` for an http(s) link that
    ``remote_media_refusal`` refuses. Returns ``("skip", "")`` for everything else: an empty
    value, a local file name (the ``--media`` case, which is never read), or a Discord-host
    link with an undocumented path. The caller then looks the author up instead.

    The caller applies ``AVATAR_LINK_MAX_LENGTH``.
    """
    if not url.lower().startswith(("http://", "https://")):
        return "skip", ""
    if remote_media_refusal(url) is not None:
        return "unusable", ""
    target = URL(url)
    if _PICTURE_PATH.fullmatch(target.raw_path) is None:
        return "skip", ""
    return "link", f"https://{target.host}{target.raw_path}"


def lookup_avatar_link(user_id: str, avatar_hash: object) -> str | None:
    """Build the picture link for a looked-up user, or ``None`` when it is not safe to.

    The id must be all digits and the hash must be ``(a_)?`` followed by lower-case hex, so
    neither can add a path segment, a query or a host to the link. An animated picture (hash
    starting ``a_``) gets ``.gif``, anything else ``.png``.
    """
    if _USER_ID.fullmatch(user_id) is None:
        return None
    if not isinstance(avatar_hash, str) or _AVATAR_HASH.fullmatch(avatar_hash) is None:
        return None
    extension = "gif" if avatar_hash.startswith("a_") else "png"
    return f"{_DISCORD_CDN}/avatars/{user_id}/{avatar_hash}.{extension}"


def _collect_unique_authors(exports: list[DCEExport]) -> dict[str, DCEAuthor]:
    """Scan all messages across all exports and return unique authors.

    Args:
        exports: List of parsed DCE exports to scan.

    Returns:
        Dict of ``author_id -> DCEAuthor`` (first occurrence wins for dedup).
    """
    authors: dict[str, DCEAuthor] = {}
    for export in exports:
        msg_iter = (
            stream_messages(export.json_path)
            if export.json_path is not None
            else iter(export.messages)
        )
        for msg in msg_iter:
            if msg.author.id not in authors:
                authors[msg.author.id] = msg.author
    return authors


@dataclass
class _Lookups:
    """Run-wide Discord lookup bookkeeping.

    ``stopped_reason`` is set once Discord has shown that every later lookup would fail the
    same way. ``succeeded`` counts lookups Discord answered, which decides whether a 403 means
    "this token cannot use the route" (none yet) or "this one author" (some already worked).
    """

    stopped_reason: str | None = None
    succeeded: int = 0


def _is_settled(state: MigrationState, author_id: str) -> bool:
    """True when this author needs no further work on a resumed run."""
    if author_id in state.author_avatar_links:
        return True
    reason = state.author_avatar_reasons.get(author_id)
    return reason is not None and reason not in _TOKEN_REASONS


def _record_link(state: MigrationState, author_id: str, link: str) -> None:
    state.author_avatar_reasons.pop(author_id, None)
    state.author_avatar_links[author_id] = link


def _record_reason(state: MigrationState, author_id: str, reason: str) -> None:
    state.author_avatar_links.pop(author_id, None)
    state.author_avatar_reasons[author_id] = reason


def _settle_link(state: MigrationState, author_id: str, link: str) -> None:
    """Save ``link``, or ``link_too_long`` when Stoat would refuse the masquerade."""
    if len(link) > AVATAR_LINK_MAX_LENGTH:
        _record_reason(state, author_id, "link_too_long")
    else:
        _record_link(state, author_id, link)


async def _lookup_picture(
    session: aiohttp.ClientSession, token: str, author_id: str, lookups: _Lookups
) -> tuple[str | None, str]:
    """Ask Discord for one author's picture.

    Returns ``(link, "")`` on success, or ``(None, reason_code)``. Discord's error text is
    never read here, so it cannot reach state, a log line or the report.
    """
    if lookups.stopped_reason is not None:
        return None, lookups.stopped_reason
    try:
        user = await fetch_user(session, token, author_id)
    except DiscordAuthError:
        lookups.stopped_reason = "token_rejected"
        return None, "token_rejected"
    except DiscordForbiddenError:
        if lookups.succeeded == 0:
            lookups.stopped_reason = "lookup_not_allowed"
            return None, "lookup_not_allowed"
        return None, "lookup_failed"
    except (MigrationError, TimeoutError):
        # A request that outlasts the session's total timeout raises a plain TimeoutError,
        # which the client's retry loop does not convert. It costs this author only.
        return None, "lookup_failed"
    lookups.succeeded += 1
    avatar_hash = user.get("avatar")
    if avatar_hash is None:
        return None, "no_picture"
    link = lookup_avatar_link(author_id, avatar_hash)
    if link is None:
        return None, "lookup_failed"
    return link, ""


async def _resolve_author(
    config: FerryConfig,
    state: MigrationState,
    session: aiohttp.ClientSession,
    author: DCEAuthor,
    lookups: _Lookups,
) -> None:
    """Save either a picture link or a reason code for one author."""
    kind, export_link = classify_export_link(author.avatar_url)
    if kind == "unusable":
        _record_reason(state, author.id, "export_link_unusable")
        return
    if kind == "link":
        _settle_link(state, author.id, export_link)
        return
    if not config.discord_token:
        _record_reason(state, author.id, "no_discord_token")
        return
    link, reason = await _lookup_picture(session, config.discord_token, author.id, lookups)
    if link is not None:
        _settle_link(state, author.id, link)
    else:
        _record_reason(state, author.id, reason)


async def run_avatars(
    config: FerryConfig,
    state: MigrationState,
    exports: list[DCEExport],
    on_event: EventCallback,
) -> None:
    """Resolve each unique author's picture link before message migration.

    Every author ends in exactly one of ``state.author_avatar_links`` (a link on Discord's
    picture host, at most 256 characters) or ``state.author_avatar_reasons`` (a fixed code).
    Nothing is uploaded to Autumn, no local file is read, and nothing is sent to Stoat. A dry
    run does the same lookups, as the Discord metadata fetch does.

    A resumed run skips authors that are already settled: those with a link, ``no_picture``
    or ``lookup_failed``. It asks again for the reasons that depend on the token.

    Args:
        config: Ferry configuration (``discord_token`` enables lookups).
        state: Migration state; the two author picture maps are filled in.
        exports: Parsed DCE exports to scan for unique authors.
        on_event: Event callback for progress reporting.
    """
    on_event(
        MigrationEvent(phase="avatars", status="started", message="Scanning exports for authors")
    )
    authors = sorted(_collect_unique_authors(exports).items(), key=lambda item: item[0])
    pending = [author for author_id, author in authors if not _is_settled(state, author_id)]
    lookups = _Lookups()
    last_save = time.monotonic()
    async with new_session() as session:
        for author in pending:
            await _resolve_author(config, state, session, author, lookups)
            now = time.monotonic()
            if now - last_save >= _SAVE_INTERVAL_SECONDS:
                save_state(state, config.output_dir)
                last_save = now
    on_event(
        MigrationEvent(
            phase="avatars",
            status="completed",
            message=f"Resolved pictures for {len(authors)} authors.",
        )
    )
