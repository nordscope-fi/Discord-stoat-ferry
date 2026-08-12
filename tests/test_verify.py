"""Tests for the migration check tool (#107 batch 9).

Fixture rule for this whole module: Discord ids and Stoat ids are LITERAL and
visibly different, ``"d-100"`` against ``"01JSTOAT..."``. They are never derived
from one variable and never equal. Seeding both sides of a comparison from a
single value is what lets a check test pass against an implementation comparing
the wrong one, and this project has shipped five assertions that could not fail
against what they guarded.
"""

from __future__ import annotations

import pytest
from aioresponses import aioresponses

from discord_ferry.errors import CheckError
from discord_ferry.migrator.verify import CheckReport, CheckResult, run_check
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


def _register(mock: aioresponses, payload: dict[str, object]) -> None:
    mock.get(f"{BASE_URL}/servers/{SRV}?include_channels=true", payload=payload)
    mock.get(f"{BASE_URL}/servers/{SRV}/emojis", payload=[])


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
    channel_results = [r for r in report.results if r.discord_id == "d-100"]
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
    result = next(r for r in report.results if r.discord_id == "d-100")
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
    result = next(r for r in report.results if r.discord_id == "d-100")
    assert result.status == "unverifiable"
    assert result.kind == "channel_not_visible"
    assert report.has_failures is False


async def test_a_renamed_channel_is_not_reported(
    mock_aiohttp: aioresponses,
) -> None:
    """KNOWN LIMIT, pinned deliberately. This is NOT coverage of an intended check.

    MigrationState records no channel name. Its only name fields are
    category_names, forum_category_names and author_names, and every write to
    channel_map is id to id. So there is no expected name to compare a found
    name against, and a renamed channel is undetectable.

    Tracked as spec P2 S11, which would record the names for FUTURE migrations.
    It cannot help any migration that already exists, which is the population
    this tool serves, so it was deferred rather than built.
    """
    stoat_id = "01JSTOATCH00000000000AAA"
    _register(
        mock_aiohttp,
        _server_payload([stoat_id], [{"_id": stoat_id, "name": "renamed-by-someone"}]),
    )
    report = await run_check(
        BASE_URL, TOKEN, _state_with_channels({"d-100": stoat_id}), _noop_event
    )
    result = next(r for r in report.results if r.discord_id == "d-100")
    assert result.status == "ok"


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
    result = next(r for r in report.results if r.discord_id == "forum-index-cat-9")
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
    state = MigrationState()
    state.stoat_server_id = SRV
    state.emoji_map = {"d-e1": emoji_id}
    report = await run_check(BASE_URL, TOKEN, state, _noop_event)
    assert next(r for r in report.results if r.discord_id == "d-e1").status == "ok"


# ---------------------------------------------------------------------------
# structure: categories, and the only warn in the tool (task #254)
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
    """The ONLY place warn is reachable in the whole tool.

    category_names is the one expected name MigrationState records, so a
    category title is the one name comparison possible. It warns rather than
    fails because the entity exists and its content is intact: someone renamed
    a heading, nothing was lost.

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
    report = await run_check(
        BASE_URL, TOKEN, _state_with_category(cat_id, "Original"), _noop_event
    )
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
    report = await run_check(
        BASE_URL, TOKEN, _state_with_category(cat_id, "Original"), _noop_event
    )
    assert next(r for r in report.results if r.discord_id == "d-cat-1").status == "ok"


async def test_a_missing_category_is_a_failure(mock_aiohttp: aioresponses) -> None:
    """A category absent from the server is a real loss, not a cosmetic one."""
    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SRV}?include_channels=true",
        payload={"server": {"_id": SRV, "channels": [], "categories": []}, "channels": []},
    )
    mock_aiohttp.get(f"{BASE_URL}/servers/{SRV}/emojis", payload=[])
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
    report = await run_check(
        BASE_URL, TOKEN, _state_with_category("01JSTOATCAT00000000000A", "Original"), _noop_event
    )
    assert next(r for r in report.results if r.discord_id == "d-cat-1").status == "fail"
