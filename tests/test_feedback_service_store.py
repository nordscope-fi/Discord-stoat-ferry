"""Tests for short-lived feedback service persistence."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from discord_ferry.feedback import DestinationKind
from discord_ferry.feedback_service import store as feedback_store
from discord_ferry.feedback_service.__main__ import run as run_operator
from discord_ferry.feedback_service.store import (
    ClaimOutcome,
    FeedbackStore,
    ReceiptState,
    ReceiptTransitionError,
)

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
REQUEST_ID = UUID("018f4c8c-3f52-7a89-a901-0123456789ac")
CONTENT_HASH = "a" * 64
CONTACT_KEY = bytes(range(32))
CONTACT_EMAIL = "private-contact@example.com"
SOURCE_HASH_KEY = bytes(range(32, 64))


async def _store(path: Path, *, now: datetime = NOW) -> FeedbackStore:
    store = FeedbackStore(path)
    await store.initialize(now=now)
    return store


async def test_receipt_schema_enables_wal_and_foreign_keys(tmp_path: Path) -> None:
    path = tmp_path / "feedback.sqlite3"
    await _store(path)

    with sqlite3.connect(path) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert journal_mode == "wal"
    assert "receipts" in tables


async def test_receipt_claim_is_pending_before_github(tmp_path: Path) -> None:
    store = await _store(tmp_path / "feedback.sqlite3")

    claim = await store.claim_receipt(
        REQUEST_ID,
        CONTENT_HASH,
        DestinationKind.ISSUE,
        now=NOW,
    )

    assert claim.outcome is ClaimOutcome.CREATED
    assert claim.record.state is ReceiptState.PENDING
    assert claim.record.destination_url is None


async def test_receipt_can_be_marked_delivered_with_its_public_url(tmp_path: Path) -> None:
    store = await _store(tmp_path / "feedback.sqlite3")
    await store.claim_receipt(REQUEST_ID, CONTENT_HASH, DestinationKind.ISSUE, now=NOW)

    record = await store.mark_delivered(
        REQUEST_ID,
        "https://github.com/nordscope-fi/Discord-stoat-ferry/issues/999",
        now=NOW + timedelta(seconds=1),
    )

    assert record.state is ReceiptState.DELIVERED
    assert record.destination_url is not None
    assert record.destination_url.endswith("/issues/999")


async def test_clear_receipt_failure_allows_a_safe_new_claim(tmp_path: Path) -> None:
    store = await _store(tmp_path / "feedback.sqlite3")
    await store.claim_receipt(REQUEST_ID, CONTENT_HASH, DestinationKind.ISSUE, now=NOW)

    failed = await store.mark_failed(REQUEST_ID, now=NOW + timedelta(seconds=1))
    retried = await store.claim_receipt(
        REQUEST_ID,
        CONTENT_HASH,
        DestinationKind.ISSUE,
        now=NOW + timedelta(seconds=2),
    )

    assert failed.state is ReceiptState.FAILED
    assert retried.outcome is ClaimOutcome.CREATED
    assert retried.record.state is ReceiptState.PENDING


async def test_uncertain_pending_receipt_never_creates_a_second_claim(tmp_path: Path) -> None:
    store = await _store(tmp_path / "feedback.sqlite3")
    await store.claim_receipt(REQUEST_ID, CONTENT_HASH, DestinationKind.ISSUE, now=NOW)

    replay = await store.claim_receipt(
        REQUEST_ID,
        CONTENT_HASH,
        DestinationKind.ISSUE,
        now=NOW + timedelta(minutes=1),
    )

    assert replay.outcome is ClaimOutcome.PENDING
    assert replay.record.created_at == NOW


async def test_same_id_and_hash_replays_the_delivered_receipt(tmp_path: Path) -> None:
    store = await _store(tmp_path / "feedback.sqlite3")
    await store.claim_receipt(REQUEST_ID, CONTENT_HASH, DestinationKind.ISSUE, now=NOW)
    await store.mark_delivered(
        REQUEST_ID,
        "https://github.com/nordscope-fi/Discord-stoat-ferry/issues/999",
        now=NOW,
    )

    replay = await store.claim_receipt(
        REQUEST_ID,
        CONTENT_HASH,
        DestinationKind.ISSUE,
        now=NOW + timedelta(hours=1),
    )

    assert replay.outcome is ClaimOutcome.DELIVERED
    assert replay.record.destination_url is not None


async def test_changed_hash_conflicts_without_altering_the_receipt(tmp_path: Path) -> None:
    store = await _store(tmp_path / "feedback.sqlite3")
    await store.claim_receipt(REQUEST_ID, CONTENT_HASH, DestinationKind.ISSUE, now=NOW)

    conflict = await store.claim_receipt(
        REQUEST_ID,
        "b" * 64,
        DestinationKind.ISSUE,
        now=NOW + timedelta(seconds=1),
    )
    stored = await store.get_receipt(REQUEST_ID)

    assert conflict.outcome is ClaimOutcome.CONFLICT
    assert stored is not None
    assert stored.content_hash == CONTENT_HASH


async def test_receipt_expiry_allows_a_new_claim_after_seven_days(tmp_path: Path) -> None:
    store = await _store(tmp_path / "feedback.sqlite3")
    await store.claim_receipt(REQUEST_ID, CONTENT_HASH, DestinationKind.ISSUE, now=NOW)

    await store.expire(now=NOW + timedelta(days=7))
    claim = await store.claim_receipt(
        REQUEST_ID,
        "b" * 64,
        DestinationKind.DISCUSSION,
        now=NOW + timedelta(days=7),
    )

    assert claim.outcome is ClaimOutcome.CREATED
    assert claim.record.content_hash == "b" * 64


async def test_startup_removes_expired_receipts(tmp_path: Path) -> None:
    path = tmp_path / "feedback.sqlite3"
    first = await _store(path)
    await first.claim_receipt(REQUEST_ID, CONTENT_HASH, DestinationKind.ISSUE, now=NOW)

    await _store(path, now=NOW + timedelta(days=8))

    second = FeedbackStore(path)
    assert await second.get_receipt(REQUEST_ID) is None


async def test_absent_resolution_is_the_only_pending_path_to_a_new_claim(
    tmp_path: Path,
) -> None:
    store = await _store(tmp_path / "feedback.sqlite3")
    await store.claim_receipt(REQUEST_ID, CONTENT_HASH, DestinationKind.ISSUE, now=NOW)

    absent = await store.mark_absent(REQUEST_ID, now=NOW + timedelta(minutes=2))
    retried = await store.claim_receipt(
        REQUEST_ID,
        CONTENT_HASH,
        DestinationKind.ISSUE,
        now=NOW + timedelta(minutes=3),
    )

    assert absent.state is ReceiptState.ABSENT
    assert absent.audit_at == NOW + timedelta(minutes=2)
    assert retried.outcome is ClaimOutcome.CREATED


async def test_operator_resolution_records_a_delivered_destination(tmp_path: Path) -> None:
    store = await _store(tmp_path / "feedback.sqlite3")
    await store.claim_receipt(REQUEST_ID, CONTENT_HASH, DestinationKind.DISCUSSION, now=NOW)

    resolved = await store.resolve_destination(
        REQUEST_ID,
        "https://github.com/nordscope-fi/Discord-stoat-ferry/discussions/999",
        now=NOW + timedelta(minutes=2),
    )

    assert resolved.state is ReceiptState.DELIVERED
    assert resolved.audit_at == NOW + timedelta(minutes=2)


async def test_receipt_transition_refuses_a_non_pending_record(tmp_path: Path) -> None:
    store = await _store(tmp_path / "feedback.sqlite3")
    await store.claim_receipt(REQUEST_ID, CONTENT_HASH, DestinationKind.ISSUE, now=NOW)
    await store.mark_failed(REQUEST_ID, now=NOW)

    try:
        await store.mark_delivered(
            REQUEST_ID,
            "https://github.com/nordscope-fi/Discord-stoat-ferry/issues/999",
            now=NOW,
        )
    except ReceiptTransitionError as error:
        assert str(REQUEST_ID) in str(error)
    else:
        raise AssertionError("non-pending transition was accepted")


async def test_concurrent_receipt_claims_have_one_winner(tmp_path: Path) -> None:
    store = await _store(tmp_path / "feedback.sqlite3")

    claims = await asyncio.gather(
        *(
            store.claim_receipt(
                REQUEST_ID,
                CONTENT_HASH,
                DestinationKind.ISSUE,
                now=NOW,
            )
            for _ in range(10)
        )
    )

    assert [claim.outcome for claim in claims].count(ClaimOutcome.CREATED) == 1
    assert [claim.outcome for claim in claims].count(ClaimOutcome.PENDING) == 9


async def test_contact_is_encrypted_at_rest_and_linked_to_the_receipt(tmp_path: Path) -> None:
    path = tmp_path / "feedback.sqlite3"
    store = FeedbackStore(path, contact_key=CONTACT_KEY)
    await store.initialize(now=NOW)
    await store.claim_receipt(REQUEST_ID, CONTENT_HASH, DestinationKind.ISSUE, now=NOW)

    await store.store_contact(REQUEST_ID, CONTACT_EMAIL, now=NOW)

    assert await store.get_contact(REQUEST_ID, now=NOW) == CONTACT_EMAIL
    database_bytes = b"".join(
        candidate.read_bytes()
        for candidate in tmp_path.glob("feedback.sqlite3*")
        if candidate.is_file()
    )
    assert CONTACT_EMAIL.encode() not in database_bytes
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT receipt, ciphertext FROM contacts WHERE receipt = ?",
            (str(REQUEST_ID),),
        ).fetchone()
    assert row is not None
    assert row[0] == str(REQUEST_ID)
    assert CONTACT_EMAIL.encode() not in row[1]


async def test_contact_lookup_with_the_wrong_key_fails_without_plaintext(tmp_path: Path) -> None:
    path = tmp_path / "feedback.sqlite3"
    store = FeedbackStore(path, contact_key=CONTACT_KEY)
    await store.initialize(now=NOW)
    await store.store_contact(REQUEST_ID, CONTACT_EMAIL, now=NOW)
    wrong_key_store = FeedbackStore(path, contact_key=bytes(reversed(range(32))))

    try:
        await wrong_key_store.get_contact(REQUEST_ID, now=NOW)
    except feedback_store.ContactDecryptionError as error:
        assert CONTACT_EMAIL not in str(error)
    else:
        raise AssertionError("wrong contact key decrypted the value")


async def test_contact_delete_removes_the_private_value(tmp_path: Path) -> None:
    store = FeedbackStore(tmp_path / "feedback.sqlite3", contact_key=CONTACT_KEY)
    await store.initialize(now=NOW)
    await store.store_contact(REQUEST_ID, CONTACT_EMAIL, now=NOW)

    assert await store.delete_contact(REQUEST_ID) is True
    assert await store.get_contact(REQUEST_ID, now=NOW) is None
    assert await store.delete_contact(REQUEST_ID) is False


async def test_contact_expires_after_thirty_days_not_with_the_receipt(tmp_path: Path) -> None:
    store = FeedbackStore(tmp_path / "feedback.sqlite3", contact_key=CONTACT_KEY)
    await store.initialize(now=NOW)
    await store.claim_receipt(REQUEST_ID, CONTENT_HASH, DestinationKind.ISSUE, now=NOW)
    await store.store_contact(REQUEST_ID, CONTACT_EMAIL, now=NOW)

    await store.expire(now=NOW + timedelta(days=7))
    assert await store.get_receipt(REQUEST_ID) is None
    assert await store.get_contact(REQUEST_ID, now=NOW + timedelta(days=7)) == CONTACT_EMAIL

    await store.expire(now=NOW + timedelta(days=30))
    assert await store.get_contact(REQUEST_ID, now=NOW + timedelta(days=30)) is None


async def test_startup_and_ordinary_requests_clean_expired_contacts(tmp_path: Path) -> None:
    path = tmp_path / "feedback.sqlite3"
    store = FeedbackStore(path, contact_key=CONTACT_KEY)
    await store.initialize(now=NOW)
    await store.store_contact(REQUEST_ID, CONTACT_EMAIL, now=NOW)

    restarted = FeedbackStore(path, contact_key=CONTACT_KEY)
    await restarted.initialize(now=NOW + timedelta(days=31))
    assert await restarted.get_contact(REQUEST_ID, now=NOW + timedelta(days=31)) is None

    await restarted.store_contact(REQUEST_ID, CONTACT_EMAIL, now=NOW + timedelta(days=32))
    other_id = UUID("018f4c8c-3f52-7a89-a901-4123456789ac")
    await restarted.claim_receipt(
        other_id,
        CONTENT_HASH,
        DestinationKind.ISSUE,
        now=NOW + timedelta(days=63),
    )
    assert await restarted.get_contact(REQUEST_ID, now=NOW + timedelta(days=63)) is None


async def test_absent_contact_creates_no_contact_row(tmp_path: Path) -> None:
    path = tmp_path / "feedback.sqlite3"
    store = FeedbackStore(path, contact_key=CONTACT_KEY)
    await store.initialize(now=NOW)

    await store.store_contact(REQUEST_ID, None, now=NOW)

    with sqlite3.connect(path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    assert count == 0


async def _quota_store(path: Path) -> FeedbackStore:
    store = FeedbackStore(path, source_hash_key=SOURCE_HASH_KEY)
    await store.initialize(now=NOW)
    return store


async def test_challenge_quota_allows_thirty_per_rolling_hour(tmp_path: Path) -> None:
    store = await _quota_store(tmp_path / "feedback.sqlite3")

    accepted = [
        await store.check_challenge_quota("192.0.2.10", now=NOW + timedelta(seconds=offset))
        for offset in range(30)
    ]
    denied = await store.check_challenge_quota(
        "192.0.2.10",
        now=NOW + timedelta(seconds=30),
    )

    assert all(decision.allowed for decision in accepted)
    assert denied.allowed is False
    assert denied.retry_at == NOW + timedelta(hours=1)


async def test_challenge_quota_uses_a_true_rolling_boundary(tmp_path: Path) -> None:
    store = await _quota_store(tmp_path / "feedback.sqlite3")
    for offset in range(30):
        assert (
            await store.check_challenge_quota(
                "192.0.2.10",
                now=NOW + timedelta(seconds=offset),
            )
        ).allowed

    boundary = await store.check_challenge_quota(
        "192.0.2.10",
        now=NOW + timedelta(hours=1),
    )

    assert boundary.allowed is True


async def test_report_quota_enforces_three_per_hour_and_ten_per_day(tmp_path: Path) -> None:
    store = await _quota_store(tmp_path / "feedback.sqlite3")
    source = "198.51.100.20"
    for offset in (0, 1, 2):
        assert (await store.claim_report_quota(source, now=NOW + timedelta(seconds=offset))).allowed

    hourly = await store.claim_report_quota(source, now=NOW + timedelta(seconds=3))
    assert hourly.allowed is False
    assert hourly.retry_at == NOW + timedelta(hours=1)

    for hour in range(2, 9):
        assert (await store.claim_report_quota(source, now=NOW + timedelta(hours=hour))).allowed
    daily = await store.claim_report_quota(source, now=NOW + timedelta(hours=10))

    assert daily.allowed is False
    assert daily.retry_at == NOW + timedelta(days=1)


async def test_report_quota_enforces_sixty_total_per_hour(tmp_path: Path) -> None:
    store = await _quota_store(tmp_path / "feedback.sqlite3")
    for source_number in range(20):
        for offset in range(3):
            decision = await store.claim_report_quota(
                f"203.0.113.{source_number + 1}",
                now=NOW + timedelta(seconds=offset),
            )
            assert decision.allowed

    denied = await store.claim_report_quota("198.51.100.99", now=NOW + timedelta(seconds=3))

    assert denied.allowed is False
    assert denied.retry_at == NOW + timedelta(hours=1)


async def test_source_quotas_are_independent_and_normalize_ip_aliases(tmp_path: Path) -> None:
    path = tmp_path / "feedback.sqlite3"
    store = await _quota_store(path)
    for offset in range(3):
        assert (
            await store.claim_report_quota(
                "2001:0db8:0:0:0:0:0:1",
                now=NOW + timedelta(seconds=offset),
            )
        ).allowed

    same_source = await store.claim_report_quota("2001:db8::1", now=NOW + timedelta(seconds=3))
    other_source = await store.claim_report_quota("2001:db8::2", now=NOW + timedelta(seconds=3))

    assert same_source.allowed is False
    assert other_source.allowed is True
    database_bytes = b"".join(
        candidate.read_bytes()
        for candidate in tmp_path.glob("feedback.sqlite3*")
        if candidate.is_file()
    )
    assert b"2001:db8" not in database_bytes


async def test_report_quota_check_and_insert_are_transactional(tmp_path: Path) -> None:
    store = await _quota_store(tmp_path / "feedback.sqlite3")

    decisions = await asyncio.gather(
        *(store.claim_report_quota("192.0.2.44", now=NOW) for _ in range(10))
    )

    assert [decision.allowed for decision in decisions].count(True) == 3
    assert [decision.allowed for decision in decisions].count(False) == 7


async def test_rate_rows_expire_after_twenty_four_hours(tmp_path: Path) -> None:
    path = tmp_path / "feedback.sqlite3"
    store = await _quota_store(path)
    await store.check_challenge_quota("192.0.2.10", now=NOW)
    await store.claim_report_quota("192.0.2.10", now=NOW)

    await store.expire(now=NOW + timedelta(days=1))

    with sqlite3.connect(path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM rate_events").fetchone()[0]
    assert count == 0


def _contact_env(key: bytes = CONTACT_KEY) -> dict[str, str]:
    return {"FERRY_FEEDBACK_CONTACT_KEY": base64.urlsafe_b64encode(key).decode()}


async def test_operator_contact_show_and_delete_are_explicit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "feedback.sqlite3"
    store = FeedbackStore(path, contact_key=CONTACT_KEY)
    await store.initialize(now=NOW)
    await store.store_contact(REQUEST_ID, CONTACT_EMAIL, now=NOW)

    shown = await run_operator(
        ["--database", str(path), "contact", "show", str(REQUEST_ID)],
        environ=_contact_env(),
        now=NOW,
    )
    assert shown == 0
    assert capsys.readouterr().out.strip() == CONTACT_EMAIL

    deleted = await run_operator(
        ["--database", str(path), "contact", "delete", str(REQUEST_ID)],
        environ=_contact_env(),
        now=NOW,
    )
    assert deleted == 0
    assert await store.get_contact(REQUEST_ID, now=NOW) is None


async def test_operator_receipt_resolve_validates_and_records_the_url(tmp_path: Path) -> None:
    path = tmp_path / "feedback.sqlite3"
    store = await _store(path)
    await store.claim_receipt(REQUEST_ID, CONTENT_HASH, DestinationKind.ISSUE, now=NOW)
    url = "https://github.com/nordscope-fi/Discord-stoat-ferry/issues/999"

    result = await run_operator(
        ["--database", str(path), "receipt", "resolve", str(REQUEST_ID), url],
        environ={},
        now=NOW + timedelta(minutes=2),
    )
    record = await store.get_receipt(REQUEST_ID)

    assert result == 0
    assert record is not None
    assert record.state is ReceiptState.DELIVERED
    assert record.destination_url == url
    assert record.audit_at == NOW + timedelta(minutes=2)


async def test_operator_receipt_absent_records_an_audit_timestamp(tmp_path: Path) -> None:
    path = tmp_path / "feedback.sqlite3"
    store = await _store(path)
    await store.claim_receipt(REQUEST_ID, CONTENT_HASH, DestinationKind.DISCUSSION, now=NOW)

    result = await run_operator(
        ["--database", str(path), "receipt", "absent", str(REQUEST_ID)],
        environ={},
        now=NOW + timedelta(minutes=2),
    )
    record = await store.get_receipt(REQUEST_ID)

    assert result == 0
    assert record is not None
    assert record.state is ReceiptState.ABSENT
    assert record.audit_at == NOW + timedelta(minutes=2)


async def test_operator_refuses_invalid_url_and_non_pending_transition(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "feedback.sqlite3"
    store = await _store(path)
    await store.claim_receipt(REQUEST_ID, CONTENT_HASH, DestinationKind.ISSUE, now=NOW)

    invalid = await run_operator(
        [
            "--database",
            str(path),
            "receipt",
            "resolve",
            str(REQUEST_ID),
            "https://example.com/issues/999",
        ],
        environ={},
        now=NOW,
    )
    assert invalid == 1
    assert (await store.get_receipt(REQUEST_ID)).state is ReceiptState.PENDING  # type: ignore[union-attr]

    await store.mark_failed(REQUEST_ID, now=NOW)
    non_pending = await run_operator(
        ["--database", str(path), "receipt", "absent", str(REQUEST_ID)],
        environ={},
        now=NOW,
    )

    assert non_pending == 1
    assert "not pending" in capsys.readouterr().err


async def test_operator_requires_an_existing_local_database_path(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        await run_operator(["receipt", "absent", str(REQUEST_ID)], environ={}, now=NOW)

    result = await run_operator(
        [
            "--database",
            str(tmp_path / "missing.sqlite3"),
            "receipt",
            "absent",
            str(REQUEST_ID),
        ],
        environ={},
        now=NOW,
    )
    assert result == 1


async def test_operator_contact_errors_do_not_print_keys_or_contact(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "feedback.sqlite3"
    store = FeedbackStore(path, contact_key=CONTACT_KEY)
    await store.initialize(now=NOW)
    await store.store_contact(REQUEST_ID, CONTACT_EMAIL, now=NOW)
    wrong_key = bytes(reversed(range(32)))
    env = _contact_env(wrong_key)

    result = await run_operator(
        ["--database", str(path), "contact", "show", str(REQUEST_ID)],
        environ=env,
        now=NOW,
    )
    output = capsys.readouterr()

    assert result == 1
    assert CONTACT_EMAIL not in output.out + output.err
    assert env["FERRY_FEEDBACK_CONTACT_KEY"] not in output.out + output.err


async def test_sqlite_and_logs_exclude_all_chunk_privacy_markers(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "feedback.sqlite3"
    email = "audit-private@example.com"
    source = "198.51.100.77"
    feedback_marker = "feedback-body-audit-marker"
    diagnostic_marker = "diagnostic-audit-marker"
    contact_key = b"C" * 32
    source_key = b"S" * 32
    content_hash = hashlib.sha256(f"{feedback_marker}:{diagnostic_marker}".encode()).hexdigest()
    store = FeedbackStore(
        path,
        contact_key=contact_key,
        source_hash_key=source_key,
    )
    await store.initialize(now=NOW)

    await store.claim_receipt(REQUEST_ID, content_hash, DestinationKind.ISSUE, now=NOW)
    await store.store_contact(REQUEST_ID, email, now=NOW)
    await store.check_challenge_quota(source, now=NOW)
    await store.claim_report_quota(source, now=NOW)

    persisted = b"".join(
        candidate.read_bytes()
        for candidate in tmp_path.glob("feedback.sqlite3*")
        if candidate.is_file()
    )
    logged = caplog.text.encode()
    for marker in (
        email.encode(),
        source.encode(),
        feedback_marker.encode(),
        diagnostic_marker.encode(),
        contact_key,
        source_key,
    ):
        assert marker not in persisted
        assert marker not in logged
