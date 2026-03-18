"""Phase 7.5: AVATARS — Pre-flight avatar download and Autumn upload."""

from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import aiohttp

from discord_ferry.core.events import MigrationEvent
from discord_ferry.migrator.api import get_session
from discord_ferry.parser.dce_parser import stream_messages
from discord_ferry.uploader.autumn import upload_with_cache

if TYPE_CHECKING:
    from discord_ferry.config import FerryConfig
    from discord_ferry.core.events import EventCallback
    from discord_ferry.parser.models import DCEAuthor, DCEExport
    from discord_ferry.state import MigrationState

logger = logging.getLogger(__name__)


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


async def _download_remote_avatar(
    session: aiohttp.ClientSession,
    url: str,
    output_dir: Path,
    author_id: str,
) -> Path | None:
    """Download a remote avatar image and save it locally.

    Args:
        session: An active aiohttp ClientSession.
        url: Remote URL of the avatar image.
        output_dir: Base output directory (avatars saved to ``output_dir/avatars/``).
        author_id: Discord author ID, used as the filename stem.

    Returns:
        Path to the downloaded file, or ``None`` if the download failed.
    """
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                logger.warning("Avatar download returned status %d for %s", resp.status, url)
                return None

            content_type = resp.headers.get("Content-Type", "")
            if not content_type.startswith("image/"):
                logger.warning(
                    "Avatar URL returned non-image Content-Type '%s' for %s",
                    content_type,
                    url,
                )
                return None

            # Derive extension from URL path, falling back to .webp.
            url_path = PurePosixPath(url.split("?")[0])
            ext = url_path.suffix if url_path.suffix else ".webp"

            avatar_dir = output_dir / "avatars"
            avatar_dir.mkdir(parents=True, exist_ok=True)
            dest = avatar_dir / f"{author_id}{ext}"

            data = await resp.read()
            dest.write_bytes(data)
            return dest
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to download avatar from %s: %s", url, exc)
        return None


async def run_avatars(
    config: FerryConfig,
    state: MigrationState,
    exports: list[DCEExport],
    on_event: EventCallback,
) -> None:
    """Upload unique author avatars to Autumn before message migration.

    Args:
        config: Ferry configuration (provides export_dir, stoat_url, token, upload_delay,
            output_dir).
        state: Migration state; ``avatar_cache`` will be populated with author_id -> Autumn ID.
        exports: Parsed DCE exports to scan for unique authors.
        on_event: Event callback for progress reporting.
    """
    on_event(
        MigrationEvent(phase="avatars", status="started", message="Scanning exports for avatars")
    )

    # Step 1: Collect unique authors across all exports.
    all_authors = _collect_unique_authors(exports)

    # Step 2: Filter out authors with empty avatar_url.
    authors_with_avatar = {aid: author for aid, author in all_authors.items() if author.avatar_url}

    # Step 3: Filter out authors already in cache.
    to_upload = {
        aid: author for aid, author in authors_with_avatar.items() if aid not in state.avatar_cache
    }

    if not to_upload:
        on_event(
            MigrationEvent(
                phase="avatars",
                status="completed",
                message="No unique avatars found.",
            )
        )
        return

    total = len(to_upload)
    uploaded = 0
    failed = 0

    # Sort deterministically by author ID.
    sorted_authors = sorted(to_upload.items(), key=lambda item: item[0])

    on_event(
        MigrationEvent(
            phase="avatars",
            status="progress",
            message=f"Found {total} unique avatars to upload",
            current=0,
            total=total,
        )
    )

    async with get_session(config) as session:
        for idx, (author_id, author) in enumerate(sorted_authors, start=1):
            on_event(
                MigrationEvent(
                    phase="avatars",
                    status="progress",
                    message=f"Uploading avatar {idx} of {total}",
                    current=idx,
                    total=total,
                )
            )

            avatar_url = author.avatar_url
            file_path: Path | None = None

            try:
                if avatar_url.startswith("http://") or avatar_url.startswith("https://"):
                    # Remote URL — download first.
                    file_path = await _download_remote_avatar(
                        session, avatar_url, config.output_dir, author_id
                    )
                    if file_path is None:
                        state.warnings.append(
                            {
                                "phase": "avatars",
                                "type": "avatar_download_failed",
                                "message": (
                                    f"Failed to download avatar for {author.name} "
                                    f"(non-image content type or HTTP error)"
                                ),
                            }
                        )
                        failed += 1
                        continue
                else:
                    # Local path — resolve against export_dir.
                    file_path = config.export_dir / avatar_url
                    if not file_path.exists():
                        state.warnings.append(
                            {
                                "phase": "avatars",
                                "type": "avatar_file_missing",
                                "message": (
                                    f"Avatar file not found for {author.name}: {file_path}"
                                ),
                            }
                        )
                        failed += 1
                        continue

                autumn_id = await upload_with_cache(
                    session,
                    state.autumn_url,
                    "avatars",
                    file_path,
                    config.token,
                    state.upload_cache,
                    config.upload_delay,
                )
                state.avatar_cache[author_id] = autumn_id
                uploaded += 1

            except Exception as exc:  # noqa: BLE001
                state.warnings.append(
                    {
                        "phase": "avatars",
                        "type": "avatar_upload_failed",
                        "message": f"Failed to upload avatar for {author.name}: {exc}",
                    }
                )
                failed += 1

    summary = f"Uploaded {uploaded} of {total} unique avatars." + (
        f" {failed} failed (will use default avatar)." if failed else ""
    )
    on_event(MigrationEvent(phase="avatars", status="completed", message=summary))
