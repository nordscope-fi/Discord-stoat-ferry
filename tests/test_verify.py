"""Tests for the migration check tool (#107 batch 9).

Fixture rule for this whole module: Discord ids and Stoat ids are LITERAL and
visibly different, ``"d-100"`` against ``"01JSTOAT..."``. They are never derived
from one variable and never equal. Seeding both sides of a comparison from a
single value is what lets a check test pass against an implementation comparing
the wrong one, and this project has shipped five assertions that could not fail
against what they guarded.
"""

from __future__ import annotations

import ast
import pathlib
import re

import aiohttp
import pytest
from aioresponses import aioresponses

from discord_ferry.errors import CheckError
from discord_ferry.migrator.verify import (
    STATUSES,
    CheckReport,
    CheckResult,
    RepairOutcome,
    run_check,
)
from discord_ferry.state import MigrationState

BASE_URL = "https://api.test"
TOKEN = "test-session-token"


@pytest.fixture()
def mock_aiohttp() -> object:
    with aioresponses() as m:
        yield m


def _noop_event(_event: object) -> None:
    return None


# ---------------------------------------------------------------------------
# CheckResult / CheckReport (task #250)
# ---------------------------------------------------------------------------


def test_a_report_of_only_unverifiable_counts_zero_ok() -> None:
    """Kills an implementation that folds unverifiable into ok.

    The exit code cannot separate those two, because a report of only
    unverifiable and a report of all ok BOTH exit 0. Only the count can tell
    them apart, which is why the count is asserted here rather than only the
    command's status.
    """
    report = CheckReport()
    report.add(
        name="channel:d-100",
        status="unverifiable",
        kind="tail_not_recorded",
        detail="newest message was not recorded by Ferry",
    )
    counts = report.counts()
    assert counts["ok"] == 0
    assert counts["unverifiable"] == 1
    assert report.has_failures is False


def test_counts_reports_every_status_even_at_zero() -> None:
    """A caller rendering a summary line needs all four keys present.

    Kills a Counter-based implementation that omits statuses with no results,
    which would make the summary line drop "0 failed" exactly when a reader
    most wants to see it stated.
    """
    report = CheckReport()
    report.add(name="a", status="ok", kind="tail_present", detail="")
    counts = report.counts()
    assert set(counts) == {"ok", "warn", "fail", "unverifiable"}
    assert counts["fail"] == 0
    assert counts["warn"] == 0


def test_has_failures_is_true_only_for_a_fail() -> None:
    """warn and unverifiable must NOT make the command exit non-zero.

    Kills an implementation treating anything non-ok as a failure, which would
    exit non-zero on every merge migration and on every renamed category.
    """
    report = CheckReport()
    report.add(name="a", status="warn", kind="category_title_mismatch", detail="")
    report.add(name="b", status="unverifiable", kind="tail_not_recorded", detail="")
    assert report.has_failures is False
    report.add(name="c", status="fail", kind="channel_missing", detail="")
    assert report.has_failures is True


def test_a_result_carries_the_entity_ids_a_repair_would_need() -> None:
    """The report is the contract batch 10's repair tool consumes.

    A status plus prose is not actionable. Kills a shape carrying only the
    ProbeCheck fields (name, status, detail), which would force a repair tool
    to parse ids back out of a sentence.
    """
    result = CheckResult(
        name="channel:d-100",
        status="fail",
        kind="channel_missing",
        detail="recorded channel is absent from the server",
        discord_id="d-100",
        stoat_id="01JSTOATCH00000000000AAA",
    )
    assert result.discord_id == "d-100"
    assert result.stoat_id == "01JSTOATCH00000000000AAA"
    assert result.expected is None
    assert result.found is None


SRV = "01JSTOATSRV0000000000AAA"


def _state_with_channels(channel_map: dict[str, str]) -> MigrationState:
    state = MigrationState()
    state.stoat_server_id = SRV
    state.channel_map = dict(channel_map)
    return state


def _server_payload(all_ids: list[str], visible: list[dict[str, str]]) -> dict[str, object]:
    """A ServerWithChannels body.

    The two lists are separate on purpose and that is the whole point of this
    route: ``server.channels`` is returned unfiltered and names every channel
    id, while the sibling array holds only objects the caller may ViewChannel.
    """
    return {"server": {"_id": SRV, "channels": all_ids}, "channels": visible}


def _allow_any_message_window(mock: aioresponses) -> None:
    """Serve an empty message window for ANY channel.

    For tests whose subject is the structure pass: the tail check still runs and
    needs somewhere to send its request.
    """
    mock.get(re.compile(r".*/channels/.*/messages.*"), payload=[], repeat=True)


def _register(mock: aioresponses, payload: dict[str, object]) -> None:
    """Register the two structure routes, plus an empty message window for every
    channel id the payload mentions.

    The tail check runs for every entry in channel_map, so a structure-focused
    test still needs a message route or it fails on a missing mock rather than
    on what it is testing. An empty window with a zero recorded count reports
    ok/nothing_expected, which these tests ignore.
    """
    mock.get(f"{BASE_URL}/servers/{SRV}?include_channels=true", payload=payload)
    mock.get(f"{BASE_URL}/servers/{SRV}/emojis", payload=[])
    server = payload.get("server") or {}
    ids = set(server.get("channels") or [])
    ids.update(
        c["_id"] for c in (payload.get("channels") or []) if isinstance(c, dict) and "_id" in c
    )
    for cid in ids:
        mock.get(
            f"{BASE_URL}/channels/{cid}/messages?limit=100&sort=Latest",
            payload=[],
            repeat=True,
        )
    # Also cover a channel the payload does not list. Until task #259 lands, the
    # tail check runs for every channel_map entry even when the structure pass
    # already failed it, so the request still has to go somewhere.
    _allow_any_message_window(mock)


# ---------------------------------------------------------------------------
# run_check preconditions (task #251)
# ---------------------------------------------------------------------------


async def test_a_dry_run_state_is_refused_before_any_request() -> None:
    """A dry run records dry-ch- and dry-msg- sentinels for entities that were
    never created, so there is nothing on a server to check against.

    --resume has refused a dry-run state since it was written and --incremental
    gained the same refusal in v2.15.0. This is the third sibling.

    No route is registered, so the refusal is proven by the absence of a
    request rather than only by the exception: an implementation checking the
    flag AFTER its first fetch would raise a different error here.
    """
    state = MigrationState()
    state.is_dry_run = True
    state.stoat_server_id = "01JSTOATSRV0000000000AAA"
    with aioresponses(), pytest.raises(CheckError, match="dry-run"):
        await run_check(BASE_URL, TOKEN, state, _noop_event)


async def test_an_empty_server_id_is_refused_before_any_request() -> None:
    """Without a server id there is nothing to check against, and the URL would
    be built with an empty path segment rather than failing honestly."""
    state = MigrationState()
    state.stoat_server_id = ""
    with aioresponses(), pytest.raises(CheckError, match="server"):
        await run_check(BASE_URL, TOKEN, state, _noop_event)


async def test_a_state_predating_this_feature_degrades_rather_than_refusing(
    mock_aiohttp: aioresponses,
) -> None:
    """An older state.json loads with its newer optional fields defaulted,
    because load_state reads every one through data.get.

    Kills an implementation that hard-requires a field a released state file
    does not carry, which would make the tool useless for exactly the
    migrations it exists to inspect.

    Scoped to the preconditions in chunk 2, where run_check made no requests
    and this could assert nothing more. Chunk 3 gave it something to degrade,
    so it now drives the real structure pass over an empty state and asserts
    the run completes with nothing to report rather than raising.
    """
    state = MigrationState()
    state.stoat_server_id = SRV
    # No channel_map, no message_map, no channel_high_water, no
    # channel_message_counts: the shape an early state.json presents.
    _register(mock_aiohttp, _server_payload([], []))
    report = await run_check(BASE_URL, TOKEN, state, _noop_event)
    assert report.counts()["fail"] == 0
    assert report.results == []


# ---------------------------------------------------------------------------
# structure: channel identity, the three-way rule (task #252)
# ---------------------------------------------------------------------------


async def test_a_channel_present_in_both_lists_is_ok(
    mock_aiohttp: aioresponses,
) -> None:
    """The ordinary case."""
    stoat_id = "01JSTOATCH00000000000AAA"
    _register(
        mock_aiohttp,
        _server_payload([stoat_id], [{"_id": stoat_id, "name": "general"}]),
    )
    report = await run_check(
        BASE_URL, TOKEN, _state_with_channels({"d-100": stoat_id}), _noop_event
    )
    channel_results = [r for r in report.results if r.name == "channel:d-100"]
    assert [r.status for r in channel_results] == ["ok"]
    assert channel_results[0].stoat_id == stoat_id


async def test_a_channel_absent_from_the_id_list_is_a_failure(
    mock_aiohttp: aioresponses,
) -> None:
    """Deleted, and reportable as such.

    Kills an implementation that consults only the visible-objects array. That
    one cannot tell deletion from a permission filter, so it would have to
    report unverifiable here, and `channel_missing` would be unreachable.
    Reporting a deleted channel is the point of the tool.
    """
    _register(mock_aiohttp, _server_payload([], []))
    report = await run_check(
        BASE_URL,
        TOKEN,
        _state_with_channels({"d-100": "01JSTOATCH00000000000AAA"}),
        _noop_event,
    )
    result = next(r for r in report.results if r.name == "channel:d-100")
    assert result.status == "fail"
    assert result.kind == "channel_missing"
    assert report.has_failures is True


async def test_a_channel_the_token_cannot_see_is_unverifiable_not_a_failure(
    mock_aiohttp: aioresponses,
) -> None:
    """Present in the unfiltered id list, absent from the permission-filtered
    objects: the channel exists and this token simply may not view it.

    Kills an implementation treating any absence from the objects array as
    deletion, which would report fail on every private channel and teach users
    to ignore the tool. The two fixtures differ ONLY in the objects array, so
    this test and the one above cannot both pass against a one-list
    implementation.
    """
    stoat_id = "01JSTOATCH00000000000AAA"
    _register(mock_aiohttp, _server_payload([stoat_id], []))
    report = await run_check(
        BASE_URL, TOKEN, _state_with_channels({"d-100": stoat_id}), _noop_event
    )
    result = next(r for r in report.results if r.name == "channel:d-100")
    assert result.status == "unverifiable"
    assert result.kind == "channel_not_visible"
    assert report.has_failures is False


