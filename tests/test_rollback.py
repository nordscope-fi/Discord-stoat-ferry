"""Tests for the rollback engine (issue #10).

Covers SC-1, SC-5, SC-8, SC-11–SC-28 from
``docs/plans/test-scenarios/2026-05-13-rollback-engine.md``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import aiohttp
import pytest
from aioresponses import aioresponses

from discord_ferry.config import FerryConfig
from discord_ferry.core.engine import (
    _build_rollback_targets,
    _clean_categories,
    _parse_http_status,
    _validate_rollback_inputs,
    run_rollback,
)
from discord_ferry.errors import MigrationError
from discord_ferry.migrator.api import (
    _reset_circuit_state,
    _reset_rate_state,
)
from discord_ferry.state import (
    MigrationState,
    RollbackProgress,
    load_state,
    save_state,
)

if TYPE_CHECKING:
    from discord_ferry.core.events import MigrationEvent
    from discord_ferry.review import RollbackSummary

BASE_URL = "https://api.test"
TOKEN = "test-session-token"
SERVER_ID = "srv01"
FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_circuit() -> None:  # type: ignore[misc]
    """Reset circuit breaker + rate state + semaphore between tests."""
    import discord_ferry.migrator.api as _api_mod

    _reset_circuit_state()
    _reset_rate_state()
    _api_mod._request_semaphore = None
    yield  # type: ignore[misc]
    _reset_circuit_state()
    _reset_rate_state()
    _api_mod._request_semaphore = None


@pytest.fixture
def mock_aiohttp() -> aioresponses:
    with aioresponses() as m:
        yield m


def _make_state_from_fixture(tmp_path: Path) -> MigrationState:
    """Copy the rollback_state.json fixture into ``tmp_path`` and load it."""
    fixture = FIXTURES_DIR / "rollback_state.json"
    (tmp_path / "state.json").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "message_map.json").write_text("{}", encoding="utf-8")
    return load_state(tmp_path)


def _make_config(tmp_path: Path, **overrides: object) -> FerryConfig:
    defaults: dict[str, object] = {
        "export_dir": FIXTURES_DIR,
        "stoat_url": BASE_URL,
        "token": TOKEN,
        "output_dir": tmp_path,
        "skip_export": True,
        "max_concurrent_requests": 5,
        "force_unlock": True,  # don't deal with the lock-marker handling in most tests
    }
    defaults.update(overrides)
    return FerryConfig(**defaults)  # type: ignore[arg-type]


def _mock_lock_acquire_release(
    m: aioresponses,
    server_id: str = SERVER_ID,
    *,
    initial_description: str = "",
    repeat: bool = False,
) -> None:
    """Wire api_fetch_server + api_edit_server (PATCH) for lock acquire/release.

    The lock helpers call:
      - api_fetch_server (GET /servers/<id>)  to inspect description
      - api_edit_server (PATCH /servers/<id>) to write the new description
      - run_rollback then calls api_fetch_server again to build the summary
      - _release_migration_lock calls fetch + patch one more time

    Default: server has empty description, no live lock.
    """
    # repeat=True lets the same mock URL be used many times.
    m.get(
        f"{BASE_URL}/servers/{server_id}",
        payload={
            "_id": server_id,
            "name": "Test Server",
            "description": initial_description,
            "channels": [],
            "roles": {},
        },
        repeat=True,
    )
    m.patch(
        f"{BASE_URL}/servers/{server_id}",
        payload={"_id": server_id, "description": ""},
        repeat=True,
    )


def _emit_collector() -> tuple[list[MigrationEvent], Any]:
    events: list[MigrationEvent] = []
    return events, events.append


# ---------------------------------------------------------------------------
# _parse_http_status
# ---------------------------------------------------------------------------


def test_parse_http_status_api_error() -> None:
    assert _parse_http_status("API error 404: NotFound") == 404
    assert _parse_http_status("API error 403: forbidden") == 403


def test_parse_http_status_retry_exhausted() -> None:
    assert _parse_http_status("API request failed after 3 retries: 503 boom") == 503


def test_parse_http_status_network_error_returns_none() -> None:
    assert _parse_http_status("Network error after 3 retries: ConnectionReset") is None


# ---------------------------------------------------------------------------
# _validate_rollback_inputs (SC-14)
# ---------------------------------------------------------------------------


def test_validate_empty_server_id_raises(tmp_path: Path) -> None:
    """SC-14: both empty → MigrationError."""
    state = MigrationState()  # empty stoat_server_id
    config = _make_config(tmp_path, server_id=None)
    with pytest.raises(MigrationError, match="no Stoat server ID"):
        _validate_rollback_inputs(state, config)


def test_validate_uses_state_id(tmp_path: Path) -> None:
    state = MigrationState(stoat_server_id="srv99")
    config = _make_config(tmp_path, server_id=None)
    assert _validate_rollback_inputs(state, config) == "srv99"


def test_validate_falls_back_to_config_id(tmp_path: Path) -> None:
    state = MigrationState()  # empty stoat_server_id
    config = _make_config(tmp_path, server_id="srv_from_config")
    assert _validate_rollback_inputs(state, config) == "srv_from_config"


# ---------------------------------------------------------------------------
# _build_rollback_targets
# ---------------------------------------------------------------------------


def test_build_rollback_targets_finds_suspects() -> None:
    """SC-9: server channels not in channel_map become suspects."""
    state = MigrationState(channel_map={"d1": "s1", "d2": "s2"})
    server = {"channels": ["s1", "s2", "s3", "s4"], "roles": {}}
    suspects = _build_rollback_targets(state, server)
    assert sorted(s.stoat_id for s in suspects) == ["s3", "s4"]
    # opted_in defaults False.
    assert all(not s.opted_in for s in suspects)


def test_build_rollback_targets_empty_when_no_orphans() -> None:
    state = MigrationState(channel_map={"d1": "s1", "d2": "s2"})
    server = {"channels": ["s1", "s2"], "roles": {}}
    assert _build_rollback_targets(state, server) == []


def test_build_rollback_targets_ulid_decoded() -> None:
    """Suspects with ULID-shaped IDs get decoded created_at_iso strings."""
    state = MigrationState(channel_map={})
    server = {"channels": ["01KPTJT1G00123456789ABCDEF"], "roles": {}}
    suspects = _build_rollback_targets(state, server)
    assert len(suspects) == 1
    assert suspects[0].created_at_iso == "2026-04-22T12:32:00+00:00"


# ---------------------------------------------------------------------------
# SC-1: Full happy-path rollback
# ---------------------------------------------------------------------------


async def test_run_rollback_full_happy_path(mock_aiohttp: aioresponses, tmp_path: Path) -> None:
    """SC-1: deletes all entities, populates progress counters, no failures."""
    state = _make_state_from_fixture(tmp_path)
    save_state(state, tmp_path)
    config = _make_config(tmp_path)

    # Lock acquire + release mocks (fetch + patch repeatable).
    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SERVER_ID}",
        payload={
            "_id": SERVER_ID,
            "name": "Test",
            "description": "",
            "channels": ["ch01", "ch02", "ch03", "ch04", "ch05"],
            "roles": {
                "role01": {"name": "Mod"},
                "role02": {"name": "User"},
                "role03": {"name": "Guest"},
            },
            "categories": [
                {"id": "cat_uuid_1", "title": "Gaming", "channels": []},
                {"id": "cat_uuid_2", "title": "Music", "channels": []},
            ],
        },
        repeat=True,
    )
    mock_aiohttp.patch(f"{BASE_URL}/servers/{SERVER_ID}", payload={}, repeat=True)

    for cid in ["ch01", "ch02", "ch03", "ch04", "ch05"]:
        mock_aiohttp.delete(f"{BASE_URL}/channels/{cid}", status=204)
    for rid in ["role01", "role02", "role03"]:
        mock_aiohttp.delete(f"{BASE_URL}/servers/{SERVER_ID}/roles/{rid}", status=204)
    for eid in ["em01", "em02"]:
        mock_aiohttp.delete(f"{BASE_URL}/custom/emoji/{eid}", status=204)

    events, emit = _emit_collector()
    result = await run_rollback(config, state, exports=[], on_event=emit)

    rp = result.rollback_progress
    assert rp is not None
    assert rp.channels_deleted == 5
    assert rp.roles_deleted == 3
    assert rp.emoji_deleted == 2
    assert rp.categories_cleaned is True
    assert rp.failures == []
    assert rp.rolled_back_ids == {
        "ch01",
        "ch02",
        "ch03",
        "ch04",
        "ch05",
        "role01",
        "role02",
        "role03",
        "em01",
        "em02",
    }
    # Forensic preservation — maps unchanged.
    assert result.channel_map == {
        "d_ch_1": "ch01",
        "d_ch_2": "ch02",
        "d_ch_3": "ch03",
        "d_ch_4": "ch04",
        "d_ch_5": "ch05",
    }
    assert result.role_map == {
        "d_role_1": "role01",
        "d_role_2": "role02",
        "d_role_3": "role03",
    }

    statuses = [e.status for e in events]
    assert "started" in statuses
    assert "confirm_rollback" in statuses
    assert any(s == "completed" for s in statuses)


# ---------------------------------------------------------------------------
# SC-5: Idempotent re-run (all 404s)
# ---------------------------------------------------------------------------


async def test_run_rollback_all_404s_idempotent(mock_aiohttp: aioresponses, tmp_path: Path) -> None:
    """SC-5: all DELETEs return 404 — treated as success, no warnings."""
    state = _make_state_from_fixture(tmp_path)
    save_state(state, tmp_path)
    config = _make_config(tmp_path)

    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SERVER_ID}",
        payload={
            "_id": SERVER_ID,
            "name": "T",
            "description": "",
            "channels": ["ch01", "ch02", "ch03", "ch04", "ch05"],
            "roles": {"role01": {}, "role02": {}, "role03": {}},
            "categories": [],
        },
        repeat=True,
    )
    mock_aiohttp.patch(f"{BASE_URL}/servers/{SERVER_ID}", payload={}, repeat=True)

    for cid in ["ch01", "ch02", "ch03", "ch04", "ch05"]:
        mock_aiohttp.delete(f"{BASE_URL}/channels/{cid}", status=404, payload={"type": "NotFound"})
    for rid in ["role01", "role02", "role03"]:
        mock_aiohttp.delete(
            f"{BASE_URL}/servers/{SERVER_ID}/roles/{rid}",
            status=404,
            payload={"type": "NotFound"},
        )
    for eid in ["em01", "em02"]:
        mock_aiohttp.delete(
            f"{BASE_URL}/custom/emoji/{eid}", status=404, payload={"type": "NotFound"}
        )

    events, emit = _emit_collector()
    result = await run_rollback(config, state, exports=[], on_event=emit)

    rp = result.rollback_progress
    assert rp is not None
    assert rp.channels_deleted == 5
    assert rp.roles_deleted == 3
    assert rp.emoji_deleted == 2
    assert rp.failures == []
    # Forensic preservation
    assert result.channel_map["d_ch_1"] == "ch01"
    # No warning events emitted (idempotent re-rolls shouldn't flood).
    warn_for_already_deleted = [
        e for e in events if e.status == "warning" and "Already deleted" in (e.message or "")
    ]
    assert warn_for_already_deleted == []


# ---------------------------------------------------------------------------
# SC-8: Shared semaphore concurrency cap
# ---------------------------------------------------------------------------


async def test_shared_semaphore_caps_concurrency(
    mock_aiohttp: aioresponses, tmp_path: Path
) -> None:
    """SC-8: max_concurrent_requests=5 → peak in-flight ≤ 5 across 20 channels."""
    state = MigrationState(
        stoat_server_id=SERVER_ID,
        channel_map={f"d{i}": f"ch{i:02d}" for i in range(20)},
    )
    save_state(state, tmp_path)
    config = _make_config(tmp_path, max_concurrent_requests=5)

    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SERVER_ID}",
        payload={
            "_id": SERVER_ID,
            "name": "T",
            "description": "",
            "channels": [f"ch{i:02d}" for i in range(20)],
            "roles": {},
            "categories": [],
        },
        repeat=True,
    )
    mock_aiohttp.patch(f"{BASE_URL}/servers/{SERVER_ID}", payload={}, repeat=True)

    # We need to observe concurrency. Use a wrapper around api_delete_channel
    # via patch, since aioresponses doesn't expose per-request timing controls.
    inflight = 0
    peak = 0
    lock = asyncio.Lock()

    async def slow_delete(*args: Any, **kwargs: Any) -> None:
        nonlocal inflight, peak
        async with lock:
            inflight += 1
            peak = max(peak, inflight)
        try:
            await asyncio.sleep(0.05)
        finally:
            async with lock:
                inflight -= 1

    events, emit = _emit_collector()
    with patch("discord_ferry.core.engine.api_delete_channel", new=slow_delete):
        await run_rollback(config, state, exports=[], on_event=emit)

    assert peak <= 5, f"peak inflight = {peak}, expected ≤ 5"


# ---------------------------------------------------------------------------
# SC-11: Opt-in to one untracked-suspect channel
# ---------------------------------------------------------------------------


async def test_opted_in_suspect_channel_deleted(mock_aiohttp: aioresponses, tmp_path: Path) -> None:
    """SC-11: opted-in suspect is deleted; opted-out suspect is not."""
    state = MigrationState(
        stoat_server_id=SERVER_ID,
        channel_map={"d1": "s1", "d2": "s2"},
    )
    save_state(state, tmp_path)
    config = _make_config(tmp_path)

    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SERVER_ID}",
        payload={
            "_id": SERVER_ID,
            "name": "T",
            "description": "",
            "channels": ["s1", "s2", "s3", "s4"],
            "roles": {},
            "categories": [],
        },
        repeat=True,
    )
    mock_aiohttp.patch(f"{BASE_URL}/servers/{SERVER_ID}", payload={}, repeat=True)

    mock_aiohttp.delete(f"{BASE_URL}/channels/s1", status=204)
    mock_aiohttp.delete(f"{BASE_URL}/channels/s2", status=204)
    mock_aiohttp.delete(f"{BASE_URL}/channels/s3", status=204)
    # s4 is opted-out — NOT mocked. If a DELETE is attempted, the test fails.

    def emit_with_optin(event: MigrationEvent) -> None:
        if event.status == "confirm_rollback":
            summary: RollbackSummary = event.detail["summary"]  # type: ignore[index, assignment]
            # Opt in to s3 only.
            for suspect in summary.untracked_ferry_suspect:
                if suspect.stoat_id == "s3":
                    suspect.opted_in = True

    await run_rollback(config, state, exports=[], on_event=emit_with_optin)

    rp = state.rollback_progress
    assert rp is not None
    assert rp.channels_deleted == 2  # s1, s2
    assert rp.untracked_channels_deleted == 1  # s3
    assert "s1" in rp.rolled_back_ids
    assert "s2" in rp.rolled_back_ids
    assert "s3" in rp.rolled_back_ids
    assert "s4" not in rp.rolled_back_ids


# ---------------------------------------------------------------------------
# SC-12: Category cleanup filters Ferry IDs only
# ---------------------------------------------------------------------------


async def test_clean_categories_preserves_user_categories(
    mock_aiohttp: aioresponses, tmp_path: Path
) -> None:
    """SC-12: PATCH body excludes Ferry IDs, preserves user-owned categories."""
    state = MigrationState(
        stoat_server_id=SERVER_ID,
        category_map={"d_cat1": "cat_uuid_1", "d_cat2": "cat_uuid_2"},
        rollback_progress=RollbackProgress(),
    )
    save_state(state, tmp_path)
    config = _make_config(tmp_path)

    user_cat = {"id": "user_cat_3", "title": "My Stuff", "channels": ["userch1"]}
    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SERVER_ID}",
        payload={
            "_id": SERVER_ID,
            "description": "",
            "categories": [
                {"id": "cat_uuid_1", "title": "Gaming", "channels": []},
                {"id": "cat_uuid_2", "title": "Music", "channels": []},
                user_cat,
            ],
        },
    )

    patch_calls: list[Any] = []
    mock_aiohttp.patch(
        f"{BASE_URL}/servers/{SERVER_ID}",
        payload={},
        callback=lambda url, **kw: patch_calls.append(kw),
    )

    events, emit = _emit_collector()
    async with aiohttp.ClientSession() as session:
        await _clean_categories(state, config, SERVER_ID, session, emit)

    assert state.rollback_progress is not None
    assert state.rollback_progress.categories_cleaned is True
    # Inspect the captured PATCH body.
    assert len(patch_calls) == 1
    sent_cats = patch_calls[0]["json"]["categories"]
    sent_ids = [c["id"] for c in sent_cats]
    assert sent_ids == ["user_cat_3"]


# ---------------------------------------------------------------------------
# SC-15: Lock acquire returns False → raises BEFORE try
# ---------------------------------------------------------------------------


async def test_lock_acquire_false_raises_before_try(
    mock_aiohttp: aioresponses, tmp_path: Path
) -> None:
    """SC-15: when _acquire_migration_lock returns False, raise BEFORE try.

    Guards the implementation gotcha — `finally` must NOT call
    `_release_migration_lock` when lock was never written.
    """
    state = _make_state_from_fixture(tmp_path)
    save_state(state, tmp_path)
    config = _make_config(tmp_path)

    # No DELETE mocks — if any are called, the test would error.

    async def fake_acquire(*args: Any, **kwargs: Any) -> bool:
        return False

    release_calls: list[Any] = []

    async def fake_release(*args: Any, **kwargs: Any) -> None:
        release_calls.append(args)

    events, emit = _emit_collector()
    with (
        patch("discord_ferry.core.engine._acquire_migration_lock", new=fake_acquire),
        patch("discord_ferry.core.engine._release_migration_lock", new=fake_release),
        pytest.raises(MigrationError, match="Could not acquire rollback lock"),
    ):
        await run_rollback(config, state, exports=[], on_event=emit)

    # Release MUST NOT have been called.
    assert release_calls == []


# ---------------------------------------------------------------------------
# SC-16: Lock acquire raises → propagates BEFORE try
# ---------------------------------------------------------------------------


async def test_lock_acquire_raise_propagates_before_try(
    mock_aiohttp: aioresponses, tmp_path: Path
) -> None:
    """SC-16: live-lock conflict raise propagates; release NOT called."""
    state = _make_state_from_fixture(tmp_path)
    save_state(state, tmp_path)
    config = _make_config(tmp_path)

    async def fake_acquire(*args: Any, **kwargs: Any) -> bool:
        raise MigrationError("Another migration is in progress (lock age: 30s).")

    release_calls: list[Any] = []

    async def fake_release(*args: Any, **kwargs: Any) -> None:
        release_calls.append(args)

    events, emit = _emit_collector()
    with (
        patch("discord_ferry.core.engine._acquire_migration_lock", new=fake_acquire),
        patch("discord_ferry.core.engine._release_migration_lock", new=fake_release),
        pytest.raises(MigrationError, match="Another migration is in progress"),
    ):
        await run_rollback(config, state, exports=[], on_event=emit)

    assert release_calls == []


# ---------------------------------------------------------------------------
# SC-17: 403 on channel delete → DLQ, continue
# ---------------------------------------------------------------------------


async def test_403_on_channel_delete_continues(mock_aiohttp: aioresponses, tmp_path: Path) -> None:
    """SC-17: ch2 returns 403, ch1/ch3 succeed. failures has one entry."""
    state = MigrationState(
        stoat_server_id=SERVER_ID,
        channel_map={"d1": "ch1", "d2": "ch2", "d3": "ch3"},
    )
    save_state(state, tmp_path)
    config = _make_config(tmp_path)

    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SERVER_ID}",
        payload={
            "_id": SERVER_ID,
            "description": "",
            "channels": ["ch1", "ch2", "ch3"],
            "roles": {},
            "categories": [],
        },
        repeat=True,
    )
    mock_aiohttp.patch(f"{BASE_URL}/servers/{SERVER_ID}", payload={}, repeat=True)

    mock_aiohttp.delete(f"{BASE_URL}/channels/ch1", status=204)
    mock_aiohttp.delete(f"{BASE_URL}/channels/ch2", status=403, payload={"type": "Forbidden"})
    mock_aiohttp.delete(f"{BASE_URL}/channels/ch3", status=204)

    events, emit = _emit_collector()
    await run_rollback(config, state, exports=[], on_event=emit)

    rp = state.rollback_progress
    assert rp is not None
    assert rp.channels_deleted == 2
    assert len(rp.failures) == 1
    assert rp.failures[0].entity_type == "channel"
    assert rp.failures[0].stoat_id == "ch2"
    assert rp.failures[0].http_status == 403
    # Final event status should reflect the failure.
    completed = [e for e in events if e.phase == "rollback" and e.status.startswith("completed")]
    assert any(e.status == "completed_with_failures" for e in completed)


# ---------------------------------------------------------------------------
# SC-18: 403 on emoji → DLQ, continues to categories
# ---------------------------------------------------------------------------


async def test_403_on_emoji_continues_to_categories(
    mock_aiohttp: aioresponses, tmp_path: Path
) -> None:
    """SC-18: emoji 403 (ManageCustomisation missing) doesn't abort rollback."""
    state = MigrationState(
        stoat_server_id=SERVER_ID,
        channel_map={"d1": "ch1", "d2": "ch2"},
        role_map={"d_r1": "role1"},
        emoji_map={"d_em1": "em_bad", "d_em2": "em_ok"},
        category_map={"d_cat1": "cat_uuid_1"},
    )
    save_state(state, tmp_path)
    config = _make_config(tmp_path)

    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SERVER_ID}",
        payload={
            "_id": SERVER_ID,
            "description": "",
            "channels": ["ch1", "ch2"],
            "roles": {"role1": {}},
            "categories": [{"id": "cat_uuid_1", "title": "x", "channels": []}],
        },
        repeat=True,
    )
    mock_aiohttp.patch(f"{BASE_URL}/servers/{SERVER_ID}", payload={}, repeat=True)

    mock_aiohttp.delete(f"{BASE_URL}/channels/ch1", status=204)
    mock_aiohttp.delete(f"{BASE_URL}/channels/ch2", status=204)
    mock_aiohttp.delete(f"{BASE_URL}/servers/{SERVER_ID}/roles/role1", status=204)
    mock_aiohttp.delete(f"{BASE_URL}/custom/emoji/em_bad", status=403, payload={})
    mock_aiohttp.delete(f"{BASE_URL}/custom/emoji/em_ok", status=204)

    events, emit = _emit_collector()
    await run_rollback(config, state, exports=[], on_event=emit)

    rp = state.rollback_progress
    assert rp is not None
    assert rp.channels_deleted == 2
    assert rp.roles_deleted == 1
    assert rp.emoji_deleted == 1
    assert rp.categories_cleaned is True
    assert len(rp.failures) == 1
    assert rp.failures[0].entity_type == "emoji"


