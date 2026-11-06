"""Tests for conservative incremental-export decisions."""

import aiohttp
import pytest
from aioresponses import aioresponses

from discord_ferry.discord.client import fetch_export_source_ids
from discord_ferry.errors import DiscordAuthError, MigrationError
from discord_ferry.exporter.inventory import SourceInventory, select_export_decision

_API = "https://discord.com/api/v10"
_GUILD = "1543572361285083187"
_THREAD = "1543572368977428611"


def test_decision_includes_old_failed_message_below_high_water() -> None:
    source_id = "1543572361285083187"
    failed_id = "1543572368977428611"

    decision = select_export_decision(
        SourceInventory((source_id,), complete=True),
        {source_id: "1543572393384087642"},
        {source_id: (failed_id,)},
        previous_complete=True,
    )

    assert decision.scope == "bounded"
    assert decision.after_id == str(int(failed_id) - 1)


def test_decision_falls_back_for_malformed_retry_evidence() -> None:
    source_id = "1543572361285083187"

    decision = select_export_decision(
        SourceInventory((source_id,), complete=True),
        {source_id: "1543572393384087642"},
        {source_id: ("not-a-snowflake",)},
        previous_complete=True,
    )

    assert decision.scope == "full"
    assert decision.after_id is None


@pytest.mark.parametrize(
    ("inventory", "markers", "complete"),
    [
        (SourceInventory((), True), {}, True),
        (SourceInventory((_GUILD,), False), {_GUILD: "1543572393384087642"}, True),
        (SourceInventory((_GUILD,), True), {}, True),
        (SourceInventory((_GUILD,), True), {_GUILD: ""}, True),
        (SourceInventory((_GUILD,), True), {_GUILD: "legacy"}, True),
        (SourceInventory((_GUILD,), True), {_GUILD: "1543572393384087642"}, False),
    ],
)
def test_decision_falls_back_without_complete_marker_coverage(
    inventory: SourceInventory, markers: dict[str, str], complete: bool
) -> None:
    assert select_export_decision(inventory, markers, {}, complete).scope == "full"


async def test_user_inventory_includes_active_and_archived_threads() -> None:
    with aioresponses() as mocked:
        mocked.get(f"{_API}/guilds/{_GUILD}/channels", payload=[{"id": _GUILD}])
        mocked.get(
            f"{_API}/channels/{_GUILD}/threads/search?archived=false&limit=100&offset=0",
            payload={"threads": [{"id": _THREAD}], "has_more": False},
        )
        mocked.get(
            f"{_API}/channels/{_GUILD}/threads/search?archived=true&limit=100&offset=0",
            payload={"threads": [{"id": _THREAD}], "has_more": False},
        )
        mocked.get(
            f"{_API}/channels/{_GUILD}/threads/archived/public?limit=100&offset=0",
            payload={"threads": [], "has_more": False},
        )
        mocked.get(
            f"{_API}/channels/{_GUILD}/threads/archived/private?limit=100&offset=0",
            payload={"threads": [], "has_more": False},
        )
        async with aiohttp.ClientSession() as session:
            ids, complete = await fetch_export_source_ids(session, "user-token", _GUILD)
    assert complete is True
    assert ids == tuple(sorted((_GUILD, _THREAD)))


async def test_bot_inventory_includes_active_threads() -> None:
    with aioresponses() as mocked:
        mocked.get(f"{_API}/guilds/{_GUILD}/channels", payload=[{"id": _GUILD}])
        mocked.get(
            f"{_API}/guilds/{_GUILD}/threads/active",
            payload={"threads": [{"id": _THREAD}]},
        )
        for kind in ("public", "private"):
            mocked.get(
                f"{_API}/channels/{_GUILD}/threads/archived/{kind}?limit=100&offset=0",
                payload={"threads": [], "has_more": False},
            )
        async with aiohttp.ClientSession() as session:
            ids, complete = await fetch_export_source_ids(session, "Bot token", _GUILD)
    assert complete is True
    assert ids == tuple(sorted((_GUILD, _THREAD)))


async def test_inventory_refuses_malformed_thread_page() -> None:
    with aioresponses() as mocked:
        mocked.get(f"{_API}/guilds/{_GUILD}/channels", payload=[{"id": _GUILD}])
        mocked.get(
            f"{_API}/channels/{_GUILD}/threads/search?archived=false&limit=100&offset=0",
            payload={"threads": [{"id": "bad"}], "has_more": False},
        )
        async with aiohttp.ClientSession() as session:
            ids, complete = await fetch_export_source_ids(session, "user-token", _GUILD)
    assert ids == ()
    assert complete is False


async def test_inventory_refuses_nonprogressing_thread_page() -> None:
    with aioresponses() as mocked:
        mocked.get(f"{_API}/guilds/{_GUILD}/channels", payload=[{"id": _GUILD}])
        mocked.get(
            f"{_API}/channels/{_GUILD}/threads/search?archived=false&limit=100&offset=0",
            payload={"threads": [], "has_more": True},
        )
        async with aiohttp.ClientSession() as session:
            ids, complete = await fetch_export_source_ids(session, "user-token", _GUILD)
    assert ids == ()
    assert complete is False


async def test_inventory_propagates_authentication_failure() -> None:
    with aioresponses() as mocked:
        mocked.get(f"{_API}/guilds/{_GUILD}/channels", status=401)
        async with aiohttp.ClientSession() as session:
            with pytest.raises(DiscordAuthError):
                await fetch_export_source_ids(session, "user-token", _GUILD)


async def test_inventory_surfaces_inaccessible_parent() -> None:
    with aioresponses() as mocked:
        mocked.get(f"{_API}/guilds/{_GUILD}/channels", payload=[{"id": _GUILD}])
        mocked.get(
            f"{_API}/channels/{_GUILD}/threads/search?archived=false&limit=100&offset=0",
            status=403,
        )
        async with aiohttp.ClientSession() as session:
            with pytest.raises(MigrationError):
                await fetch_export_source_ids(session, "user-token", _GUILD)
