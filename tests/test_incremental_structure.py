"""Tests for incremental-mode structure phase behaviour.

Role/server scenarios: SC-3, SC-4 (role), SC-5, SC-11, SC-12.
Category/channel idempotency scenarios (Task 3): SC-1, SC-2, SC-4 (channel),
SC-6, SC-7, SC-9.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from aioresponses import aioresponses

from discord_ferry.config import FerryConfig
from discord_ferry.core.engine import _rebuild_forum_indexes
from discord_ferry.discord.metadata import (
    DiscordMetadata,
    PermissionPair,
    RoleMeta,
    save_discord_metadata,
)
from discord_ferry.errors import DuplicateSendError, MigrationError
from discord_ferry.migrator.messages import ChannelResult, _merge_channel_result, run_messages
from discord_ferry.migrator.structure import (
    run_categories,
    run_channels,
    run_roles,
    run_server,
)
from discord_ferry.parser.models import (
    DCEAuthor,
    DCEChannel,
    DCEExport,
    DCEGuild,
    DCEMessage,
    DCERole,
)
from discord_ferry.state import MigrationState

if TYPE_CHECKING:
    from pathlib import Path


STOAT_URL = "https://api.test"


# ---------------------------------------------------------------------------
# Helpers (mirrors tests/test_structure.py)
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path, **overrides: object) -> FerryConfig:
    defaults: dict[str, object] = {
        "export_dir": tmp_path,
        "stoat_url": STOAT_URL,
        "token": "tok",
        "output_dir": tmp_path,
    }
    defaults.update(overrides)
    return FerryConfig(**defaults)  # type: ignore[arg-type]


def _make_export(
    guild_id: str = "111",
    guild_name: str = "Test",
    guild_icon_url: str = "",
    channel_id: str = "222",
    channel_name: str = "general",
    channel_type: int = 0,
    category_id: str = "cat1",
    category: str = "General",
    is_thread: bool = False,
    parent_channel_name: str = "",
    messages: list[DCEMessage] | None = None,
    message_count: int = 0,
) -> DCEExport:
    guild = DCEGuild(id=guild_id, name=guild_name, icon_url=guild_icon_url)
    channel = DCEChannel(
        id=channel_id,
        type=channel_type,
        name=channel_name,
        category_id=category_id,
        category=category,
    )
    return DCEExport(
        guild=guild,
        channel=channel,
        messages=messages or [],
        message_count=message_count,
        is_thread=is_thread,
        parent_channel_name=parent_channel_name,
    )


# ---------------------------------------------------------------------------
# SC-12: Server reuse via carried id (no --server-id)
# ---------------------------------------------------------------------------


async def test_run_server_reuses_carried_stoat_server_id(tmp_path: Path) -> None:
    """SC-12: run_server uses state.stoat_server_id when config.server_id is None.

    POST /servers/create is intentionally NOT registered. If run_server were to
    attempt a create, aioresponses would raise a ConnectionError for the
    unmatched request and the test would fail.
    """
    state = MigrationState(stoat_server_id="srv1")
    config = _make_config(tmp_path, incremental=True, server_id=None)
    exports = [_make_export()]

    with aioresponses() as m:
        m.get(f"{STOAT_URL}/servers/srv1", payload={"_id": "srv1"})
        # /servers/create intentionally absent — a create attempt would error.
        await run_server(config, state, exports, lambda e: None)

    assert state.stoat_server_id == "srv1"


# ---------------------------------------------------------------------------
# SC-11: Deleted prior server → clear MigrationError
# ---------------------------------------------------------------------------


async def test_run_server_deleted_prior_server_raises_clear_error(tmp_path: Path) -> None:
    """SC-11: run_server raises MigrationError naming the id when server is 404.

    No POST /servers/create should be attempted after a 404 on the carried id.
    """
    state = MigrationState(stoat_server_id="gone")
    config = _make_config(tmp_path, incremental=True, server_id=None)
    exports = [_make_export()]

    with aioresponses() as m:
        m.get(f"{STOAT_URL}/servers/gone", status=404, payload={"type": "NotFound"})
        with pytest.raises(MigrationError, match="gone"):
            await run_server(config, state, exports, lambda e: None)


# ---------------------------------------------------------------------------
# Helpers for role tests
# ---------------------------------------------------------------------------


def _make_role_export(roles: list[DCERole], guild_id: str = "111") -> DCEExport:
    """Build a minimal DCEExport whose single message carries the given roles."""
    msg = DCEMessage(
        id="m1",
        type="Default",
        timestamp="2024-01-01T00:00:00Z",
        content="hi",
        author=DCEAuthor(id="u1", name="User", roles=roles),
    )
    return _make_export(guild_id=guild_id, messages=[msg])


# ---------------------------------------------------------------------------
# SC-3: Existing roles get zero attribute/permission edits on re-run
# ---------------------------------------------------------------------------


async def test_run_roles_skips_all_passes_for_already_migrated_roles(
    tmp_path: Path,
) -> None:
    """SC-3: re-run with all roles already in role_map → 0 creates, 0 edits, 0 perm calls.

    Registers create/edit/perm endpoints with call-counting callbacks.
    Any call to these endpoints means the guard is missing.
    """
    # Two roles; role r1 has position=2 (would trigger attributes pass) and we
    # supply Discord metadata with hoist=True so both passes would definitely fire
    # if the guard were absent.
    role_a = DCERole(id="r1", name="Admin", position=2)
    role_b = DCERole(id="r2", name="Mod", position=1)

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={"r1": PermissionPair(allow=8, deny=0)},
        channel_metadata={},
        role_metadata={
            "r1": RoleMeta(hoist=True, position=2),
            "r2": RoleMeta(hoist=False, position=1),
        },
    )
    save_discord_metadata(meta, tmp_path)

    # Prior COMPLETED migration: both roles mapped AND finalized. Under S3, roles_finalized
    # (not role_map membership) is the signal that the attrs/perms passes already ran, so an
    # incremental run skips them. (A completed migration carries/seeds roles_finalized.)
    state = MigrationState(
        stoat_server_id="srv1",
        role_map={"r1": "stoat-r1", "r2": "stoat-r2"},
        roles_finalized={"r1", "r2"},
    )
    config = _make_config(tmp_path, incremental=True)
    exports = [_make_role_export([role_a, role_b])]

    create_calls: list[object] = []
    edit_calls: list[object] = []
    perm_calls: list[object] = []

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/roles",
            repeat=True,
            callback=lambda url, **kw: create_calls.append(url),  # type: ignore[misc]
        )
        m.patch(
            f"{STOAT_URL}/servers/srv1/roles/stoat-r1",
            repeat=True,
            callback=lambda url, **kw: edit_calls.append(url),  # type: ignore[misc]
        )
        m.patch(
            f"{STOAT_URL}/servers/srv1/roles/stoat-r2",
            repeat=True,
            callback=lambda url, **kw: edit_calls.append(url),  # type: ignore[misc]
        )
        m.put(
            f"{STOAT_URL}/servers/srv1/permissions/stoat-r1",
            repeat=True,
            callback=lambda url, **kw: perm_calls.append(url),  # type: ignore[misc]
        )
        m.put(
            f"{STOAT_URL}/servers/srv1/permissions/stoat-r2",
            repeat=True,
            callback=lambda url, **kw: perm_calls.append(url),  # type: ignore[misc]
        )

        await run_roles(config, state, exports, lambda e: None)

    assert create_calls == [], f"Expected 0 create calls, got {len(create_calls)}"
    assert edit_calls == [], f"Expected 0 attribute edit calls, got {len(edit_calls)}"
    assert perm_calls == [], f"Expected 0 permission calls, got {len(perm_calls)}"
    # Maps unchanged.
    assert state.role_map == {"r1": "stoat-r1", "r2": "stoat-r2"}


# ---------------------------------------------------------------------------
# SC-4: New role since prior run IS created (only the new one)
# ---------------------------------------------------------------------------


async def test_run_roles_creates_only_new_roles_on_incremental_rerun(
    tmp_path: Path,
) -> None:
    """SC-4: prior role_map has r1; export now also has r2 → only r2 created."""
    role_a = DCERole(id="r1", name="Admin")
    role_b = DCERole(id="r2", name="Mod")

    # Prior state: only r1 mapped.
    state = MigrationState(
        stoat_server_id="srv1",
        role_map={"r1": "stoat-r1"},
    )
    config = _make_config(tmp_path, incremental=True)
    exports = [_make_role_export([role_a, role_b])]

    create_calls: list[object] = []

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/roles",
            payload={"id": "stoat-r2", "name": "Mod"},
            callback=lambda url, **kw: create_calls.append(url),  # type: ignore[misc]
        )

        await run_roles(config, state, exports, lambda e: None)

    assert len(create_calls) == 1, f"Expected exactly 1 create call, got {len(create_calls)}"
    assert state.role_map["r2"] == "stoat-r2"
    assert state.role_map["r1"] == "stoat-r1"  # untouched


# ---------------------------------------------------------------------------
# SC-5: Fresh run still creates + attributes ALL roles (snapshot empty)
# ---------------------------------------------------------------------------


async def test_run_roles_fresh_run_creates_and_attributes_all_roles(
    tmp_path: Path,
) -> None:
    """SC-5: empty state → pre_existing_role_ids is empty → every role created AND
    api_edit_role (attributes) fires for every role carrying an attribute.

    Guards against the trap where a guard on the live role_map would wrongly
    skip attributes for roles created earlier in the same pass.

    Both roles carry hoist metadata. Before #380 role_a carried none and leaned on
    ``position != 0`` to trigger the pass through the ``rank`` field, which the Stoat
    backend discarded. With that field gone role_a would have had no attribute at all,
    and the ">= 2 edits" assertion would have passed vacuously at 1 while no longer
    guarding the skip it was written for.
    """
    # role_a: hoist=True via metadata → attributes pass fires
    # role_b: hoist=True via metadata → attributes pass fires
    role_a = DCERole(id="r1", name="Admin", position=2)
    role_b = DCERole(id="r2", name="Mod", position=1)

    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
        role_metadata={
            "r1": RoleMeta(hoist=True, position=2),
            "r2": RoleMeta(hoist=True, position=1),
        },
    )
    save_discord_metadata(meta, tmp_path)

    state = MigrationState(stoat_server_id="srv1")  # empty role_map
    config = _make_config(tmp_path)
    exports = [_make_role_export([role_a, role_b])]

    create_calls: list[object] = []
    edit_calls: list[object] = []

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/roles",
            payload={"id": "stoat-r1", "name": "Admin"},
            callback=lambda url, **kw: create_calls.append(url),  # type: ignore[misc]
        )
        m.post(
            f"{STOAT_URL}/servers/srv1/roles",
            payload={"id": "stoat-r2", "name": "Mod"},
            callback=lambda url, **kw: create_calls.append(url),  # type: ignore[misc]
        )
        m.patch(
            f"{STOAT_URL}/servers/srv1/roles/stoat-r1",
            payload={},
            repeat=True,
            callback=lambda url, **kw: edit_calls.append(url),  # type: ignore[misc]
        )
        m.patch(
            f"{STOAT_URL}/servers/srv1/roles/stoat-r2",
            payload={},
            repeat=True,
            callback=lambda url, **kw: edit_calls.append(url),  # type: ignore[misc]
        )
        m.put(f"{STOAT_URL}/servers/srv1/permissions/stoat-r1", payload={}, repeat=True)
        m.put(f"{STOAT_URL}/servers/srv1/permissions/stoat-r2", payload={}, repeat=True)

        await run_roles(config, state, exports, lambda e: None)

    assert len(create_calls) == 2, f"Expected 2 creates on fresh run, got {len(create_calls)}"
    # Both roles carry hoist, so both must be edited. Asserting on the set of URLs
    # rather than a count means a pass that edited one role twice cannot satisfy it.
    assert {str(u) for u in edit_calls} == {
        f"{STOAT_URL}/servers/srv1/roles/stoat-r1",
        f"{STOAT_URL}/servers/srv1/roles/stoat-r2",
    }, f"Expected an attribute edit for each role on a fresh run, got {edit_calls}"


# ===========================================================================
# Task 3: run_categories + run_channels map-aware idempotency
# ===========================================================================


def _make_forum_export(
    channel_id: str,
    channel_name: str,
    parent_channel_name: str,
    message_count: int = 0,
) -> DCEExport:
    """Build a forum-thread export (type 15) keyed under a parent forum."""
    return _make_export(
        channel_id=channel_id,
        channel_name=channel_name,
        channel_type=15,
        is_thread=True,
        parent_channel_name=parent_channel_name,
        category_id="cat1",
        category="General",
        message_count=message_count,
    )


# ---------------------------------------------------------------------------
# SC-1: Unchanged-export re-run creates zero structure
# ---------------------------------------------------------------------------


async def test_rerun_unchanged_export_creates_zero_structure(tmp_path: Path) -> None:
    """SC-1: re-running run_categories + run_channels with carried maps → 0 creates.

    No POST endpoints are registered; the only registered endpoint is the
    authoritative PATCH /servers/srv1. On the unfixed code, the create loop fires
    POST /servers/srv1/channels (and the categories phase PATCHes a transient
    array), so the test fails. After the fix, the maps are reused verbatim.
    """
    exports = [
        _make_export(
            channel_id="ch1", channel_name="general", category_id="cat1", category="General"
        ),
        _make_export(
            channel_id="ch2", channel_name="random", category_id="cat1", category="General"
        ),
    ]

    prior_category_map = {"cat1": "stoat-cat1"}
    prior_channel_map = {"ch1": "stoat-ch1", "ch2": "stoat-ch2"}
    state = MigrationState(
        stoat_server_id="srv1",
        category_map=dict(prior_category_map),
        channel_map=dict(prior_channel_map),
        category_names={"cat1": "General"},
    )
    config = _make_config(tmp_path, incremental=True)

    create_calls: list[object] = []

    with aioresponses() as m:
        # Create endpoints registered ONLY to count — any call means a regression.
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            repeat=True,
            callback=lambda url, **kw: create_calls.append(url),  # type: ignore[misc]
        )
        # The channels phase still issues an authoritative full-replace PATCH.
        m.patch(f"{STOAT_URL}/servers/srv1", payload={"_id": "srv1"}, repeat=True)

        await run_categories(config, state, exports, lambda e: None)
        await run_channels(config, state, exports, lambda e: None)

    assert create_calls == [], f"Expected 0 channel creates, got {len(create_calls)}"
    assert state.category_map == prior_category_map
    assert state.channel_map == prior_channel_map


# ---------------------------------------------------------------------------
# SC-2: Existing channels stay attached to their categories
# ---------------------------------------------------------------------------


async def test_rerun_keeps_existing_channels_attached_to_categories(tmp_path: Path) -> None:
    """SC-2: re-run upsert still lists carried channels under their category.

    Guards against a naive ``continue`` that would skip the category-membership
    recording for carried channels, orphaning them in the full-replace PATCH.
    """
    exports = [
        _make_export(
            channel_id="ch1", channel_name="general", category_id="cat1", category="General"
        ),
        _make_export(
            channel_id="ch2", channel_name="random", category_id="cat1", category="General"
        ),
    ]
    state = MigrationState(
        stoat_server_id="srv1",
        category_map={"cat1": "stoat-cat1"},
        channel_map={"ch1": "stoat-ch1", "ch2": "stoat-ch2"},
        category_names={"cat1": "General"},
    )
    config = _make_config(tmp_path, incremental=True)

    patch_bodies: list[dict[str, object]] = []

    with aioresponses() as m:
        m.post(f"{STOAT_URL}/servers/srv1/channels", repeat=True)
        m.patch(
            f"{STOAT_URL}/servers/srv1",
            payload={"_id": "srv1"},
            repeat=True,
            callback=lambda url, **kwargs: patch_bodies.append(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )

        await run_channels(config, state, exports, lambda e: None)

    # The authoritative upsert is the last PATCH body.
    assert patch_bodies, "Expected an api_upsert_categories PATCH"
    categories = patch_bodies[-1].get("categories", [])
    by_id = {c["id"]: c for c in categories}  # type: ignore[index,union-attr]
    # Every category present in the map must be present in the payload.
    for stoat_cat_id in state.category_map.values():
        assert stoat_cat_id in by_id, f"Category {stoat_cat_id} orphaned from upsert"
    # The carried channels must still be attached to cat1.
    cat1_channels = by_id["stoat-cat1"]["channels"]  # type: ignore[index]
    assert set(cat1_channels) == {"stoat-ch1", "stoat-ch2"}


# ---------------------------------------------------------------------------
# SC-4: New structure since prior run IS created (full delta)
# ---------------------------------------------------------------------------


async def test_rerun_creates_only_new_channel_and_category(tmp_path: Path) -> None:
    """SC-4: prior maps cover all-but-one channel + category; export adds new ones.

    Exactly the new channel is created (1 POST), the new category is generated,
    existing ones skipped, and the new channel is attached to its category.
    """
    exports = [
        _make_export(
            channel_id="ch1", channel_name="general", category_id="cat1", category="General"
        ),
        _make_export(
            channel_id="ch2", channel_name="announcements", category_id="cat2", category="News"
        ),
    ]
    # Prior: cat1/ch1 mapped; cat2 + ch2 are new this run.
    state = MigrationState(
        stoat_server_id="srv1",
        category_map={"cat1": "stoat-cat1"},
        channel_map={"ch1": "stoat-ch1"},
        category_names={"cat1": "General"},
    )
    config = _make_config(tmp_path, incremental=True)

    create_calls: list[object] = []
    patch_bodies: list[dict[str, object]] = []

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "stoat-ch2", "name": "announcements"},
            repeat=True,
            callback=lambda url, **kw: create_calls.append(url),  # type: ignore[misc]
        )
        m.patch(
            f"{STOAT_URL}/servers/srv1",
            payload={"_id": "srv1"},
            repeat=True,
            callback=lambda url, **kwargs: patch_bodies.append(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )

        await run_categories(config, state, exports, lambda e: None)
        await run_channels(config, state, exports, lambda e: None)

    # Exactly one channel created (ch2); ch1 reused.
    assert len(create_calls) == 1, f"Expected exactly 1 channel create, got {len(create_calls)}"
    assert state.channel_map["ch1"] == "stoat-ch1"
    assert state.channel_map["ch2"] == "stoat-ch2"
    # New category generated.
    assert "cat2" in state.category_map
    # New channel attached to its (new) category in the final upsert.
    categories = patch_bodies[-1].get("categories", [])
    by_id = {c["id"]: c for c in categories}  # type: ignore[index,union-attr]
    new_cat_id = state.category_map["cat2"]
    assert new_cat_id in by_id
    assert "stoat-ch2" in by_id[new_cat_id]["channels"]  # type: ignore[index]


# ---------------------------------------------------------------------------
# SC-6: Channels near --max-channels are not dropped on re-run
# ---------------------------------------------------------------------------


async def test_rerun_carried_channels_not_dropped_near_max(tmp_path: Path) -> None:
    """SC-6: 5 carried channels with max_channels=5 + 1 new → carried survive, new dropped.

    The truncation budget excludes carried channels:
    ``new_budget = max(0, max_channels - index_slots - len(carried))`` = 0 here,
    so the one new channel is dropped (with a warning) and all 5 carried pass
    through. On the unfixed code, the 6 candidates are sorted/sliced to 5 and a
    carried channel is dropped.
    """
    exports = []
    for i in range(5):
        exports.append(
            _make_export(channel_id=f"ch{i}", channel_name=f"channel-{i}", category_id="")
        )
    # One brand-new channel beyond the prior 5.
    exports.append(_make_export(channel_id="new1", channel_name="new-channel", category_id=""))

    prior_channel_map = {f"ch{i}": f"stoat-ch{i}" for i in range(5)}
    state = MigrationState(
        stoat_server_id="srv1",
        channel_map=dict(prior_channel_map),
    )
    config = _make_config(tmp_path, incremental=True, max_channels=5)

    create_calls: list[object] = []
    warnings_seen: list[str] = []

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            repeat=True,
            callback=lambda url, **kw: create_calls.append(url),  # type: ignore[misc]
        )
        m.patch(f"{STOAT_URL}/servers/srv1", payload={"_id": "srv1"}, repeat=True)

        await run_channels(
            config,
            state,
            exports,
            lambda e: warnings_seen.append(e.message) if e.status == "warning" else None,
        )

    # All 5 carried channels survive (no create, still in map).
    for i in range(5):
        assert f"ch{i}" in state.channel_map
        assert state.channel_map[f"ch{i}"] == f"stoat-ch{i}"
    # The new channel is dropped (budget 0) and not created.
    assert create_calls == [], f"Expected 0 creates (new channel over budget), got {create_calls}"
    assert "new1" not in state.channel_map
    assert any("new-channel" in w for w in warnings_seen), (
        "Expected a drop warning for the new channel"
    )


# ---------------------------------------------------------------------------
# SC-7: Partial re-export — carried-only category not dropped/orphaned
# ---------------------------------------------------------------------------


async def test_rerun_partial_export_keeps_carried_only_category(tmp_path: Path) -> None:
    """SC-7: category C is carried but absent from the (partial) current export.

    The full-replace upsert must still include C with its carried channels +
    a title, sourced from ``state.category_names`` (I-NEW-2). Otherwise C's
    channels are orphaned.
    """
    # Current export contains ONLY a new channel in a different category (cat2).
    exports = [
        _make_export(
            channel_id="new1", channel_name="newchan", category_id="cat2", category="Fresh"
        ),
    ]
    # Prior: category C (cat1) with two carried channels; cat2 not yet mapped.
    # channel_categories is what a real prior run persists — the upsert seeds
    # carried-channel membership from it (exact), not from a heuristic.
    state = MigrationState(
        stoat_server_id="srv1",
        category_map={"cat1": "stoat-cat1"},
        channel_map={"ch1": "stoat-ch1", "ch2": "stoat-ch2"},
        category_names={"cat1": "General"},
        channel_categories={"ch1": "cat1", "ch2": "cat1"},
    )
    config = _make_config(tmp_path, incremental=True)

    patch_bodies: list[dict[str, object]] = []

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "stoat-new1", "name": "newchan"},
            repeat=True,
        )
        m.patch(
            f"{STOAT_URL}/servers/srv1",
            payload={"_id": "srv1"},
            repeat=True,
            callback=lambda url, **kwargs: patch_bodies.append(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )

        await run_categories(config, state, exports, lambda e: None)
        await run_channels(config, state, exports, lambda e: None)

    categories = patch_bodies[-1].get("categories", [])
    by_id = {c["id"]: c for c in categories}  # type: ignore[index,union-attr]
    # Carried-only category C must still be present, titled, with its channels.
    assert "stoat-cat1" in by_id, "Carried-only category orphaned from upsert"
    assert by_id["stoat-cat1"]["title"] == "General"  # type: ignore[index]
    assert set(by_id["stoat-cat1"]["channels"]) == {"stoat-ch1", "stoat-ch2"}  # type: ignore[index]


# ---------------------------------------------------------------------------
# SC-9: 2nd run over a forum export does NOT re-create the forum-index channel
# ---------------------------------------------------------------------------


async def test_rerun_forum_index_channel_not_recreated(tmp_path: Path) -> None:
    """SC-9: forum-index channel is reused (no create, no pin, no index message).

    Prior state carries the forum category, the forum post channel, the
    forum-index channel, and forum_channel_members. On re-run, run_channels must
    NOT POST a new index channel nor send/pin a new index message; it reuses the
    carried index channel and re-attaches it at position 0.
    """
    exports = [
        _make_forum_export(
            channel_id="fp1",
            channel_name="first-post",
            parent_channel_name="my-forum",
            message_count=42,
        ),
    ]
    forum_key = "forum-my-forum"
    index_key = f"forum-index-{forum_key}"
    state = MigrationState(
        stoat_server_id="srv1",
        category_map={forum_key: "stoat-forumcat"},
        channel_map={"fp1": "stoat-fp1", index_key: "stoat-idx1"},
        forum_category_names={forum_key: "my-forum"},
        forum_channel_members={forum_key: ["fp1"]},
    )
    config = _make_config(tmp_path, incremental=True)

    create_calls: list[object] = []
    send_calls: list[object] = []
    pin_calls: list[object] = []
    patch_bodies: list[dict[str, object]] = []

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            repeat=True,
            callback=lambda url, **kw: create_calls.append(url),  # type: ignore[misc]
        )
        # Index-message send + pin would only fire if the index were re-created.
        m.post(
            f"{STOAT_URL}/channels/stoat-idx1/messages",
            repeat=True,
            callback=lambda url, **kw: send_calls.append(url),  # type: ignore[misc]
        )
        m.put(
            f"{STOAT_URL}/channels/stoat-idx1/messages/idx-msg1/pin",
            repeat=True,
            callback=lambda url, **kw: pin_calls.append(url),  # type: ignore[misc]
        )
        m.patch(
            f"{STOAT_URL}/servers/srv1",
            payload={"_id": "srv1"},
            repeat=True,
            callback=lambda url, **kwargs: patch_bodies.append(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )

        await run_channels(config, state, exports, lambda e: None)

    assert create_calls == [], f"Expected 0 channel creates on forum re-run, got {create_calls}"
    assert send_calls == [], "Expected 0 forum-index messages on re-run"
    assert pin_calls == [], "Expected 0 forum-index pins on re-run"
    # forum_channel_members must NOT double-append fp1.
    assert state.forum_channel_members[forum_key] == ["fp1"]
    # The carried index channel is re-attached at position 0 of the forum category.
    categories = patch_bodies[-1].get("categories", [])
    by_id = {c["id"]: c for c in categories}  # type: ignore[index,union-attr]
    forum_channels = by_id["stoat-forumcat"]["channels"]  # type: ignore[index]
    assert forum_channels[0] == "stoat-idx1", "Carried index channel not re-attached at top"
    assert "stoat-fp1" in forum_channels


# ---------------------------------------------------------------------------
# SC-7b: Two simultaneously carried-only categories — exact membership
# ---------------------------------------------------------------------------


async def test_rerun_two_carried_only_categories_keep_own_channels(tmp_path: Path) -> None:
    """SC-7b: two carried-only categories each retain ONLY their own channels.

    Prior state has cat A with {a1, a2} and cat B with {b1, b2}, all carried,
    plus a persisted ``channel_categories`` mapping every channel to its category.
    The current (partial) export contains only a new channel in a THIRD category.

    The authoritative upsert must place a1,a2 under A and b1,b2 under B — no
    cross-contamination and no duplication. This FAILS against Task 3's
    best-effort heuristic (which extends every unplaced carried channel onto
    every carried-only category) and PASSES with exact channel_categories seeding.
    """
    # Current export: only a new channel in a third category (catC).
    exports = [
        _make_export(
            channel_id="new1", channel_name="newchan", category_id="catC", category="Fresh"
        ),
    ]
    state = MigrationState(
        stoat_server_id="srv1",
        category_map={"catA": "stoat-catA", "catB": "stoat-catB"},
        channel_map={
            "a1": "stoat-a1",
            "a2": "stoat-a2",
            "b1": "stoat-b1",
            "b2": "stoat-b2",
        },
        category_names={"catA": "Alpha", "catB": "Bravo"},
        channel_categories={
            "a1": "catA",
            "a2": "catA",
            "b1": "catB",
            "b2": "catB",
        },
    )
    config = _make_config(tmp_path, incremental=True, upload_delay=0)

    patch_bodies: list[dict[str, object]] = []

    with aioresponses() as m:
        m.post(
            f"{STOAT_URL}/servers/srv1/channels",
            payload={"_id": "stoat-new1", "name": "newchan"},
            repeat=True,
        )
        m.patch(
            f"{STOAT_URL}/servers/srv1",
            payload={"_id": "srv1"},
            repeat=True,
            callback=lambda url, **kwargs: patch_bodies.append(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )

        await run_categories(config, state, exports, lambda e: None)
        await run_channels(config, state, exports, lambda e: None)

    categories = patch_bodies[-1].get("categories", [])
    by_id = {c["id"]: c for c in categories}  # type: ignore[index,union-attr]
    # Each carried-only category contains ONLY its own channels — no cross-talk.
    assert set(by_id["stoat-catA"]["channels"]) == {"stoat-a1", "stoat-a2"}  # type: ignore[index]
    assert set(by_id["stoat-catB"]["channels"]) == {"stoat-b1", "stoat-b2"}  # type: ignore[index]
    # No duplication within either category.
    assert len(by_id["stoat-catA"]["channels"]) == 2  # type: ignore[index]
    assert len(by_id["stoat-catB"]["channels"]) == 2  # type: ignore[index]


# ---------------------------------------------------------------------------
# SC-9 (rebuild): _rebuild_forum_indexes PATCHes the carried index message
# ---------------------------------------------------------------------------


async def test_rebuild_forum_indexes_patches_on_rerun(tmp_path: Path) -> None:
    """SC-9: with a carried forum_index_message_ids, the rebuild edits (PATCHes).

    When ``forum_index_message_ids`` is carried from the prior run, the REPORT
    phase's ``_rebuild_forum_indexes`` must call ``api_edit_message`` (PATCH on
    /channels/{id}/messages/{msg}) exactly once per forum and must NOT POST a new
    message. On main this map is reset, so the rebuild re-POSTs a duplicate.
    """
    forum_key = "forum-my-forum"
    state = MigrationState(
        stoat_server_id="srv1",
        channel_map={"fp1": "stoat-fp1", f"forum-index-{forum_key}": "stoat-idx1"},
        forum_category_names={forum_key: "my-forum"},
        forum_channel_members={forum_key: ["fp1"]},
        forum_index_message_ids={forum_key: "idx-msg-1"},
        channel_message_counts={"fp1": 5},
    )
    config = _make_config(tmp_path, incremental=True, upload_delay=0)

    patch_calls: list[object] = []
    post_calls: list[object] = []

    with aioresponses() as m:
        m.patch(
            f"{STOAT_URL}/channels/stoat-idx1/messages/idx-msg-1",
            payload={"_id": "idx-msg-1"},
            repeat=True,
            callback=lambda url, **kw: patch_calls.append(url),  # type: ignore[misc]
        )
        # A POST would only fire if the carried message id were ignored (the bug).
        m.post(
            f"{STOAT_URL}/channels/stoat-idx1/messages",
            payload={"_id": "new-msg"},
            repeat=True,
            callback=lambda url, **kw: post_calls.append(url),  # type: ignore[misc]
        )

        await _rebuild_forum_indexes(config, state, lambda e: None)

    assert len(patch_calls) == 1, f"Expected exactly 1 PATCH (edit), got {len(patch_calls)}"
    assert post_calls == [], "Expected 0 new index-message POSTs on re-run"


# ---------------------------------------------------------------------------
# SC-10: Rebuilt forum index reports CUMULATIVE counts (prior + new)
# ---------------------------------------------------------------------------


async def test_rebuild_forum_indexes_reports_cumulative_counts(tmp_path: Path) -> None:
    """SC-10: carried count (800) + this-run delta (47) → index shows 847.

    Mirrors the 2nd-run flow: the incremental carry-over seeds
    ``channel_message_counts={'fp1': 800}``; the MESSAGES phase then accumulates
    47 new messages via ``+=`` (simulated through ``_merge_channel_result``);
    the rebuild's PATCH body must report 847, not the delta 47. On main the count
    is not carried, so the body would show 47.
    """
    forum_key = "forum-my-forum"
    state = MigrationState(
        stoat_server_id="srv1",
        channel_map={"fp1": "stoat-fp1", f"forum-index-{forum_key}": "stoat-idx1"},
        forum_category_names={forum_key: "my-forum"},
        forum_channel_members={forum_key: ["fp1"]},
        forum_index_message_ids={forum_key: "idx-msg-1"},
        # Carried cumulative count from the prior run.
        channel_message_counts={"fp1": 800},
    )
    # This run's MESSAGES phase migrates 47 new messages; accumulates via +=.
    _merge_channel_result(state, ChannelResult(channel_id="fp1", messages_migrated=47))
    assert state.channel_message_counts["fp1"] == 847  # sanity: carry + delta

    config = _make_config(tmp_path, incremental=True, upload_delay=0)

    patch_bodies: list[dict[str, object]] = []

    with aioresponses() as m:
        m.patch(
            f"{STOAT_URL}/channels/stoat-idx1/messages/idx-msg-1",
            payload={"_id": "idx-msg-1"},
            repeat=True,
            callback=lambda url, **kwargs: patch_bodies.append(  # type: ignore[misc]
                kwargs.get("json", {})
            ),
        )

        await _rebuild_forum_indexes(config, state, lambda e: None)

    assert patch_bodies, "Expected an api_edit_message PATCH"
    content = patch_bodies[-1].get("content", "")
    assert isinstance(content, str)
    # The fp1 line must report the cumulative 847, not the delta 47. Match the
    # full per-channel line so "847" is not mistaken for a substring of "47".
    assert "<#stoat-fp1> — 847 messages migrated" in content, (
        f"Expected cumulative 847 for fp1, got: {content!r}"
    )
    assert "<#stoat-fp1> — 47 messages migrated" not in content, (
        "Index showed delta count, not cumulative"
    )


# ---------------------------------------------------------------------------
# Batch 7 S2 — non-destructive incremental category upsert
# ---------------------------------------------------------------------------


async def test_incremental_run_categories_skips_early_upsert(tmp_path: Path) -> None:
    """SC-14: incremental run_categories does NOT fire the destructive early upsert.

    This also closes the critique M2 concern: because run_categories never PATCHes in
    incremental mode, there is no destructive intermediate state for a crash/cancel to
    land in (carried-only categories stay live until run_channels' authoritative upsert),
    even in the zero-channel edge where run_channels would skip its own upsert.
    """
    exports = [
        _make_export(
            channel_id="chNew", channel_name="new", category_id="catNew", category="NewCat"
        ),
    ]
    state = MigrationState(
        stoat_server_id="srv1",
        category_map={"catOld": "stoat-catOld"},  # carried-only, absent from this export
        category_names={"catOld": "OldCat"},
    )
    config = _make_config(tmp_path, incremental=True)
    patch_bodies: list[dict[str, object]] = []
    with aioresponses() as m:
        m.patch(
            f"{STOAT_URL}/servers/srv1",
            payload={"_id": "srv1"},
            repeat=True,
            callback=lambda url, **kwargs: patch_bodies.append(kwargs.get("json", {})),  # type: ignore[misc]
        )
        await run_categories(config, state, exports, lambda e: None)
    assert patch_bodies == []  # no destructive early upsert in incremental mode
    assert "catNew" in state.category_map  # map population still happens


async def test_fresh_run_categories_fires_early_upsert(tmp_path: Path) -> None:
    """SC-15: a fresh (non-incremental) run still fires run_categories' early upsert."""
    exports = [
        _make_export(
            channel_id="chNew", channel_name="new", category_id="catNew", category="NewCat"
        ),
    ]
    state = MigrationState(stoat_server_id="srv1")
    config = _make_config(tmp_path, incremental=False)
    patch_bodies: list[dict[str, object]] = []
    with aioresponses() as m:
        m.patch(
            f"{STOAT_URL}/servers/srv1",
            payload={"_id": "srv1"},
            repeat=True,
            callback=lambda url, **kwargs: patch_bodies.append(kwargs.get("json", {})),  # type: ignore[misc]
        )
        await run_categories(config, state, exports, lambda e: None)
    assert patch_bodies, "fresh run must fire the early upsert (non-regression)"


async def test_incremental_final_upsert_includes_carried_and_new(tmp_path: Path) -> None:
    """SC-16: after run_channels, the authoritative upsert holds carried + new categories."""
    exports = [
        _make_export(
            channel_id="chNew", channel_name="new", category_id="catNew", category="NewCat"
        ),
    ]
    state = MigrationState(
        stoat_server_id="srv1",
        category_map={"catOld": "stoat-catOld"},  # carried-only
        channel_map={"chOld": "stoat-chOld"},
        category_names={"catOld": "OldCat"},
        channel_categories={"chOld": "catOld"},
    )
    config = _make_config(tmp_path, incremental=True)
    patch_bodies: list[dict[str, object]] = []
    with aioresponses() as m:
        m.post(f"{STOAT_URL}/servers/srv1/channels", payload={"_id": "stoat-chNew"}, repeat=True)
        m.patch(
            f"{STOAT_URL}/servers/srv1",
            payload={"_id": "srv1"},
            repeat=True,
            callback=lambda url, **kwargs: patch_bodies.append(kwargs.get("json", {})),  # type: ignore[misc]
        )
        await run_categories(config, state, exports, lambda e: None)
        await run_channels(config, state, exports, lambda e: None)
    assert patch_bodies, "run_channels must own the authoritative upsert"
    categories = patch_bodies[-1].get("categories", [])
    by_id = {c["id"]: c for c in categories}  # type: ignore[index,union-attr]
    assert "stoat-catOld" in by_id  # carried-only category survives
    assert state.category_map["catNew"] in by_id  # new category materialized
    assert "stoat-chOld" in by_id["stoat-catOld"]["channels"]  # carried channel re-attached


async def test_incremental_multiple_carried_only_categories_survive(tmp_path: Path) -> None:
    """SC-18: multiple simultaneously carried-only categories all survive (no contamination)."""
    exports = [
        _make_export(
            channel_id="chNew", channel_name="new", category_id="catNew", category="NewCat"
        ),
    ]
    state = MigrationState(
        stoat_server_id="srv1",
        category_map={"catA": "stoat-catA", "catB": "stoat-catB"},  # two carried-only
        channel_map={"chA": "stoat-chA", "chB": "stoat-chB"},
        category_names={"catA": "Alpha", "catB": "Beta"},
        channel_categories={"chA": "catA", "chB": "catB"},
    )
    config = _make_config(tmp_path, incremental=True)
    patch_bodies: list[dict[str, object]] = []
    with aioresponses() as m:
        m.post(f"{STOAT_URL}/servers/srv1/channels", payload={"_id": "stoat-chNew"}, repeat=True)
        m.patch(
            f"{STOAT_URL}/servers/srv1",
            payload={"_id": "srv1"},
            repeat=True,
            callback=lambda url, **kwargs: patch_bodies.append(kwargs.get("json", {})),  # type: ignore[misc]
        )
        await run_categories(config, state, exports, lambda e: None)
        await run_channels(config, state, exports, lambda e: None)
    categories = patch_bodies[-1].get("categories", [])
    by_id = {c["id"]: c for c in categories}  # type: ignore[index,union-attr]
    assert "stoat-catA" in by_id and "stoat-catB" in by_id
    assert by_id["stoat-catA"]["channels"] == ["stoat-chA"]
    assert by_id["stoat-catB"]["channels"] == ["stoat-chB"]  # no cross-contamination


async def test_incremental_gate_does_not_touch_dry_run(tmp_path: Path) -> None:
    """SC-19: dry-run still maps categories and makes no HTTP (gate is after the return)."""
    exports = [
        _make_export(
            channel_id="chNew", channel_name="new", category_id="catNew", category="NewCat"
        ),
    ]
    state = MigrationState(stoat_server_id="srv1")
    config = _make_config(tmp_path, incremental=True, dry_run=True)
    with aioresponses():  # any HTTP would raise (none registered)
        await run_categories(config, state, exports, lambda e: None)
    assert state.category_map["catNew"] == "dry-cat-catNew"


# ---------------------------------------------------------------------------
# SC-I1: the defect end to end (#107 batch 7, chunk #197, task #209)
# ---------------------------------------------------------------------------


async def test_a_duplicate_does_not_become_a_real_duplicate_next_run(tmp_path: Path) -> None:
    """SC-I1: the whole chain, and the only test that reaches the user-visible harm.

    Run 1: a send's response is lost, the retry hits the still-cached Idempotency-Key,
    and Stoat answers 409 DuplicateNonce. The message IS on the server.

    Run 2 with --incremental: the self-heal set is built from state.failed_messages
    (messages.py:914-919). If run 1 recorded the message as failed, run 2 re-sends it.
    By then the 1000-entry LRU has evicted the key, so the re-send SUCCEEDS and the
    user's channel holds the message twice.

    Every other test in this batch checks one link. This one checks that the chain
    cannot close.
    """
    msg = DCEMessage(
        id="1506019505778987190",
        type="Default",
        timestamp="2024-01-15T12:00:00+00:00",
        content="hello",
        author=DCEAuthor(id="u1", name="Alice"),
        is_pinned=False,
        attachments=[],
        embeds=[],
        stickers=[],
        reactions=[],
        reference=None,
    )
    exports = [_make_export(channel_id="222", messages=[msg], message_count=1)]

    state = MigrationState(channel_map={"222": "stoat-222"}, autumn_url="https://autumn.test")

    async def always_duplicate(*a: object, **k: object) -> dict[str, object]:
        raise DuplicateSendError("already on the server")

    config1 = _make_config(tmp_path, message_rate_limit=0.0, upload_delay=0.0)
    with patch("discord_ferry.migrator.messages.api_send_message", always_duplicate):
        await run_messages(config1, state, exports, lambda e: None)

    # The mechanism. Everything below depends on this being empty.
    assert not state.failed_messages, (
        "run 1 recorded a landed message as failed, which is what run 2 re-sends"
    )

    sent_second_run: list[str] = []

    async def capture(*a: object, **k: object) -> dict[str, object]:
        sent_second_run.append(str(k.get("idempotency_key", "")))
        return {"_id": "stoat-msg1"}

    config2 = _make_config(tmp_path, incremental=True, message_rate_limit=0.0, upload_delay=0.0)
    with patch("discord_ferry.migrator.messages.api_send_message", capture):
        await run_messages(config2, state, exports, lambda e: None)

    assert not any("1506019505778987190" in key for key in sent_second_run), (
        "the incremental run re-sent a message that was already on the server, "
        f"creating a real duplicate in the user's channel. sent: {sent_second_run}"
    )