# ---------------------------------------------------------------------------
# SC-19: Sustained 5xx on one channel → DLQ
# ---------------------------------------------------------------------------


async def test_sustained_5xx_exhausts_retries_dlq(
    mock_aiohttp: aioresponses, tmp_path: Path
) -> None:
    """SC-19: ch1 returns 503 on every attempt, ch2 succeeds. failures has 1 entry."""
    state = MigrationState(
        stoat_server_id=SERVER_ID,
        channel_map={"d1": "ch1", "d2": "ch2"},
    )
    save_state(state, tmp_path)
    config = _make_config(tmp_path)

    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SERVER_ID}",
        payload={
            "_id": SERVER_ID,
            "description": "",
            "channels": ["ch1", "ch2"],
            "roles": {},
            "categories": [],
        },
        repeat=True,
    )
    mock_aiohttp.patch(f"{BASE_URL}/servers/{SERVER_ID}", payload={}, repeat=True)

    # 503 every retry attempt.
    mock_aiohttp.delete(f"{BASE_URL}/channels/ch1", status=503, payload={}, repeat=True)
    mock_aiohttp.delete(f"{BASE_URL}/channels/ch2", status=204)

    events, emit = _emit_collector()
    # Patch asyncio.sleep so the 503-backoff doesn't slow the test.
    real_sleep = asyncio.sleep

    async def fast_sleep(seconds: float) -> None:
        # Use 0.0 for backoff sleeps; keep tiny real-sleep for cooperative scheduling.
        await real_sleep(0)

    with patch("discord_ferry.migrator.api.asyncio.sleep", new=fast_sleep):
        await run_rollback(config, state, exports=[], on_event=emit)

    rp = state.rollback_progress
    assert rp is not None
    assert rp.channels_deleted == 1
    assert len(rp.failures) == 1
    assert rp.failures[0].stoat_id == "ch1"
    assert rp.failures[0].http_status == 503