async def test_a_forum_index_entry_is_checked_as_an_ordinary_channel(
    mock_aiohttp: aioresponses,
) -> None:
    """channel_map keys are not all Discord channel ids.

    The forum index writer stores a SYNTHETIC key, `forum-index-{forum_key}`,
    whose value is nonetheless a real Stoat channel id. Identity checking is
    valid for it because only the value is sent to the server.

    Kills an implementation that assumes every key is a Discord snowflake and
    skips, or crashes on, the ones that are not.
    """
    stoat_id = "01JSTOATIDX00000000000AA"
    _register(
        mock_aiohttp,
        _server_payload([stoat_id], [{"_id": stoat_id, "name": "forum-index"}]),
    )
    report = await run_check(
        BASE_URL,
        TOKEN,
        _state_with_channels({"forum-index-cat-9": stoat_id}),
        _noop_event,
    )
    result = next(r for r in report.results if r.name == "channel:forum-index-cat-9")
    assert result.status == "ok"


# ---------------------------------------------------------------------------
# structure: roles and emoji (task #253)
# ---------------------------------------------------------------------------


async def test_a_missing_role_is_a_failure(mock_aiohttp: aioresponses) -> None:
    """Server.roles is a map keyed by role id, so membership is a key lookup.

    Unlike channels there is no second list and no permission filter, so an
    absence here is unambiguous and reports fail rather than unverifiable.
    """
    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SRV}?include_channels=true",
        payload={"server": {"_id": SRV, "channels": [], "roles": {}}, "channels": []},
    )
    mock_aiohttp.get(f"{BASE_URL}/servers/{SRV}/emojis", payload=[])
    _allow_any_message_window(mock_aiohttp)
    state = MigrationState()
    state.stoat_server_id = SRV
    state.role_map = {"d-r1": "01JSTOATROLE0000000000A"}
    report = await run_check(BASE_URL, TOKEN, state, _noop_event)
    result = next(r for r in report.results if r.discord_id == "d-r1")
    assert result.status == "fail"
    assert result.kind == "role_missing"


async def test_a_present_role_is_ok(mock_aiohttp: aioresponses) -> None:
    """Kills an implementation reporting every role missing, which the failure
    test above cannot catch on its own."""
    role_id = "01JSTOATROLE0000000000A"
    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SRV}?include_channels=true",
        payload={
            "server": {"_id": SRV, "channels": [], "roles": {role_id: {"name": "mods"}}},
            "channels": [],
        },
    )
    mock_aiohttp.get(f"{BASE_URL}/servers/{SRV}/emojis", payload=[])
    _allow_any_message_window(mock_aiohttp)
    state = MigrationState()
    state.stoat_server_id = SRV
    state.role_map = {"d-r1": role_id}
    report = await run_check(BASE_URL, TOKEN, state, _noop_event)
    assert next(r for r in report.results if r.discord_id == "d-r1").status == "ok"


async def test_a_missing_emoji_is_a_failure(mock_aiohttp: aioresponses) -> None:
    """Emoji come from their own route, because Server carries no emoji field."""
    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SRV}?include_channels=true",
        payload={"server": {"_id": SRV, "channels": []}, "channels": []},
    )
    mock_aiohttp.get(f"{BASE_URL}/servers/{SRV}/emojis", payload=[])
    _allow_any_message_window(mock_aiohttp)
    state = MigrationState()
    state.stoat_server_id = SRV
    state.emoji_map = {"d-e1": "01JAUTUMNEMOJI00000000A"}
    report = await run_check(BASE_URL, TOKEN, state, _noop_event)
    result = next(r for r in report.results if r.discord_id == "d-e1")
    assert result.status == "fail"
    assert result.kind == "emoji_missing"


async def test_a_present_emoji_is_ok(mock_aiohttp: aioresponses) -> None:
    """The emoji list is a list of objects keyed by _id, not a map."""
    emoji_id = "01JAUTUMNEMOJI00000000A"
    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SRV}?include_channels=true",
        payload={"server": {"_id": SRV, "channels": []}, "channels": []},
    )
    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SRV}/emojis",
        payload=[{"_id": emoji_id, "name": "party"}],
    )
    _allow_any_message_window(mock_aiohttp)
    state = MigrationState()
    state.stoat_server_id = SRV
    state.emoji_map = {"d-e1": emoji_id}
    report = await run_check(BASE_URL, TOKEN, state, _noop_event)
    assert next(r for r in report.results if r.discord_id == "d-e1").status == "ok"


async def test_a_renamed_emoji_warns(mock_aiohttp: aioresponses) -> None:
    """A renamed emoji reports warn with kind emoji_renamed, not ok."""
    emoji_id = "01JAUTUMNEMOJI00000000A"
    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SRV}?include_channels=true",
        payload={"server": {"_id": SRV, "channels": []}, "channels": []},
    )
    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SRV}/emojis",
        payload=[{"_id": emoji_id, "name": "renamed"}],
    )
    _allow_any_message_window(mock_aiohttp)
    state = MigrationState()
    state.stoat_server_id = SRV
    state.emoji_map = {"d-e1": emoji_id}
    state.created_emoji_names = {"d-e1": "party"}
    report = await run_check(BASE_URL, TOKEN, state, _noop_event)
    result = next(r for r in report.results if r.discord_id == "d-e1")
    assert result.status == "warn"
    assert result.kind == "emoji_renamed"
    assert result.expected == "party"
    assert result.found == "renamed"
    assert report.has_failures is False


async def test_a_matching_emoji_name_is_ok(mock_aiohttp: aioresponses) -> None:
    """When the server name matches the recorded name, the emoji reports ok."""
    emoji_id = "01JAUTUMNEMOJI00000000A"
    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SRV}?include_channels=true",
        payload={"server": {"_id": SRV, "channels": []}, "channels": []},
    )
    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SRV}/emojis",
        payload=[{"_id": emoji_id, "name": "party"}],
    )
    _allow_any_message_window(mock_aiohttp)
    state = MigrationState()
    state.stoat_server_id = SRV
    state.emoji_map = {"d-e1": emoji_id}
    state.created_emoji_names = {"d-e1": "party"}
    report = await run_check(BASE_URL, TOKEN, state, _noop_event)
    result = next(r for r in report.results if r.discord_id == "d-e1")
    assert result.status == "ok"
    assert result.kind == "emoji_present"


async def test_emoji_with_no_recorded_name_degrades_to_ok(
    mock_aiohttp: aioresponses,
) -> None:
    """A pre-change state file with no created_emoji_names skips the comparison."""
    emoji_id = "01JAUTUMNEMOJI00000000A"
    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SRV}?include_channels=true",
        payload={"server": {"_id": SRV, "channels": []}, "channels": []},
    )
    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SRV}/emojis",
        payload=[{"_id": emoji_id, "name": "renamed"}],
    )
    _allow_any_message_window(mock_aiohttp)
    state = MigrationState()
    state.stoat_server_id = SRV
    state.emoji_map = {"d-e1": emoji_id}
    report = await run_check(BASE_URL, TOKEN, state, _noop_event)
    result = next(r for r in report.results if r.discord_id == "d-e1")
    assert result.status == "ok"
    assert result.kind == "emoji_present"


# ---------------------------------------------------------------------------
# structure: categories, warn's first producer and still its clearest (task #254)
# ---------------------------------------------------------------------------


def _state_with_category(stoat_cat_id: str, expected_title: str) -> MigrationState:
    state = MigrationState()
    state.stoat_server_id = SRV
    state.category_map = {"d-cat-1": stoat_cat_id}
    state.category_names = {"d-cat-1": expected_title}
    return state


async def test_a_renamed_category_warns_rather_than_failing(
    mock_aiohttp: aioresponses,
) -> None:
    """warn's first producer, and the precedent the other two follow.

    Until 2.17.0 this was warn's ONLY producer, because category_names was the
    one expected name MigrationState recorded. channel_renamed and role_renamed
    joined it once the two name maps were added, and all three warn for the same
    reason: the entity exists and its content is intact, and only the label
    moved. Someone renamed a heading, nothing was lost.

    Kills an implementation reporting a cosmetic rename as fail, which would
    exit non-zero on a migration that is entirely intact.
    """
    cat_id = "01JSTOATCAT00000000000A"
    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SRV}?include_channels=true",
        payload={
            "server": {
                "_id": SRV,
                "channels": [],
                "categories": [{"id": cat_id, "title": "Renamed", "channels": []}],
            },
            "channels": [],
        },
    )
    mock_aiohttp.get(f"{BASE_URL}/servers/{SRV}/emojis", payload=[])
    _allow_any_message_window(mock_aiohttp)
    report = await run_check(BASE_URL, TOKEN, _state_with_category(cat_id, "Original"), _noop_event)
    result = next(r for r in report.results if r.discord_id == "d-cat-1")
    assert result.status == "warn"
    assert result.kind == "category_title_mismatch"
    assert result.expected == "Original"
    assert result.found == "Renamed"
    assert report.has_failures is False


async def test_a_matching_category_title_is_ok(mock_aiohttp: aioresponses) -> None:
    """Kills an implementation that warns on every category regardless."""
    cat_id = "01JSTOATCAT00000000000A"
    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SRV}?include_channels=true",
        payload={
            "server": {
                "_id": SRV,
                "channels": [],
                "categories": [{"id": cat_id, "title": "Original", "channels": []}],
            },
            "channels": [],
        },
    )
    mock_aiohttp.get(f"{BASE_URL}/servers/{SRV}/emojis", payload=[])
    _allow_any_message_window(mock_aiohttp)
    report = await run_check(BASE_URL, TOKEN, _state_with_category(cat_id, "Original"), _noop_event)
    assert next(r for r in report.results if r.discord_id == "d-cat-1").status == "ok"


async def test_a_missing_category_is_a_failure(mock_aiohttp: aioresponses) -> None:
    """A category absent from the server is a real loss, not a cosmetic one."""
    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SRV}?include_channels=true",
        payload={"server": {"_id": SRV, "channels": [], "categories": []}, "channels": []},
    )
    mock_aiohttp.get(f"{BASE_URL}/servers/{SRV}/emojis", payload=[])
    _allow_any_message_window(mock_aiohttp)
    report = await run_check(
        BASE_URL, TOKEN, _state_with_category("01JSTOATCAT00000000000A", "Original"), _noop_event
    )
    result = next(r for r in report.results if r.discord_id == "d-cat-1")
    assert result.status == "fail"
    assert result.kind == "category_missing"


