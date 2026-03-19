"""Tests for exporter binary manager."""

from __future__ import annotations

import io
import subprocess
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aioresponses import aioresponses

from discord_ferry.exporter.manager import (
    DCE_VERSION,
    _get_asset_name,
    _get_dce_dir,
    _verify_dce_checksum,
    check_export_freshness,
    detect_dotnet,
    download_dce,
    get_dce_path,
)


def test_dce_version_is_pinned():
    assert DCE_VERSION == "2.46.1"


def test_get_dce_dir():
    """DCE binary directory is under ~/.discord-ferry/bin/dce/{version}/."""
    dce_dir = _get_dce_dir()
    assert dce_dir == Path.home() / ".discord-ferry" / "bin" / "dce" / DCE_VERSION


class TestGetAssetName:
    def test_windows_x64(self):
        with (
            patch("platform.system", return_value="Windows"),
            patch("platform.machine", return_value="AMD64"),
        ):
            assert "win-x64" in _get_asset_name()

    def test_linux_x64(self):
        with (
            patch("platform.system", return_value="Linux"),
            patch("platform.machine", return_value="x86_64"),
        ):
            assert "linux-x64" in _get_asset_name()

    def test_macos_arm64(self):
        with (
            patch("platform.system", return_value="Darwin"),
            patch("platform.machine", return_value="arm64"),
        ):
            assert "osx-arm64" in _get_asset_name()

    def test_linux_arm64(self):
        with (
            patch("platform.system", return_value="Linux"),
            patch("platform.machine", return_value="aarch64"),
        ):
            assert "linux-arm64" in _get_asset_name()

    def test_macos_x64(self):
        with (
            patch("platform.system", return_value="Darwin"),
            patch("platform.machine", return_value="x86_64"),
        ):
            assert "osx-x64" in _get_asset_name()

    def test_unsupported_os_raises(self):
        with (
            patch("platform.system", return_value="FreeBSD"),
            pytest.raises(ValueError, match="Unsupported"),
        ):
            _get_asset_name()

    def test_windows_x86_raises(self):
        with (
            patch("platform.system", return_value="Windows"),
            patch("platform.machine", return_value="x86"),
            pytest.raises(ValueError, match="Unsupported"),
        ):
            _get_asset_name()


class TestDetectDotnet:
    def test_windows_always_true(self):
        with patch("platform.system", return_value="Windows"):
            assert detect_dotnet() is True

    def test_linux_with_dotnet_8(self):
        with (
            patch("platform.system", return_value="Linux"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="8.0.100\n"),
            ),
        ):
            assert detect_dotnet() is True

    def test_linux_without_dotnet(self):
        with (
            patch("platform.system", return_value="Linux"),
            patch("subprocess.run", side_effect=FileNotFoundError),
        ):
            assert detect_dotnet() is False

    def test_linux_with_old_dotnet(self):
        with (
            patch("platform.system", return_value="Linux"),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="6.0.400\n"),
            ),
        ):
            assert detect_dotnet() is False


class TestGetDcePath:
    def test_returns_path_when_binary_exists(self, tmp_path):
        dce_dir = tmp_path / "dce"
        dce_dir.mkdir()
        exe = dce_dir / "DiscordChatExporter.Cli"
        exe.touch()
        exe.chmod(0o755)

        with patch("discord_ferry.exporter.manager._get_dce_dir", return_value=dce_dir):
            result = get_dce_path()
            assert result is not None
            assert result.exists()

    def test_returns_exe_path_on_windows(self, tmp_path):
        dce_dir = tmp_path / "dce"
        dce_dir.mkdir()
        exe = dce_dir / "DiscordChatExporter.Cli.exe"
        exe.touch()

        with (
            patch("discord_ferry.exporter.manager._get_dce_dir", return_value=dce_dir),
            patch("platform.system", return_value="Windows"),
        ):
            result = get_dce_path()
            assert result is not None
            assert result.name == "DiscordChatExporter.Cli.exe"

    def test_returns_none_when_not_found(self, tmp_path):
        with patch(
            "discord_ferry.exporter.manager._get_dce_dir", return_value=tmp_path / "nonexistent"
        ):
            result = get_dce_path()
            assert result is None


