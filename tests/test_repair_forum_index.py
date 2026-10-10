"""Tests for forum-index repair in run_repair (#311).

The forum index lives under a synthetic ``forum-index-{forum_key}`` key that names no
Discord channel, so the generic recreation path cannot restore it. ``_recreate_forum_index``
recreates the channel and rebuilds its index message via the shared ``_rebuild_one_forum_index``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import aiohttp
from aioresponses import aioresponses

from discord_ferry.config import FerryConfig
from discord_ferry.core.engine import _recreate_forum_index, run_repair
from discord_ferry.migrator.verify import UNREPAIRED_WARNING_TYPES, CheckReport, CheckResult
from discord_ferry.state import MigrationState

if TYPE_CHECKING:
    from pathlib import Path

_ENGINE = "discord_ferry.core.engine"
_CHECK = "discord_ferry.migrator.verify.run_check"
_LIVE = f"{_ENGINE}._live_server_view"
_RECREATE = f"{_ENGINE}._recreate_forum_index"
_REBUILD = f"{_ENGINE}._rebuild_one_forum_index"
_DEDUPE = f"{_ENGINE}._remove_duplicate_forum_indexes"


def _config(tmp_path: Path) -> FerryConfig:
    return FerryConfig(
        export_dir=tmp_path,
        stoat_url="https://api.test",
        token="t",
        upload_delay=0.0,
        output_dir=tmp_path,
    )


def _result() -> CheckResult:
    return CheckResult(
        name="channel:forum-index-f",
        status="fail",
        kind="channel_missing",
        detail="the server does not list this channel",
        discord_id="forum-index-f",
        stoat_id="old-idx",
    )


async def test_recreate_forum_index_creates_and_rebuilds(tmp_path: Path) -> None:
    """SC-3.1/SC-4.1: the channel is recreated at position 0 and its index message rebuilt."""
    config = _config(tmp_path)
    state = MigrationState(
        stoat_server_id="srv",
        category_map={"f": "cat-s"},
        created_channel_names={"forum-index-f": "F-index"},
        forum_channel_members={"f": ["d1"]},
        forum_category_names={"f": "F"},
        channel_map={"d1": "s1"},
        channel_message_counts={"d1": 2},
    )
    live_categories = [{"id": "cat-s", "title": "F", "channels": ["old-idx"]}]

    with aioresponses() as mock:
        mock.post(
            "https://api.test/servers/srv/channels",
            payload={"_id": "new-idx", "name": "F-index"},
        )
        mock.patch("https://api.test/servers/srv", payload={})
        mock.post("https://api.test/channels/new-idx/messages", payload={"_id": "msg-1"})
        mock.post("https://api.test/channels/new-idx/messages/msg-1/pin", payload={})
        async with aiohttp.ClientSession() as session:
            created = await _recreate_forum_index(
                session, config, state, _result(), set(), live_categories, lambda e: None
            )

    assert created is True
    assert state.channel_map["forum-index-f"] == "new-idx"
    assert state.forum_index_message_ids["f"] == "msg-1"
    # The recreated index sits at position 0 and the dead id is dropped.
    assert live_categories[0]["channels"] == ["new-idx"]


async def test_recreate_forum_index_sends_fresh_when_a_stale_id_is_recorded(tmp_path: Path) -> None:
    """Whole-branch review: a migrated forum already has a forum_index_message_ids entry.

    That recorded id belongs to the DELETED old channel, so editing it in the new channel
    would fail silently. Repair must send a fresh message and record its id, not edit the
    stale one.
    """
    config = _config(tmp_path)
    state = MigrationState(
        stoat_server_id="srv",
        category_map={"f": "cat-s"},
        created_channel_names={"forum-index-f": "F-index"},
        forum_channel_members={"f": ["d1"]},
        forum_category_names={"f": "F"},
        channel_map={"d1": "s1"},
        channel_message_counts={"d1": 2},
        forum_index_message_ids={"f": "stale-old-msg"},  # from the original migration
    )
    live_categories = [{"id": "cat-s", "title": "F", "channels": ["old-idx"]}]

    with aioresponses() as mock:
        mock.post(
            "https://api.test/servers/srv/channels",
            payload={"_id": "new-idx", "name": "F-index"},
        )
        mock.patch("https://api.test/servers/srv", payload={})
        # Only the SEND route is registered. If the code took the edit branch against the
        # stale id, the PATCH would have no route and the rebuild would fail silently.
        mock.post("https://api.test/channels/new-idx/messages", payload={"_id": "fresh-msg"})
        mock.post("https://api.test/channels/new-idx/messages/fresh-msg/pin", payload={})
        async with aiohttp.ClientSession() as session:
            created = await _recreate_forum_index(
                session, config, state, _result(), set(), live_categories, lambda e: None
            )

    assert created is True
    assert state.forum_index_message_ids["f"] == "fresh-msg", "repair edited the stale id"


async def test_recreate_forum_index_clears_a_stale_present_unknown_mark(tmp_path: Path) -> None:
    """Whole-branch/second-opinion: a carried present-id-unknown mark must not suppress the send.

    force_new discards the mark so the new channel gets a fresh index message.
    """
    config = _config(tmp_path)
    state = MigrationState(
        stoat_server_id="srv",
        category_map={"f": "cat-s"},
        created_channel_names={"forum-index-f": "F-index"},
        forum_channel_members={"f": ["d1"]},
        forum_category_names={"f": "F"},
        channel_map={"d1": "s1"},
        forum_index_present_unknown_id={"f"},  # marked from an earlier duplicate
    )
    live_categories = [{"id": "cat-s", "title": "F", "channels": []}]
    with aioresponses() as mock:
        mock.post(
            "https://api.test/servers/srv/channels",
            payload={"_id": "new-idx", "name": "F-index"},
        )
        mock.patch("https://api.test/servers/srv", payload={})
        mock.post("https://api.test/channels/new-idx/messages", payload={"_id": "fresh-msg"})
        mock.post("https://api.test/channels/new-idx/messages/fresh-msg/pin", payload={})
        async with aiohttp.ClientSession() as session:
            created = await _recreate_forum_index(
                session, config, state, _result(), set(), live_categories, lambda e: None
            )
    assert created is True
    assert state.forum_index_message_ids["f"] == "fresh-msg"
    assert "f" not in state.forum_index_present_unknown_id, "the stale mark suppressed the send"


async def test_recreate_forum_index_declines_missing_category(tmp_path: Path) -> None:
    """SC-5.2: a gone forum category yields a distinct decline and creates nothing."""
    config = _config(tmp_path)
    state = MigrationState(
        stoat_server_id="srv",
        category_map={},  # the forum category itself is gone
        created_channel_names={"forum-index-f": "F-index"},
    )

    with aioresponses():  # no routes: any HTTP call would raise
        async with aiohttp.ClientSession() as session:
            created = await _recreate_forum_index(
                session, config, state, _result(), set(), [], lambda e: None
            )

    assert created is False
    assert any(w.get("type") == "forum_index_category_missing" for w in state.warnings)
    assert "forum-index-f" not in state.channel_map


async def test_recreate_forum_index_declines_a_dead_but_mapped_category(tmp_path: Path) -> None:
    """Second-opinion finding 3: a category_map id absent from the live server is dead.

    Check and repair preserve dead ids, so a truthy category_map entry is not proof the
    category exists. Resolving against live_categories declines instead of creating an
    orphan channel and reporting success.
    """
    config = _config(tmp_path)
    state = MigrationState(
        stoat_server_id="srv",
        category_map={"f": "deleted-category"},  # truthy, but not on the server
        created_channel_names={"forum-index-f": "F-index"},
    )

    with aioresponses():  # no routes: creating a channel would raise
        async with aiohttp.ClientSession() as session:
            created = await _recreate_forum_index(
                session, config, state, _result(), set(), [], lambda e: None
            )

    assert created is False
    assert any(w.get("type") == "forum_index_category_missing" for w in state.warnings)
    assert "forum-index-f" not in state.channel_map, "an orphan channel was created"


# --- Routing through run_repair (Task 2.2) ---------------------------------


def _report(kind: str) -> CheckReport:
    report = CheckReport()
    report.add(
        name="channel:forum-index-f",
        status="fail",
        kind=kind,
        detail="forum index needs repair",
        discord_id="forum-index-f",
        stoat_id="old-idx",
    )
    return report


def _routing_state(tmp_path: Path) -> MigrationState:
    return MigrationState(
        stoat_server_id="srv",
        category_map={"f": "cat-s"},
        created_channel_names={"forum-index-f": "F-index"},
        forum_channel_members={"f": ["d1"]},
        forum_category_names={"f": "F"},
        channel_map={"d1": "s1", "forum-index-f": "old-idx"},
        forum_index_message_ids={"f": "old-idx-msg"},
    )


async def test_repair_recreates_lone_forum_index(tmp_path: Path) -> None:
    """SC-I2/SC-3.2/SC-3.3: the forum index as the ONLY broken entity is still repaired.

    structure_work is empty, so the round-2 session gap would have dropped this silently.
    """
    config = _config(tmp_path)
    state = _routing_state(tmp_path)
    with (
        patch(_DEDUPE, new=AsyncMock()),
        patch(_CHECK, new=AsyncMock(return_value=_report("channel_missing"))),
        patch(_LIVE, new=AsyncMock(return_value=(set(), [{"id": "cat-s", "channels": []}]))),
        patch(_RECREATE, new=AsyncMock(return_value=True)) as recreate,
    ):
        await run_repair(config, state, [], [].append)
    recreate.assert_awaited_once()
    assert not any(w.get("type") == "no_discord_metadata" for w in state.warnings)
    assert not any(w.get("type") == "forum_index_not_repairable" for w in state.warnings)


async def test_repair_routes_tail_absent_to_rebuild_without_create(tmp_path: Path) -> None:
    """SC-I4: a forum index whose channel survives but message is gone rebuilds, no create."""
    config = _config(tmp_path)
    state = _routing_state(tmp_path)
    with (
        patch(_DEDUPE, new=AsyncMock()),
        patch(_CHECK, new=AsyncMock(return_value=_report("tail_absent"))),
        patch(_LIVE, new=AsyncMock(return_value=(set(), []))),
        patch(_RECREATE, new=AsyncMock(return_value=True)) as recreate,
        patch(_REBUILD, new=AsyncMock()) as rebuild,
    ):
        await run_repair(config, state, [], [].append)
    rebuild.assert_awaited_once()
    assert rebuild.await_args.args[3] == "f"  # forum_key
    # force_new: the recorded id is the message just confirmed gone, so send fresh.
    assert rebuild.await_args.kwargs.get("force_new") is True
    assert rebuild.await_args.kwargs.get("phase") == "repair"
    recreate.assert_not_awaited()


async def test_repair_forum_index_dry_run_mutates_nothing(tmp_path: Path) -> None:
    """SC-3.4: --dry-run recreates nothing and writes no state."""
    config = FerryConfig(
        export_dir=tmp_path,
        stoat_url="https://api.test",
        token="t",
        upload_delay=0.0,
        output_dir=tmp_path,
        dry_run=True,
    )
    state = _routing_state(tmp_path)
    with (
        patch(_DEDUPE, new=AsyncMock()),
        patch(_CHECK, new=AsyncMock(return_value=_report("channel_missing"))),
        patch(_RECREATE, new=AsyncMock(return_value=True)) as recreate,
    ):
        await run_repair(config, state, [], [].append)
    recreate.assert_not_awaited()
    assert state.channel_map["forum-index-f"] == "old-idx"  # unchanged


async def test_tail_rebuild_records_a_restored_tail(tmp_path: Path) -> None:
    """Second-opinion finding 4: a successful tail rebuild is recorded in the outcome.

    Without this the rebuild-only path did real work but reported nothing to --json / GUI.
    """
    config = _config(tmp_path)
    state = _routing_state(tmp_path)

    async def _fake_rebuild(_sess, _cfg, st, forum_key, _ev, *, phase="report", force_new=False):  # type: ignore[no-untyped-def]
        st.forum_index_message_ids[forum_key] = "rebuilt-msg"

    with (
        patch(_DEDUPE, new=AsyncMock()),
        patch(_CHECK, new=AsyncMock(return_value=_report("tail_absent"))),
        patch(_REBUILD, new=_fake_rebuild),
    ):
        outcome = await run_repair(config, state, [], [].append)
    assert any(t["discord_id"] == "forum-index-f" for t in outcome.restored_tails)


def test_category_missing_decline_is_in_exit_set() -> None:
    """SC-5.2: the distinct decline forces a non-zero exit, like other unrepaired gaps."""
    assert "forum_index_category_missing" in UNREPAIRED_WARNING_TYPES


def test_rebuild_failed_is_in_exit_set() -> None:
    """Second-opinion finding 1/2: a repair rebuild failure forces a non-zero exit."""
    assert "forum_index_rebuild_failed" in UNREPAIRED_WARNING_TYPES


# --- Extra pinned forum index left by older migrations (#1142) ---------------
#
# Before v2.41.30 the create path posted `ferry-forum-index-{key}` and never recorded its id,
# so the REPORT rebuild posted a second message, `ferry-forum-index-rebuilt-{key}`, and only
# that one is recorded. Repair removes the unrecorded Ferry copy.

_API = "https://api.test"
_ME = f"{_API}/users/@me"
_SEARCH = f"{_API}/channels/idx/search"


def _clean_report() -> CheckReport:
    return CheckReport()


def _dup_state() -> MigrationState:
    return MigrationState(
        stoat_server_id="srv",
        category_map={"f": "cat-s"},
        created_channel_names={"forum-index-f": "F-index"},
        forum_channel_members={"f": ["d1"]},
        forum_category_names={"f": "F"},
        channel_map={"d1": "s1", "forum-index-f": "idx"},
        forum_index_message_ids={"f": "rebuilt"},
    )


def _pinned(
    mid: str,
    *,
    author: str = "me",
    masquerade_name: str | None = "Discord Ferry",
    content: str = "**Forum: F**\n- <#s1> - 2 messages",
) -> dict[str, object]:
    msg: dict[str, object] = {"_id": mid, "author": author, "content": content}
    if masquerade_name is not None:
        msg["masquerade"] = {"name": masquerade_name}
    return msg


def _deletes(mock: aioresponses) -> list[str]:
    return [str(url) for (method, url) in mock.requests if method == "DELETE"]


def _warning_types(state: MigrationState) -> list[str]:
    return [str(w.get("type")) for w in state.warnings]


async def test_repair_removes_only_the_unrecorded_ferry_copy(tmp_path: Path) -> None:
    """Two pinned Ferry copies, one recorded: only the unrecorded one is deleted."""
    config = _config(tmp_path)
    state = _dup_state()
    with patch(_CHECK, new=AsyncMock(return_value=_clean_report())), aioresponses() as mock:
        mock.get(_ME, payload={"_id": "me"})
        mock.post(_SEARCH, payload=[_pinned("rebuilt"), _pinned("created")])
        mock.delete(f"{_API}/channels/idx/messages/created", status=204)
        outcome = await run_repair(config, state, [], [].append)
        assert _deletes(mock) == [f"{_API}/channels/idx/messages/created"]
    assert state.forum_index_message_ids == {"f": "rebuilt"}
    assert outcome.removed_duplicate_indexes == [
        {"forum_key": "f", "stoat_channel_id": "idx", "message_id": "created", "removed": True}
    ]
    assert outcome.to_dict()["actions"]["removed_duplicate_indexes"][0]["message_id"] == "created"


async def test_repair_leaves_other_authors_and_non_ferry_messages_alone(tmp_path: Path) -> None:
    """Same header, but not Ferry's own message: never deleted."""
    config = _config(tmp_path)
    state = _dup_state()
    pinned = [
        _pinned("rebuilt"),
        _pinned("other-author", author="someone-else"),  # Ferry masquerade, wrong author
        _pinned("other-name", masquerade_name="Somebody"),  # right author, wrong masquerade
        _pinned("no-masq", masquerade_name=None),  # right author, no masquerade
        _pinned("other-text", content="**Forum: Other**\nstuff"),  # not this forum's header
    ]
    with patch(_CHECK, new=AsyncMock(return_value=_clean_report())), aioresponses() as mock:
        mock.get(_ME, payload={"_id": "me"})
        mock.post(_SEARCH, payload=pinned)
        outcome = await run_repair(config, state, [], [].append)
        assert _deletes(mock) == []
    assert outcome.removed_duplicate_indexes == []


