"""Ferry configuration dataclass."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio

    import aiohttp

    from discord_ferry.core.security import SecureTokenStore


@dataclass
class FerryConfig:
    """Configuration for a migration run."""

    export_dir: Path
    stoat_url: str
    token: str = field(repr=False)
    server_id: str | None = None
    server_name: str | None = None
    dry_run: bool = False
    skip_messages: bool = False
    skip_emoji: bool = False
    skip_reactions: bool = False
    skip_threads: bool = False
    message_rate_limit: float = 1.0
    upload_delay: float = 0.5
    output_dir: Path = Path("./ferry-output")
    resume: bool = False
    incremental: bool = False
    verbose: bool = False
    max_channels: int = 200
    max_emoji: int = 100
    checkpoint_interval: int = 50
    skip_avatars: bool = False
    reaction_mode: str = "text"
    thread_strategy: str = "flatten"  # "flatten" | "merge" | "archive"
    min_thread_messages: int = 0
    validate_after: bool = False
    force: bool = False
    max_concurrent_requests: int = 5
    max_concurrent_channels: int = 3

    # Discord credentials (orchestrated mode only — never persisted to disk)
    discord_token: str | None = field(default=None, repr=False)
    discord_server_id: str | None = None

    # Skip the export phase (auto-set when export_dir is user-provided in offline mode)
    skip_export: bool = False

    # Skip SHA-256 verification of DCE binary downloads
    skip_dce_verify: bool = False

    # Post-upload verification: compare returned file metadata against local size.
    # Best-effort — not all Autumn responses include a size field.
    verify_uploads: bool = False

    # S16: Detect and log orphaned Autumn uploads after migration.
    # Compares state.autumn_uploads against state.referenced_autumn_ids and logs
    # any unreferenced files. Does not DELETE files (endpoint unverified).
    cleanup_orphans: bool = False

    # S17: Advisory migration lock via server description.
    # When True, overrides an existing lock older than 24h without prompting.
    force_unlock: bool = False

    # Runtime-only fields (not serialized)
    pause_event: asyncio.Event | None = field(default=None, repr=False)
    cancel_event: asyncio.Event | None = field(default=None, repr=False)
    session: aiohttp.ClientSession | None = field(default=None, repr=False)
    token_store: SecureTokenStore | None = field(default=None, repr=False)