def _make_dce_zip() -> bytes:
    """Create a minimal valid DCE zip in memory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("DiscordChatExporter.Cli", "#!/bin/sh\necho ok\n")
    return buf.getvalue()


class TestDownloadDceRetry:
    @pytest.mark.asyncio
    async def test_retries_once_on_network_error(self, tmp_path):
        """download_dce retries once on network error then succeeds."""
        events = []
        dce_zip = _make_dce_zip()
        release_url = (
            f"https://api.github.com/repos/Tyrrrz/DiscordChatExporter/releases/tags/{DCE_VERSION}"
        )

        with (
            aioresponses() as m,
            patch("discord_ferry.exporter.manager._get_dce_dir", return_value=tmp_path),
            patch("discord_ferry.exporter.manager._get_asset_name", return_value="test.zip"),
            patch(
                "discord_ferry.exporter.manager.get_dce_path",
                return_value=tmp_path / "DiscordChatExporter.Cli",
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            # First attempt: network error
            m.get(release_url, exception=aiohttp.ClientError("network error"))
            # Second attempt: success
            m.get(
                release_url,
                status=200,
                payload={
                    "assets": [
                        {
                            "name": "test.zip",
                            "browser_download_url": "https://example.com/test.zip",
                        }
                    ]
                },
            )
            m.get("https://example.com/test.zip", status=200, body=dce_zip)
            (tmp_path / "DiscordChatExporter.Cli").touch()

            result = await download_dce(events.append)
            assert result is not None
            retry_msgs = [e for e in events if "retrying" in e.message.lower()]
            assert len(retry_msgs) >= 1

    @pytest.mark.asyncio
    async def test_fails_after_two_attempts(self, tmp_path):
        """download_dce raises after both attempts fail."""
        from discord_ferry.errors import DCENotFoundError

        events = []
        release_url = (
            f"https://api.github.com/repos/Tyrrrz/DiscordChatExporter/releases/tags/{DCE_VERSION}"
        )

        with (
            aioresponses() as m,
            patch("discord_ferry.exporter.manager._get_dce_dir", return_value=tmp_path),
            patch("discord_ferry.exporter.manager._get_asset_name", return_value="test.zip"),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            m.get(release_url, exception=aiohttp.ClientError("fail 1"))
            m.get(release_url, exception=aiohttp.ClientError("fail 2"))

            with pytest.raises(DCENotFoundError):
                await download_dce(events.append)


# ---------------------------------------------------------------------------
# check_export_freshness
# ---------------------------------------------------------------------------


class TestCheckExportFreshness:
    def _write_json_with_age(self, tmp_path: Path, age_days: float) -> Path:
        """Write a dummy JSON file with an mtime set to age_days days ago."""
        import time

        json_file = tmp_path / "export.json"
        json_file.write_text("{}")
        target_mtime = time.time() - age_days * 86400
        import os

        os.utime(json_file, (target_mtime, target_mtime))
        return json_file

    def test_export_freshness_recent(self, tmp_path: Path) -> None:
        """Files <7 days old produce no warnings."""
        self._write_json_with_age(tmp_path, 3)
        warnings = check_export_freshness(tmp_path)
        assert warnings == []

    def test_export_freshness_warning(self, tmp_path: Path) -> None:
        """Files 10 days old produce a warning string."""
        self._write_json_with_age(tmp_path, 10)
        warnings = check_export_freshness(tmp_path)
        assert len(warnings) == 1
        assert "stale" in warnings[0]

    def test_export_freshness_error(self, tmp_path: Path) -> None:
        """Files 45 days old raise ValidationError (without force)."""
        from discord_ferry.errors import ValidationError

        self._write_json_with_age(tmp_path, 45)
        with pytest.raises(ValidationError, match="45 days"):
            check_export_freshness(tmp_path)

    def test_export_freshness_error_with_force(self, tmp_path: Path) -> None:
        """Files 45 days old with force=True produce a warning but no error."""
        self._write_json_with_age(tmp_path, 45)
        warnings = check_export_freshness(tmp_path, force=True)
        assert len(warnings) == 1
        assert "stale" in warnings[0]

    def test_export_freshness_no_json_files(self, tmp_path: Path) -> None:
        """Directory with no JSON files produces no warnings."""
        warnings = check_export_freshness(tmp_path)
        assert warnings == []


# ---------------------------------------------------------------------------
# _verify_dce_checksum
# ---------------------------------------------------------------------------


def _checksums_json(version: str, platform_key: str, sha256: str) -> str:
    """Build a minimal checksums JSON string for tests."""
    import json

    return json.dumps({version: {platform_key: sha256}})


def _patch_checksums(checksums_json: str):  # type: ignore[return]
    """Context manager: patch importlib.resources.files to return checksums_json."""
    mock_files = patch("importlib.resources.files")

    class _Ctx:
        def __enter__(self) -> None:
            self._patcher = mock_files.__enter__()
            mock_ref = self._patcher.return_value.joinpath.return_value
            mock_ref.read_text.return_value = checksums_json

        def __exit__(self, *args: object) -> None:
            mock_files.__exit__(*args)

    return patch("importlib.resources.files")


class TestVerifyDceChecksum:
    def test_dce_checksum_verification_passes(self) -> None:
        """Matching hash produces no error."""
        import hashlib
        import json

        zip_data = b"fake-zip-content"
        expected_hash = hashlib.sha256(zip_data).hexdigest()
        checksums_json = _checksums_json("2.46.1", "linux-x64", expected_hash)

        with patch("importlib.resources.files") as mock_files:
            mock_ref = mock_files.return_value.joinpath.return_value
            mock_ref.read_text.return_value = checksums_json
            # Should not raise
            _verify_dce_checksum(zip_data, "2.46.1", "linux-x64")

    def test_dce_checksum_verification_fails(self) -> None:
        """Mismatched hash raises DCENotFoundError."""
        from discord_ferry.errors import DCENotFoundError

        zip_data = b"fake-zip-content"
        wrong_hash = "a" * 64  # clearly wrong
        checksums_json = _checksums_json("2.46.1", "linux-x64", wrong_hash)

        with patch("importlib.resources.files") as mock_files:
            mock_ref = mock_files.return_value.joinpath.return_value
            mock_ref.read_text.return_value = checksums_json
            with pytest.raises(DCENotFoundError, match="hash mismatch"):
                _verify_dce_checksum(zip_data, "2.46.1", "linux-x64")

    def test_dce_checksum_empty_hash_skips(self) -> None:
        """Empty string in checksums skips verification without error."""
        zip_data = b"fake-zip-content"
        checksums_json = _checksums_json("2.46.1", "linux-x64", "")

        with patch("importlib.resources.files") as mock_files:
            mock_ref = mock_files.return_value.joinpath.return_value
            mock_ref.read_text.return_value = checksums_json
            # Should not raise — empty hash means skip
            _verify_dce_checksum(zip_data, "2.46.1", "linux-x64")

    def test_dce_checksum_missing_version_skips(self) -> None:
        """Version not present in checksums file skips verification without error."""
        import json

        zip_data = b"fake-zip-content"
        # Only 2.99.0 is in the file, not 2.46.1
        checksums_json = json.dumps({"2.99.0": {"linux-x64": "a" * 64}})

        with patch("importlib.resources.files") as mock_files:
            mock_ref = mock_files.return_value.joinpath.return_value
            mock_ref.read_text.return_value = checksums_json
            # Should not raise — version not found means skip
            _verify_dce_checksum(zip_data, "2.46.1", "linux-x64")