# ---------------------------------------------------------------------------
# SC-20: 401 → DLQ + higher-severity event
# ---------------------------------------------------------------------------


async def test_401_emits_error_severity(mock_aiohttp: aioresponses, tmp_path: Path) -> None:
    """SC-20: 401 routes to error-severity event with token-invalid hint."""
    state = MigrationState(
        stoat_server_id=SERVER_ID,
        channel_map={"d1": "ch1"},
    )
    save_state(state, tmp_path)
    config = _make_config(tmp_path)

    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SERVER_ID}",
        payload={
            "_id": SERVER_ID,
            "description": "",
            "channels": ["ch1"],
            "roles": {},
            "categories": [],
        },
        repeat=True,
    )
    mock_aiohttp.patch(f"{BASE_URL}/servers/{SERVER_ID}", payload={}, repeat=True)

    mock_aiohttp.delete(f"{BASE_URL}/channels/ch1", status=401, payload={})

    events, emit = _emit_collector()
    await run_rollback(config, state, exports=[], on_event=emit)

    rp = state.rollback_progress
    assert rp is not None
    assert rp.channels_deleted == 0
    assert len(rp.failures) == 1
    assert rp.failures[0].http_status == 401
    # An error-severity rollback event was emitted with the token-invalid hint.
    error_events = [
        e
        for e in events
        if e.phase == "rollback"
        and e.status == "error"
        and "session token may be invalid" in (e.message or "")
    ]
    assert len(error_events) >= 1


