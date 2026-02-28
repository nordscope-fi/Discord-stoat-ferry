"""Tests for FerryConfig dataclass."""

from pathlib import Path

from discord_ferry.config import FerryConfig


def test_default_discord_fields_are_none():
    """New Discord fields default to None."""
    cfg = FerryConfig(export_dir=Path("/tmp/test"), stoat_url="https://stoat.example", token="tok")
    assert cfg.discord_token is None
    assert cfg.discord_server_id is None
    assert cfg.skip_export is False


def test_discord_token_not_in_repr():
    """discord_token must be excluded from repr (security)."""
    cfg = FerryConfig(
        export_dir=Path("/tmp/test"),
        stoat_url="https://stoat.example",
        token="tok",
        discord_token="secret-discord-token",
    )
    assert "secret-discord-token" not in repr(cfg)


def test_orchestrated_mode_detection():
    """When discord_token and discord_server_id are set, skip_export remains False."""
    cfg = FerryConfig(
        export_dir=Path("/tmp/test"),
        stoat_url="https://stoat.example",
        token="tok",
        discord_token="dt",
        discord_server_id="123",
    )
    assert cfg.discord_token == "dt"
    assert cfg.discord_server_id == "123"
    assert cfg.skip_export is False


def test_offline_mode_detection():
    """When skip_export is True, we're in offline mode."""
    cfg = FerryConfig(
        export_dir=Path("/tmp/exports"),
        stoat_url="https://stoat.example",
        token="tok",
        skip_export=True,
    )
    assert cfg.skip_export is True
    assert cfg.discord_token is None
