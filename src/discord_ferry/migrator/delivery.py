"""Receipt-backed delivery, without selecting work or committing migration progress."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary, WeakValueDictionary

from discord_ferry.errors import (
    DeliveryUnresolvedError,
    MessageSendFailure,
    MigrationError,
    StateError,
)
from discord_ferry.migrator.api import api_fetch_messages, api_send_message
from discord_ferry.state import (
    DeliveryPart,
    DeliveryReceipt,
    DeliveryRef,
    DeliveryToken,
    delivery_receipt_id,
    ensure_delivery_namespace,
    is_stoat_file_id,
    is_stoat_id,
    read_delivery_receipt,
    write_delivery_receipt,
)

if TYPE_CHECKING:
    from pathlib import Path

    import aiohttp

    from discord_ferry.config import FerryConfig
    from discord_ferry.state import FailedMessage, MigrationState

_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
# Only identities prepared by this live session may dispatch without lookup.
# Loading a prepared receipt does not reconstruct that authorization.
_fresh_parts: WeakKeyDictionary[aiohttp.ClientSession, set[tuple[Path, str, str]]] = (
    WeakKeyDictionary()
)


@dataclass(frozen=True)
class DeliveryResult:
    """A confirmed identifier and original media, ready for normal bookkeeping."""

    message_id: str
    autumn_ids: tuple[str, ...]
    token: DeliveryToken
    fully_resolved: bool
    recovered: bool


def delivery_key(namespace: str, ref: DeliveryRef, attempt: int, part_index: int) -> str:
    """Derive a fixed 49-byte key from this migration's structural identity."""
    delivery_receipt_id(ref)  # Validate identity without turning source identifiers into paths.
    if not re.fullmatch(r"[0-9a-f]{32}", namespace) or any(
        type(value) is not int or value < 0 for value in (attempt, part_index)
    ):
        raise ValueError("Invalid delivery key identity")
    identity = [2, namespace, asdict(ref), attempt, part_index]
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode())
    return "ferry-v2-" + digest.hexdigest()[:40]


def retry_delivery_context(
    state: MigrationState, output_dir: Path, ref: DeliveryRef, failure: FailedMessage
) -> tuple[DeliveryRef, tuple[str, ...]]:
    """Validate attempt evidence for an already-selected source, without dispatch."""
    if (failure.discord_msg_id, failure.stoat_channel_id) != (
        ref.source_message_id,
        ref.destination_channel_id,
    ):
        raise DeliveryUnresolvedError("Selected retry does not match its recorded destination")
    recorded = failure.delivery_ref
    if recorded is not None:
        if replace(recorded, generation="base") != replace(ref, generation="base"):
            raise DeliveryUnresolvedError("Selected retry does not match its recorded source")
        if (
            not state.delivery_namespace
            or read_delivery_receipt(state, output_dir, recorded) is None
        ):
            raise DeliveryUnresolvedError("Selected retry has no preserved attempt evidence")
        return recorded, ()
    if state.delivery_namespace and read_delivery_receipt(state, output_dir, ref) is not None:
        return ref, ()
    if ref.family not in {"ordinary", "merged"}:
        raise DeliveryUnresolvedError("Legacy retry has no supported delivery family")
    prefix = "ferry-merge" if ref.family == "merged" else "ferry"
    key = f"{prefix}-{ref.source_message_id}"
    return ref, (key, f"{key}-{ref.destination_channel_id}")


async def _find_messages(
    session: aiohttp.ClientSession,
    config: FerryConfig,
    channel_id: str,
    keys: set[str],
) -> dict[str, dict[str, Any]]:
    """Scan to a valid history end, retaining only exact pending-key matches."""
    found: dict[str, dict[str, Any]] = {}
    before: str | None = None
    boundary_nonce: str | None = None
    while True:
        try:
            rows = await api_fetch_messages(
                session,
                config.stoat_url,
                config.token,
                channel_id,
                limit=100,
                sort="Latest",
                before=before,
            )
        except (MigrationError, TimeoutError) as exc:
            raise DeliveryUnresolvedError("Message history could not establish delivery") from exc
        if len(rows) > 100:
            raise DeliveryUnresolvedError("Message history exceeds the requested page size")
        if not rows:
            return found
        previous = before
        page_seen: dict[str, str | None] = {}
        advanced = False
        for row in rows:
            message_id = row.get("_id")
            nonce = row.get("nonce")
            if (
                not isinstance(message_id, str)
                or not is_stoat_id(message_id)
                or row.get("channel") != channel_id
                or (nonce is not None and not isinstance(nonce, str))
            ):
                raise DeliveryUnresolvedError("Message history contains invalid delivery evidence")
            if message_id in page_seen:
                if page_seen[message_id] != nonce:
                    raise DeliveryUnresolvedError(
                        "Message history contains conflicting identifiers"
                    )
                continue
            page_seen[message_id] = nonce
            if before is not None and message_id == before:
                if advanced or nonce != boundary_nonce:
                    raise DeliveryUnresolvedError("Message history contains an invalid boundary")
                # Tolerate a repeated first boundary, but it cannot advance pagination.
                continue
            if previous is not None and message_id >= previous:
                raise DeliveryUnresolvedError("Message history cursor did not progress")
            previous = message_id
            advanced = True
            if nonce in keys:
                if nonce in found and found[nonce]["_id"] != message_id:
                    raise DeliveryUnresolvedError("Message delivery has multiple history matches")
                found[nonce] = row
        if not advanced or previous is None:
            raise DeliveryUnresolvedError("Message history cursor did not progress")
        if len(rows) < 100:
            return found
        before = previous
        boundary_nonce = page_seen[previous]


