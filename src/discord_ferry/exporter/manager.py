"""DCE binary download, verification, and platform detection."""

from __future__ import annotations

import asyncio
import io
import logging
import platform
import subprocess
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp

from discord_ferry.errors import DCENotFoundError

if TYPE_CHECKING:
    from discord_ferry.core.events import EventCallback

logger = logging.getLogger(__name__)

DCE_VERSION = "2.46.1"

# Map (system, machine) to DCE release asset suffix.
_PLATFORM_MAP: dict[tuple[str, str], str] = {
    ("Windows", "AMD64"): "win-x64",
    ("Linux", "x86_64"): "linux-x64",
    ("Linux", "aarch64"): "linux-arm64",
    ("Darwin", "x86_64"): "osx-x64",
    ("Darwin", "arm64"): "osx-arm64",
}

_GITHUB_RELEASE_URL = (
    "https://api.github.com/repos/Tyrrrz/DiscordChatExporter/releases/tags/{version}"
)

_MAX_DCE_BYTES = 150 * 1024 * 1024  # 150 MB hard ceiling


def _get_dce_dir() -> Path:
    """Return the directory where DCE binary should be stored."""
    return Path.home() / ".discord-ferry" / "bin" / "dce" / DCE_VERSION


def _get_asset_name() -> str:
    """Return the DCE release asset name for the current platform."""
    system = platform.system()
    machine = platform.machine()
    suffix = _PLATFORM_MAP.get((system, machine))
    if suffix is None:
        raise ValueError(f"Unsupported platform: {system} {machine}")
    return f"DiscordChatExporter.Cli.{suffix}.zip"


def detect_dotnet() -> bool:
    """Check if .NET 8+ runtime is available. Always True on Windows (self-contained)."""
    if platform.system() == "Windows":
        return True
    try:
        result = subprocess.run(
            ["dotnet", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return False
        version_str = result.stdout.strip().split("-")[0]  # strip pre-release suffix
        major = int(version_str.split(".")[0])
        return major >= 8
    except (FileNotFoundError, ValueError, subprocess.TimeoutExpired):
        return False


def get_dce_path() -> Path | None:
    """Return path to DCE executable if it exists, else None."""
    dce_dir = _get_dce_dir()
    if not dce_dir.exists():
        return None

    if platform.system() == "Windows":
        exe = dce_dir / "DiscordChatExporter.Cli.exe"
    else:
        exe = dce_dir / "DiscordChatExporter.Cli"

    return exe if exe.exists() else None


async def download_dce(on_event: EventCallback) -> Path:
    """Download the pinned DCE release from GitHub and extract it.

    Args:
        on_event: Callback for progress events.

    Returns:
        Path to the DCE executable.

    Raises:
        DCENotFoundError: If download or extraction fails.
    """
    from discord_ferry.core.events import MigrationEvent

    asset_name = _get_asset_name()
    release_url = _GITHUB_RELEASE_URL.format(version=DCE_VERSION)
    dce_dir = _get_dce_dir()

    on_event(
        MigrationEvent(
            phase="export",
            status="progress",
            message=f"Downloading DiscordChatExporter v{DCE_VERSION}...",
        )
    )

    data: bytes | None = None
    for attempt in range(2):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    release_url, headers={"Accept": "application/vnd.github.v3+json"}
                ) as resp:
                    if resp.status != 200:
                        raise DCENotFoundError(
                            f"GitHub API returned {resp.status} for DCE v{DCE_VERSION}"
                        )
                    release_data = await resp.json()

                download_url: str | None = None
                for asset in release_data.get("assets", []):
                    if asset["name"] == asset_name:
                        download_url = asset["browser_download_url"]
                        break

                if download_url is None:
                    raise DCENotFoundError(
                        f"Asset {asset_name} not found in DCE v{DCE_VERSION} release"
                    )

                async with session.get(download_url) as resp:
                    if resp.status != 200:
                        raise DCENotFoundError(
                            f"Failed to download {asset_name}: HTTP {resp.status}"
                        )
                    data = await resp.read()
                    if len(data) > _MAX_DCE_BYTES:
                        raise DCENotFoundError(
                            f"DCE download unexpectedly large ({len(data)} bytes); aborting"
                        )

            break  # success — exit retry loop

        except (aiohttp.ClientError, DCENotFoundError) as e:
            if attempt == 0:
                on_event(
                    MigrationEvent(
                        phase="export",
                        status="progress",
                        message="Download failed, retrying in 3s...",
                    )
                )
                await asyncio.sleep(3)
            else:
                raise DCENotFoundError(f"Network error downloading DCE: {e}") from e

    assert data is not None  # unreachable — both-fail case raises above

    dce_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for member in zf.infolist():
                member_path = (dce_dir / member.filename).resolve()
                if not str(member_path).startswith(str(dce_dir.resolve())):
                    raise DCENotFoundError(
                        f"Zip entry {member.filename!r} would extract outside target directory"
                    )
            zf.extractall(dce_dir)
    except zipfile.BadZipFile as e:
        raise DCENotFoundError(f"Downloaded file is not a valid zip: {e}") from e

    exe_path = get_dce_path()
    if exe_path is None:
        raise DCENotFoundError(f"Extraction succeeded but executable not found in {dce_dir}")

    if platform.system() != "Windows":
        exe_path.chmod(0o755)

    on_event(
        MigrationEvent(
            phase="export",
            status="progress",
            message=f"DiscordChatExporter v{DCE_VERSION} ready.",
        )
    )

    return exe_path