# ---------------------------------------------------------------------------
# SC-21: Network error after retries → DLQ
# ---------------------------------------------------------------------------


async def test_network_error_after_retries_dlq(mock_aiohttp: aioresponses, tmp_path: Path) -> None:
    """SC-21: aiohttp ClientConnectionError on every retry → failure with http_status=None."""
    state = MigrationState(
        stoat_server_id=SERVER_ID,
        channel_map={"d1": "ch1"},
    )
    save_state(state, tmp_path)
    config = _make_config(tmp_path)

    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SERVER_ID}",
        payload={
            "_id": SERVER_ID,
            "description": "",
            "channels": ["ch1"],
            "roles": {},
            "categories": [],
        },
        repeat=True,
    )
    mock_aiohttp.patch(f"{BASE_URL}/servers/{SERVER_ID}", payload={}, repeat=True)

    # Raise ClientConnectionError on every attempt.
    mock_aiohttp.delete(
        f"{BASE_URL}/channels/ch1",
        exception=aiohttp.ClientConnectionError("reset"),
        repeat=True,
    )

    real_sleep = asyncio.sleep

    async def fast_sleep(seconds: float) -> None:
        await real_sleep(0)

    events, emit = _emit_collector()
    with patch("discord_ferry.migrator.api.asyncio.sleep", new=fast_sleep):
        await run_rollback(config, state, exports=[], on_event=emit)

    rp = state.rollback_progress
    assert rp is not None
    assert rp.channels_deleted == 0
    assert len(rp.failures) == 1
    assert rp.failures[0].http_status is None
    assert "reset" in rp.failures[0].error or "Network error" in rp.failures[0].error