async def test_an_absent_categories_key_does_not_crash(
    mock_aiohttp: aioresponses,
) -> None:
    """Server.categories is Optional upstream, so the key can be absent entirely
    rather than an empty list.

    Kills an implementation doing server["categories"] or assuming a list,
    which would raise on a server that has never had a category.
    """
    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SRV}?include_channels=true",
        payload={"server": {"_id": SRV, "channels": []}, "channels": []},
    )
    mock_aiohttp.get(f"{BASE_URL}/servers/{SRV}/emojis", payload=[])
    _allow_any_message_window(mock_aiohttp)
    report = await run_check(
        BASE_URL, TOKEN, _state_with_category("01JSTOATCAT00000000000A", "Original"), _noop_event
    )
    assert next(r for r in report.results if r.discord_id == "d-cat-1").status == "fail"


# ---------------------------------------------------------------------------
# structure: the request budget (task #255)
# ---------------------------------------------------------------------------


async def test_the_structure_family_costs_exactly_two_requests(
    mock_aiohttp: aioresponses,
) -> None:
    """Cost is a separate property from correctness and needs its own assertion.

    Kills a per-entity implementation: a loop calling api_fetch_channel per
    channel produces entirely CORRECT verdicts and 91 requests here, so any
    test checking only outcomes passes against it. On a 200-channel server that
    is 200 extra round trips against a 5-per-10-second bucket.

    Deliberately oversized fixture, 91 entities against 2 requests, so the two
    numbers cannot be confused for each other.
    """
    channels = {f"d-c{i}": f"01JSTOATCH{i:013d}" for i in range(40)}
    roles = {f"d-r{i}": f"01JSTOATRL{i:013d}" for i in range(15)}
    cats = {f"d-k{i}": f"01JSTOATCT{i:013d}" for i in range(6)}
    emoji = {f"d-e{i}": f"01JAUTUMNEM{i:012d}" for i in range(30)}
    assert len(channels) + len(roles) + len(cats) + len(emoji) == 91

    # repeat=True on BOTH routes is load-bearing for this test's honesty.
    # aioresponses serves a registered route once by default, so an
    # implementation making extra calls would die on ClientConnectionError
    # before the count assertion below ever ran, and the test would "pass"
    # against the mutant for entirely the wrong reason. Serving repeats lets
    # the over-fetching implementation succeed, so the count is the only thing
    # that can catch it. Verified by mutation: a duplicated server fetch now
    # fails on the count line, not on a connection error.
    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SRV}?include_channels=true",
        repeat=True,
        payload={
            "server": {
                "_id": SRV,
                "channels": list(channels.values()),
                "roles": dict.fromkeys(roles.values(), {"name": "r"}),
                "categories": [{"id": cid, "title": "t", "channels": []} for cid in cats.values()],
            },
            "channels": [{"_id": c, "name": "n"} for c in channels.values()],
        },
    )
    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SRV}/emojis",
        repeat=True,
        payload=[{"_id": e, "name": "e"} for e in emoji.values()],
    )
    _allow_any_message_window(mock_aiohttp)

    state = MigrationState()
    state.stoat_server_id = SRV
    state.channel_map = channels
    state.role_map = roles
    state.category_map = cats
    state.category_names = dict.fromkeys(cats, "t")
    state.emoji_map = emoji

    report = await run_check(BASE_URL, TOKEN, state, _noop_event)

    # Count the STRUCTURE requests only. The tail check deliberately costs one
    # request per channel and is budgeted separately; counting everything here
    # was only accidentally right while the tail check did not exist yet.
    structure_calls = sum(
        len(v) for k, v in mock_aiohttp.requests.items() if "/servers/" in str(k[1])
    )
    assert structure_calls == 2, (
        f"expected 2 structure requests for 91 entities, made {structure_calls}"
    )
    structure_results = [r for r in report.results if not r.name.startswith("tail:")]
    assert len(structure_results) == 91
    assert report.has_failures is False


async def test_a_category_with_no_recorded_title_is_unverifiable_not_ok(
    mock_aiohttp: aioresponses,
) -> None:
    """The category exists but nothing says what it should be called.

    Kills reporting ok here, which would claim the title was verified when it
    was never compared. Unreachable for a state a current Ferry writes, because
    category_map and category_names are written on adjacent lines; reachable for
    a state.json written before category_names existed, which is the population
    the degrade-rather-than-refuse rule serves.
    """
    cat_id = "01JSTOATCAT00000000000A"
    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SRV}?include_channels=true",
        payload={
            "server": {
                "_id": SRV,
                "channels": [],
                "categories": [{"id": cat_id, "title": "Whatever", "channels": []}],
            },
            "channels": [],
        },
    )
    mock_aiohttp.get(f"{BASE_URL}/servers/{SRV}/emojis", payload=[])
    _allow_any_message_window(mock_aiohttp)
    state = MigrationState()
    state.stoat_server_id = SRV
    state.category_map = {"d-cat-1": cat_id}
    # category_names deliberately left empty: an older state.json.
    report = await run_check(BASE_URL, TOKEN, state, _noop_event)
    result = next(r for r in report.results if r.discord_id == "d-cat-1")
    assert result.status == "unverifiable"
    assert result.kind == "category_title_unknown"
    assert result.found == "Whatever"


@pytest.mark.parametrize(
    "malformed",
    [
        pytest.param(["not-a-dict"], id="element-is-not-a-dict"),
        pytest.param([{"name": "general"}], id="element-lacks-_id"),
        pytest.param([{"id": "01JSTOATCH00000000000AAA"}], id="element-uses-id-not-_id"),
    ],
)
async def test_a_malformed_channel_object_never_reports_a_false_deletion(
    mock_aiohttp: aioresponses, malformed: list[object]
) -> None:
    """A property the three-way rule gives for free, pinned so it stays.

    A chunk review predicted that the isinstance filter would make a real
    channel look deleted. It cannot: the unfiltered server.channels id list is a
    SECOND, independent witness, and a fail needs both witnesses to agree the
    channel is absent. Whatever goes wrong with the object array degrades to
    unverifiable instead.

    Kills any refactor that drops the id list and decides from the objects
    alone, which would turn every one of these into a false report of deletion.
    """
    stoat_id = "01JSTOATCH00000000000AAA"
    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SRV}?include_channels=true",
        payload={"server": {"_id": SRV, "channels": [stoat_id]}, "channels": malformed},
    )
    mock_aiohttp.get(f"{BASE_URL}/servers/{SRV}/emojis", payload=[])
    _allow_any_message_window(mock_aiohttp)
    report = await run_check(
        BASE_URL, TOKEN, _state_with_channels({"d-100": stoat_id}), _noop_event
    )
    result = next(r for r in report.results if r.name == "channel:d-100")
    assert result.status == "unverifiable"
    assert report.has_failures is False


# ---------------------------------------------------------------------------
# the tail check (tasks #256, #257, #258, #259)
# ---------------------------------------------------------------------------

CH1 = "01JSTOATCH00000000000AAA"


def _sid(n: int) -> str:
    """A 26-char Stoat message id, monotonic in n.

    Stoat ids are ULIDs whose leading bits are a millisecond timestamp in
    Crockford base32, so lexicographic order on equal-length ids is time order.
    These are not real ULIDs but they share the two properties the classifier
    depends on: fixed width, and sorting in creation order.
    """
    return f"01JSTOATMSG{n:015d}"


def _did(n: int) -> str:
    """A Discord message id. Visibly different from the Stoat id for the same
    message, so a comparison against the wrong one cannot pass by accident."""
    return f"d-msg-{n}"


def _tail_state(
    *,
    high_water: int | None,
    count: int,
    mapped: bool = True,
    channel: str = "d-100",
) -> MigrationState:
    """A state describing one migrated channel.

    ``high_water`` is the index of the last message Ferry believes it sent.
    ``mapped`` False leaves message_map without an entry for it, which is the
    409 DuplicateNonce shape: the send landed but batch 7 deliberately records
    no id for it.
    """
    state = MigrationState()
    state.stoat_server_id = SRV
    state.channel_map = {channel: CH1}
    state.channel_message_counts = {channel: count}
    if high_water is not None:
        state.channel_high_water[channel] = _did(high_water)
        if mapped:
            state.message_map[_did(high_water)] = _sid(high_water)
    return state


def _register_tail(mock: aioresponses, window: list[int], *, channel_id: str = CH1) -> None:
    """Register the structure routes plus one channel's message window.

    The window is served NEWEST FIRST, which is what MessageSort::Latest
    produces upstream: doc!{"_id": -1}, not reversed downstream.
    """
    mock.get(
        f"{BASE_URL}/servers/{SRV}?include_channels=true",
        payload={
            "server": {"_id": SRV, "channels": [channel_id]},
            "channels": [{"_id": channel_id, "name": "general"}],
        },
    )
    mock.get(f"{BASE_URL}/servers/{SRV}/emojis", payload=[])
    mock.get(
        f"{BASE_URL}/channels/{channel_id}/messages?limit=100&sort=Latest",
        payload=[{"_id": _sid(n)} for n in sorted(window, reverse=True)],
    )


def _tail_result(report: CheckReport) -> CheckResult:
    return next(r for r in report.results if r.name.startswith("tail:"))


async def test_the_tail_is_the_newest_message(mock_aiohttp: aioresponses) -> None:
    """SC-3.1. The ordinary flatten case."""
    _register_tail(mock_aiohttp, list(range(10)))
    report = await run_check(BASE_URL, TOKEN, _tail_state(high_water=9, count=10), _noop_event)
    result = _tail_result(report)
    assert result.status == "ok"
    assert result.kind == "tail_present"


async def test_a_channel_shorter_than_the_window_is_ok(
    mock_aiohttp: aioresponses,
) -> None:
    """SC-3.2. The window is the whole channel."""
    _register_tail(mock_aiohttp, list(range(5)))
    report = await run_check(BASE_URL, TOKEN, _tail_state(high_water=4, count=5), _noop_event)
    assert _tail_result(report).status == "ok"


async def test_a_merge_parent_with_content_after_the_tail_is_ok(
    mock_aiohttp: aioresponses,
) -> None:
    """SC-3.3. Forty merged thread messages sit after the parent's own tail.

    This is the case the window approach exists for. An equality test asking
    "is the recorded tail the NEWEST message" fails here, and would fail on
    every merge parent, which is spec S3 criterion 5.
    """
    _register_tail(mock_aiohttp, list(range(50)))
    report = await run_check(BASE_URL, TOKEN, _tail_state(high_water=9, count=10), _noop_event)
    assert _tail_result(report).status == "ok"


async def test_a_channel_that_migrated_nothing_is_ok_and_does_not_raise(
    mock_aiohttp: aioresponses,
) -> None:
    """SC-3.8. An ordinary empty Discord channel.

    channel_high_water is written only under `if _channel_max_id:`, so a channel
    that sent nothing has NO entry. Kills the unconditional two-hop lookup,
    which raises KeyError here and reports the commonest correct case as a check
    error. Added by critique round 1.
    """
    _register_tail(mock_aiohttp, [])
    report = await run_check(BASE_URL, TOKEN, _tail_state(high_water=None, count=0), _noop_event)
    result = _tail_result(report)
    assert result.status == "ok"
    assert result.kind == "nothing_expected"


