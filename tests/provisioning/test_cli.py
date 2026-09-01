"""Tests for tests/provisioning/provision_test_server.py CLI."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from aioresponses import aioresponses
from click.testing import CliRunner

from tests.provisioning.provision_test_server import cli

if TYPE_CHECKING:
    from collections.abc import Generator


DISCORD_API = "https://discord.com/api/v10"


@pytest.fixture
def mock_discord_for_state() -> Generator[aioresponses, None, None]:
    with aioresponses() as m:
        yield m


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


def test_cli_teardown_requires_guild_id() -> None:
    with patch.dict(os.environ, {"DISCORD_TEST_BOT_TOKEN": "test-token"}):
        runner = CliRunner()
        result = runner.invoke(cli, ["teardown"])
        assert result.exit_code != 0


def test_cli_verify_requires_guild_id() -> None:
    with patch.dict(os.environ, {"DISCORD_TEST_BOT_TOKEN": "test-token"}):
        runner = CliRunner()
        result = runner.invoke(cli, ["verify"])
        assert result.exit_code != 0


def test_cli_teardown_deletes_marker_channels_with_yes_flag(
    mock_discord_for_state: aioresponses,
) -> None:
    """SC-S4-HP1: teardown deletes only marker-carrying entities."""
    guild = "111"
    mock_discord_for_state.get(
        f"{DISCORD_API}/guilds/{guild}/channels",
        payload=[
            {"id": "100", "name": "general", "type": 0, "topic": "[ferry-fixture] x"},
            {"id": "101", "name": "Feedback Forum", "type": 15, "topic": "[ferry-fixture] y"},
            {"id": "200", "name": "user-channel", "type": 0, "topic": None},
        ],
    )
    mock_discord_for_state.get(
        f"{DISCORD_API}/guilds/{guild}/threads/active",
        payload={"threads": [], "members": []},
    )
    mock_discord_for_state.get(
        f"{DISCORD_API}/channels/100/threads/archived/public",
        payload={"threads": [], "members": [], "has_more": False},
    )
    mock_discord_for_state.get(
        f"{DISCORD_API}/channels/200/threads/archived/public",
        payload={"threads": [], "members": [], "has_more": False},
    )
    for ch_id in ("100", "101", "200"):
        mock_discord_for_state.get(
            f"{DISCORD_API}/channels/{ch_id}/messages?limit=100",
            payload=[],
        )
    mock_discord_for_state.delete(f"{DISCORD_API}/channels/100", status=200, payload={})
    mock_discord_for_state.delete(f"{DISCORD_API}/channels/101", status=200, payload={})

    with patch.dict(os.environ, {"DISCORD_TEST_BOT_TOKEN": "test-token"}):
        runner = CliRunner()
        result = runner.invoke(cli, ["teardown", "--guild-id", guild, "--yes"])

    assert result.exit_code == 0
    delete_calls = [k for k in mock_discord_for_state.requests if k[0] == "DELETE"]
    assert len(delete_calls) == 2


def test_cli_teardown_without_yes_prompts_and_aborts_on_no(
    mock_discord_for_state: aioresponses,
) -> None:
    """SC-S4-ER1: teardown without --yes prompts; user typing 'n' aborts."""
    guild = "111"
    mock_discord_for_state.get(
        f"{DISCORD_API}/guilds/{guild}/channels",
        payload=[
            {"id": "100", "name": "general", "type": 0, "topic": "[ferry-fixture] x"},
        ],
    )
    mock_discord_for_state.get(
        f"{DISCORD_API}/guilds/{guild}/threads/active",
        payload={"threads": [], "members": []},
    )
    mock_discord_for_state.get(
        f"{DISCORD_API}/channels/100/threads/archived/public",
        payload={"threads": [], "members": [], "has_more": False},
    )
    mock_discord_for_state.get(
        f"{DISCORD_API}/channels/100/messages?limit=100",
        payload=[],
    )

    with patch.dict(os.environ, {"DISCORD_TEST_BOT_TOKEN": "test-token"}):
        runner = CliRunner()
        result = runner.invoke(cli, ["teardown", "--guild-id", guild], input="n\n")

    delete_calls = [k for k in mock_discord_for_state.requests if k[0] == "DELETE"]
    assert len(delete_calls) == 0
    assert "aborted" in result.output.lower() or "abort" in result.output.lower()


def test_cli_verify_perfect_match_exits_zero(
    mock_discord_for_state: aioresponses,
) -> None:
    """SC-S5-HP1: verify on a perfect-match guild exits 0."""
    guild = "111"
    mock_discord_for_state.get(
        f"{DISCORD_API}/guilds/{guild}/channels",
        payload=[
            {
                "id": "ch_text",
                "name": "general",
                "type": 0,
                "topic": "[ferry-fixture] primary test channel for DCE fixture capture",
            },
            {
                "id": "ch_forum",
                "name": "Feedback Forum",
                "type": 15,
                "topic": "[ferry-fixture] forum channel for DCE fixture capture",
            },
        ],
    )
    mock_discord_for_state.get(
        f"{DISCORD_API}/guilds/{guild}/threads/active",
        payload={
            "threads": [
                {
                    "id": "thread_cool",
                    "name": "Cool Thread",
                    "type": 11,
                    "parent_id": "ch_text",
                    "topic": None,
                },
                {
                    "id": "post_bug",
                    "name": "Bug Report",
                    "type": 11,
                    "parent_id": "ch_forum",
                    "topic": None,
                },
            ],
            "members": [],
        },
    )
    mock_discord_for_state.get(
        f"{DISCORD_API}/channels/ch_text/threads/archived/public",
        payload={"threads": [], "members": [], "has_more": False},
    )
    # Messages must match manifest content + [ferry:msg-NNN] markers
    from tests.provisioning._applier import load_manifest

    manifest = load_manifest(Path(__file__).parent / "fixture-spec.json")
    text_ch = manifest.text_channels[0]
    text_msgs = []
    for i, m in enumerate(text_ch.messages):
        msg_payload: dict[str, Any] = {
            "id": f"msg_{i}",
            "channel_id": "ch_text",
            "content": f"{m.content} [ferry:{m.id}]",
            "embeds": [],
        }
        if m.embed is not None:
            msg_payload["embeds"] = [
                {
                    "title": m.embed.title,
                    "description": m.embed.description,
                    "color": m.embed.color,
                    "fields": [
                        {"name": f.name, "value": f.value, "inline": f.inline}
                        for f in m.embed.fields
                    ],
                }
            ]
        text_msgs.append(msg_payload)
    mock_discord_for_state.get(
        f"{DISCORD_API}/channels/ch_text/messages?limit=100",
        payload=text_msgs,
    )
    thread_spec = manifest.threads[0]
    mock_discord_for_state.get(
        f"{DISCORD_API}/channels/thread_cool/messages?limit=100",
        payload=[
            {
                "id": "thread_first_msg",
                "channel_id": "thread_cool",
                "content": f"{thread_spec.first_message_content} [ferry:{thread_spec.id}]",
                "embeds": [],
            }
        ],
    )
    post_spec = manifest.forum_channels[0].posts[0]
    mock_discord_for_state.get(
        f"{DISCORD_API}/channels/post_bug/messages?limit=100",
        payload=[
            {
                "id": "post_first_msg",
                "channel_id": "post_bug",
                "content": f"{post_spec.first_message_content} [ferry:{post_spec.id}]",
                "embeds": [],
            }
        ],
    )

    with patch.dict(os.environ, {"DISCORD_TEST_BOT_TOKEN": "test-token"}):
        runner = CliRunner()
        result = runner.invoke(cli, ["verify", "--guild-id", guild])

    assert result.exit_code == 0, f"verify failed: stderr={result.stderr}, stdout={result.output}"


def test_cli_verify_missing_thread_exits_one(
    mock_discord_for_state: aioresponses,
) -> None:
    """SC-S5-EC1 variant: missing thread → exit 1 with drift report."""
    guild = "111"
    mock_discord_for_state.get(
        f"{DISCORD_API}/guilds/{guild}/channels",
        payload=[
            {
                "id": "ch_text",
                "name": "general",
                "type": 0,
                "topic": "[ferry-fixture] primary test channel for DCE fixture capture",
            },
        ],
    )
    mock_discord_for_state.get(
        f"{DISCORD_API}/guilds/{guild}/threads/active",
        payload={"threads": [], "members": []},
    )
    mock_discord_for_state.get(
        f"{DISCORD_API}/channels/ch_text/threads/archived/public",
        payload={"threads": [], "members": [], "has_more": False},
    )
    mock_discord_for_state.get(
        f"{DISCORD_API}/channels/ch_text/messages?limit=100",
        payload=[],
    )

    with patch.dict(os.environ, {"DISCORD_TEST_BOT_TOKEN": "test-token"}):
        runner = CliRunner()
        result = runner.invoke(cli, ["verify", "--guild-id", guild])

    assert result.exit_code == 1
    assert "missing" in result.stderr.lower() or "missing" in result.output.lower()


def test_cli_verify_401_exits_two(
    mock_discord_for_state: aioresponses,
) -> None:
    """SC-S5-ER1: verify with 401 exits 2 (couldn't determine), NOT 1 (drift)."""
    guild = "111"
    mock_discord_for_state.get(
        f"{DISCORD_API}/guilds/{guild}/channels",
        status=401,
        payload={},
    )
    with patch.dict(os.environ, {"DISCORD_TEST_BOT_TOKEN": "test-token"}):
        runner = CliRunner()
        result = runner.invoke(cli, ["verify", "--guild-id", guild])
    assert result.exit_code == 2


def test_cli_provision_partial_failure_prints_resume_hint(
    mock_discord_for_state: aioresponses,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SC-S3-ER3: partial failure mid-provision exits 1 with resume hint."""
    slept = AsyncMock()
    monkeypatch.setattr("tests.provisioning._bot_api.asyncio.sleep", slept)
    guild = "111"
    mock_discord_for_state.get(
        f"{DISCORD_API}/guilds/{guild}/channels",
        payload=[],
    )
    mock_discord_for_state.get(
        f"{DISCORD_API}/guilds/{guild}/threads/active",
        payload={"threads": [], "members": []},
    )
    mock_discord_for_state.post(
        f"{DISCORD_API}/guilds/{guild}/channels",
        payload={"id": "ch_text", "name": "general", "type": 0},
        status=201,
    )
    mock_discord_for_state.post(
        f"{DISCORD_API}/channels/ch_text/messages",
        payload={"id": "msg_1", "channel_id": "ch_text"},
        status=200,
    )
    mock_discord_for_state.post(
        f"{DISCORD_API}/channels/ch_text/messages",
        status=500,
        payload={},
        repeat=4,
    )
    with patch.dict(os.environ, {"DISCORD_TEST_BOT_TOKEN": "test-token"}):
        runner = CliRunner()
        result = runner.invoke(cli, ["provision", "--guild-id", guild])
    assert result.exit_code == 1
    assert (
        "re-run" in (result.output + result.stderr).lower()
        or "resume" in (result.output + result.stderr).lower()
    )
    assert [call.args[0] for call in slept.await_args_list] == [1.0, 2.0, 4.0]


def test_cli_create_guild_preflight_fails_at_ten_guilds(
    mock_discord_for_state: aioresponses,
) -> None:
    """SC-S3-ER1: --create-guild aborts if bot is in >=10 guilds."""
    mock_discord_for_state.get(
        f"{DISCORD_API}/users/@me/guilds",
        payload=[{"id": str(i), "name": f"Guild {i}"} for i in range(10)],
    )
    with patch.dict(os.environ, {"DISCORD_TEST_BOT_TOKEN": "test-token"}):
        runner = CliRunner()
        result = runner.invoke(cli, ["provision", "--create-guild", "Test"])
    assert result.exit_code != 0
    combined = result.output + (result.stderr or "")
    assert "10" in combined or "guilds" in combined.lower()
