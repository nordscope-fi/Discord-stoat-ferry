"""Receipt-backed message delivery behavior."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
from typing import TYPE_CHECKING, Literal

import aiohttp
import pytest
from aioresponses import CallbackResult, aioresponses

from discord_ferry.config import FerryConfig
from discord_ferry.errors import DeliveryUnresolvedError, StateError
from discord_ferry.state import (
    DeliveryPart,
    DeliveryReceipt,
    DeliveryRef,
    MigrationState,
    ensure_delivery_namespace,
    read_delivery_receipt,
    write_delivery_receipt,
)
from tests.stoat_ids import stoat_id

if TYPE_CHECKING:
    from pathlib import Path

    from discord_ferry.parser.models import DCEExport


async def seed_rejected_messages(
    config: FerryConfig,
    state: MigrationState,
    exports: list[DCEExport],
    *,
    family: Literal["ordinary", "merged"] = "ordinary",
) -> None:
    """Arrange real pre-creation rejections for tests that expect a selected retry."""
    from dataclasses import replace

    from discord_ferry.errors import MessageSendFailure
    from discord_ferry.migrator import messages
    from discord_ferry.parser.dce_parser import stream_messages

    original = list(state.failed_messages)
    seeded = {}
    source_messages = [
        (export, msg)
        for export in exports
        for msg in (
            stream_messages(export.json_path) if export.json_path is not None else export.messages
        )
    ]
    with aioresponses() as http:
        for destination in {failure.stoat_channel_id for failure in original}:
            http.post(
                f"{config.stoat_url}/channels/{destination}/messages",
                status=403,
                payload={"type": "MissingPermission", "permission": "SendMessage"},
                repeat=True,
            )
        async with aiohttp.ClientSession() as session:
            for failure in original:
                choices = [
                    (export, msg)
                    for export, msg in source_messages
                    if msg.id == failure.discord_msg_id
                    and (
                        (
                            family == "ordinary"
                            and state.channel_map.get(export.channel.id) == failure.stoat_channel_id
                        )
                        or (family == "merged" and export.is_thread)
                    )
                ]
                if not choices:
                    continue
                assert len(choices) == 1
                export, msg = choices[0]
                before = len(state.failed_messages)
                if family == "ordinary":
                    with pytest.raises(MessageSendFailure):
                        await messages._process_message(
                            msg=msg,
                            stoat_channel_id=failure.stoat_channel_id,
                            config=config,
                            state=state,
                            session=session,
                            on_event=lambda event: None,
                            export_channel_id=export.channel.id,
                        )
                else:
                    assert not await messages._process_merged_message(
                        export=export,
                        msg=msg,
                        stoat_channel_id=failure.stoat_channel_id,
                        config=config,
                        state=state,
                        session=session,
                        on_event=lambda event: None,
                    )
                assert len(state.failed_messages) == before + 1
                recorded = state.failed_messages[-1]
                assert recorded.delivery_ref is not None
                receipt = read_delivery_receipt(state, config.output_dir, recorded.delivery_ref)
                assert receipt is not None
                assert all(part.status == "confirmed_rejected" for part in receipt.parts)
                seeded[id(failure)] = replace(
                    failure,
                    delivery_ref=recorded.delivery_ref,
                    error=recorded.error,
                    content_preview=recorded.content_preview,
                )
    state.failed_messages = [seeded.get(id(failure), failure) for failure in original]


def test_delivery_key_is_fixed_length() -> None:
    assert importlib.util.find_spec("discord_ferry.migrator.delivery") is not None, (
        "Receipt-backed delivery must define its key protocol"
    )
    module = importlib.import_module("discord_ferry.migrator.delivery")
    ref = DeliveryRef("source_channel", "source_message", stoat_id(1), "ordinary")
    key = module.delivery_key("a" * 32, ref, 0, 0)
    assert key == module.delivery_key("a" * 32, ref, 0, 0)
    assert key.startswith("ferry-v2-")
    assert len(key.encode("ascii")) == 49
    assert key != module.delivery_key("b" * 32, ref, 0, 0)
    assert key != module.delivery_key("a" * 32, ref, 1, 0)
    assert key != module.delivery_key("a" * 32, ref, 0, 1)


def _delivery_fixture(tmp_path: Path, *, status: str = "uncertain"):
    assert importlib.util.find_spec("discord_ferry.migrator.delivery") is not None, (
        "Receipt-backed delivery must define its recovery protocol"
    )
    module = importlib.import_module("discord_ferry.migrator.delivery")
    config = FerryConfig(
        export_dir=tmp_path,
        output_dir=tmp_path,
        stoat_url="https://api.test",
        token="test-token",
    )
    state = MigrationState()
    ensure_delivery_namespace(state, tmp_path, config.stoat_url)
    ref = DeliveryRef("123", "456", stoat_id(1), "ordinary")
    receipt = DeliveryReceipt(
        1,
        state.delivery_namespace,
        state.delivery_service_digest,
        ref,
        "c" * 64,
        [
            DeliveryPart(
                0,
                module.delivery_key(state.delivery_namespace, ref, 0, 0),
                status=status,
                message_id=stoat_id(7) if status == "acknowledged" else "",
                autumn_ids=["original_autumn"],
            )
        ],
    )
    write_delivery_receipt(state, tmp_path, receipt)
    return module, config, state, ref, receipt


async def test_delivery_receipt_acknowledgement_uses_original_media_without_http(tmp_path: Path):
    module, config, state, ref, receipt = _delivery_fixture(tmp_path, status="acknowledged")
    with aioresponses() as http:
        async with aiohttp.ClientSession() as session:
            result = await module.deliver_part(
                session=session,
                config=config,
                state=state,
                ref=ref,
                part_index=0,
                part_count=1,
                render_digest="c" * 64,
                content="same render",
                attachments=["unused_new_upload"],
            )
        assert not http.requests
    assert result.message_id == stoat_id(7)
    assert list(result.autumn_ids) == ["original_autumn"]
    assert read_delivery_receipt(state, tmp_path, ref) == receipt


@pytest.mark.parametrize("duplicate", [False, True])
async def test_delivery_receipt_scans_later_history_before_accepting_unique_match(
    tmp_path: Path,
    duplicate: bool,
):
    module, config, state, ref, receipt = _delivery_fixture(tmp_path)
    key = receipt.parts[0].key
    first_page = [
        {"_id": stoat_id(index), "channel": ref.destination_channel_id, "nonce": f"other-{index}"}
        for index in range(500, 400, -1)
    ]
    if duplicate:
        first_page[0]["nonce"] = key
    second_page = [{"_id": stoat_id(400), "channel": ref.destination_channel_id, "nonce": key}]
    url = f"{config.stoat_url}/channels/{ref.destination_channel_id}/messages?limit=100&sort=Latest"
    with aioresponses() as http:
        http.get(url, payload=first_page)
        http.get(f"{url}&before={stoat_id(401)}", payload=second_page)
        async with aiohttp.ClientSession() as session:
            call = module.deliver_part(
                session=session,
                config=config,
                state=state,
                ref=ref,
                part_index=0,
                part_count=1,
                render_digest="c" * 64,
                content="same render",
            )
            if duplicate:
                with pytest.raises(DeliveryUnresolvedError):
                    await call
            else:
                result = await call
                assert result.message_id == stoat_id(400)
                assert list(result.autumn_ids) == ["original_autumn"]
        assert [method for method, _ in http.requests] == ["GET", "GET"]
    saved = read_delivery_receipt(state, tmp_path, ref)
    assert saved is not None
    assert saved.parts[0].status == ("uncertain" if duplicate else "acknowledged")


@pytest.mark.parametrize(
    "case",
    ["empty", "wrong_channel", "invalid_id", "permission", "cursor", "boundary", "oversized"],
)
async def test_delivery_receipt_invalid_or_missing_history_never_authorizes_send(
    tmp_path: Path,
    case: str,
):
    module, config, state, ref, receipt = _delivery_fixture(tmp_path)
    row = {"_id": stoat_id(7), "channel": ref.destination_channel_id, "nonce": receipt.parts[0].key}
    if case == "wrong_channel":
        row["channel"] = stoat_id(99)
    if case == "invalid_id":
        row["_id"] = "invalid"
    url = f"{config.stoat_url}/channels/{ref.destination_channel_id}/messages?limit=100&sort=Latest"
    with aioresponses() as http:
        if case == "permission":
            http.get(url, status=403, payload={"type": "MissingPermission"})
        elif case in {"cursor", "boundary"}:
            first = [
                {"_id": stoat_id(i), "channel": ref.destination_channel_id, "nonce": None}
                for i in range(200, 100, -1)
            ]
            http.get(url, payload=first)
            second = (
                first
                if case == "cursor"
                else [
                    {
                        "_id": stoat_id(100),
                        "channel": ref.destination_channel_id,
                        "nonce": receipt.parts[0].key,
                    },
                    first[-1],
                ]
            )
            http.get(f"{url}&before={stoat_id(101)}", payload=second)
        elif case == "oversized":
            oversized = [
                {"_id": stoat_id(i), "channel": ref.destination_channel_id, "nonce": None}
                for i in range(500, 399, -1)
            ]
            oversized[0]["nonce"] = receipt.parts[0].key
            http.get(url, payload=oversized)
            http.get(f"{url}&before={stoat_id(400)}", payload=[])
        else:
            http.get(url, payload=[] if case == "empty" else [row])
        async with aiohttp.ClientSession() as session:
            with pytest.raises(DeliveryUnresolvedError):
                await module.deliver_part(
                    session=session,
                    config=config,
                    state=state,
                    ref=ref,
                    part_index=0,
                    part_count=1,
                    render_digest="c" * 64,
                    content="same render",
                )
        assert all(method == "GET" for method, _ in http.requests)
    assert read_delivery_receipt(state, tmp_path, ref) == receipt


async def test_delivery_receipt_fresh_multipart_prepares_every_part_without_replaying(
    tmp_path: Path,
):
    assert importlib.util.find_spec("discord_ferry.migrator.delivery") is not None, (
        "Receipt-backed delivery must define its multipart protocol"
    )
    module = importlib.import_module("discord_ferry.migrator.delivery")
    config = FerryConfig(
        export_dir=tmp_path,
        output_dir=tmp_path,
        stoat_url="https://api.test",
        token="test-token",
    )
    state = MigrationState()
    ref = DeliveryRef("123", "456", stoat_id(1), "ordinary")
    posts = []

    async def create(url, **kwargs):
        posts.append(kwargs)
        return CallbackResult(payload={"_id": stoat_id(10 + len(posts))})

    with aioresponses() as http:
        http.post(
            f"{config.stoat_url}/channels/{ref.destination_channel_id}/messages",
            callback=create,
            repeat=True,
        )
        async with aiohttp.ClientSession() as session:
            results = [
                await module.deliver_part(
                    session=session,
                    config=config,
                    state=state,
                    ref=ref,
                    part_index=i,
                    part_count=2,
                    render_digest="c" * 64,
                    content=f"part {i}",
                )
                for i in range(2)
            ]
    assert [result.message_id for result in results] == [stoat_id(11), stoat_id(12)]
    assert len({post["headers"]["Idempotency-Key"] for post in posts}) == 2
    receipt = read_delivery_receipt(state, tmp_path, ref)
    assert receipt is not None
    assert all(part.status == "acknowledged" for part in receipt.parts)


async def test_delivery_receipt_rejects_key_not_bound_to_attempt_before_http(tmp_path: Path):
    module, config, state, ref, receipt = _delivery_fixture(tmp_path)
    receipt.parts[0].key = "ferry-v2-" + "f" * 40
    write_delivery_receipt(state, tmp_path, receipt)
    with aioresponses() as http:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(StateError, match="key"):
                await module.deliver_part(
                    session=session,
                    config=config,
                    state=state,
                    ref=ref,
                    part_index=0,
                    part_count=1,
                    render_digest="c" * 64,
                    content="same render",
                )
        assert not http.requests


async def test_delivery_receipt_legacy_match_retains_remote_media_not_new_uploads(tmp_path: Path):
    module, config, state, ref, receipt = _delivery_fixture(tmp_path, status="prepared")
    receipt.parts[0].autumn_ids = []
    write_delivery_receipt(state, tmp_path, receipt)
    url = f"{config.stoat_url}/channels/{ref.destination_channel_id}/messages?limit=100&sort=Latest"
    with aioresponses() as http:
        http.get(
            url,
            payload=[
                {
                    "_id": stoat_id(8),
                    "channel": ref.destination_channel_id,
                    "nonce": "ferry-456",
                    "attachments": [{"_id": "legacy_original"}],
                }
            ],
        )
        async with aiohttp.ClientSession() as session:
            result = await module.deliver_part(
                session=session,
                config=config,
                state=state,
                ref=ref,
                part_index=0,
                part_count=1,
                render_digest="c" * 64,
                content="same render",
                attachments=["unused_new_upload"],
                legacy_keys=("ferry-456",),
            )
        assert all(method == "GET" for method, _ in http.requests)
    assert result.message_id == stoat_id(8)
    assert list(result.autumn_ids) == ["legacy_original"]


@pytest.mark.parametrize("case", ["complete", "missing", "ambiguous", "mixed_generations"])
async def test_legacy_delivery_converts_only_complete_unambiguous_multipart(
    tmp_path: Path, case: str
):
    """Legacy parts become one acknowledged receipt, or remain unconverted without POST."""
    module = importlib.import_module("discord_ferry.migrator.delivery")
    config = FerryConfig(
        export_dir=tmp_path, output_dir=tmp_path, stoat_url="https://api.test", token="tok"
    )
    state = MigrationState()
    ensure_delivery_namespace(state, tmp_path, config.stoat_url)
    ref = DeliveryRef("123", "456", stoat_id(1), "ordinary")
    rows = [
        {
            "_id": stoat_id(80),
            "channel": ref.destination_channel_id,
            "nonce": "ferry-456_p1",
            "content": "first part",
            "attachments": [{"_id": "original_attachment", "filename": "private-name.png"}],
            "embeds": [{"type": "Text", "media": {"_id": "original_embed"}}],
        },
    ]
    if case != "missing":
        rows.insert(
            0,
            {
                "_id": stoat_id(81),
                "channel": ref.destination_channel_id,
                "nonce": (
                    f"ferry-456-{ref.destination_channel_id}_p2"
                    if case == "mixed_generations"
                    else "ferry-456_p2"
                ),
                "content": "second part",
            },
        )
    if case == "ambiguous":
        rows.insert(
            0,
            {
                "_id": stoat_id(82),
                "channel": ref.destination_channel_id,
                "nonce": "ferry-456_p1",
                "content": "another first part",
            },
        )
    url = f"{config.stoat_url}/channels/{ref.destination_channel_id}/messages?limit=100&sort=Latest"
    with aioresponses() as http:
        http.get(url, payload=rows)
        async with aiohttp.ClientSession() as session:
            call = module.deliver_part(
                session=session,
                config=config,
                state=state,
                ref=ref,
                part_index=0,
                part_count=2,
                render_digest="c" * 64,
                content="first part",
                attachments=["unused_upload"],
                legacy_keys=("ferry-456", f"ferry-456-{ref.destination_channel_id}"),
            )
            if case == "complete":
                result = await call
                assert result.message_id == stoat_id(80)
                assert result.fully_resolved
                assert set(result.autumn_ids) == {"original_attachment", "original_embed"}
                saved = read_delivery_receipt(state, tmp_path, ref)
                assert saved is not None
                assert [part.message_id for part in saved.parts] == [stoat_id(80), stoat_id(81)]
                assert all(part.status == "acknowledged" for part in saved.parts)
            else:
                with pytest.raises(DeliveryUnresolvedError):
                    await call
                assert read_delivery_receipt(state, tmp_path, ref) is None
        assert [method for method, _ in http.requests] == ["GET"]


async def test_delivery_receipt_corrected_rejection_mints_one_selected_attempt(tmp_path: Path):
    assert importlib.util.find_spec("discord_ferry.migrator.delivery") is not None
    module = importlib.import_module("discord_ferry.migrator.delivery")
    config = FerryConfig(
        export_dir=tmp_path,
        output_dir=tmp_path,
        stoat_url="https://api.test",
        token="test-token",
    )
    state = MigrationState()
    ref = DeliveryRef("123", "456", stoat_id(1), "ordinary")
    keys = []
    created = []

    async def create(url, **kwargs):
        key = kwargs["headers"]["Idempotency-Key"]
        if key in keys:
            return CallbackResult(status=409, payload={"type": "DuplicateNonce"})
        keys.append(key)
        if len(keys) == 1:
            return CallbackResult(
                status=403,
                payload={"type": "MissingPermission", "permission": "SendMessage"},
            )
        created.append({"_id": stoat_id(7)})
        return CallbackResult(payload=created[-1])

    with aioresponses() as http:
        http.post(
            f"{config.stoat_url}/channels/{ref.destination_channel_id}/messages",
            callback=create,
            repeat=True,
        )
        async with aiohttp.ClientSession() as session:
            with pytest.raises(module.MessageSendFailure):
                await module.deliver_part(
                    session=session,
                    config=config,
                    state=state,
                    ref=ref,
                    part_index=0,
                    part_count=1,
                    render_digest="c" * 64,
                    content="rejected",
                )
            receipt = read_delivery_receipt(state, tmp_path, ref)
            assert receipt is not None
            assert receipt.parts[0].status == "confirmed_rejected"
            result = await module.deliver_part(
                session=session,
                config=config,
                state=state,
                ref=ref,
                part_index=0,
                part_count=1,
                render_digest="d" * 64,
                content="corrected",
                allow_rejected_retry=True,
            )
    assert result.message_id == stoat_id(7)
    assert result.fully_resolved
    assert len(keys) == len(set(keys)) == 2
    assert created == [{"_id": stoat_id(7)}]
    assert not state.message_map
    assert not state.pending_pins
    assert not state.channel_message_counts
    receipt = read_delivery_receipt(state, tmp_path, ref)
    assert receipt is not None
    assert receipt.parts[0].attempt == 1


async def test_delivery_receipt_concurrent_same_reference_creates_once(tmp_path: Path):
    assert importlib.util.find_spec("discord_ferry.migrator.delivery") is not None
    module = importlib.import_module("discord_ferry.migrator.delivery")
    config = FerryConfig(
        export_dir=tmp_path,
        output_dir=tmp_path,
        stoat_url="https://api.test",
        token="test-token",
    )
    state = MigrationState()
    ref = DeliveryRef("123", "456", stoat_id(1), "ordinary")
    created = []

    async def create(url, **kwargs):
        await asyncio.sleep(0.01)
        created.append({"_id": stoat_id(7)})
        return CallbackResult(payload=created[-1])

    with aioresponses() as http:
        http.post(
            f"{config.stoat_url}/channels/{ref.destination_channel_id}/messages",
            callback=create,
            repeat=True,
        )
        async with aiohttp.ClientSession() as session:
            results = await asyncio.gather(
                *(
                    module.deliver_part(
                        session=session,
                        config=config,
                        state=state,
                        ref=ref,
                        part_index=0,
                        part_count=1,
                        render_digest="c" * 64,
                        content="one",
                    )
                    for _ in range(3)
                )
            )
    assert [result.message_id for result in results] == [stoat_id(7)] * 3
    assert created == [{"_id": stoat_id(7)}]


async def test_delivery_receipt_failed_ack_write_recovers_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    assert importlib.util.find_spec("discord_ferry.migrator.delivery") is not None
    module = importlib.import_module("discord_ferry.migrator.delivery")
    config = FerryConfig(
        export_dir=tmp_path,
        output_dir=tmp_path,
        stoat_url="https://api.test",
        token="test-token",
    )
    state = MigrationState()
    ref = DeliveryRef("123", "456", stoat_id(1), "ordinary")
    remote = []
    writer = module.write_delivery_receipt
    fault = True

    def write_with_ack_fault(state, output_dir, receipt):
        nonlocal fault
        if fault and receipt.parts[0].status == "acknowledged":
            fault = False
            raise StateError("ack fault")
        return writer(state, output_dir, receipt)

    monkeypatch.setattr(module, "write_delivery_receipt", write_with_ack_fault)

    async def create(url, **kwargs):
        remote.append(
            {
                "_id": stoat_id(7),
                "channel": ref.destination_channel_id,
                "nonce": kwargs["headers"]["Idempotency-Key"],
            }
        )
        return CallbackResult(payload=remote[-1])

    async def history(url, **kwargs):
        return CallbackResult(payload=remote)

    with aioresponses() as http:
        http.post(
            f"{config.stoat_url}/channels/{ref.destination_channel_id}/messages",
            callback=create,
            repeat=True,
        )
        http.get(
            f"{config.stoat_url}/channels/{ref.destination_channel_id}/messages?limit=100&sort=Latest",
            callback=history,
        )
        async with aiohttp.ClientSession() as session:
            with pytest.raises(StateError, match="ack fault"):
                await module.deliver_part(
                    session=session,
                    config=config,
                    state=state,
                    ref=ref,
                    part_index=0,
                    part_count=1,
                    render_digest="c" * 64,
                    content="one",
                    attachments=["original_autumn"],
                )
        saved = read_delivery_receipt(state, tmp_path, ref)
        assert saved is not None
        assert saved.parts[0].status == "uncertain"
        from discord_ferry.state import load_state

        restarted = load_state(tmp_path)
        async with aiohttp.ClientSession() as session:
            result = await module.deliver_part(
                session=session,
                config=config,
                state=restarted,
                ref=ref,
                part_index=0,
                part_count=1,
                render_digest="c" * 64,
                content="one",
                attachments=["new_unused_autumn"],
            )
    assert len(remote) == 1
    assert result.message_id == stoat_id(7)
    assert list(result.autumn_ids) == ["original_autumn"]


async def test_delivery_receipt_preserves_outbound_embed_media(tmp_path: Path):
    module, config, state, _, _ = _delivery_fixture(tmp_path)
    ref = DeliveryRef("123", "457", stoat_id(1), "ordinary")
    with aioresponses() as http:
        http.post(
            f"{config.stoat_url}/channels/{ref.destination_channel_id}/messages",
            payload={"_id": stoat_id(9)},
        )
        async with aiohttp.ClientSession() as session:
            result = await module.deliver_part(
                session=session,
                config=config,
                state=state,
                ref=ref,
                part_index=0,
                part_count=1,
                render_digest="c" * 64,
                content="one",
                attachments=["original_attachment"],
                embeds=[{"type": "Text", "media": "original_embed"}],
            )
    assert list(result.autumn_ids) == ["original_attachment", "original_embed"]