async def test_a_high_water_id_with_no_map_entry_is_unverifiable(
    mock_aiohttp: aioresponses,
) -> None:
    """SC-3.9. KNOWN GAP, pinned deliberately, tracked as #240.

    A send that returned 409 DuplicateNonce landed on the server, but batch 7
    deliberately writes no message_map entry for it because an empty-valued
    entry is worse than none. So the expected Stoat id cannot be resolved.

    unverifiable is the honest answer, and it must not raise. This asserts
    CURRENT behaviour and is not coverage of an intended outcome; #240 would
    close it by recording those sends durably.
    """
    _register_tail(mock_aiohttp, list(range(10)))
    report = await run_check(
        BASE_URL, TOKEN, _tail_state(high_water=9, count=10, mapped=False), _noop_event
    )
    result = _tail_result(report)
    assert result.status == "unverifiable"
    assert result.kind == "tail_not_recorded"


async def test_post_migration_activity_under_the_window_is_ok(
    mock_aiohttp: aioresponses,
) -> None:
    """SC-3.10. Someone posted after the migration. Not a defect."""
    _register_tail(mock_aiohttp, list(range(60)))
    report = await run_check(BASE_URL, TOKEN, _tail_state(high_water=9, count=10), _noop_event)
    assert _tail_result(report).status == "ok"


async def test_run_check_does_not_close_an_injected_session(
    mock_aiohttp: aioresponses,
) -> None:
    """SC-I4. Mirrors test_probe_does_not_close_an_injected_session.

    A caller that supplied the session owns it. Closing it would break a caller
    reusing one across calls, which batch 10's repair tool will do.
    """
    _register_tail(mock_aiohttp, list(range(3)))
    async with aiohttp.ClientSession() as injected:
        await run_check(
            BASE_URL,
            TOKEN,
            _tail_state(high_water=2, count=3),
            _noop_event,
            session=injected,
        )
        assert injected.closed is False


async def test_a_deleted_tail_inside_the_window_is_a_failure(
    mock_aiohttp: aioresponses,
) -> None:
    """SC-3.6. THE MANDATORY DISCRIMINATOR. Do not delete this test.

    A five-message channel whose recorded tail was deleted, with messages still
    present on BOTH sides of where it should be. So the window's oldest id sorts
    BELOW the expected tail and its newest sorts ABOVE it.

    This is the only one of the twelve tail scenarios that kills either ordering
    mutant, measured on the runnable prototype at
    docs/plans/designs/2026-08-11-check-tool-classifier-prototype.py:

        read the NEWEST id where the oldest belongs (max)   -> only this one
        take the oldest positionally as window[0]           -> only this one

    Every other scenario either contains the tail, so the oldest id is never
    consulted, or overflows the window, where both ends sort above the expected
    id and min and max agree. This is the only input where the two ends of the
    window disagree.

    Omitting it ships the ordering bug green while the suite looks thorough, and
    that bug inverts the verdicts: real data loss reported as unverifiable,
    ordinary post-migration activity reported as fail.
    """
    _register_tail(mock_aiohttp, [0, 1, 2, 3, 5, 6, 7, 8, 9])
    report = await run_check(BASE_URL, TOKEN, _tail_state(high_water=4, count=5), _noop_event)
    result = _tail_result(report)
    assert result.status == "fail"
    assert result.kind == "tail_absent"


async def test_the_whole_window_predating_the_tail_is_a_failure(
    mock_aiohttp: aioresponses,
) -> None:
    """SC-3.11. Every id in the window sorts below the expected tail, so the
    tail AND everything after it is gone while older content survives.

    An earlier acceptance criterion asked for warn here. Running the classifier
    across its whole input space showed warn was reachable from no path at all,
    and working out the meaning inverts the severity: this is STRONGER evidence
    of loss than SC-3.6, where only the tail itself is missing. It is a fail
    with its own kind, because a repair tool would treat the two differently.
    """
    _register_tail(mock_aiohttp, list(range(50)))
    report = await run_check(BASE_URL, TOKEN, _tail_state(high_water=200, count=201), _noop_event)
    result = _tail_result(report)
    assert result.status == "fail"
    assert result.kind == "tail_and_after_absent"


async def test_a_merge_parent_whose_tail_overflowed_the_window_is_unverifiable(
    mock_aiohttp: aioresponses,
) -> None:
    """SC-3.4. Three hundred merged messages pushed the parent's own tail out.

    The window is entirely NEWER than the expected tail, so it simply does not
    reach far enough back. That is not evidence of loss, and calling it fail
    would break spec S3 criterion 5: a merge migration must produce zero fail.
    """
    _register_tail(mock_aiohttp, list(range(210, 310)))
    report = await run_check(BASE_URL, TOKEN, _tail_state(high_water=9, count=10), _noop_event)
    result = _tail_result(report)
    assert result.status == "unverifiable"
    assert result.kind == "tail_window_exhausted"


async def test_post_migration_activity_over_the_window_is_unverifiable(
    mock_aiohttp: aioresponses,
) -> None:
    """SC-3.12. Same shape as the merge overflow, different cause.

    Both are "the window does not reach the tail", and neither is a defect.
    """
    _register_tail(mock_aiohttp, list(range(150, 250)))
    report = await run_check(BASE_URL, TOKEN, _tail_state(high_water=9, count=10), _noop_event)
    assert _tail_result(report).status == "unverifiable"


async def test_an_empty_channel_that_should_have_messages_is_a_failure(
    mock_aiohttp: aioresponses,
) -> None:
    """SC-3.7. State records ten migrated messages; the server returns none."""
    _register_tail(mock_aiohttp, [])
    report = await run_check(BASE_URL, TOKEN, _tail_state(high_water=9, count=10), _noop_event)
    result = _tail_result(report)
    assert result.status == "fail"
    assert result.kind == "channel_empty"


@pytest.mark.parametrize(
    ("window", "high_water", "count"),
    [
        pytest.param(list(range(10)), 9, 10, id="tail-is-newest"),
        pytest.param(list(range(5)), 4, 5, id="channel-shorter-than-window"),
        pytest.param(list(range(50)), 9, 10, id="merge-parent-small"),
        pytest.param(list(range(210, 310)), 9, 10, id="merge-parent-overflow"),
        pytest.param([0, 1, 2, 3, 5, 6, 7, 8, 9], 4, 5, id="deleted-tail"),
        pytest.param([], 9, 10, id="empty-channel"),
        pytest.param([], None, 0, id="nothing-expected"),
        pytest.param(list(range(60)), 9, 10, id="human-activity-small"),
        pytest.param(list(range(150, 250)), 9, 10, id="human-activity-overflow"),
        pytest.param(list(range(50)), 200, 201, id="whole-window-predates-tail"),
    ],
)
async def test_the_tail_check_never_emits_warn(
    mock_aiohttp: aioresponses,
    window: list[int],
    high_water: int | None,
    count: int,
) -> None:
    """SC-3.13. warn belongs to the NAME comparisons, and never to the tail.

    Driven across every tail scenario. Kills reintroducing the inverted-severity
    warn that critique round 2 removed, and documents in executable form that a
    warn in a report can only have come from a name comparison: a renamed
    category, channel or role. The tail check has no expected name to compare,
    so it has nothing to warn about.

    The assertion below is unchanged by 2.17.0 adding two more warn producers,
    because it filters to tail: results and both new producers live in the
    structure pass. Only this docstring needed correcting.
    """
    _register_tail(mock_aiohttp, window)
    report = await run_check(
        BASE_URL, TOKEN, _tail_state(high_water=high_water, count=count), _noop_event
    )
    tail_statuses = {r.status for r in report.results if r.name.startswith("tail:")}
    assert "warn" not in tail_statuses
    assert tail_statuses <= {"ok", "fail", "unverifiable"}


async def test_a_forum_index_channel_resolves_through_its_own_map(
    mock_aiohttp: aioresponses,
) -> None:
    """SC-3.5. The two maps are keyed DIFFERENTLY, and that is the whole test.

    _rebuild_forum_indexes reads state.channel_map.get(f"forum-index-{key}") but
    writes state.forum_index_message_ids[key], bare. So the check must strip the
    prefix before the second lookup.

    Kills looking up forum_index_message_ids[channel_map_key], which never
    matches and drops every forum index channel into unverifiable. Only a
    fixture using the literal prefixed key makes that visible, which is why the
    key here is spelled out rather than built from a variable.

    The index message is legitimately the newest in its channel, because
    _rebuild_forum_indexes runs AFTER the messages phase.
    """
    index_channel = "01JSTOATIDX00000000000AA"
    index_message = "01JSTOATIDXMSG0000000AA"
    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SRV}?include_channels=true",
        payload={
            "server": {"_id": SRV, "channels": [index_channel]},
            "channels": [{"_id": index_channel, "name": "forum-index"}],
        },
    )
    mock_aiohttp.get(f"{BASE_URL}/servers/{SRV}/emojis", payload=[])
    mock_aiohttp.get(
        f"{BASE_URL}/channels/{index_channel}/messages?limit=100&sort=Latest",
        payload=[{"_id": index_message}],
    )
    state = MigrationState()
    state.stoat_server_id = SRV
    state.channel_map = {"forum-index-cat-9": index_channel}
    # NO channel_message_counts entry, deliberately. That map is keyed by real
    # export channels and a forum index channel is synthetic, so a real state
    # never has one. An earlier version of this test set a count of 1, which
    # fabricated a shape that does not occur and let the zero-count rule hide
    # the fact that the forum branch was never reached.
    state.forum_index_message_ids = {"cat-9": index_message}
    report = await run_check(BASE_URL, TOKEN, state, _noop_event)
    result = _tail_result(report)
    assert result.status == "ok"
    assert result.kind == "tail_present"


async def test_a_channel_that_failed_the_structure_check_is_not_checked_again(
    mock_aiohttp: aioresponses,
) -> None:
    """SC-I2. One deleted channel should yield ONE finding, not two.

    No message route is registered for the missing channel, so the absence of a
    request is what proves the skip rather than only the result count: an
    implementation that fetched anyway would raise here.

    Kills reporting both channel_missing and channel_empty for the same cause,
    which doubles the apparent damage and sends a repair tool after a channel
    that does not exist.
    """
    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SRV}?include_channels=true",
        payload={"server": {"_id": SRV, "channels": []}, "channels": []},
    )
    mock_aiohttp.get(f"{BASE_URL}/servers/{SRV}/emojis", payload=[])
    report = await run_check(BASE_URL, TOKEN, _tail_state(high_water=9, count=10), _noop_event)
    for_channel = [r for r in report.results if r.discord_id == "d-100"]
    assert len(for_channel) == 1
    assert for_channel[0].kind == "channel_missing"


