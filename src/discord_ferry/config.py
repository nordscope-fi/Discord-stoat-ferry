"""Ferry configuration dataclass."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio

    import aiohttp


@dataclass
class FerryConfig:
    """Configuration for a migration run."""

    export_dir: Path
    stoat_url: str
    token: str
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
    verbose: bool = False
    max_channels: int = 200
    max_emoji: int = 100

    # Discord credentials (orchestrated mode only — never persisted to disk)
    discord_token: str | None = field(default=None, repr=False)
    discord_server_id: str | None = None

    # Skip the export phase (auto-set when export_dir is user-provided in offline mode)
    skip_export: bool = False

    # Runtime-only fields (not serialized)
    pause_event: asyncio.Event | None = field(default=None, repr=False)
    cancel_event: asyncio.Event | None = field(default=None, repr=False)
    session: aiohttp.ClientSession | None = field(default=None, repr=False)