# ---------------------------------------------------------------------------
# SC-23: Cancel between API and save_state — re-run recovers
# ---------------------------------------------------------------------------


async def test_cancel_mid_save_recovers_on_rerun(
    mock_aiohttp: aioresponses, tmp_path: Path
) -> None:
    """SC-23: cancel kicks in after first save_state; re-run gets 404 (idempotent)."""
    state = MigrationState(
        stoat_server_id=SERVER_ID,
        channel_map={"d1": "ch1", "d2": "ch2", "d3": "ch3"},
    )
    save_state(state, tmp_path)

    cancel_event = asyncio.Event()
    config = _make_config(tmp_path, cancel_event=cancel_event, max_concurrent_requests=1)

    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SERVER_ID}",
        payload={
            "_id": SERVER_ID,
            "description": "",
            "channels": ["ch1", "ch2", "ch3"],
            "roles": {},
            "categories": [],
        },
        repeat=True,
    )
    mock_aiohttp.patch(f"{BASE_URL}/servers/{SERVER_ID}", payload={}, repeat=True)

    # Run 1: all DELETEs return 204.
    mock_aiohttp.delete(f"{BASE_URL}/channels/ch1", status=204)
    mock_aiohttp.delete(f"{BASE_URL}/channels/ch2", status=204)
    mock_aiohttp.delete(f"{BASE_URL}/channels/ch3", status=204)

    # Patch save_state to set the cancel event after first call.
    real_save = save_state
    save_calls = [0]

    def cancel_after_first(state_arg: MigrationState, path_arg: Path) -> None:
        save_calls[0] += 1
        real_save(state_arg, path_arg)
        if save_calls[0] == 1:
            cancel_event.set()

    events, emit = _emit_collector()
    with patch("discord_ferry.core.engine.save_state", side_effect=cancel_after_first):
        result = await run_rollback(config, state, exports=[], on_event=emit)

    rp = result.rollback_progress
    assert rp is not None
    assert rp.channels_deleted >= 1  # at least the first task completed
    assert len(rp.rolled_back_ids) >= 1

    # Run 2 — fresh cancel_event, remaining channels return 404.
    cancel_event2 = asyncio.Event()  # don't set
    config2 = _make_config(tmp_path, cancel_event=cancel_event2, max_concurrent_requests=1)

    # Re-load state from disk to simulate fresh process.
    state2 = load_state(tmp_path)

    mock_aiohttp.delete(f"{BASE_URL}/channels/ch1", status=404, payload={}, repeat=True)
    mock_aiohttp.delete(f"{BASE_URL}/channels/ch2", status=404, payload={}, repeat=True)
    mock_aiohttp.delete(f"{BASE_URL}/channels/ch3", status=404, payload={}, repeat=True)

    events2, emit2 = _emit_collector()
    await run_rollback(config2, state2, exports=[], on_event=emit2)

    rp2 = state2.rollback_progress
    assert rp2 is not None
    # Total across both runs is 3.
    assert rp2.channels_deleted == 3
    assert rp2.failures == []