async def test_an_unverifiable_channel_is_also_skipped(
    mock_aiohttp: aioresponses,
) -> None:
    """A channel this token cannot view cannot have its messages read either.

    Kills skipping only on fail: an invisible channel would then be fetched,
    get a 403, and surface a second, confusing result for a cause already
    reported.
    """
    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SRV}?include_channels=true",
        payload={"server": {"_id": SRV, "channels": [CH1]}, "channels": []},
    )
    mock_aiohttp.get(f"{BASE_URL}/servers/{SRV}/emojis", payload=[])
    report = await run_check(BASE_URL, TOKEN, _tail_state(high_water=9, count=10), _noop_event)
    for_channel = [r for r in report.results if r.discord_id == "d-100"]
    assert len(for_channel) == 1
    assert for_channel[0].kind == "channel_not_visible"


# ---------------------------------------------------------------------------
# Integration (chunk 4 gate)
# ---------------------------------------------------------------------------


def _multi_server(visible: list[str], all_ids: list[str] | None = None) -> dict[str, object]:
    return {
        "server": {"_id": SRV, "channels": all_ids if all_ids is not None else visible},
        "channels": [{"_id": c, "name": f"ch-{c[-3:]}"} for c in visible],
    }


async def test_a_whole_merge_migration_produces_zero_failures(
    mock_aiohttp: aioresponses,
) -> None:
    """SC-I1. Spec S3 criterion 5, and the single most important outcome here.

    Three merge parents whose own tails sit UNDER later merged thread content,
    one of them so far under that the window cannot reach it, plus a forum index
    channel and an empty channel. Every one of these is a correct migration.

    Kills any regression to newest-message equality, which fails every merge
    parent, and any classifier change routing a window overflow to fail. A tool
    that cries wolf on a correct migration gets switched off, and then it
    protects nothing.
    """
    small, big, plain = (
        "01JSTOATCH00000000000001",
        "01JSTOATCH00000000000002",
        "01JSTOATCH00000000000003",
    )
    idx, empty = "01JSTOATIDX00000000000AA", "01JSTOATCH00000000000004"
    visible = [small, big, plain, idx, empty]
    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SRV}?include_channels=true", payload=_multi_server(visible)
    )
    mock_aiohttp.get(f"{BASE_URL}/servers/{SRV}/emojis", payload=[])

    def window(cid: str, ns: list[int]) -> None:
        mock_aiohttp.get(
            f"{BASE_URL}/channels/{cid}/messages?limit=100&sort=Latest",
            payload=[{"_id": _sid(n)} for n in sorted(ns, reverse=True)],
        )

    window(small, list(range(40)))  # tail at 9, 30 merged after it
    window(big, list(range(400, 500)))  # tail at 9, far out of reach
    window(plain, list(range(12)))  # ordinary flatten channel
    window(idx, [900])  # the index message
    window(empty, [])  # never received anything

    state = MigrationState()
    state.stoat_server_id = SRV
    state.channel_map = {
        "d-small": small,
        "d-big": big,
        "d-plain": plain,
        "forum-index-cat-9": idx,
        "d-empty": empty,
    }
    for key, hw, count in (("d-small", 9, 10), ("d-big", 9, 10), ("d-plain", 11, 12)):
        state.channel_high_water[key] = _did(hw)
        state.message_map[_did(hw)] = _sid(hw)
        state.channel_message_counts[key] = count
    state.forum_index_message_ids = {"cat-9": _sid(900)}

    report = await run_check(BASE_URL, TOKEN, state, _noop_event)

    failures = [r for r in report.results if r.status == "fail"]
    assert failures == [], f"a correct merge migration reported {len(failures)} failures"
    assert report.has_failures is False

    # "Zero failures" alone would also be satisfied by an implementation that
    # reported unverifiable for everything, which would be useless while
    # technically meeting the criterion. Pin the actual verdicts so this cannot
    # pass by giving up.
    tails = {r.discord_id: (r.status, r.kind) for r in report.results if r.name.startswith("tail:")}
    assert tails["d-small"] == ("ok", "tail_present")
    assert tails["d-plain"] == ("ok", "tail_present")
    assert tails["forum-index-cat-9"] == ("ok", "tail_present")
    assert tails["d-empty"] == ("ok", "nothing_expected")
    assert tails["d-big"] == ("unverifiable", "tail_window_exhausted")


async def test_one_channel_failing_does_not_abort_the_others(
    mock_aiohttp: aioresponses,
) -> None:
    """SC-I3. A per-channel error is a RESULT, not the end of the run.

    This is the deliberate contrast with the existing validate_after block,
    which catches everything into a warning and leaves its result dict empty, so
    a check that failed is indistinguishable from one that passed. Here a
    failure to check is recorded.

    Kills an asyncio.gather that propagates the first exception, and equally one
    using return_exceptions=True that then discards what it collected, which is
    a recorded silent-failure pattern in this project.
    """
    good1, bad, good2 = (
        "01JSTOATCH00000000000001",
        "01JSTOATCH00000000000002",
        "01JSTOATCH00000000000003",
    )
    visible = [good1, bad, good2]
    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SRV}?include_channels=true", payload=_multi_server(visible)
    )
    mock_aiohttp.get(f"{BASE_URL}/servers/{SRV}/emojis", payload=[])
    for cid in (good1, good2):
        mock_aiohttp.get(
            f"{BASE_URL}/channels/{cid}/messages?limit=100&sort=Latest",
            payload=[{"_id": _sid(9)}],
        )
    mock_aiohttp.get(
        f"{BASE_URL}/channels/{bad}/messages?limit=100&sort=Latest",
        status=500,
        payload={},
        repeat=True,
    )

    state = MigrationState()
    state.stoat_server_id = SRV
    state.channel_map = {"d-1": good1, "d-2": bad, "d-3": good2}
    for key in ("d-1", "d-2", "d-3"):
        state.channel_high_water[key] = _did(9)
        state.channel_message_counts[key] = 10
    state.message_map[_did(9)] = _sid(9)

    report = await run_check(BASE_URL, TOKEN, state, _noop_event)

    tails = {r.discord_id: r for r in report.results if r.name.startswith("tail:")}
    assert set(tails) == {"d-1", "d-2", "d-3"}
    assert tails["d-1"].status == "ok"
    assert tails["d-3"].status == "ok"
    assert tails["d-2"].status == "unverifiable"
    assert tails["d-2"].kind == "check_error"
    # check_error is unverifiable, not fail, so it does not fail the command:
    # Ferry could not look, which is not the same as finding something wrong.
    assert report.has_failures is False


async def test_a_flatten_migration_with_later_activity_is_all_ok(
    mock_aiohttp: aioresponses,
) -> None:
    """SC-I5. The third strategy, with humans having posted since."""
    a, b = "01JSTOATCH00000000000001", "01JSTOATCH00000000000002"
    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SRV}?include_channels=true", payload=_multi_server([a, b])
    )
    mock_aiohttp.get(f"{BASE_URL}/servers/{SRV}/emojis", payload=[])
    for cid, extra in ((a, 30), (b, 5)):
        mock_aiohttp.get(
            f"{BASE_URL}/channels/{cid}/messages?limit=100&sort=Latest",
            payload=[{"_id": _sid(n)} for n in range(9 + extra, -1, -1)],
        )
    state = MigrationState()
    state.stoat_server_id = SRV
    state.channel_map = {"d-a": a, "d-b": b}
    for key in ("d-a", "d-b"):
        state.channel_high_water[key] = _did(9)
        state.channel_message_counts[key] = 10
    state.message_map[_did(9)] = _sid(9)

    report = await run_check(BASE_URL, TOKEN, state, _noop_event)
    assert {r.status for r in report.results} == {"ok"}


async def test_all_seven_populations_in_one_run(mock_aiohttp: aioresponses) -> None:
    """SC-I6. Every population the design names, together, in one report.

    Kills a fix for one population that breaks another, which per-population
    tests structurally cannot see. The eighth population, a dry-run state, is
    absent on purpose: the precondition refuses that whole state before this
    point is reached, which is why its spec criterion was struck as unreachable
    rather than implemented.
    """
    flat = "01JSTOATCH00000000000001"
    merge_small = "01JSTOATCH00000000000002"
    merge_big = "01JSTOATCH00000000000003"
    idx = "01JSTOATIDX00000000000AA"
    dupe = "01JSTOATCH00000000000004"
    hidden = "01JSTOATCH00000000000005"
    silent = "01JSTOATCH00000000000006"

    visible = [flat, merge_small, merge_big, idx, dupe, silent]
    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SRV}?include_channels=true",
        # `hidden` is in the unfiltered id list but NOT among the objects: the
        # shape ViewChannel filtering produces.
        payload=_multi_server(visible, all_ids=[*visible, hidden]),
    )
    mock_aiohttp.get(f"{BASE_URL}/servers/{SRV}/emojis", payload=[])

    def window(cid: str, ns: list[int]) -> None:
        mock_aiohttp.get(
            f"{BASE_URL}/channels/{cid}/messages?limit=100&sort=Latest",
            payload=[{"_id": _sid(n)} for n in sorted(ns, reverse=True)],
        )

    window(flat, list(range(10)))
    window(merge_small, list(range(40)))
    window(merge_big, list(range(400, 500)))
    window(idx, [900])
    window(dupe, list(range(10)))
    window(silent, [])
    # No window for `hidden`: the tail pass must skip it. If it fetched anyway
    # this test fails on a missing mock, which is the point.

    state = MigrationState()
    state.stoat_server_id = SRV
    state.channel_map = {
        "d-flat": flat,
        "d-merge-small": merge_small,
        "d-merge-big": merge_big,
        "forum-index-cat-9": idx,
        "d-dupe": dupe,
        "d-hidden": hidden,
        "d-silent": silent,
    }
    for key in ("d-flat", "d-merge-small", "d-merge-big", "d-dupe", "d-hidden"):
        state.channel_high_water[key] = _did(9)
        state.channel_message_counts[key] = 10
    # message_map maps the Discord id to a DIFFERENT-looking Stoat id, which is
    # what makes a comparison against the wrong one impossible to pass.
    state.message_map[_did(9)] = _sid(9)
    # d-dupe's tail landed under a 409 DuplicateNonce, so batch 7 recorded no id
    # for it. Give it its own unmapped high-water.
    state.channel_high_water["d-dupe"] = _did(99)
    state.forum_index_message_ids = {"cat-9": _sid(900)}

    report = await run_check(BASE_URL, TOKEN, state, _noop_event)

    structure = {
        r.discord_id: (r.status, r.kind) for r in report.results if r.name.startswith("channel:")
    }
    tails = {r.discord_id: (r.status, r.kind) for r in report.results if r.name.startswith("tail:")}

    assert structure["d-hidden"] == ("unverifiable", "channel_not_visible")
    assert "d-hidden" not in tails, "an unreadable channel must not be reported twice"

    assert tails["d-flat"] == ("ok", "tail_present")
    assert tails["d-merge-small"] == ("ok", "tail_present")
    # A DIFFERENT kind from d-dupe below, deliberately: this one is merely out
    # of the window's reach, while that one can never be confirmed at all.
    assert tails["d-merge-big"] == ("unverifiable", "tail_window_exhausted")
    assert tails["forum-index-cat-9"] == ("ok", "tail_present")
    assert tails["d-dupe"] == ("unverifiable", "tail_not_recorded")
    assert tails["d-silent"] == ("ok", "nothing_expected")

    assert report.has_failures is False
    assert report.counts()["fail"] == 0


