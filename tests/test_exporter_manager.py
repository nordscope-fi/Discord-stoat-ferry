"""Tests for exporter binary manager."""

from __future__ import annotations

import io
import ssl
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
    assert DCE_VERSION == "2.47.1"


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
            patch("discord_ferry.exporter.manager._verify_dce_checksum"),
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

    @pytest.mark.asyncio
    async def test_certificate_error_skips_the_download_retry(self, tmp_path):
        """SC-134-25.

        The mock must raise from an awaited call that yields. A synchronously
        raising mock inside a retry loop has produced an unbounded hang in this
        project before.
        """
        from discord_ferry.errors import DCENotFoundError

        events = []
        release_url = (
            f"https://api.github.com/repos/Tyrrrz/DiscordChatExporter/releases/tags/{DCE_VERSION}"
        )
        key = aiohttp.client_reqrep.ConnectionKey(
            "api.github.com", 443, True, True, None, None, None
        )
        cert_error = aiohttp.ClientConnectorCertificateError(
            key, ssl.SSLCertVerificationError("bad")
        )

        with (
            aioresponses() as m,
            patch("discord_ferry.exporter.manager._get_dce_dir", return_value=tmp_path),
            patch("discord_ferry.exporter.manager._get_asset_name", return_value="test.zip"),
            patch("asyncio.sleep", new_callable=AsyncMock) as slept,
        ):
            m.get(release_url, exception=cert_error)

            with pytest.raises(DCENotFoundError) as caught:
                await download_dce(events.append)

        assert "SSL_CERT_FILE" in str(caught.value)
        assert slept.await_count == 0, "a certificate error must not pay the 3s retry sleep"


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


class TestVerifyDceChecksum:
    def test_dce_checksum_verification_passes(self) -> None:
        """Matching hash produces no error."""
        import hashlib

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

    def test_dce_checksum_empty_hash_raises(self) -> None:
        """Empty string in checksums hard-fails (issue #37 phase 2)."""
        from discord_ferry.errors import DCENotFoundError

        zip_data = b"fake-zip-content"
        checksums_json = _checksums_json("2.46.1", "linux-x64", "")

        with patch("importlib.resources.files") as mock_files:
            mock_ref = mock_files.return_value.joinpath.return_value
            mock_ref.read_text.return_value = checksums_json
            # Empty hash is no longer a silent skip — refuse the unverified binary.
            with pytest.raises(DCENotFoundError, match="No SHA-256 hash is pinned"):
                _verify_dce_checksum(zip_data, "2.46.1", "linux-x64")

    def test_dce_checksum_missing_version_raises(self) -> None:
        """Version not present in checksums file hard-fails (issue #37 phase 2)."""
        import json

        from discord_ferry.errors import DCENotFoundError

        zip_data = b"fake-zip-content"
        # Only 2.99.0 is in the file, not 2.46.1
        checksums_json = json.dumps({"2.99.0": {"linux-x64": "a" * 64}})

        with patch("importlib.resources.files") as mock_files:
            mock_ref = mock_files.return_value.joinpath.return_value
            mock_ref.read_text.return_value = checksums_json
            with pytest.raises(DCENotFoundError, match="No SHA-256 hash is pinned"):
                _verify_dce_checksum(zip_data, "2.46.1", "linux-x64")

    def test_dce_checksum_unpinned_platform_raises(self) -> None:
        """A platform with no pinned hash hard-fails instead of silently passing.

        This is the exact hole issue #37 closes: before phase 2, an unpinned
        platform key (e.g. a hypothetical 'win-arm64') silently skipped
        verification, shipping an unverified binary with no signal.
        """
        from discord_ferry.errors import DCENotFoundError

        zip_data = b"fake-zip-content"
        # Hash pinned for linux-x64 only; the synthetic platform is absent.
        checksums_json = _checksums_json("2.46.1", "linux-x64", "a" * 64)

        with patch("importlib.resources.files") as mock_files:
            mock_ref = mock_files.return_value.joinpath.return_value
            mock_ref.read_text.return_value = checksums_json
            with pytest.raises(DCENotFoundError, match="win-arm64"):
                _verify_dce_checksum(zip_data, "2.46.1", "win-arm64")