# ---------------------------------------------------------------------------
# SC-24: cancel_event pre-set → no DELETEs
# ---------------------------------------------------------------------------


async def test_cancel_pre_set_no_deletes(mock_aiohttp: aioresponses, tmp_path: Path) -> None:
    """SC-24: cancel set before invocation → zero DELETEs, lock acquired+released cleanly."""
    state = MigrationState(
        stoat_server_id=SERVER_ID,
        channel_map={"d1": "ch1", "d2": "ch2"},
    )
    save_state(state, tmp_path)

    cancel_event = asyncio.Event()
    cancel_event.set()
    config = _make_config(tmp_path, cancel_event=cancel_event)

    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SERVER_ID}",
        payload={
            "_id": SERVER_ID,
            "description": "",
            "channels": ["ch1", "ch2"],
            "roles": {},
            "categories": [],
        },
        repeat=True,
    )
    mock_aiohttp.patch(f"{BASE_URL}/servers/{SERVER_ID}", payload={}, repeat=True)
    # No DELETE mocks — if any are called, aioresponses raises.

    events, emit = _emit_collector()
    await run_rollback(config, state, exports=[], on_event=emit)

    rp = state.rollback_progress
    assert rp is not None
    assert rp.channels_deleted == 0
    # cancelled event was emitted (either at confirmation gate or after).
    assert any(e.status == "cancelled" for e in events)