async def test_tail_results_keep_channel_map_order_despite_the_fan_out(
    mock_aiohttp: aioresponses,
) -> None:
    """The tail checks run concurrently, so their ORDER is a property worth
    pinning rather than an accident.

    asyncio.gather returns in INPUT order regardless of completion order, so a
    slow channel does not shuffle the report. Kills an append-as-completed
    implementation, which would render the CLI table in a different order on
    every run and make two reports of the same server impossible to diff.
    """
    ids = [f"01JSTOATCH0000000000000{n}" for n in range(6)]
    mock_aiohttp.get(f"{BASE_URL}/servers/{SRV}?include_channels=true", payload=_multi_server(ids))
    mock_aiohttp.get(f"{BASE_URL}/servers/{SRV}/emojis", payload=[])
    for cid in ids:
        mock_aiohttp.get(
            f"{BASE_URL}/channels/{cid}/messages?limit=100&sort=Latest",
            payload=[{"_id": _sid(9)}],
        )
    state = MigrationState()
    state.stoat_server_id = SRV
    state.channel_map = {f"d-{n}": cid for n, cid in enumerate(ids)}
    for n in range(6):
        state.channel_high_water[f"d-{n}"] = _did(9)
        state.channel_message_counts[f"d-{n}"] = 1
    state.message_map[_did(9)] = _sid(9)

    report = await run_check(BASE_URL, TOKEN, state, _noop_event)
    tail_order = [r.discord_id for r in report.results if r.name.startswith("tail:")]
    assert tail_order == [f"d-{n}" for n in range(6)]


@pytest.mark.parametrize(
    ("label", "window", "high_water", "count", "expected_status"),
    [
        pytest.param(
            "hole in the middle, tail intact",
            [0, 1, 2, 7, 8, 9],
            9,
            10,
            "ok",
            id="hole-in-the-middle",
        ),
        pytest.param(
            "tail deleted, then 100 newer messages arrived",
            list(range(200, 300)),
            9,
            10,
            "unverifiable",
            id="tail-deleted-then-overflowed",
        ),
    ],
)
async def test_two_shapes_of_real_loss_this_check_cannot_see(
    mock_aiohttp: aioresponses,
    label: str,
    window: list[int],
    high_water: int,
    count: int,
    expected_status: str,
) -> None:
    """KNOWN LIMITS, pinned so nobody later claims the tool catches these.

    Both inputs describe a channel that genuinely lost content, and neither is
    reported as a failure. That is accepted, documented in the design, and the
    direct consequence of checking a tail rather than reconciling every message.

    A chunk review asserted there were NO such inputs. There are exactly these
    two, which is why the claim is pinned in executable form rather than left
    to prose that can drift.

    An ok result here means "the recorded tail is present". It must never be
    read, or worded, as "this channel is complete". Full per-message
    reconciliation is batch 10's territory (spec P2 S9).
    """
    _register_tail(mock_aiohttp, window)
    report = await run_check(
        BASE_URL, TOKEN, _tail_state(high_water=high_water, count=count), _noop_event
    )
    result = _tail_result(report)
    assert result.status == expected_status
    assert report.has_failures is False


# ---------------------------------------------------------------------------
# The recorded thread strategy names the cause (#293, SC-1.2, SC-1.8, SC-1.9)
# ---------------------------------------------------------------------------


async def test_an_unrecorded_tail_names_the_recorded_strategy(
    mock_aiohttp: aioresponses,
) -> None:
    """SC-1.8. The whole point of recording thread_strategy.

    A merge parent legitimately has no recorded tail, because _merge_threads
    never writes message_map. Naming the strategy turns a list of possibilities
    into the one that applies.
    """
    _register_tail(mock_aiohttp, [0, 1, 2])
    state = _tail_state(high_water=2, count=3, mapped=False)
    state.thread_strategy = "merge"

    report = await run_check(BASE_URL, TOKEN, state, _noop_event)

    result = next(r for r in report.results if r.name == "tail:d-100")
    assert result.status == "unverifiable"
    assert result.kind == "tail_not_recorded"
    assert "merge" in result.detail


async def test_an_unrecorded_tail_keeps_the_old_wording_when_unrecorded(
    mock_aiohttp: aioresponses,
) -> None:
    """SC-1.9. A state.json written before 2.17.0 has no recorded strategy.

    It must keep the wording that shipped in v2.16.0 rather than naming an empty
    or unknown strategy, which would read as a defect rather than as an old file.
    """
    _register_tail(mock_aiohttp, [0, 1, 2])
    state = _tail_state(high_water=2, count=3, mapped=False)
    assert state.thread_strategy == ""

    report = await run_check(BASE_URL, TOKEN, state, _noop_event)

    result = next(r for r in report.results if r.name == "tail:d-100")
    assert result.status == "unverifiable"
    assert result.kind == "tail_not_recorded"
    assert "duplicate" in result.detail
    assert "merge" not in result.detail


async def test_an_empty_strategy_and_flatten_are_different_claims() -> None:
    """SC-1.2. This is the test that kills defaulting the field to "flatten".

    "" means no strategy was recorded, which every pre-2.17.0 migration reports.
    "flatten" means one was chosen. A default of "flatten" would assert a
    strategy nobody selected, and the check would then name a cause it cannot
    know. The two must produce different text.
    """
    details: dict[str, str] = {}
    for strategy in ("", "flatten"):
        with aioresponses() as m:
            _register_tail(m, [0, 1, 2])
            state = _tail_state(high_water=2, count=3, mapped=False)
            state.thread_strategy = strategy
            report = await run_check(BASE_URL, TOKEN, state, _noop_event)
        details[strategy] = next(r for r in report.results if r.name == "tail:d-100").detail

    assert details[""] != details["flatten"]
    assert "flatten" in details["flatten"]
    assert "flatten" not in details[""]


# ---------------------------------------------------------------------------
# channel_renamed (#295, SC-3.3, SC-3.5, SC-3.7, SC-3.8, SC-3.11, SC-3.13, SC-3.14)
# ---------------------------------------------------------------------------
#
# Fixture rule throughout: the Discord id, the Stoat id, the recorded name and
# the server's name are four distinct literals, so no assertion can pass by
# comparing a value with itself.


async def test_a_renamed_channel_reports_warn(mock_aiohttp: aioresponses) -> None:
    """SC-3.3. The feature this release exists for.

    warn rather than fail: the channel exists under its recorded id and its
    content is intact, and only the label moved. That matches what a renamed
    category has reported since v2.16.0, and it keeps the exit code at 0.
    """
    stoat_id = "01JSTOATCH00000000000AAA"
    _register(
        mock_aiohttp, _server_payload([stoat_id], [{"_id": stoat_id, "name": "renamed-here"}])
    )
    state = _state_with_channels({"d-100": stoat_id})
    state.created_channel_names = {"d-100": "general"}

    report = await run_check(BASE_URL, TOKEN, state, _noop_event)

    result = next(r for r in report.results if r.name == "channel:d-100")
    assert result.status == "warn"
    assert result.kind == "channel_renamed"
    assert result.expected == "general"
    assert result.found == "renamed-here"


async def test_a_matching_name_emits_no_rename_result(mock_aiohttp: aioresponses) -> None:
    """SC-3.5. A match keeps channel_present and emits nothing extra.

    Not a rename result with status ok: exactly one result for the channel, and
    it is the identity verdict.
    """
    stoat_id = "01JSTOATCH00000000000AAA"
    _register(mock_aiohttp, _server_payload([stoat_id], [{"_id": stoat_id, "name": "general"}]))
    state = _state_with_channels({"d-100": stoat_id})
    state.created_channel_names = {"d-100": "general"}

    report = await run_check(BASE_URL, TOKEN, state, _noop_event)

    results = [r for r in report.results if r.name == "channel:d-100"]
    assert len(results) == 1
    assert results[0].status == "ok"
    assert results[0].kind == "channel_present"


async def test_a_missing_channel_yields_exactly_one_result(mock_aiohttp: aioresponses) -> None:
    """SC-3.7. One cause, one result, and it is structural rather than guarded.

    A recorded name exists here, so an implementation that compared names before
    deciding presence would emit a rename alongside the failure. The comparison
    lives inside the arm that has already decided the channel is present, so a
    missing channel takes a different arm and cannot reach it.

    COUNT the results rather than checking the first one.
    """
    stoat_id = "01JSTOATCH00000000000AAA"
    _register(mock_aiohttp, _server_payload([], []))
    state = _state_with_channels({"d-100": stoat_id})
    state.created_channel_names = {"d-100": "general"}

    report = await run_check(BASE_URL, TOKEN, state, _noop_event)

    results = [r for r in report.results if r.name == "channel:d-100"]
    assert len(results) == 1
    assert results[0].kind == "channel_missing"
    assert results[0].status == "fail"


async def test_an_invisible_channel_yields_exactly_one_result(
    mock_aiohttp: aioresponses,
) -> None:
    """SC-3.8. The same rule for the middle arm of the three-way conditional."""
    stoat_id = "01JSTOATCH00000000000AAA"
    _register(mock_aiohttp, _server_payload([stoat_id], []))
    state = _state_with_channels({"d-100": stoat_id})
    state.created_channel_names = {"d-100": "general"}

    report = await run_check(BASE_URL, TOKEN, state, _noop_event)

    results = [r for r in report.results if r.name == "channel:d-100"]
    assert len(results) == 1
    assert results[0].kind == "channel_not_visible"
    assert results[0].status == "unverifiable"


