"""Transaction-safe short-lived state for the feedback intake service."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, cast
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken

from discord_ferry.feedback import DestinationKind

if TYPE_CHECKING:
    from pathlib import Path

_RECEIPT_RETENTION = timedelta(days=7)
_CONTACT_RETENTION = timedelta(days=30)
_RATE_RETENTION = timedelta(days=1)
_CONTENT_HASH = re.compile(r"^[0-9a-f]{64}$")


class ReceiptState(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    ABSENT = "absent"


class ClaimOutcome(StrEnum):
    CREATED = "created"
    PENDING = "pending"
    DELIVERED = "delivered"
    CONFLICT = "conflict"


class ReceiptTransitionError(RuntimeError):
    """Raised when an operator or adapter requests an unsafe transition."""


class ContactDecryptionError(RuntimeError):
    """Raised when retained contact data cannot be decrypted by this service."""


@dataclass(frozen=True)
class ReceiptRecord:
    request_id: UUID
    content_hash: str
    state: ReceiptState
    destination_kind: DestinationKind
    destination_url: str | None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    audit_at: datetime | None


@dataclass(frozen=True)
class ReceiptClaim:
    outcome: ClaimOutcome
    record: ReceiptRecord


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    retry_at: datetime | None = None


def _timestamp(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("store timestamps must include a timezone")
    return int(value.timestamp())


def _datetime(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=UTC)


class FeedbackStore:
    """SQLite access serialized per service process and run off the event loop."""

    def __init__(
        self,
        path: Path,
        *,
        contact_key: bytes | None = None,
        source_hash_key: bytes | None = None,
    ) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        if contact_key is not None and len(contact_key) != 32:
            raise ValueError("contact_key must contain exactly 32 bytes")
        self._contact_cipher = (
            None if contact_key is None else Fernet(base64.urlsafe_b64encode(contact_key))
        )
        if source_hash_key is not None and len(source_hash_key) != 32:
            raise ValueError("source_hash_key must contain exactly 32 bytes")
        self._source_hash_key = source_hash_key

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    async def initialize(self, *, now: datetime | None = None) -> None:
        current = datetime.now(tz=UTC) if now is None else now
        async with self._lock:
            await asyncio.to_thread(self._initialize_sync, current)

    def _initialize_sync(self, now: datetime) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS receipts (
                    request_id TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL,
                    state TEXT NOT NULL
                        CHECK (state IN ('pending', 'delivered', 'failed', 'absent')),
                    destination_kind TEXT NOT NULL
                        CHECK (destination_kind IN ('issue', 'discussion')),
                    destination_url TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    audit_at INTEGER
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS receipts_expiry ON receipts(expires_at)")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS contacts (
                    receipt TEXT PRIMARY KEY,
                    ciphertext BLOB NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS contacts_expiry ON contacts(expires_at)")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS rate_events (
                    id INTEGER PRIMARY KEY,
                    source_hash TEXT NOT NULL,
                    event_kind TEXT NOT NULL CHECK (event_kind IN ('challenge', 'report')),
                    created_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS rate_events_lookup
                ON rate_events(event_kind, source_hash, created_at)
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS rate_events_expiry ON rate_events(created_at)"
            )
            self._cleanup_expired(connection, _timestamp(now))

    @staticmethod
    def _record(row: sqlite3.Row) -> ReceiptRecord:
        created_at = _datetime(cast("int", row["created_at"]))
        updated_at = _datetime(cast("int", row["updated_at"]))
        expires_at = _datetime(cast("int", row["expires_at"]))
        assert created_at is not None
        assert updated_at is not None
        assert expires_at is not None
        return ReceiptRecord(
            request_id=UUID(cast("str", row["request_id"])),
            content_hash=cast("str", row["content_hash"]),
            state=ReceiptState(cast("str", row["state"])),
            destination_kind=DestinationKind(cast("str", row["destination_kind"])),
            destination_url=cast("str | None", row["destination_url"]),
            created_at=created_at,
            updated_at=updated_at,
            expires_at=expires_at,
            audit_at=_datetime(cast("int | None", row["audit_at"])),
        )

    @staticmethod
    def _select_receipt(connection: sqlite3.Connection, request_id: UUID) -> sqlite3.Row | None:
        return cast(
            "sqlite3.Row | None",
            connection.execute(
                "SELECT * FROM receipts WHERE request_id = ?",
                (str(request_id),),
            ).fetchone(),
        )

    async def claim_receipt(
        self,
        request_id: UUID,
        content_hash: str,
        destination_kind: DestinationKind,
        *,
        now: datetime,
    ) -> ReceiptClaim:
        if not _CONTENT_HASH.fullmatch(content_hash):
            raise ValueError("content_hash must be a lowercase SHA-256 digest")
        async with self._lock:
            return await asyncio.to_thread(
                self._claim_receipt_sync,
                request_id,
                content_hash,
                destination_kind,
                now,
            )

    def _claim_receipt_sync(
        self,
        request_id: UUID,
        content_hash: str,
        destination_kind: DestinationKind,
        now: datetime,
    ) -> ReceiptClaim:
        timestamp = _timestamp(now)
        expires_at = _timestamp(now + _RECEIPT_RETENTION)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._cleanup_expired(connection, timestamp)
            row = self._select_receipt(connection, request_id)
            if row is None:
                connection.execute(
                    """
                    INSERT INTO receipts (
                        request_id, content_hash, state, destination_kind, destination_url,
                        created_at, updated_at, expires_at, audit_at
                    ) VALUES (?, ?, 'pending', ?, NULL, ?, ?, ?, NULL)
                    """,
                    (
                        str(request_id),
                        content_hash,
                        destination_kind.value,
                        timestamp,
                        timestamp,
                        expires_at,
                    ),
                )
                created = self._select_receipt(connection, request_id)
                assert created is not None
                return ReceiptClaim(ClaimOutcome.CREATED, self._record(created))

            record = self._record(row)
            if record.content_hash != content_hash:
                return ReceiptClaim(ClaimOutcome.CONFLICT, record)
            if record.state is ReceiptState.DELIVERED:
                return ReceiptClaim(ClaimOutcome.DELIVERED, record)
            if record.state is ReceiptState.PENDING:
                return ReceiptClaim(ClaimOutcome.PENDING, record)

            connection.execute(
                """
                UPDATE receipts
                SET state = 'pending', destination_kind = ?, destination_url = NULL,
                    created_at = ?, updated_at = ?, expires_at = ?, audit_at = NULL
                WHERE request_id = ?
                """,
                (destination_kind.value, timestamp, timestamp, expires_at, str(request_id)),
            )
            retried = self._select_receipt(connection, request_id)
            assert retried is not None
            return ReceiptClaim(ClaimOutcome.CREATED, self._record(retried))

    async def get_receipt(self, request_id: UUID) -> ReceiptRecord | None:
        async with self._lock:
            return await asyncio.to_thread(self._get_receipt_sync, request_id)

    def _get_receipt_sync(self, request_id: UUID) -> ReceiptRecord | None:
        with self._connect() as connection:
            row = self._select_receipt(connection, request_id)
        return None if row is None else self._record(row)

    async def mark_delivered(
        self,
        request_id: UUID,
        destination_url: str,
        *,
        now: datetime,
    ) -> ReceiptRecord:
        return await self._transition(
            request_id,
            ReceiptState.DELIVERED,
            now=now,
            destination_url=destination_url,
            audit=False,
        )

    async def mark_failed(self, request_id: UUID, *, now: datetime) -> ReceiptRecord:
        return await self._transition(
            request_id,
            ReceiptState.FAILED,
            now=now,
            destination_url=None,
            audit=False,
        )

    async def mark_absent(self, request_id: UUID, *, now: datetime) -> ReceiptRecord:
        return await self._transition(
            request_id,
            ReceiptState.ABSENT,
            now=now,
            destination_url=None,
            audit=True,
        )

    async def resolve_destination(
        self,
        request_id: UUID,
        destination_url: str,
        *,
        now: datetime,
    ) -> ReceiptRecord:
        return await self._transition(
            request_id,
            ReceiptState.DELIVERED,
            now=now,
            destination_url=destination_url,
            audit=True,
        )

    async def _transition(
        self,
        request_id: UUID,
        state: ReceiptState,
        *,
        now: datetime,
        destination_url: str | None,
        audit: bool,
    ) -> ReceiptRecord:
        async with self._lock:
            return await asyncio.to_thread(
                self._transition_sync,
                request_id,
                state,
                now,
                destination_url,
                audit,
            )

    def _transition_sync(
        self,
        request_id: UUID,
        state: ReceiptState,
        now: datetime,
        destination_url: str | None,
        audit: bool,
    ) -> ReceiptRecord:
        timestamp = _timestamp(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select_receipt(connection, request_id)
            if row is None or ReceiptState(cast("str", row["state"])) is not ReceiptState.PENDING:
                raise ReceiptTransitionError(f"receipt {request_id} is not pending")
            connection.execute(
                """
                UPDATE receipts
                SET state = ?, destination_url = ?, updated_at = ?, audit_at = ?
                WHERE request_id = ?
                """,
                (
                    state.value,
                    destination_url,
                    timestamp,
                    timestamp if audit else None,
                    str(request_id),
                ),
            )
            updated = self._select_receipt(connection, request_id)
            assert updated is not None
            return self._record(updated)

    async def expire(self, *, now: datetime) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._expire_sync, now)

    def _expire_sync(self, now: datetime) -> int:
        with self._connect() as connection:
            return self._cleanup_expired(connection, _timestamp(now))

    @staticmethod
    def _cleanup_expired(connection: sqlite3.Connection, timestamp: int) -> int:
        rates = connection.execute(
            "DELETE FROM rate_events WHERE created_at <= ?",
            (timestamp - int(_RATE_RETENTION.total_seconds()),),
        ).rowcount
        contacts = connection.execute(
            "DELETE FROM contacts WHERE expires_at <= ?",
            (timestamp,),
        ).rowcount
        receipts = connection.execute(
            "DELETE FROM receipts WHERE expires_at <= ?",
            (timestamp,),
        ).rowcount
        return rates + contacts + receipts

    def _cipher(self) -> Fernet:
        if self._contact_cipher is None:
            raise RuntimeError("contact encryption is not configured")
        return self._contact_cipher

    async def store_contact(
        self,
        receipt: UUID,
        email: str | None,
        *,
        now: datetime,
    ) -> None:
        if email is None:
            return
        ciphertext = self._cipher().encrypt(email.encode("utf-8"))
        async with self._lock:
            await asyncio.to_thread(self._store_contact_sync, receipt, ciphertext, now)

    def _store_contact_sync(self, receipt: UUID, ciphertext: bytes, now: datetime) -> None:
        created_at = _timestamp(now)
        expires_at = _timestamp(now + _CONTACT_RETENTION)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._cleanup_expired(connection, created_at)
            connection.execute(
                """
                INSERT INTO contacts (receipt, ciphertext, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(receipt) DO UPDATE SET
                    ciphertext = excluded.ciphertext,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                """,
                (str(receipt), ciphertext, created_at, expires_at),
            )

    async def get_contact(self, receipt: UUID, *, now: datetime) -> str | None:
        async with self._lock:
            ciphertext = await asyncio.to_thread(self._get_contact_sync, receipt, now)
        if ciphertext is None:
            return None
        try:
            return self._cipher().decrypt(ciphertext).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise ContactDecryptionError("contact data could not be decrypted") from exc

    def _get_contact_sync(self, receipt: UUID, now: datetime) -> bytes | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._cleanup_expired(connection, _timestamp(now))
            row = connection.execute(
                "SELECT ciphertext FROM contacts WHERE receipt = ?",
                (str(receipt),),
            ).fetchone()
        return None if row is None else cast("bytes", row[0])

    async def delete_contact(self, receipt: UUID) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._delete_contact_sync, receipt)

    def _delete_contact_sync(self, receipt: UUID) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM contacts WHERE receipt = ?",
                (str(receipt),),
            )
            return cursor.rowcount == 1

    def source_digest(self, source: str) -> str:
        """Normalize and hash one network source with the service-only key."""

        if self._source_hash_key is None:
            raise RuntimeError("source hashing is not configured")
        try:
            address = ipaddress.ip_address(source.strip())
        except ValueError as exc:
            raise ValueError("network source must be an IP address") from exc
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            normalized = address.ipv4_mapped.compressed
        else:
            normalized = address.compressed
        return hmac.new(
            self._source_hash_key,
            normalized.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()

    async def check_challenge_quota(self, source: str, *, now: datetime) -> QuotaDecision:
        return await self.check_challenge_quota_hash(self.source_digest(source), now=now)

    async def check_challenge_quota_hash(
        self,
        source_hash: str,
        *,
        now: datetime,
    ) -> QuotaDecision:
        """Claim one challenge slot after the caller has removed the raw source."""

        if not _CONTENT_HASH.fullmatch(source_hash):
            raise ValueError("source_hash must be a lowercase SHA-256 digest")
        async with self._lock:
            return await asyncio.to_thread(
                self._claim_quota_sync,
                "challenge",
                source_hash,
                now,
            )

    async def claim_report_quota(self, source: str, *, now: datetime) -> QuotaDecision:
        return await self.claim_report_quota_hash(self.source_digest(source), now=now)

    async def claim_report_quota_hash(
        self,
        source_hash: str,
        *,
        now: datetime,
    ) -> QuotaDecision:
        """Claim one report slot after the caller has removed the raw source."""

        if not _CONTENT_HASH.fullmatch(source_hash):
            raise ValueError("source_hash must be a lowercase SHA-256 digest")
        async with self._lock:
            return await asyncio.to_thread(
                self._claim_quota_sync,
                "report",
                source_hash,
                now,
            )

    @staticmethod
    def _retry_for_window(
        connection: sqlite3.Connection,
        *,
        event_kind: str,
        cutoff: int,
        limit: int,
        window_seconds: int,
        source_hash: str | None,
    ) -> int | None:
        if source_hash is None:
            rows = connection.execute(
                """
                SELECT created_at FROM rate_events
                WHERE event_kind = ? AND created_at > ?
                ORDER BY created_at
                """,
                (event_kind, cutoff),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT created_at FROM rate_events
                WHERE event_kind = ? AND source_hash = ? AND created_at > ?
                ORDER BY created_at
                """,
                (event_kind, source_hash, cutoff),
            ).fetchall()
        if len(rows) < limit:
            return None
        threshold = cast("int", rows[len(rows) - limit][0])
        return threshold + window_seconds

    def _claim_quota_sync(
        self,
        event_kind: str,
        source_hash: str,
        now: datetime,
    ) -> QuotaDecision:
        timestamp = _timestamp(now)
        hour = 60 * 60
        day = 24 * hour
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._cleanup_expired(connection, timestamp)
            retry_times: list[int] = []
            if event_kind == "challenge":
                retry = self._retry_for_window(
                    connection,
                    event_kind=event_kind,
                    cutoff=timestamp - hour,
                    limit=30,
                    window_seconds=hour,
                    source_hash=source_hash,
                )
                if retry is not None:
                    retry_times.append(retry)
            else:
                for cutoff, limit, window_seconds, scoped_source in (
                    (timestamp - hour, 3, hour, source_hash),
                    (timestamp - day, 10, day, source_hash),
                    (timestamp - hour, 60, hour, None),
                ):
                    retry = self._retry_for_window(
                        connection,
                        event_kind=event_kind,
                        cutoff=cutoff,
                        limit=limit,
                        window_seconds=window_seconds,
                        source_hash=scoped_source,
                    )
                    if retry is not None:
                        retry_times.append(retry)
            if retry_times:
                return QuotaDecision(
                    allowed=False,
                    retry_at=datetime.fromtimestamp(max(retry_times), tz=UTC),
                )
            connection.execute(
                "INSERT INTO rate_events (source_hash, event_kind, created_at) VALUES (?, ?, ?)",
                (source_hash, event_kind, timestamp),
            )
            return QuotaDecision(allowed=True)


__all__ = [
    "ClaimOutcome",
    "ContactDecryptionError",
    "FeedbackStore",
    "QuotaDecision",
    "ReceiptClaim",
    "ReceiptRecord",
    "ReceiptState",
    "ReceiptTransitionError",
]