# ---------------------------------------------------------------------------
# SC-26: Partial-rollback resume — rolled_back_ids skipped on second run
# ---------------------------------------------------------------------------


async def test_rolled_back_ids_skipped_on_resume(
    mock_aiohttp: aioresponses, tmp_path: Path
) -> None:
    """SC-26: prior rolled_back_ids skip those channels on re-run."""
    state = MigrationState(
        stoat_server_id=SERVER_ID,
        channel_map={"d1": "ch1", "d2": "ch2", "d3": "ch3", "d4": "ch4"},
        rollback_progress=RollbackProgress(rolled_back_ids={"ch1", "ch2"}, channels_deleted=2),
    )
    save_state(state, tmp_path)
    config = _make_config(tmp_path)

    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SERVER_ID}",
        payload={
            "_id": SERVER_ID,
            "description": "",
            "channels": ["ch1", "ch2", "ch3", "ch4"],
            "roles": {},
            "categories": [],
        },
        repeat=True,
    )
    mock_aiohttp.patch(f"{BASE_URL}/servers/{SERVER_ID}", payload={}, repeat=True)

    # Only ch3, ch4 mocked. If ch1 or ch2 are called, aioresponses raises.
    mock_aiohttp.delete(f"{BASE_URL}/channels/ch3", status=204)
    mock_aiohttp.delete(f"{BASE_URL}/channels/ch4", status=204)

    events, emit = _emit_collector()
    await run_rollback(config, state, exports=[], on_event=emit)

    rp = state.rollback_progress
    assert rp is not None
    assert rp.channels_deleted == 4  # 2 prior + 2 new
    assert rp.rolled_back_ids == {"ch1", "ch2", "ch3", "ch4"}


# ---------------------------------------------------------------------------
# SC-27: Category TOCTOU — user-added categories survive
# ---------------------------------------------------------------------------


async def test_clean_categories_preserves_concurrently_added_user_category(
    mock_aiohttp: aioresponses, tmp_path: Path
) -> None:
    """SC-27: a category added between rollback start and PATCH survives."""
    state = MigrationState(
        stoat_server_id=SERVER_ID,
        category_map={"d_cat1": "cat_uuid_1", "d_cat2": "cat_uuid_2"},
        rollback_progress=RollbackProgress(),
    )
    save_state(state, tmp_path)
    config = _make_config(tmp_path)

    # Server has the original 4 (2 Ferry, 2 user) PLUS a newly-added user category.
    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SERVER_ID}",
        payload={
            "_id": SERVER_ID,
            "description": "",
            "categories": [
                {"id": "user_a", "title": "User A", "channels": []},
                {"id": "user_b", "title": "User B", "channels": []},
                {"id": "cat_uuid_1", "title": "Ferry 1", "channels": []},
                {"id": "cat_uuid_2", "title": "Ferry 2", "channels": []},
                {"id": "user_added_during", "title": "Added mid-rollback", "channels": []},
            ],
        },
    )

    patch_payload: list[Any] = []
    mock_aiohttp.patch(
        f"{BASE_URL}/servers/{SERVER_ID}",
        payload={},
        callback=lambda url, **kw: patch_payload.append(kw["json"]["categories"]),
    )

    events, emit = _emit_collector()
    async with aiohttp.ClientSession() as session:
        await _clean_categories(state, config, SERVER_ID, session, emit)

    sent_ids = [c["id"] for c in patch_payload[0]]
    assert "user_a" in sent_ids
    assert "user_b" in sent_ids
    assert "user_added_during" in sent_ids
    assert "cat_uuid_1" not in sent_ids
    assert "cat_uuid_2" not in sent_ids