async def test_a_channel_with_no_recorded_name_reports_present(
    mock_aiohttp: aioresponses,
) -> None:
    """SC-3.6, the back-compatibility half. KNOWN LIMIT, pinned deliberately.

    This is NOT a missing check. A state.json written before 2.17.0 records no
    channel name, so there is nothing to compare against and a renamed channel is
    undetectable for that migration. Documented in
    docs/guides/known-limitations.md, and it is what makes an old state file
    honest rather than noisy.
    """
    stoat_id = "01JSTOATCH00000000000AAA"
    _register(
        mock_aiohttp, _server_payload([stoat_id], [{"_id": stoat_id, "name": "renamed-here"}])
    )
    state = _state_with_channels({"d-100": stoat_id})
    assert state.created_channel_names == {}

    report = await run_check(BASE_URL, TOKEN, state, _noop_event)

    result = next(r for r in report.results if r.name == "channel:d-100")
    assert result.status == "ok"
    assert result.kind == "channel_present"


async def test_a_rename_keeps_the_exit_code_at_zero(mock_aiohttp: aioresponses) -> None:
    """SC-3.10. warn is not fail, and the v2.16.0 exit contract is unchanged."""
    stoat_id = "01JSTOATCH00000000000AAA"
    _register(mock_aiohttp, _server_payload([stoat_id], [{"_id": stoat_id, "name": "renamed"}]))
    state = _state_with_channels({"d-100": stoat_id})
    state.created_channel_names = {"d-100": "general"}

    report = await run_check(BASE_URL, TOKEN, state, _noop_event)

    assert report.counts()["warn"] == 1
    assert report.has_failures is False


async def test_the_truncation_cases_report_ok_end_to_end(
    mock_aiohttp: aioresponses,
) -> None:
    """SC-3.13 and SC-3.14. The check side of the trap the recording avoids.

    A Discord name over 32 characters is sent truncated, and a collision pair is
    sent with a numeric suffix. Because the RECORDED value is what Ferry sent,
    both compare equal against the live server and report ok. Recording ch.name
    would report a rename for both, against a server nobody edited.
    """
    long_truncated = "this-channel-name-is-definitely-"
    collided = "a-very-long-channel-name-that-c-1"
    first, second = "01JSTOATCH00000000000AAA", "01JSTOATCH00000000000BBB"
    _register(
        mock_aiohttp,
        _server_payload(
            [first, second],
            [{"_id": first, "name": long_truncated}, {"_id": second, "name": collided}],
        ),
    )
    state = _state_with_channels({"d-100": first, "d-101": second})
    state.created_channel_names = {"d-100": long_truncated, "d-101": collided}

    report = await run_check(BASE_URL, TOKEN, state, _noop_event)

    channel_results = [r for r in report.results if r.name.startswith("channel:")]
    assert len(channel_results) == 2
    assert {r.status for r in channel_results} == {"ok"}


async def test_the_rename_comparison_costs_no_extra_request(
    mock_aiohttp: aioresponses,
) -> None:
    """SC-3.11. The structure family is still two requests at any entity count.

    The names were already in the response the check has always made, so the
    comparison is free. This drives several renamed channels and several renamed
    roles at once and counts the structure requests.
    """
    ids = [f"01JSTOATCH0000000000{i:03d}" for i in range(6)]
    _register(
        mock_aiohttp,
        _server_payload(ids, [{"_id": i, "name": f"live-{n}"} for n, i in enumerate(ids)]),
    )
    state = _state_with_channels({f"d-{n}": i for n, i in enumerate(ids)})
    state.created_channel_names = {f"d-{n}": f"recorded-{n}" for n in range(6)}

    report = await run_check(BASE_URL, TOKEN, state, _noop_event)

    assert report.counts()["warn"] == 6
    structure_calls = [
        key
        for key in mock_aiohttp.requests
        if "/servers/" in str(key[1]) and "/messages" not in str(key[1])
    ]
    assert len(structure_calls) == 2, f"expected 2 structure requests, got {structure_calls}"


# ---------------------------------------------------------------------------
# role_renamed, and the guard reading a name introduces (#296, SC-3.4, SC-3.9)
# ---------------------------------------------------------------------------


def _roles_payload(roles: dict[str, object]) -> dict[str, object]:
    """A ServerWithChannels body carrying only roles."""
    return {"server": {"_id": SRV, "channels": [], "roles": roles}, "channels": []}


async def test_a_renamed_role_reports_warn(mock_aiohttp: aioresponses) -> None:
    """SC-3.4. The role half of the rename check.

    Roles have no permission-filtered sibling list, so presence is unambiguous
    and the comparison sits inside the present branch on the same reasoning as
    channels: a missing role takes the other branch and cannot be renamed too.
    """
    stoat_id = "01JSTOATRL0000000000AAA"
    _register(mock_aiohttp, _roles_payload({stoat_id: {"name": "moderators"}}))
    state = MigrationState()
    state.stoat_server_id = SRV
    state.role_map = {"d-role-1": stoat_id}
    state.created_role_names = {"d-role-1": "mods"}

    report = await run_check(BASE_URL, TOKEN, state, _noop_event)

    result = next(r for r in report.results if r.name == "role:d-role-1")
    assert result.status == "warn"
    assert result.kind == "role_renamed"
    assert result.expected == "mods"
    assert result.found == "moderators"
    assert report.has_failures is False


async def test_a_matching_role_name_emits_no_rename(mock_aiohttp: aioresponses) -> None:
    """SC-3.4, the negative half. A match keeps role_present and nothing else."""
    stoat_id = "01JSTOATRL0000000000AAA"
    _register(mock_aiohttp, _roles_payload({stoat_id: {"name": "mods"}}))
    state = MigrationState()
    state.stoat_server_id = SRV
    state.role_map = {"d-role-1": stoat_id}
    state.created_role_names = {"d-role-1": "mods"}

    report = await run_check(BASE_URL, TOKEN, state, _noop_event)

    results = [r for r in report.results if r.name == "role:d-role-1"]
    assert len(results) == 1
    assert results[0].kind == "role_present"


async def test_a_missing_role_is_not_also_renamed(mock_aiohttp: aioresponses) -> None:
    """SC-3.4. One cause, one result, for roles."""
    _register(mock_aiohttp, _roles_payload({}))
    state = MigrationState()
    state.stoat_server_id = SRV
    state.role_map = {"d-role-1": "01JSTOATRL0000000000AAA"}
    state.created_role_names = {"d-role-1": "mods"}

    report = await run_check(BASE_URL, TOKEN, state, _noop_event)

    results = [r for r in report.results if r.name == "role:d-role-1"]
    assert len(results) == 1
    assert results[0].kind == "role_missing"


async def test_a_malformed_role_value_degrades_rather_than_raising(
    mock_aiohttp: aioresponses,
) -> None:
    """SC-3.9. Reading a name introduces a raise that does not exist today.

    Before this change the roles map was consumed with `set(...)`, which takes
    the KEYS and never touches a value, so a None or a bare string was harmless.
    Calling .get("name") on one raises AttributeError, and `mypy --strict` cannot
    catch it because the payload is typed dict[str, Any]. The channel and
    category branches both hand-write the same isinstance guard for exactly this
    reason.

    A malformed value degrades to no-name-found and keeps role_present, so a
    broken response cannot turn into an aborted check.
    """
    none_id = "01JSTOATRL0000000000AAA"
    str_id = "01JSTOATRL0000000000BBB"
    list_id = "01JSTOATRL0000000000CCC"
    _register(
        mock_aiohttp,
        _roles_payload({none_id: None, str_id: "not-an-object", list_id: ["also", "wrong"]}),
    )
    state = MigrationState()
    state.stoat_server_id = SRV
    state.role_map = {"d-1": none_id, "d-2": str_id, "d-3": list_id}
    state.created_role_names = {"d-1": "mods", "d-2": "admins", "d-3": "helpers"}

    report = await run_check(BASE_URL, TOKEN, state, _noop_event)

    role_results = [r for r in report.results if r.name.startswith("role:")]
    assert len(role_results) == 3
    assert {r.kind for r in role_results} == {"role_present"}
    assert report.has_failures is False


async def test_a_channel_object_with_no_name_is_not_reported_renamed(
    mock_aiohttp: aioresponses,
) -> None:
    """The channel half of the same defect the role guard exposed.

    A channel object carrying no "name" key, or a non-string one, means Ferry
    could not read a name. That is NOT an empty name, and comparing a recorded
    name against "" would report a rename on every such channel, on a response
    Ferry simply could not parse.

    Found while writing the role guard: no existing channel fixture omits the
    name, so nothing else in the suite reaches this.
    """
    absent = "01JSTOATCH00000000000AAA"
    wrong_type = "01JSTOATCH00000000000BBB"
    _register(
        mock_aiohttp,
        _server_payload(
            [absent, wrong_type],
            [{"_id": absent}, {"_id": wrong_type, "name": 12345}],
        ),
    )
    state = _state_with_channels({"d-100": absent, "d-101": wrong_type})
    state.created_channel_names = {"d-100": "general", "d-101": "random"}

    report = await run_check(BASE_URL, TOKEN, state, _noop_event)

    channel_results = [r for r in report.results if r.name.startswith("channel:")]
    assert len(channel_results) == 2
    assert {r.kind for r in channel_results} == {"channel_present"}
    assert report.counts()["warn"] == 0


def test_no_prose_still_claims_warn_has_a_single_home() -> None:
    """SC-5.13. A claim asserted only in prose is the shape this project ships wrong.

    warn had exactly one producer until 2.17.0, and that fact was written into
    five places: two in verify.py's own module comment and three here. The design
    counted three and a fresh-context review confirmed three, so two survived
    both. This scans instead of counting.

    It reads the files as text rather than importing them, because the claims
    live in comments, which no runtime introspection can reach.
    """
    import pathlib

    # Built from fragments so the literals never appear in this file, which
    # would otherwise make the scan match its own search terms. That is not
    # hypothetical: the first version of this test failed against itself.
    stale = [
        "ONLY place warn" + " is reachable",
        "only warn in" + " the tool",
        "can only have come" + " from a renamed category",
        "single legitimate" + " home",
        "the only such difference" + " is a category title",
    ]
    root = pathlib.Path(__file__).resolve().parent.parent
    targets = [
        root / "src" / "discord_ferry" / "migrator" / "verify.py",
        root / "tests" / "test_verify.py",
    ]
    for path in targets:
        assert path.exists(), f"{path} moved; update this test rather than deleting it"
        text = path.read_text(encoding="utf-8")
        for phrase in stale:
            assert phrase not in text, (
                f"{path.name} still claims warn has one home: {phrase!r}. "
                "It has four producers: category_title_mismatch, "
                "channel_renamed, role_renamed and emoji_renamed."
            )


