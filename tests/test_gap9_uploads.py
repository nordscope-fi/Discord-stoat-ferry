"""Regression coverage for dependency-gap upload handling (#1187, #1209)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aioresponses import CallbackResult, aioresponses

from discord_ferry.config import FerryConfig
from discord_ferry.core import engine as engine_module
from discord_ferry.core.engine import run_retry_failed
from discord_ferry.discord.client import download_role_icon
from discord_ferry.errors import AutumnUploadError, MigrationError
from discord_ferry.migrator.avatars import _download_remote_avatar
from discord_ferry.migrator.emoji import upload_and_create_emoji
from discord_ferry.migrator.messages import _upload_attachments
from discord_ferry.migrator.structure import _download_banner, run_server
from discord_ferry.migrator.verify import CheckReport
from discord_ferry.parser.models import (
    DCEAttachment,
    DCEAuthor,
    DCEChannel,
    DCEExport,
    DCEGuild,
    DCEMessage,
)
from discord_ferry.state import FailedMessage, MigrationState
from discord_ferry.uploader import autumn as autumn_module
from tests.test_delivery import (
    publication_permission_collaborator as publication_permission_collaborator,
)
from tests.test_delivery import seed_rejected_messages
from tests.test_engine import repair_permission_unit_boundary as repair_permission_unit_boundary
from tests.test_structure import structure_binding_collaborator as structure_binding_collaborator

if TYPE_CHECKING:
    from pathlib import Path

STOAT_URL = "https://stoat.test"
AUTUMN_URL = "https://autumn.test"
TOKEN = "test-token"
_VALID_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360606060000000050001a5f645400000000049454e44ae426082"
)
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


@pytest.fixture(autouse=True)
def _reset_upload_limits() -> Any:
    autumn_module.configure_upload_limits({}, None)
    yield
    autumn_module.configure_upload_limits({}, None)


def _ulid_at(moment: datetime) -> str:
    value = int(moment.timestamp() * 1000) << 80
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def _root_limits(*, new: int, default: int, new_user_hours: int = 72) -> dict[str, Any]:
    return {
        "features": {
            "limits": {
                "global": {"new_user_hours": new_user_hours},
                "new_user": {"file_upload_size_limits": {"attachments": new}},
                "default": {"file_upload_size_limits": {"attachments": default}},
            }
        }
    }


def _config(tmp_path: Path) -> FerryConfig:
    return FerryConfig(
        export_dir=tmp_path,
        output_dir=tmp_path,
        stoat_url=STOAT_URL,
        token=TOKEN,
        upload_delay=0.0,
    )


def _state() -> MigrationState:
    return MigrationState(stoat_server_id="srv1", autumn_url=AUTUMN_URL)


def test_upload_limits_select_same_new_user_tier_as_stoat() -> None:
    now = datetime(2026, 10, 13, tzinfo=UTC)

    tier = autumn_module.configure_upload_limits(
        _root_limits(new=123, default=456),
        _ulid_at(now - timedelta(hours=1)),
        now=now,
    )

    assert tier == "new_user"
    assert autumn_module.get_tag_size_limit("attachments") == 123


def test_upload_limits_select_default_for_old_or_unknown_user() -> None:
    now = datetime(2026, 10, 13, tzinfo=UTC)
    root = _root_limits(new=123, default=456)

    assert (
        autumn_module.configure_upload_limits(root, _ulid_at(now - timedelta(days=4)), now=now)
        == "default"
    )
    assert autumn_module.get_tag_size_limit("attachments") == 456
    assert autumn_module.configure_upload_limits(root, "not-a-ulid", now=now) == "default"
    assert autumn_module.get_tag_size_limit("attachments") == 456
    invalid_suffix = f"{_ulid_at(now - timedelta(hours=1))[:-1]}!"
    assert autumn_module.configure_upload_limits(root, invalid_suffix, now=now) == "default"
    assert autumn_module.get_tag_size_limit("attachments") == 456
    assert (
        autumn_module.configure_upload_limits(root, _ulid_at(now + timedelta(hours=1)), now=now)
        == "default"
    )


def test_upload_limits_fall_back_tag_by_tag() -> None:
    autumn_module.configure_upload_limits(
        {"features": {"limits": {"default": {"file_upload_size_limits": {"attachments": 321}}}}},
        None,
    )

    assert autumn_module.get_tag_size_limit("attachments") == 321
    assert autumn_module.get_tag_size_limit("avatars") == 4_000_000
    assert dict(autumn_module.TAG_SIZE_LIMITS)["attachments"] == 321

    autumn_module.configure_upload_limits({}, None)
    assert autumn_module.get_tag_size_limit("attachments") == 20_000_000


async def test_upload_limits_are_inherited_by_child_tasks_and_reset_per_run() -> None:
    autumn_module.configure_upload_limits(_root_limits(new=123, default=456), None)

    child_limit = await asyncio.create_task(
        asyncio.to_thread(autumn_module.get_tag_size_limit, "attachments")
    )

    assert child_limit == 456
    autumn_module.configure_upload_limits({}, None)
    assert autumn_module.get_tag_size_limit("attachments") == 20_000_000


async def test_engine_initializes_upload_limits_from_one_root_read(tmp_path: Path) -> None:
    root = _root_limits(new=123, default=456)
    config = _config(tmp_path)

    with (
        patch(
            "discord_ferry.core.engine.api_fetch_root", new=AsyncMock(return_value=root)
        ) as fetch,
        patch(
            "discord_ferry.core.engine.api_fetch_self",
            new=AsyncMock(return_value={"_id": "not-a-ulid"}),
        ) as fetch_self,
    ):
        async with aiohttp.ClientSession() as session:
            tier = await engine_module._initialize_upload_limits(session, config)

    assert tier == "default"
    assert autumn_module.get_tag_size_limit("attachments") == 456
    fetch.assert_awaited_once_with(session, STOAT_URL)
    fetch_self.assert_awaited_once_with(session, STOAT_URL, TOKEN)


async def test_engine_upload_limit_timeout_falls_back_without_fetching_user(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    with (
        patch(
            "discord_ferry.core.engine.api_fetch_root",
            new=AsyncMock(side_effect=TimeoutError),
        ) as fetch,
        patch("discord_ferry.core.engine.api_fetch_self", new=AsyncMock()) as fetch_self,
    ):
        async with aiohttp.ClientSession() as session:
            tier = await engine_module._initialize_upload_limits(session, config)

    assert tier == "default"
    assert autumn_module.get_tag_size_limit("attachments") == 20_000_000
    fetch.assert_awaited_once_with(session, STOAT_URL)
    fetch_self.assert_not_awaited()


@pytest.mark.parametrize("body", ["[]", "null"])
async def test_engine_malformed_root_shape_falls_back(tmp_path: Path, body: str) -> None:
    config = _config(tmp_path)

    with aioresponses() as mocked:
        mocked.get(f"{STOAT_URL}/", body=body)
        async with aiohttp.ClientSession() as session:
            tier = await engine_module._initialize_upload_limits(session, config)

    assert tier == "default"
    assert autumn_module.get_tag_size_limit("attachments") == 20_000_000


@pytest.mark.usefixtures("publication_permission_collaborator")
async def test_retry_failed_initializes_advertised_attachment_limit(tmp_path: Path) -> None:
    attachment_path = tmp_path / "attachment.bin"
    attachment_path.write_bytes(b"four")
    config = _config(tmp_path)
    message = DCEMessage(
        id="message-1",
        type="Default",
        timestamp="2026-10-13T00:00:00Z",
        content="file",
        author=DCEAuthor(id="author-1", name="Author"),
        attachments=[
            DCEAttachment(
                id="attachment-1",
                url=attachment_path.name,
                file_name=attachment_path.name,
                file_size_bytes=4,
            )
        ],
    )
    export = DCEExport(
        guild=DCEGuild(id="guild1", name="Guild"),
        channel=DCEChannel(id="channel1", type=0, name="general"),
        messages=[message],
    )
    state = _state()
    state.channel_map["channel1"] = "01J5T006480276736221970060"
    state.failed_messages.append(
        FailedMessage(
            discord_msg_id=message.id,
            stoat_channel_id="01J5T006480276736221970060",
            error="first send failed",
        )
    )

    # Arrange a real rejected attempt without uploading this fixture first.
    # Retry must replace the limit from the fresh root response below.
    autumn_module.configure_upload_limits(
        {"features": {"limits": {"default": {"file_upload_size_limits": {"attachments": 3}}}}},
        None,
    )
    await seed_rejected_messages(config, state, [export])
    autumn_module.configure_upload_limits({}, None)

    with aioresponses() as mocked:
        mocked.get(
            f"{STOAT_URL}/",
            payload={
                "features": {"limits": {"default": {"file_upload_size_limits": {"attachments": 3}}}}
            },
        )
        mocked.post(
            f"{STOAT_URL}/channels/01J5T006480276736221970060/messages",
            payload={"_id": "01J5T009660122104522437779"},
        )

        await run_retry_failed(config, state, [export], lambda event: None)

    request_urls = [str(url) for _, url in mocked.requests]
    assert request_urls.count(f"{STOAT_URL}/") == 1
    assert f"{AUTUMN_URL}/attachments" not in request_urls
    assert any(
        warning["type"] == "attachment_skipped" and "limit: 0.0 MB" in warning["message"]
        for warning in state.warnings
    )
    assert state.failed_messages == []


@pytest.mark.usefixtures("repair_permission_unit_boundary")
async def test_repair_nested_retry_reuses_initialized_limits(tmp_path: Path) -> None:
    config = _config(tmp_path)
    state = MigrationState(
        autumn_url=AUTUMN_URL,
        failed_messages=[
            FailedMessage(
                discord_msg_id="message-1",
                stoat_channel_id="01J5T006480276736221970060",
                error="first send failed",
            )
        ],
    )

    def emit(event: Any) -> None:
        pass

    with (
        patch(
            "discord_ferry.migrator.verify.run_check",
            new=AsyncMock(return_value=CheckReport()),
        ),
        patch.object(
            engine_module,
            "_initialize_upload_limits",
            new=AsyncMock(return_value="default"),
        ) as initialize,
        patch.object(engine_module, "run_retry_failed", new=AsyncMock()) as retry,
    ):
        await engine_module.run_repair(config, state, [], emit)

    initialize.assert_awaited_once()
    counts = retry.await_args.kwargs["_delivery_outcomes"]
    assert isinstance(counts, engine_module.DeliveryOutcomes)
    retry.assert_awaited_once_with(
        config,
        state,
        [],
        emit,
        _upload_limits_ready=True,
        _delivery_outcomes=counts,
    )


async def test_advertised_limits_reach_all_download_and_upload_prechecks(
    tmp_path: Path,
) -> None:
    limits = {tag: 3 for tag in autumn_module.TAG_SIZE_LIMITS}
    autumn_module.configure_upload_limits(
        {"features": {"limits": {"default": {"file_upload_size_limits": limits}}}},
        None,
    )
    config = _config(tmp_path)
    state = _state()

    attachment = DCEAttachment(
        id="attachment-1",
        url="attachment.bin",
        file_name="attachment.bin",
        file_size_bytes=4,
    )
    message = DCEMessage(
        id="message-1",
        type="Default",
        timestamp="2026-10-13T00:00:00Z",
        content="file",
        author=DCEAuthor(id="author-1", name="Author"),
        attachments=[attachment],
    )

    async with aiohttp.ClientSession() as session:
        attachment_ids, placeholders = await _upload_attachments(
            message, config, state, session, lambda event: None
        )
    assert attachment_ids == []
    assert placeholders

    avatar_url = "https://cdn.discordapp.com/avatars/user/hash.png"
    role_icon_url = "https://cdn.discordapp.com/role-icons/role/hash.png"
    banner_url = "https://cdn.discordapp.com/banners/guild/hash.png"
    banner_path = tmp_path / "banners" / "guild.png"
    banner_path.parent.mkdir()
    with aioresponses() as mocked:
        mocked.get(
            avatar_url,
            body=b"four",
            status=200,
            headers={"Content-Type": "image/png"},
        )
        mocked.get(role_icon_url, body=b"four", status=200)
        mocked.get(banner_url, body=b"four", status=200)
        async with aiohttp.ClientSession() as session:
            avatar_path, reason = await _download_remote_avatar(
                session, avatar_url, tmp_path, "author-1"
            )
            role_icon = await download_role_icon(session, "role", "hash")
            banner_failure = await _download_banner(session, banner_url, banner_path)

    assert avatar_path is None
    assert "3 byte limit" in reason
    assert role_icon is None
    assert banner_failure == "Banner is larger than the 3 byte limit"

    image = tmp_path / "too-large.png"
    image.write_bytes(_VALID_PNG)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(AutumnUploadError, match="exceeds the emojis limit of 3 bytes"):
            await autumn_module.upload_to_autumn(session, AUTUMN_URL, "emojis", image, TOKEN)


async def test_recreating_emoji_does_not_reuse_consumed_cached_id(tmp_path: Path) -> None:
    image = tmp_path / "smile.png"
    image.write_bytes(_VALID_PNG)
    config = _config(tmp_path)
    state = _state()
    uploaded = iter(("autumn-1", "autumn-2"))
    attached: set[str] = set()

    async def upload(*args: Any, **kwargs: Any) -> str:
        return next(uploaded)

    async def create(*args: Any, **kwargs: Any) -> dict[str, str]:
        autumn_id = str(args[3])
        if autumn_id in attached:
            raise MigrationError("API error 404: NotFound")
        attached.add(autumn_id)
        return {"_id": autumn_id, "name": str(args[4])}

    with (
        patch("discord_ferry.uploader.autumn.upload_to_autumn", new=upload),
        patch("discord_ferry.migrator.emoji.api_create_emoji", new=create),
    ):
        async with aiohttp.ClientSession() as session:
            first = await upload_and_create_emoji(
                session, config, state, file_path=image, name="smile", used_names={}
            )
            second = await upload_and_create_emoji(
                session, config, state, file_path=image, name="smile", used_names={}
            )

    assert first[0] == "autumn-1"
    assert second[0] == "autumn-2"
    assert attached == {"autumn-1", "autumn-2"}


@pytest.mark.usefixtures("structure_binding_collaborator")
async def test_reentered_server_phase_refreshes_icon_and_banner_ids(tmp_path: Path) -> None:
    icon = tmp_path / "icon.png"
    icon.write_bytes(_VALID_PNG)
    exports = [
        DCEExport(
            guild=DCEGuild(id="guild1", name="Guild", icon_url="icon.png"),
            channel=DCEChannel(id="channel1", type=0, name="general"),
            messages=[],
        )
    ]
    config = _config(tmp_path)
    state = _state()
    metadata = SimpleNamespace(
        guild_id="guild1",
        guild_nsfw=False,
        guild_description="",
        banner_hash="hash",
    )
    attached: dict[str, set[str]] = {"icon": set(), "banner": set()}

    async def download_banner(session: Any, url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_VALID_PNG)

    def edit_callback(url: Any, **kwargs: Any) -> CallbackResult:
        body = kwargs.get("json", {})
        for field in ("icon", "banner"):
            file_id = body.get(field)
            if not file_id:
                continue
            if file_id in attached[field]:
                return CallbackResult(status=404, payload={"type": "NotFound"})
            attached[field].add(file_id)
        return CallbackResult(status=200, payload={"_id": "srv1"})

    with (
        patch(
            "discord_ferry.migrator.structure.load_bound_discord_metadata",
            return_value=metadata,
        ),
        patch("discord_ferry.migrator.structure._download_banner", new=download_banner),
        aioresponses() as mocked,
    ):
        mocked.get(f"{STOAT_URL}/servers/srv1", payload={"_id": "srv1"}, repeat=True)
        mocked.post(f"{AUTUMN_URL}/icons", payload={"id": "icon-1"})
        mocked.post(f"{AUTUMN_URL}/icons", payload={"id": "icon-2"})
        mocked.post(f"{AUTUMN_URL}/banners", payload={"id": "banner-1"})
        mocked.post(f"{AUTUMN_URL}/banners", payload={"id": "banner-2"})
        mocked.patch(f"{STOAT_URL}/servers/srv1", callback=edit_callback, repeat=True)

        await run_server(config, state, exports, lambda event: None)
        await run_server(config, state, exports, lambda event: None)

    assert attached == {"icon": {"icon-1", "icon-2"}, "banner": {"banner-1", "banner-2"}}
    assert not [warning for warning in state.warnings if "upload_failed" in warning["type"]]
