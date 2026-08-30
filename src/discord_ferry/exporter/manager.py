"""DCE binary download, verification, and platform detection."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json as _json
import logging
import platform
import time as _time
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

import aiohttp
from packaging.tags import platform_tags

from discord_ferry.core.http import new_session, proxy_error_is_permanent, proxy_hint, tls_hint
from discord_ferry.errors import DCENotFoundError, ValidationError

if TYPE_CHECKING:
    from discord_ferry.core.events import EventCallback

logger = logging.getLogger(__name__)

DCE_VERSION = "2.48"

_GITHUB_RELEASE_URL = (
    "https://api.github.com/repos/Tyrrrz/DiscordChatExporter/releases/tags/{version}"
)

_MAX_DCE_BYTES = 150 * 1024 * 1024  # 150 MB hard ceiling


def _get_platform_key() -> str:
    """Return the DCE release target supported by the running Python installation."""
    system = platform.system()
    machine = platform.machine()
    tags = tuple(platform_tags())

    if system == "Windows":
        for tag, target in (
            ("win_amd64", "win-x64"),
            ("win_arm64", "win-arm64"),
            ("win32", "win-x86"),
        ):
            if tag in tags:
                return target
    elif system == "Darwin":
        if any(tag.startswith("macosx_") and tag.endswith("_arm64") for tag in tags):
            return "osx-arm64"
        if any(tag.startswith("macosx_") and tag.endswith("_x86_64") for tag in tags):
            return "osx-x64"
    elif system == "Linux":
        families = (
            ("musllinux", "x86_64", "linux-musl-x64"),
            ("manylinux", "x86_64", "linux-x64"),
            ("manylinux", "aarch64", "linux-arm64"),
            ("manylinux", "armv7l", "linux-arm"),
        )
        for family, architecture, target in families:
            if any(
                tag.startswith(f"{family}_") and tag.endswith(f"_{architecture}") for tag in tags
            ):
                return target

    evidence = ", ".join(tags[:3]) or "no compatibility tags"
    raise DCENotFoundError(f"Unsupported DCE platform: {system} {machine} ({evidence})")


def _verify_dce_checksum(zip_data: bytes, version: str, platform_key: str) -> None:
    """Verify DCE binary SHA-256 hash against pinned checksums.

    Hard-fails (raises) if no hash is pinned for the given version/platform —
    refusing to use an unverified binary closes the silent-skip hole that left
    ARM platforms unverified for years (issue #37). Still skips only when the
    bundled checksums file itself is absent (a packaging edge, not a platform
    coverage gap).

    Args:
        zip_data: Raw bytes of the downloaded zip archive.
        version: DCE release version string (e.g. "2.47.3").
        platform_key: Platform identifier matching the checksums file key
            (e.g. "win-x64", "linux-x64", "osx-x64").

    Raises:
        DCENotFoundError: If the computed hash does not match the pinned hash,
            or if no hash is pinned for this version/platform.
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
        raise DCENotFoundError(
            f"No SHA-256 hash is pinned for platform '{platform_key}' (DCE {version}). "
            "Refusing to use an unverified DCE binary. This platform may be new or "
            "unsupported — please file a bug to add its hash. To bypass at your own "
            "risk, pass --skip-dce-verify (CLI) or skip_verify=True (API)."
        )

    sha256 = hashlib.sha256(zip_data).hexdigest()
    if sha256 != expected:
        raise DCENotFoundError(
            f"DCE binary hash mismatch (expected {expected[:12]}..., got {sha256[:12]}...). "
            "Possible tampering or corrupt download. Use --skip-dce-verify to bypass."
        )


def _get_dce_dir() -> Path:
    """Return the directory where DCE binary should be stored."""
    return Path.home() / ".discord-ferry" / "bin" / "dce" / DCE_VERSION


def _get_asset_name(platform_key: str | None = None) -> str:
    """Return the DCE release asset name for a resolved or current platform."""
    target = platform_key or _get_platform_key()
    return f"DiscordChatExporter.Cli.{target}.zip"


def _validated_archive_members(
    archive: zipfile.ZipFile, destination: Path
) -> list[zipfile.ZipInfo]:
    """Return all archive members after proving each path stays in the destination."""
    root = destination.resolve()
    members = archive.infolist()
    for member in members:
        posix_name = PurePosixPath(member.filename)
        windows_name = PureWindowsPath(member.filename)
        candidate = (root / member.filename).resolve()
        unsafe = (
            posix_name.is_absolute()
            or windows_name.is_absolute()
            or ".." in posix_name.parts
            or ".." in windows_name.parts
            or not candidate.is_relative_to(root)
        )
        if unsafe:
            raise DCENotFoundError(
                f"Zip entry {member.filename!r} would extract outside target directory"
            )
    return members


def detect_dotnet() -> bool:
    """Retain the former public probe for import compatibility."""
    return True


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

    platform_key = _get_platform_key()
    asset_name = _get_asset_name(platform_key)
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
    # Bound OUTSIDE the try and reassigned before each GET. The `except` below
    # spans both requests, and `download_url` is bound inside the try, so on a
    # first-GET failure `target=download_url` would raise UnboundLocalError from
    # inside an error handler. This name is always bound before it is read.
    current_url = release_url
    for attempt in range(2):
        try:
            async with new_session() as session:
                current_url = release_url
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

                current_url = download_url
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
            # Two lines: message and control flow are separate decisions over the
            # same two values. Proxy wins over the certificate hint and they are
            # never concatenated, for the reason in api.py.
            cert = tls_hint(e)
            hint = proxy_hint(e, target=current_url) or cert
            if hint is not None and (cert is not None or proxy_error_is_permanent(e)):
                # A certificate failure, a proxy 407/403 or an unreachable proxy
                # cannot succeed on retry; do not pay the sleep.
                #
                # Gated on permanence because this jumps over the `attempt == 0`
                # retry. This is the DCE download, Ferry's highest-traffic
                # external failure path, so a proxy 502 or a connect timeout must
                # keep the retry it has always had.
                raise DCENotFoundError(f"Network error downloading DCE: {e}{hint}") from e
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
                raise DCENotFoundError(f"Network error downloading DCE: {e}{hint or ''}") from e

    assert data is not None  # unreachable — both-fail case raises above

    if not skip_verify:
        _verify_dce_checksum(data, DCE_VERSION, platform_key)

    dce_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            members = _validated_archive_members(zf, dce_dir)
            zf.extractall(dce_dir, members=members)
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
