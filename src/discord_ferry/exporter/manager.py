"""DCE binary download, verification, and platform detection."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json as _json
import logging
import platform
import subprocess
import time as _time
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp

from discord_ferry.errors import DCENotFoundError, ValidationError

if TYPE_CHECKING:
    from discord_ferry.core.events import EventCallback

logger = logging.getLogger(__name__)

DCE_VERSION = "2.47.1"

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


def _get_platform_key() -> str | None:
    """Return the checksums-file platform key for the current platform, or None if unsupported."""
    system = platform.system()
    machine = platform.machine()
    return _PLATFORM_MAP.get((system, machine))


def _verify_dce_checksum(zip_data: bytes, version: str, platform_key: str) -> None:
    """Verify DCE binary SHA-256 hash against pinned checksums.

    Silently skips if no checksums file is present or no hash is pinned for
    the given version/platform combination.

    Args:
        zip_data: Raw bytes of the downloaded zip archive.
        version: DCE release version string (e.g. "2.47.1").
        platform_key: Platform identifier matching the checksums file key
            (e.g. "win-x64", "linux-x64", "osx-x64").

    Raises:
        DCENotFoundError: If the computed hash does not match the pinned hash.
    """
    try:
        import importlib.resources as pkg_resources

        checksums_ref = pkg_resources.files("discord_ferry").joinpath("dce_checksums.json")
        checksums_text = checksums_ref.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        return  # No checksums file — skip verification

    checksums = _json.loads(checksums_text)
    expected = checksums.get(version, {}).get(platform_key, "")
    if not expected:
        return  # No hash pinned for this version/platform — skip

    sha256 = hashlib.sha256(zip_data).hexdigest()
    if sha256 != expected:
        raise DCENotFoundError(
            f"DCE binary hash mismatch (expected {expected[:12]}..., got {sha256[:12]}...). "
            "Possible tampering or corrupt download. Use --skip-dce-verify to bypass."
        )


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


async def download_dce(on_event: EventCallback, *, skip_verify: bool = False) -> Path:
    """Download the pinned DCE release from GitHub and extract it.

    Args:
        on_event: Callback for progress events.
        skip_verify: If True, skip SHA-256 hash verification of the download.

    Returns:
        Path to the DCE executable.

    Raises:
        DCENotFoundError: If download, verification, or extraction fails.
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

    if not skip_verify:
        platform_key = _get_platform_key()
        if platform_key is not None:
            _verify_dce_checksum(data, DCE_VERSION, platform_key)

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


def check_export_freshness(export_dir: Path, *, force: bool = False) -> list[str]:
    """Check if DCE export files are stale. Returns list of warning strings.

    Args:
        export_dir: Directory containing the DCE export JSON files.
        force: If True, raise is suppressed for exports >30 days old (warning only).

    Returns:
        List of warning strings (may be empty).

    Raises:
        ValidationError: If the export is >30 days old and ``force`` is False.
    """
    warnings: list[str] = []
    json_files = list(export_dir.glob("**/*.json"))
    if not json_files:
        return warnings
    newest_mtime = max(f.stat().st_mtime for f in json_files)
    age_days = (_time.time() - newest_mtime) / 86400
    if age_days > 30 and not force:
        raise ValidationError(
            f"DCE export is {age_days:.0f} days old (>30 days). Use --force to proceed anyway."
        )
    elif age_days > 7:
        warnings.append(f"DCE export is {age_days:.0f} days old — data may be stale")
    return warnings