def test_the_kind_vocabulary_lists_both_rename_kinds() -> None:
    """SC-3.12. The docstring table is the contract another batch reads.

    docs/plans is gitignored, so the CheckResult docstring is the only durable
    record of the kind vocabulary the repair tool will dispatch on.
    """
    from discord_ferry.migrator.verify import CheckResult

    doc = CheckResult.__doc__ or ""
    assert "channel_renamed" in doc
    assert "role_renamed" in doc
    assert "emoji_renamed" in doc


_CHECK_REPORT_SCHEMAS = {
    1: {
        "document": {
            "schema_version": "int",
            "results": "list",
            "counts": "dict",
        },
        "result": {
            "name": {"str"},
            "status": {"str"},
            "kind": {"str"},
            "detail": {"str"},
            "discord_id": {"NoneType", "str"},
            "stoat_id": {"NoneType", "str"},
            "expected": {"NoneType", "str"},
            "found": {"NoneType", "str"},
        },
        "counts": {
            "ok": "int",
            "warn": "int",
            "fail": "int",
            "unverifiable": "int",
        },
        "statuses": {"ok", "warn", "fail", "unverifiable"},
        "kinds": {
            "category_missing",
            "category_present",
            "category_title_mismatch",
            "category_title_unknown",
            "channel_empty",
            "channel_missing",
            "channel_not_visible",
            "channel_present",
            "channel_renamed",
            "check_error",
            "emoji_missing",
            "emoji_present",
            "emoji_renamed",
            "nothing_expected",
            "role_missing",
            "role_present",
            "role_renamed",
            "tail_absent",
            "tail_and_after_absent",
            "tail_not_recorded",
            "tail_present",
            "tail_window_exhausted",
        },
    }
}


def _literal_kind_values(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return _literal_kind_values(node.body) | _literal_kind_values(node.orelse)
    raise AssertionError(f"nonliteral kind expression: {ast.unparse(node)}")


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
        return f"{node.func.value.id}.{node.func.attr}"
    return ast.unparse(node.func)


def _calls_by_function(tree: ast.AST) -> list[tuple[str, ast.Call]]:
    calls: list[tuple[str, ast.Call]] = []

    class Visitor(ast.NodeVisitor):
        function = ""

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            previous = self.function
            self.function = node.name
            self.generic_visit(node)
            self.function = previous

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            previous = self.function
            self.function = node.name
            self.generic_visit(node)
            self.function = previous

        def visit_Call(self, node: ast.Call) -> None:
            calls.append((self.function, node))
            self.generic_visit(node)

    Visitor().visit(tree)
    return calls


def _emitted_check_kinds() -> set[str]:
    source_path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "src"
        / "discord_ferry"
        / "migrator"
        / "verify.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    kinds: set[str] = set()
    allowed_forwarding = {
        ("add", "CheckResult"),
        ("_one_tail", "CheckResult"),
    }
    forwarding: set[tuple[str, str]] = set()
    for function, node in _calls_by_function(tree):
        for keyword in node.keywords:
            if keyword.arg != "kind":
                continue
            site = (function, _call_name(node))
            if isinstance(keyword.value, ast.Name) and keyword.value.id == "kind":
                assert site in allowed_forwarding, f"unexpected forwarded kind at {site}"
                forwarding.add(site)
            else:
                kinds.update(_literal_kind_values(keyword.value))
    assert forwarding == allowed_forwarding
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple):
            parts = node.value.elts
            if (
                len(parts) == 3
                and isinstance(parts[0], ast.Constant)
                and parts[0].value in STATUSES
            ):
                kinds.update(_literal_kind_values(parts[1]))
    return kinds


def test_every_emitted_kind_is_documented_and_versioned() -> None:
    """Every literal kind belongs to both the durable docs and the current schema."""
    documented = set(re.findall(r"``([a-z_]+)``", CheckResult.__doc__ or ""))
    emitted = _emitted_check_kinds()
    expected = _CHECK_REPORT_SCHEMAS[1]["kinds"]

    assert emitted == expected
    assert emitted <= documented
    source = (
        pathlib.Path(__file__).resolve().parent.parent
        / "src"
        / "discord_ferry"
        / "migrator"
        / "verify.py"
    ).read_text(encoding="utf-8")
    assert re.search(r"kind=f['\"]", source) is None


def test_kind_scanner_rejects_nonliteral_value_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A variable branch cannot add a kind that the version oracle misses."""
    source = """
def emit(present, new_kind):
    result(kind=("role_present" if present else new_kind))
"""
    monkeypatch.setattr(pathlib.Path, "read_text", lambda *args, **kwargs: source)

    with pytest.raises(AssertionError, match="nonliteral kind expression"):
        _emitted_check_kinds()


# ---------------------------------------------------------------------------
# CheckReport.to_dict() (#268)
# ---------------------------------------------------------------------------


class TestCheckReportToDict:
    """CheckReport.to_dict() serialization and sanitization."""

    def test_to_dict_shape_matches_existing_contract(self) -> None:
        """Output shape carries the schema version, results, and counts."""
        report = CheckReport()
        report.add(name="general", status="ok", kind="channel_present", detail="found")
        document = report.to_dict()
        assert set(document) == {"schema_version", "results", "counts"}
        assert document["schema_version"] == 1
        assert len(document["results"]) == 1
        assert document["counts"]["ok"] == 1

    def test_schema_version_covers_the_complete_contract(self) -> None:
        """Version 1 pins fields, types, counts, statuses, and every emitted kind."""
        report = CheckReport()
        report.add(
            name="channel:d-100",
            status="warn",
            kind="channel_renamed",
            detail="renamed",
            discord_id="d-100",
            stoat_id="01JSTOATCH00000000000AAA",
            expected="general",
            found="renamed-here",
        )
        report.add(name="role:d-200", status="ok", kind="role_present", detail="found")

        document = report.to_dict()
        rows = document["results"]
        actual = {
            "document": {key: type(value).__name__ for key, value in document.items()},
            "result": {key: {type(row[key]).__name__ for row in rows} for key in rows[0]},
            "counts": {key: type(value).__name__ for key, value in document["counts"].items()},
            "statuses": set(STATUSES),
            "kinds": _emitted_check_kinds(),
        }
        version = document["schema_version"]

        assert version in _CHECK_REPORT_SCHEMAS
        assert actual == _CHECK_REPORT_SCHEMAS[version]

    def test_ordinary_result_values_do_not_change_the_schema_version(self) -> None:
        """Data changes within the same contract retain version 1."""
        first = CheckReport()
        first.add(name="first", status="ok", kind="channel_present", detail="one")
        second = CheckReport()
        second.add(name="second", status="fail", kind="channel_missing", detail="two")

        assert first.to_dict()["schema_version"] == 1
        assert second.to_dict()["schema_version"] == 1

    def test_to_dict_strips_control_characters(self) -> None:
        """Free-text fields have C0/C1 control characters removed."""
        report = CheckReport()
        report.add(
            name="chan\x1b[31m-evil",
            status="ok",
            kind="channel_present",
            detail="detail\x9b-csi",
            discord_id="id\x00null",
            stoat_id="stoat\x07bell",
            expected="exp\nnewline",
            found="found\ttab-kept",
        )
        result = report.to_dict()["results"][0]
        assert result["name"] == "chan[31m-evil"
        assert result["detail"] == "detail-csi"
        assert result["discord_id"] == "idnull"
        assert result["stoat_id"] == "stoatbell"
        assert result["expected"] == "expnewline"
        assert result["found"] == "found\ttab-kept"

    def test_to_dict_preserves_none_optionals(self) -> None:
        """None fields stay None, not stripped."""
        report = CheckReport()
        report.add(
            name="test",
            status="ok",
            kind="channel_present",
            detail="ok",
        )
        result = report.to_dict()["results"][0]
        assert result["discord_id"] is None
        assert result["stoat_id"] is None
        assert result["expected"] is None
        assert result["found"] is None

    def test_to_dict_status_and_kind_not_stripped(self) -> None:
        """Internal literals are not processed by _strip_control."""
        report = CheckReport()
        report.add(
            name="test",
            status="ok",
            kind="channel_present",
            detail="ok",
        )
        result = report.to_dict()["results"][0]
        assert result["status"] == "ok"
        assert result["kind"] == "channel_present"

    def test_to_dict_empty_report(self) -> None:
        """Empty report produces empty results and zeroed counts."""
        report = CheckReport()
        d = report.to_dict()
        assert d["results"] == []
        assert all(v == 0 for v in d["counts"].values())


class TestRepairOutcome:
    """RepairOutcome.to_dict is the machine-readable contract for repair --json (#308)."""

    def test_to_dict_strips_control_characters(self) -> None:
        """Free-text fields pass through _strip_control on the way out."""
        outcome = RepairOutcome(
            recreated_channels=[
                {
                    "discord_id": "d-100",
                    "stoat_id": "01JSTOAT",
                    "name": "gen\x07eral",
                    "resent_count": 3,
                }
            ],
            declined=[{"type": "no_recorded_name", "message": "bad\x00name"}],
        )
        doc = outcome.to_dict()
        assert doc["actions"]["recreated_channels"][0]["name"] == "general"
        assert "\x00" not in doc["declined"][0]["message"]
        # Non-string fields are untouched.
        assert doc["actions"]["recreated_channels"][0]["resent_count"] == 3

    def test_to_dict_empty_sets_are_lists(self) -> None:
        """Empty sets serialise as [] with the keys present, never omitted."""
        doc = RepairOutcome().to_dict()
        assert doc["actions"]["recreated_channels"] == []
        assert doc["actions"]["recreated_roles"] == []
        assert doc["actions"]["recreated_categories"] == []
        assert doc["actions"]["restored_tails"] == []
        assert doc["declined"] == []
        assert doc["failed_messages"] == []
        assert doc["dry_run"] is False
        assert doc["actions"]["dead_letter"] == {"drained": 0, "remaining": 0}


def test_repair_outcome_carries_recreated_emoji() -> None:
    """SC-4.2: recreated_emoji appears under actions and is control-stripped."""
    outcome = RepairOutcome()
    outcome.recreated_emoji.append(
        {
            "discord_id": "d",
            "name": "smile\x07",
            "new_id": "n",
            "messages_rewritten": 2,
            "messages_declined": 0,
            "messages_failed": 0,
        }
    )
    row = outcome.to_dict()["actions"]["recreated_emoji"][0]
    assert row["new_id"] == "n"
    assert row["messages_rewritten"] == 2
    assert row["name"] == "smile"