async def test_repair_never_deletes_the_recorded_message_alone(tmp_path: Path) -> None:
    """A lone recorded copy is left alone, and a missing recorded copy blocks every delete.

    If the recorded message is not among the pinned results, the unrecorded copy may be the
    only index left, so nothing is removed.
    """
    config = _config(tmp_path)
    state = _dup_state()
    with patch(_CHECK, new=AsyncMock(return_value=_clean_report())), aioresponses() as mock:
        mock.get(_ME, payload={"_id": "me"})
        mock.post(_SEARCH, payload=[_pinned("rebuilt")])
        await run_repair(config, state, [], [].append)
        assert _deletes(mock) == []

    state = _dup_state()
    with patch(_CHECK, new=AsyncMock(return_value=_clean_report())), aioresponses() as mock:
        mock.get(_ME, payload={"_id": "me"})
        mock.post(_SEARCH, payload=[_pinned("created")])
        await run_repair(config, state, [], [].append)
        assert _deletes(mock) == []


async def test_repair_dry_run_reports_the_duplicate_and_deletes_nothing(tmp_path: Path) -> None:
    config = FerryConfig(
        export_dir=tmp_path,
        stoat_url=_API,
        token="t",
        upload_delay=0.0,
        output_dir=tmp_path,
        dry_run=True,
    )
    state = _dup_state()
    events: list[str] = []
    with patch(_CHECK, new=AsyncMock(return_value=_clean_report())), aioresponses() as mock:
        mock.get(_ME, payload={"_id": "me"})
        mock.post(_SEARCH, payload=[_pinned("rebuilt"), _pinned("created")])
        outcome = await run_repair(config, state, [], lambda e: events.append(e.message))
        assert _deletes(mock) == []
        assert not any(method in ("PATCH", "PUT") for (method, _url) in mock.requests)
    assert outcome.removed_duplicate_indexes == [
        {"forum_key": "f", "stoat_channel_id": "idx", "message_id": "created", "removed": False}
    ]
    assert any("created" in m and "DRY RUN" in m for m in events)
    assert state.warnings == []
    assert state.forum_index_message_ids == {"f": "rebuilt"}


