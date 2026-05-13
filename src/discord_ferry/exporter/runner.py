"""Async subprocess execution for DiscordChatExporter."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from typing import TYPE_CHECKING

import aiohttp

from discord_ferry.core.events import MigrationEvent
from discord_ferry.errors import DiscordAuthError, ExportError

if TYPE_CHECKING:
    from pathlib import Path

    from discord_ferry.config import FerryConfig
    from discord_ferry.core.events import EventCallback

logger = logging.getLogger(__name__)

# Regex to parse DCE stdout progress lines.
# Matches: "[1/15] Exporting #general... 50.0%" or "[1/15] Exporting #general..."
_DCE_PROGRESS_RE = re.compile(
    r"\[\d+/\d+\] Exporting #(?P<channel>[^\s.]+)\.{3}\s*(?:(?P<pct>[\d.]+)%)?"
)

_DISK_WARN_BYTES = 5_000_000_000  # 5 GB


def _build_dce_command(config: FerryConfig, dce_path: Path) -> list[str]:
    """Build the DCE CLI command list."""
    return [
        str(dce_path),
        "exportguild",
        "--token",
        config.discord_token or "",
        "-g",
        config.discord_server_id or "",
        "--media",
        "--reuse-media",
        "--markdown",
        "false",
        "--format",
        "Json",
        "--include-threads",
        "All",
        "--output",
        str(config.export_dir),
    ]


def _check_disk_space(export_dir: Path, on_event: EventCallback) -> None:
    """Emit a warning event if disk space is low."""
    try:
        export_dir.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(export_dir)
        if usage.free < _DISK_WARN_BYTES:
            free_gb = usage.free / 1_000_000_000
            on_event(
                MigrationEvent(
                    phase="export",
                    status="warning",
                    message=(
                        f"Low disk space ({free_gb:.1f} GB free). "
                        f"Large servers may need 5-10 GB for exports."
                    ),
                )
            )
    except OSError:
        pass  # Can't check disk space -- not critical


async def validate_discord_token(token: str) -> None:
    """Validate a Discord user token via the /users/@me endpoint.

    Raises:
        DiscordAuthError: If the token is invalid (401), API returns unexpected
            status, or the network is unreachable.
    """
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                "https://discord.com/api/v10/users/@me",
                headers={"Authorization": token},
            ) as resp,
        ):
            if resp.status == 401:
                raise DiscordAuthError("Invalid Discord token. Check that you copied it correctly.")
            if resp.status != 200:
                raise DiscordAuthError(f"Discord API returned unexpected status {resp.status}")
    except aiohttp.ClientError as exc:
        raise DiscordAuthError(f"Cannot reach Discord API: {exc}") from exc


async def run_dce_export(
    config: FerryConfig,
    dce_path: Path,
    on_event: EventCallback,
) -> Path:
    """Run DCE as an async subprocess and stream progress.

    Args:
        config: Ferry configuration with discord_token and discord_server_id.
        dce_path: Path to the DCE executable.
        on_event: Callback for progress events.

    Returns:
        Path to the export directory containing JSON files.

    Raises:
        ExportError: If DCE exits with a non-zero code.
        asyncio.CancelledError: If cancelled via config.cancel_event.
    """
    _check_disk_space(config.export_dir, on_event)

    cmd = _build_dce_command(config, dce_path)
    config.export_dir.mkdir(parents=True, exist_ok=True)

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # DCE prints nothing while it enumerates the guild's channels — on large
    # servers that pre-output phase can run several minutes and previously
    # left the GUI looking frozen on the last emitted status.
    on_event(
        MigrationEvent(
            phase="export",
            status="progress",
            message=(
                "DiscordChatExporter started — enumerating channels "
                "(large servers may take several minutes before per-channel progress appears)..."
            ),
        )
    )

    stderr_lines: list[str] = []

    try:
        assert process.stdout is not None
        assert process.stderr is not None

        async def _read_stderr() -> None:
            assert process.stderr is not None
            async for raw_line in process.stderr:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line:
                    stderr_lines.append(line)

        stderr_task = asyncio.create_task(_read_stderr())

        async for raw_line in process.stdout:
            # Check cancel event.
            if config.cancel_event and config.cancel_event.is_set():
                process.terminate()
                await process.wait()
                raise asyncio.CancelledError("Export cancelled by user")

            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            logger.debug("DCE: %s", line)

            match = _DCE_PROGRESS_RE.search(line)
            if match:
                channel = match.group("channel")
                pct_str = match.group("pct")
                pct = int(float(pct_str)) if pct_str else 0
                on_event(
                    MigrationEvent(
                        phase="export",
                        status="progress",
                        message=f"Exporting #{channel}...",
                        channel_name=channel,
                        current=pct,
                        total=100,
                    )
                )

        await stderr_task
        await process.wait()

    except asyncio.CancelledError:
        process.terminate()
        await process.wait()
        raise

    if process.returncode != 0:
        last_err = stderr_lines[-1] if stderr_lines else "Unknown error"
        raise ExportError(f"DCE export failed (exit code {process.returncode}): {last_err}")

    return config.export_dir
