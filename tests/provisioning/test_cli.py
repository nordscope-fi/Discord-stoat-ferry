"""Tests for tests/provisioning/provision_test_server.py CLI."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import patch

from click.testing import CliRunner

from tests.provisioning.provision_test_server import cli

if TYPE_CHECKING:
    import pytest


def test_cli_missing_env_var_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_TEST_BOT_TOKEN", raising=False)
    # Click 8.2+ separates stderr from stdout by default; result.stderr is available.
    runner = CliRunner()
    result = runner.invoke(cli, ["provision", "--guild-id", "111"])
    assert result.exit_code == 2
    assert "DISCORD_TEST_BOT_TOKEN" in result.stderr


def test_cli_provision_requires_guild_id_or_create_guild() -> None:
    with patch.dict(os.environ, {"DISCORD_TEST_BOT_TOKEN": "test-token"}):
        runner = CliRunner()
        result = runner.invoke(cli, ["provision"])
        # Click should error on missing --guild-id and --create-guild
        assert result.exit_code != 0