async def test_repair_failed_delete_warns_and_continues(tmp_path: Path) -> None:
    config = _config(tmp_path)
    state = _dup_state()
    with patch(_CHECK, new=AsyncMock(return_value=_clean_report())), aioresponses() as mock:
        mock.get(_ME, payload={"_id": "me"})
        mock.post(_SEARCH, payload=[_pinned("rebuilt"), _pinned("dup-a"), _pinned("dup-b")])
        mock.delete(f"{_API}/channels/idx/messages/dup-a", status=403, body="forbidden")
        mock.delete(f"{_API}/channels/idx/messages/dup-b", status=204)
        outcome = await run_repair(config, state, [], [].append)
    assert _warning_types(state).count("forum_index_duplicate_remove_failed") == 1
    assert [(r["message_id"], r["removed"]) for r in outcome.removed_duplicate_indexes] == [
        ("dup-b", True)
    ]
    assert any(d["type"] == "forum_index_duplicate_remove_failed" for d in outcome.declined)


async def test_repair_skips_forums_that_are_not_recorded_index_channels(tmp_path: Path) -> None:
    """No recorded id, or no mapped index channel: no search and no delete."""
    config = _config(tmp_path)
    state = _dup_state()
    state.forum_index_message_ids = {"f": "rebuilt", "g": "g-msg"}  # g has no index channel
    state.forum_index_present_unknown_id = {"h"}  # h has no recorded id to keep
    state.channel_map["forum-index-h"] = "idx-h"
    with patch(_CHECK, new=AsyncMock(return_value=_clean_report())), aioresponses() as mock:
        mock.get(_ME, payload={"_id": "me"})
        mock.post(_SEARCH, payload=[_pinned("rebuilt")])
        await run_repair(config, state, [], [].append)
        searched = [str(url) for (method, url) in mock.requests if method == "POST"]
    assert searched == [_SEARCH]