# ---------------------------------------------------------------------------
# SC-14 end-to-end check
# ---------------------------------------------------------------------------


async def test_empty_server_id_aborts_before_lock(tmp_path: Path) -> None:
    """SC-14 end-to-end: rollback raises before any HTTP call when both IDs empty."""
    state = MigrationState()  # empty stoat_server_id
    save_state(state, tmp_path)
    config = _make_config(tmp_path, server_id=None)

    events, emit = _emit_collector()
    with pytest.raises(MigrationError, match="no Stoat server ID"):
        await run_rollback(config, state, exports=[], on_event=emit)


# ---------------------------------------------------------------------------
# Confirmation gate ordering regression — clear-before-emit (code review fix)
# ---------------------------------------------------------------------------


async def test_confirm_gate_clear_then_emit_not_clear_after(
    mock_aiohttp: aioresponses, tmp_path: Path
) -> None:
    """The gate must be cleared BEFORE emitting the event, not after.

    CLI handlers call ``pause_event.set()`` synchronously inside ``on_event``
    (because ``click.confirm`` blocks the event loop until the user responds).
    If we clear AFTER emit, the user's approval is erased and the wait-loop
    hangs forever. This test simulates that pattern: the on_event handler
    sets pause_event before returning; run_rollback must still see it set
    when it enters the wait-loop and proceed without hanging.
    """
    state = MigrationState(
        stoat_server_id=SERVER_ID,
        channel_map={"d1": "ch1"},
    )
    save_state(state, tmp_path)

    pause_event = asyncio.Event()
    config = _make_config(tmp_path, pause_event=pause_event)

    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SERVER_ID}",
        payload={
            "_id": SERVER_ID,
            "description": "",
            "channels": ["ch1"],
            "roles": {},
            "categories": [],
        },
        repeat=True,
    )
    mock_aiohttp.patch(f"{BASE_URL}/servers/{SERVER_ID}", payload={}, repeat=True)
    mock_aiohttp.delete(f"{BASE_URL}/channels/ch1", status=204)

    def cli_style_handler(event: MigrationEvent) -> None:
        # Simulate the CLI tracker's synchronous click.confirm + set()
        # sequence: when the confirm event arrives, "user" approves
        # immediately and pause_event.set() is called before returning.
        if event.status == "confirm_rollback":
            pause_event.set()

    # Without the fix, this awaits forever. With the fix, it completes.
    await asyncio.wait_for(
        run_rollback(config, state, exports=[], on_event=cli_style_handler),
        timeout=5.0,
    )
    rp = state.rollback_progress
    assert rp is not None
    assert rp.channels_deleted == 1


# ---------------------------------------------------------------------------
# DLQ catches unexpected exceptions (code review fix)
# ---------------------------------------------------------------------------


async def test_dlq_catches_unexpected_exception_in_channel_delete(
    mock_aiohttp: aioresponses, tmp_path: Path
) -> None:
    """An unexpected (non-MigrationError) exception in api_delete_channel
    must land in the DLQ with http_status=None, not silently swallowed by
    asyncio.gather(return_exceptions=True).
    """
    state = MigrationState(
        stoat_server_id=SERVER_ID,
        channel_map={"d1": "ch1", "d2": "ch2"},
    )
    save_state(state, tmp_path)
    config = _make_config(tmp_path)

    mock_aiohttp.get(
        f"{BASE_URL}/servers/{SERVER_ID}",
        payload={
            "_id": SERVER_ID,
            "description": "",
            "channels": ["ch1", "ch2"],
            "roles": {},
            "categories": [],
        },
        repeat=True,
    )
    mock_aiohttp.patch(f"{BASE_URL}/servers/{SERVER_ID}", payload={}, repeat=True)

    real_delete = None

    async def boom_then_succeed(*args: Any, **kwargs: Any) -> None:
        # First call to delete raises a random non-MigrationError. Second
        # succeeds (returns None, simulating a 204).
        if not boom_then_succeed.fired:  # type: ignore[attr-defined]
            boom_then_succeed.fired = True  # type: ignore[attr-defined]
            raise RuntimeError("transient library bug")

    boom_then_succeed.fired = False  # type: ignore[attr-defined]

    events, emit = _emit_collector()
    with patch("discord_ferry.core.engine.api_delete_channel", new=boom_then_succeed):
        await run_rollback(config, state, exports=[], on_event=emit)

    rp = state.rollback_progress
    assert rp is not None
    # One channel succeeded, one went to DLQ with http_status=None.
    assert rp.channels_deleted == 1
    assert len(rp.failures) == 1
    assert rp.failures[0].entity_type == "channel"
    assert rp.failures[0].http_status is None
    assert "transient library bug" in rp.failures[0].error
    # Free the unused name to silence linters.
    _ = real_delete