class TestDceChecksumsJson:
    """Schema validation for src/discord_ferry/dce_checksums.json.

    These tests prevent silent-skip regressions: if a new platform is added to
    _PLATFORM_MAP without a corresponding hash in this JSON,
    test_dce_checksums_json_covers_all_supported_platforms fires.
    """

    def _load_checksums(self) -> dict:
        import importlib.resources as pkg_resources
        import json as _json

        ref = pkg_resources.files("discord_ferry").joinpath("dce_checksums.json")
        return _json.loads(ref.read_text(encoding="utf-8"))

    def test_dce_checksums_json_is_well_formed(self):
        data = self._load_checksums()
        assert isinstance(data, dict)
        assert len(data) >= 1, "checksums file has no version entries"

    def test_dce_checksums_json_covers_current_version(self):
        data = self._load_checksums()
        assert DCE_VERSION in data, (
            f"dce_checksums.json is missing entries for the pinned DCE_VERSION={DCE_VERSION}"
        )

    def test_dce_checksums_json_covers_all_supported_platforms(self):
        """REGRESSION GUARD: every platform in _PLATFORM_MAP must have a hash for DCE_VERSION.

        This is the structural test that would have caught the original #37 bug:
        adding a platform to _PLATFORM_MAP without pinning its hash silently
        skipped verification for users on that platform.
        """
        from discord_ferry.exporter.manager import _PLATFORM_MAP

        data = self._load_checksums()
        version_block = data.get(DCE_VERSION, {})
        for (_system, _machine), platform_key in _PLATFORM_MAP.items():
            assert platform_key in version_block, (
                f"dce_checksums.json[{DCE_VERSION!r}] is missing hash for "
                f"platform '{platform_key}' (in _PLATFORM_MAP)"
            )

    def test_dce_checksums_json_values_look_like_sha256(self):
        import re

        sha256_re = re.compile(r"^[0-9a-f]{64}$")
        data = self._load_checksums()
        for version, block in data.items():
            for platform_key, value in block.items():
                assert sha256_re.match(value), (
                    f"dce_checksums.json[{version!r}][{platform_key!r}] = {value!r} "
                    "does not look like a 64-char lowercase hex SHA-256 string"
                )


# ---------------------------------------------------------------------------
# #135 — a refusing proxy must name itself at exporter/manager.py:214
# ---------------------------------------------------------------------------


async def test_a_refused_proxy_names_the_proxy(tmp_path, fake_proxy, proxy_env, os_proxy) -> None:
    """SC-135-28. Killing: proxy_hint defined and never called at manager.py:214.

    This site also carried the worst of the two target-binding hazards: the
    `except` spans both `session.get` calls and `download_url` is bound INSIDE
    the try, so a failure on the first GET left it unbound and
    `proxy_hint(e, target=download_url)` would raise UnboundLocalError from
    inside an error handler. This test fails on the FIRST GET, so it is exactly
    the case that would have raised.
    """
    from discord_ferry.errors import DCENotFoundError

    make, _ = fake_proxy
    server = await make(b"403 Forbidden")
    port = server.sockets[0].getsockname()[1]
    async with server:
        with (
            os_proxy({}),
            proxy_env(HTTPS_PROXY=f"http://127.0.0.1:{port}"),
            patch("discord_ferry.exporter.manager._get_dce_dir", return_value=tmp_path),
            patch("discord_ferry.exporter.manager._get_asset_name", return_value="test.zip"),
            patch("discord_ferry.exporter.manager.asyncio.sleep", new_callable=AsyncMock) as slept,
        ):
            with pytest.raises(DCENotFoundError) as caught:
                await download_dce(lambda _e: None)

    message = str(caught.value)
    assert "Network error downloading DCE" in message
    # ONE assertion: see the note in tests/test_exporter_runner.py. The split
    # version put `assert "api.github.com" in message` above the phrase, so the
    # phrase line never ran under this site's own mutant.
    assert f"The request to api.github.com went through the proxy at 127.0.0.1:{port}" in message
    assert slept.await_count == 0, "a refused proxy is permanent and must not pay the 3s retry"


async def test_a_proxy_502_still_retries(tmp_path, fake_proxy, proxy_env, os_proxy) -> None:
    """SC-135-37. Killing: wiring proxy_hint at manager.py without the
    permanence gate, which would turn a retryable blip into a hard failure on
    the DCE download, this repo's highest-traffic external failure path.

    A 502 from the proxy is NOT permanent, so this must take the `attempt == 0`
    retry and reach the socket twice. The message still names the proxy: the
    hint is computed either way, only the short-circuit is gated.
    """
    from discord_ferry.errors import DCENotFoundError

    make, captured = fake_proxy
    server = await make(b"502 Bad Gateway")
    port = server.sockets[0].getsockname()[1]
    async with server:
        with (
            os_proxy({}),
            proxy_env(HTTPS_PROXY=f"http://127.0.0.1:{port}"),
            patch("discord_ferry.exporter.manager._get_dce_dir", return_value=tmp_path),
            patch("discord_ferry.exporter.manager._get_asset_name", return_value="test.zip"),
            patch("discord_ferry.exporter.manager.asyncio.sleep", new_callable=AsyncMock),
        ):
            with pytest.raises(DCENotFoundError) as caught:
                await download_dce(lambda _e: None)

    assert len(captured) >= 2, "the 502 was treated as permanent and never retried"
    assert f"The request to api.github.com went through the proxy at 127.0.0.1:{port}" in str(
        caught.value
    )