async def test_repair_without_a_known_self_id_deletes_nothing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    state = _dup_state()
    with patch(_CHECK, new=AsyncMock(return_value=_clean_report())), aioresponses() as mock:
        mock.get(_ME, payload={})  # no `_id`: the author cannot be matched
        mock.post(_SEARCH, payload=[_pinned("rebuilt"), _pinned("created")])
        await run_repair(config, state, [], [].append)
        assert _deletes(mock) == []
    assert "forum_index_duplicate_remove_failed" in _warning_types(state)


async def test_repair_with_no_forum_indexes_makes_no_request(tmp_path: Path) -> None:
    config = _config(tmp_path)
    state = MigrationState(stoat_server_id="srv")
    with patch(_CHECK, new=AsyncMock(return_value=_clean_report())), aioresponses() as mock:
        await run_repair(config, state, [], [].append)
        # The #957 convergence check still spends its one GET; the cleanup adds nothing.
        urls = [str(url) for (_method, url) in mock.requests]
    assert not any("/users/@me" in u or "/search" in u for u in urls)


def test_duplicate_remove_failed_is_in_exit_set() -> None:
    assert "forum_index_duplicate_remove_failed" in UNREPAIRED_WARNING_TYPES


# --- A forum with no posts: the create path sent no heading (#1142) ----------

