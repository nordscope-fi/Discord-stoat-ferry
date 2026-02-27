"""Ferry configuration dataclass."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio


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

    # GUI pause/cancel support (not serialized, not used by CLI)
    pause_event: asyncio.Event | None = field(default=None, repr=False)
    cancel_event: asyncio.Event | None = field(default=None, repr=False)