def _history_media(row: dict[str, Any]) -> list[str]:
    media = row.get("attachments", [])
    if not isinstance(media, list) or any(
        not isinstance(item, dict) or not is_stoat_file_id(item.get("_id")) for item in media
    ):
        raise DeliveryUnresolvedError("Legacy message contains invalid media evidence")
    result = [item["_id"] for item in media]
    embeds = row.get("embeds", [])
    if not isinstance(embeds, list) or any(not isinstance(item, dict) for item in embeds):
        raise DeliveryUnresolvedError("Legacy message contains invalid embed evidence")
    for embed in embeds:
        if embed.get("type") != "Text" or embed.get("media") is None:
            continue
        original_media = embed["media"]
        if not isinstance(original_media, dict) or not is_stoat_file_id(original_media.get("_id")):
            raise DeliveryUnresolvedError("Legacy message contains invalid embed media evidence")
        result.append(original_media["_id"])
    return result


def _result(
    state: MigrationState,
    config: FerryConfig,
    receipt: DeliveryReceipt,
    part_index: int,
    *,
    recovered: bool,
) -> DeliveryResult:
    token = write_delivery_receipt(state, config.output_dir, receipt)
    part = receipt.parts[part_index]
    return DeliveryResult(
        part.message_id,
        tuple(part.autumn_ids),
        token,
        all(item.status == "acknowledged" for item in receipt.parts),
        recovered,
    )