_NO_POSTS_CREATE = "No posts migrated."
_NO_POSTS_REBUILT = "**Forum: F**\nNo posts migrated."


async def test_repair_removes_the_headerless_copy_of_a_forum_with_no_posts(tmp_path: Path) -> None:
    """The create path sent exactly `No posts migrated.`; the rebuild sent it under a heading."""
    config = _config(tmp_path)
    state = _dup_state()
    with patch(_CHECK, new=AsyncMock(return_value=_clean_report())), aioresponses() as mock:
        mock.get(_ME, payload={"_id": "me"})
        mock.post(
            _SEARCH,
            payload=[
                _pinned("rebuilt", content=_NO_POSTS_REBUILT),
                _pinned("created", content=_NO_POSTS_CREATE),
            ],
        )
        mock.delete(f"{_API}/channels/idx/messages/created", status=204)
        outcome = await run_repair(config, state, [], [].append)
        assert _deletes(mock) == [f"{_API}/channels/idx/messages/created"]
    assert [r["message_id"] for r in outcome.removed_duplicate_indexes] == ["created"]


async def test_repair_keeps_a_headerless_message_that_is_not_ferrys(tmp_path: Path) -> None:
    config = _config(tmp_path)
    state = _dup_state()
    pinned = [
        _pinned("rebuilt", content=_NO_POSTS_REBUILT),
        _pinned("other-author", author="someone-else", content=_NO_POSTS_CREATE),
        _pinned("other-name", masquerade_name="Somebody", content=_NO_POSTS_CREATE),
        _pinned("no-masq", masquerade_name=None, content=_NO_POSTS_CREATE),
        _pinned("longer", content=_NO_POSTS_CREATE + " Extra words."),
    ]
    with patch(_CHECK, new=AsyncMock(return_value=_clean_report())), aioresponses() as mock:
        mock.get(_ME, payload={"_id": "me"})
        mock.post(_SEARCH, payload=pinned)
        await run_repair(config, state, [], [].append)
        assert _deletes(mock) == []


async def test_headerless_copy_is_kept_when_the_recorded_message_is_not_pinned(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    state = _dup_state()
    with patch(_CHECK, new=AsyncMock(return_value=_clean_report())), aioresponses() as mock:
        mock.get(_ME, payload={"_id": "me"})
        mock.post(_SEARCH, payload=[_pinned("created", content=_NO_POSTS_CREATE)])
        await run_repair(config, state, [], [].append)
        assert _deletes(mock) == []
