"""Explicit permission-evidence refresh stays within the source metadata owner."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aioresponses import aioresponses

from discord_ferry import discord
from discord_ferry.core import atomicio
from discord_ferry.core.security import sanitize_secrets
from discord_ferry.discord import client
from discord_ferry.discord.metadata import (
    ChannelMeta,
    DiscordMetadata,
    PermissionPair,
    capture_permission_source,
    load_discord_metadata,
    save_discord_metadata,
    validate_permission_source,
)
from discord_ferry.discord.permissions import translate_permissions
from discord_ferry.errors import MigrationError
from discord_ferry.parser.models import DCEChannel, DCEExport, DCEGuild

if TYPE_CHECKING:
    from pathlib import Path

TOKEN = "refresh-source-test-token"
GUILD = "111"
CHANNEL = "333"
BASE = f"{client.DISCORD_API}/guilds/{GUILD}"


def refresh():
    operation = getattr(discord, "refresh_permission_metadata", None)
    assert callable(operation), "Explicit source-only permission refresh is missing"
    return operation


def exports(guild: str = GUILD) -> list[DCEExport]:
    return [DCEExport(DCEGuild(guild, "source"), DCEChannel(CHANNEL, 0, "selected"))]


def role_payload():
    return [
        {
            "id": GUILD,
            "name": "everyone",
            "permissions": str(1 << 10),
            "position": 0,
            "managed": False,
        },
        {
            "id": "222",
            "name": "ordinary",
            "permissions": str(1 << 11),
            "position": 1,
            "managed": False,
        },
    ]


def channel_payload():
    return [{"id": CHANNEL, "name": "selected", "type": 0, "permission_overwrites": []}]


def valid_metadata() -> DiscordMetadata:
    roles = [client._parse_role(value) for value in role_payload()]
    channels = [client._parse_channel(value) for value in channel_payload()]
    return DiscordMetadata(
        GUILD,
        "new-evidence",
        translate_permissions(1 << 10),
        {"222": PermissionPair(translate_permissions(1 << 11), 0)},
        {CHANNEL: ChannelMeta(False)},
        permission_source=capture_permission_source(GUILD, roles, channels),
    )


@pytest.fixture(autouse=True)
def forbid_destination_and_export_work(monkeypatch):
    from discord_ferry.core import engine
    from discord_ferry.exporter import runner
    from discord_ferry.migrator import api

    forbidden = []
    for owner, name in (
        (engine, "run_migration"),
        (engine, "run_repair"),
        (engine, "run_retry_failed"),
        (api, "_api_request"),
        (runner, "run_dce_export"),
    ):
        guard = AsyncMock(side_effect=AssertionError(f"Refresh called forbidden owner {name}"))
        monkeypatch.setattr(owner, name, guard)
        forbidden.append(guard)
    yield
    for guard in forbidden:
        guard.assert_not_called()


@pytest.fixture
def existing_output(tmp_path):
    output = tmp_path / "selected-output"
    save_discord_metadata(DiscordMetadata(GUILD, "legacy", 0, {}, {}), output)
    (output / "state.json").write_bytes(b'{"confirmed_identity":"keep-this-id"}')
    (output / "message_map.json").write_bytes(b'{"source":"delivered-target"}')
    (output / "report.json").write_bytes(b'{"previous_report":"unchanged"}')
    return output


def files_at(output: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}


def test_refresh_is_an_explicit_async_source_operation() -> None:
    operation = refresh()
    assert inspect.iscoroutinefunction(operation)
    assert "refresh_permission_metadata" in discord.__all__
    assert tuple(inspect.signature(operation).parameters) == (
        "session",
        "token",
        "guild_id",
        "exports",
        "output_dir",
    )
    assert all(
        parameter.default is inspect.Parameter.empty
        for parameter in inspect.signature(operation).parameters.values()
    )


async def test_refresh_fetches_validates_and_replaces_only_selected_metadata(
    monkeypatch, tmp_path, existing_output
) -> None:
    operation = refresh()
    before = files_at(existing_output)
    other = tmp_path / "other-output"
    save_discord_metadata(DiscordMetadata("999", "other", 0, {}, {}), other)
    other_before = files_at(other)
    original_save = discord.save_discord_metadata
    writes = []

    def checked_save(meta, output):
        assert output == existing_output
        assert not validate_permission_source(meta, frozenset({CHANNEL}))
        assert files_at(output) == before
        assert TOKEN not in sanitize_secrets(TOKEN)
        writes.append(meta)
        original_save(meta, output)

    monkeypatch.setattr(discord, "save_discord_metadata", checked_save)
    with aioresponses() as mocked:
        mocked.get(BASE, payload={"id": GUILD, "name": "source"})
        mocked.get(f"{BASE}/roles", payload=role_payload())
        mocked.get(f"{BASE}/channels", payload=channel_payload())
        async with aiohttp.ClientSession() as session:
            result = await operation(session, TOKEN, GUILD, exports(), existing_output)
            assert not session.closed
        assert len(mocked.requests) == 3
        for (method, url), calls in mocked.requests.items():
            assert method == "GET" and str(url) in {BASE, f"{BASE}/roles", f"{BASE}/channels"}
            assert calls[0].kwargs["headers"]["Authorization"] == TOKEN
    assert writes == [result]
    assert result == load_discord_metadata(existing_output)
    assert not validate_permission_source(result, frozenset({CHANNEL}))
    after = files_at(existing_output)
    assert after.pop("discord_metadata.json") != before["discord_metadata.json"]
    assert after == {key: value for key, value in before.items() if key != "discord_metadata.json"}
    assert files_at(other) == other_before
    assert TOKEN.encode() not in (existing_output / "discord_metadata.json").read_bytes()


@pytest.mark.parametrize("selection", ["wrong-guild", "mixed-guilds", "empty"])
async def test_refresh_refuses_unbound_exports_before_source_requests(
    monkeypatch, existing_output, selection
) -> None:
    operation = refresh()
    before = files_at(existing_output)
    selected = (
        exports("999")
        if selection == "wrong-guild"
        else exports() + exports("999")
        if selection == "mixed-guilds"
        else []
    )
    fetch = AsyncMock(side_effect=AssertionError("Invalid selection reached Discord"))
    monkeypatch.setattr(discord, "fetch_and_translate_guild_metadata", fetch)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(MigrationError):
            await operation(session, TOKEN, GUILD, selected, existing_output)
    fetch.assert_not_awaited()
    assert files_at(existing_output) == before


@pytest.mark.parametrize("bad_id", ["", "../users/@me", "123456789012345678901", "１２３"])
async def test_refresh_refuses_invalid_selected_identity_before_source_requests(
    monkeypatch, existing_output, bad_id
) -> None:
    operation = refresh()
    before = files_at(existing_output)
    fetch = AsyncMock(side_effect=AssertionError("Invalid guild identity reached Discord"))
    monkeypatch.setattr(discord, "fetch_and_translate_guild_metadata", fetch)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(MigrationError):
            await operation(session, TOKEN, bad_id, exports(bad_id), existing_output)
    fetch.assert_not_awaited()
    assert files_at(existing_output) == before


@pytest.mark.parametrize("binding", ["other-guild", "malformed-cache", "malformed-guild"])
async def test_refresh_refuses_foreign_or_unprovable_output_binding_before_fetch(
    monkeypatch, existing_output, binding
) -> None:
    operation = refresh()
    if binding == "malformed-cache":
        (existing_output / "discord_metadata.json").write_bytes(b"not-json")
    else:
        guild = "999" if binding == "other-guild" else "not-a-source-id"
        save_discord_metadata(DiscordMetadata(guild, "other", 0, {}, {}), existing_output)
    before = files_at(existing_output)
    fetch = AsyncMock(return_value=valid_metadata())
    monkeypatch.setattr(discord, "fetch_and_translate_guild_metadata", fetch)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(MigrationError, match="output"):
            await operation(session, TOKEN, GUILD, exports(), existing_output)
    fetch.assert_not_awaited()
    assert files_at(existing_output) == before


async def test_refresh_allows_an_explicit_output_without_prior_metadata(
    monkeypatch, tmp_path
) -> None:
    operation = refresh()
    output = tmp_path / "new-selected-output"
    meta = valid_metadata()
    monkeypatch.setattr(discord, "fetch_and_translate_guild_metadata", AsyncMock(return_value=meta))
    async with aiohttp.ClientSession() as session:
        assert await operation(session, TOKEN, GUILD, exports(), output) is meta
    assert load_discord_metadata(output) == meta
    assert set(files_at(output)) == {"discord_metadata.json"}


@pytest.mark.parametrize("change", ["guild", "legacy", "coverage", "fingerprint", "translated"])
async def test_refresh_refuses_invalid_fetched_evidence_without_replacement(
    monkeypatch, existing_output, change
) -> None:
    operation = refresh()
    before = files_at(existing_output)
    meta = valid_metadata()
    if change == "guild":
        meta.guild_id = "999"
    elif change == "legacy":
        meta.permission_source = None
    elif change == "coverage":
        meta.permission_source = replace(
            meta.permission_source, channels=(), covered_channel_ids=frozenset()
        )
    elif change == "fingerprint":
        meta.permission_source = replace(
            meta.permission_source, fingerprint="not-the-captured-evidence"
        )
    else:
        meta.server_default_permissions = 0
    monkeypatch.setattr(discord, "fetch_and_translate_guild_metadata", AsyncMock(return_value=meta))
    async with aiohttp.ClientSession() as session:
        with pytest.raises(MigrationError):
            await operation(session, TOKEN, GUILD, exports(), existing_output)
    assert files_at(existing_output) == before


@pytest.mark.parametrize("stage", ["guild", "roles", "channels"])
@pytest.mark.parametrize("failure", ["auth", "rate-limit", "network", "cancelled"])
async def test_refresh_preserves_prior_bytes_on_source_transport_failures(
    monkeypatch, existing_output, stage, failure
) -> None:
    operation = refresh()
    before = files_at(existing_output)
    real_sleep = asyncio.sleep

    async def yield_without_delay(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(client.asyncio, "sleep", yield_without_delay)
    stages = [
        ("guild", BASE, {"id": GUILD}),
        ("roles", f"{BASE}/roles", role_payload()),
        ("channels", f"{BASE}/channels", channel_payload()),
    ]
    with aioresponses() as mocked:
        for name, url, payload in stages:
            if name != stage:
                mocked.get(url, payload=payload)
                continue
            if failure == "auth":
                mocked.get(url, status=401)
            elif failure == "rate-limit":
                mocked.get(url, status=429, payload={"retry_after": 0}, repeat=True)
            elif failure == "network":
                mocked.get(
                    url,
                    exception=aiohttp.ClientConnectionError("synthetic connection failure"),
                    repeat=True,
                )
            else:
                mocked.get(url, exception=asyncio.CancelledError())
            break
        async with aiohttp.ClientSession() as session:
            exception = asyncio.CancelledError if failure == "cancelled" else MigrationError
            with pytest.raises(exception):
                await operation(session, TOKEN, GUILD, exports(), existing_output)
            assert not session.closed
        assert all(method == "GET" and str(url).startswith(BASE) for method, url in mocked.requests)
    assert TOKEN not in sanitize_secrets(TOKEN)
    assert files_at(existing_output) == before


async def test_refresh_honours_cancellation_requested_after_fetch_before_replacement(
    monkeypatch, existing_output
) -> None:
    operation = refresh()
    before = files_at(existing_output)

    async def fetched(*_args):
        assert TOKEN not in sanitize_secrets(TOKEN)
        asyncio.current_task().cancel()
        return valid_metadata()

    monkeypatch.setattr(discord, "fetch_and_translate_guild_metadata", fetched)
    async with aiohttp.ClientSession() as session:
        task = asyncio.create_task(operation(session, TOKEN, GUILD, exports(), existing_output))
        with pytest.raises(asyncio.CancelledError):
            await task
    assert files_at(existing_output) == before


@pytest.mark.parametrize(
    "malformed",
    [
        "missing-permissions",
        "boolean-permissions",
        "missing-managed",
        "missing-type",
        "missing-overwrites",
        "null-overwrites",
        "null-roles",
        "array-guild",
    ],
)
async def test_refresh_rejects_malformed_real_capture_without_replacing_old_metadata(
    existing_output, malformed
) -> None:
    operation = refresh()
    before = files_at(existing_output)
    roles, channels = role_payload(), channel_payload()
    guild = {"id": GUILD}
    if malformed == "missing-permissions":
        roles[1].pop("permissions")
    elif malformed == "boolean-permissions":
        roles[1]["permissions"] = True
    elif malformed == "missing-managed":
        roles[1].pop("managed")
    elif malformed == "missing-type":
        channels[0].pop("type")
    elif malformed == "missing-overwrites":
        channels[0].pop("permission_overwrites")
    elif malformed == "null-overwrites":
        channels[0]["permission_overwrites"] = None
    elif malformed == "null-roles":
        roles = None
    else:
        guild = []
    with aioresponses() as mocked:
        mocked.get(BASE, payload=guild)
        mocked.get(f"{BASE}/roles", payload=roles)
        mocked.get(f"{BASE}/channels", payload=channels)
        async with aiohttp.ClientSession() as session:
            with pytest.raises(MigrationError):
                await operation(session, TOKEN, GUILD, exports(), existing_output)
    assert files_at(existing_output) == before


async def test_refresh_uses_existing_atomic_replacement_and_preserves_old_file_on_failure(
    monkeypatch, existing_output
) -> None:
    operation = refresh()
    before = files_at(existing_output)
    monkeypatch.setattr(
        discord, "fetch_and_translate_guild_metadata", AsyncMock(return_value=valid_metadata())
    )

    def failed_swap(source, destination):
        assert (
            source.parent == existing_output
            and destination == existing_output / "discord_metadata.json"
        )
        assert source.read_bytes() != before["discord_metadata.json"]
        raise OSError("synthetic metadata replacement failure")

    monkeypatch.setattr(atomicio, "replace_with_retry", failed_swap)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(OSError, match="synthetic metadata replacement failure"):
            await operation(session, TOKEN, GUILD, exports(), existing_output)
    assert files_at(existing_output) == before