async def deliver_part(
    *,
    session: aiohttp.ClientSession,
    config: FerryConfig,
    state: MigrationState,
    ref: DeliveryRef,
    part_index: int,
    part_count: int,
    render_digest: str,
    content: str,
    attachments: list[str] | None = None,
    embeds: list[dict[str, Any]] | None = None,
    masquerade: dict[str, str | None] | None = None,
    replies: list[dict[str, Any]] | None = None,
    allow_rejected_retry: bool = False,
    legacy_keys: tuple[str, ...] = (),
    recover_only: bool = False,
) -> DeliveryResult:
    """Deliver one already-selected part or return its verified saved identifier."""
    if config.dry_run or state.is_dry_run:
        raise ValueError("Dry runs cannot dispatch message delivery")
    if (
        type(part_count) is not int
        or type(part_index) is not int
        or not 0 <= part_index < part_count
        or not re.fullmatch(r"[0-9a-f]{64}", render_digest)
    ):
        raise ValueError("Invalid delivery part shape")
    ensure_delivery_namespace(state, config.output_dir, config.stoat_url)
    output = config.output_dir.resolve()
    receipt_id = delivery_receipt_id(ref)
    lock_id = f"{output}:{state.delivery_namespace}:{receipt_id}"
    lock = _locks.get(lock_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[lock_id] = lock
    async with lock:
        fresh = _fresh_parts.setdefault(session, set())
        receipt = read_delivery_receipt(state, config.output_dir, ref)
        if receipt is not None and any(
            item.key != delivery_key(state.delivery_namespace, ref, item.attempt, item.index)
            for item in receipt.parts
        ):
            raise StateError("Delivery key does not match the saved attempt identity")
        legacy_rows: list[dict[str, Any]] | None = None
        if legacy_keys and (
            receipt is None or any(item.status != "acknowledged" for item in receipt.parts)
        ):
            if receipt is not None and (
                receipt.render_digest != render_digest or len(receipt.parts) != part_count
            ):
                raise DeliveryUnresolvedError("Legacy render conflicts with preserved delivery")
            groups = [
                tuple(key if part_count == 1 else f"{key}_p{index + 1}" for key in legacy_keys)
                for index in range(part_count)
            ]
            matches = await _find_messages(
                session,
                config,
                ref.destination_channel_id,
                {key for group in groups for key in group},
            )
            matched_bases = {
                key
                for key in legacy_keys
                if any(
                    (key if part_count == 1 else f"{key}_p{index + 1}") in matches
                    for index in range(part_count)
                )
            }
            if len(matched_bases) != 1:
                raise DeliveryUnresolvedError("Legacy delivery has no unique complete attempt")
            legacy_rows = []
            for index, group in enumerate(groups):
                candidates = {matches[key]["_id"]: matches[key] for key in group if key in matches}
                if len(candidates) != 1:
                    raise DeliveryUnresolvedError(
                        "Legacy delivery lacks a unique match for every part"
                    )
                row = next(iter(candidates.values()))
                if receipt is not None and (
                    receipt.parts[index].status == "acknowledged"
                    and receipt.parts[index].message_id != row["_id"]
                ):
                    raise DeliveryUnresolvedError(
                        "Legacy delivery conflicts with an acknowledged part"
                    )
                legacy_rows.append(row)
            if len({row["_id"] for row in legacy_rows}) != part_count:
                raise DeliveryUnresolvedError(
                    "Legacy delivery reuses one identifier for multiple parts"
                )
        if receipt is None:
            if recover_only:
                raise DeliveryUnresolvedError("Selected recovery has no preserved delivery")
            receipt = DeliveryReceipt(
                1,
                state.delivery_namespace,
                state.delivery_service_digest,
                ref,
                render_digest,
                [
                    DeliveryPart(index, delivery_key(state.delivery_namespace, ref, 0, index))
                    for index in range(part_count)
                ],
            )
            if legacy_rows is None:
                write_delivery_receipt(state, config.output_dir, receipt)
                fresh.update((output, state.delivery_namespace, item.key) for item in receipt.parts)
        if legacy_rows is not None:
            legacy_media_by_part = [_history_media(row) for row in legacy_rows]
            for part, row, original_media in zip(
                receipt.parts, legacy_rows, legacy_media_by_part, strict=True
            ):
                part.status = "acknowledged"
                part.message_id = row["_id"]
                part.autumn_ids = original_media
            return _result(state, config, receipt, part_index, recovered=True)
        if receipt.render_digest != render_digest or len(receipt.parts) != part_count:
            if (
                recover_only
                or not allow_rejected_retry
                or any(item.status != "confirmed_rejected" for item in receipt.parts)
            ):
                raise DeliveryUnresolvedError(
                    "Message render differs from preserved delivery evidence"
                )
            attempt = max(item.attempt for item in receipt.parts) + 1
            receipt.render_digest = render_digest
            receipt.parts = [
                DeliveryPart(
                    index, delivery_key(state.delivery_namespace, ref, attempt, index), attempt
                )
                for index in range(part_count)
            ]
            write_delivery_receipt(state, config.output_dir, receipt)
            fresh.update((output, state.delivery_namespace, item.key) for item in receipt.parts)
        part = receipt.parts[part_index]
        if part.status == "acknowledged":
            return _result(state, config, receipt, part_index, recovered=True)
        if part.status == "confirmed_rejected":
            if not allow_rejected_retry or recover_only:
                raise MessageSendFailure(
                    "Message remains rejected until selected recovery authorizes retry",
                    disposition="confirmed_rejected",
                )
            part.attempt += 1
            part.key = delivery_key(state.delivery_namespace, ref, part.attempt, part_index)
            part.status = "prepared"
            write_delivery_receipt(state, config.output_dir, receipt)
            fresh.add((output, state.delivery_namespace, part.key))

        async def recover(failure: MessageSendFailure) -> dict[str, Any]:
            pending = {
                item.key for item in receipt.parts if item.status in {"uncertain", "prepared"}
            }
            matches = await _find_messages(
                session,
                config,
                ref.destination_channel_id,
                pending,
            )
            for item in receipt.parts:
                if item.status in {"uncertain", "prepared"} and item.key in matches:
                    item.message_id = matches[item.key]["_id"]
                    item.status = "acknowledged"
            write_delivery_receipt(state, config.output_dir, receipt)
            if part.status != "acknowledged":
                raise DeliveryUnresolvedError("Message delivery has no unique history match")
            return {"_id": part.message_id}

        capability = (output, state.delivery_namespace, part.key)
        if recover_only or part.status == "uncertain" or capability not in fresh:
            await recover(
                MessageSendFailure("Preserved delivery requires lookup", disposition="uncertain")
            )
            return _result(state, config, receipt, part_index, recovered=True)

        part.status = "uncertain"
        part.autumn_ids = list(attachments or [])
        for embed in embeds or []:
            media = embed.get("media")
            if isinstance(media, str) and is_stoat_file_id(media):
                part.autumn_ids.append(media)
        write_delivery_receipt(state, config.output_dir, receipt)
        fresh.discard(capability)
        recovered = False

        async def recover_dispatch(failure: MessageSendFailure) -> dict[str, Any]:
            nonlocal recovered
            recovered = True
            return await recover(failure)

        try:
            result = await api_send_message(
                session,
                config.stoat_url,
                config.token,
                ref.destination_channel_id,
                content=content,
                attachments=attachments,
                embeds=embeds,
                masquerade=masquerade,
                replies=replies,
                idempotency_key=part.key,
                message_recovery=recover_dispatch,
            )
        except MessageSendFailure as failure:
            if failure.disposition == "confirmed_rejected":
                part.status = "confirmed_rejected"
                write_delivery_receipt(state, config.output_dir, receipt)
            raise
        part.message_id = result["_id"]
        part.status = "acknowledged"
        return _result(state, config, receipt, part_index, recovered=recovered)
