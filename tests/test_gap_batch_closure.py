"""Runtime closure checks for delivered duplicate-message recovery work."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from aioresponses import CallbackResult, aioresponses

from discord_ferry.config import FerryConfig
from discord_ferry.migrator.messages import run_messages
from discord_ferry.parser.models import (
    DCEAuthor,
    DCEChannel,
    DCEEmoji,
    DCEExport,
    DCEGuild,
    DCEMessage,
    DCEReaction,
)
from discord_ferry.state import MigrationState, load_state
from tests.stoat_ids import stoat_id
from tests.test_delivery import (
    publication_permission_collaborator as publication_permission_collaborator,
)

if TYPE_CHECKING:
    from pathlib import Path

BASE_URL = "https://api.test"
AUTUMN_URL = "https://autumn.test"


def _config(tmp_path: Path, **overrides: object) -> FerryConfig:
    defaults: dict[str, object] = {
        "export_dir": tmp_path,
        "output_dir": tmp_path,
        "stoat_url": BASE_URL,
        "token": "test-token",
        "message_rate_limit": 0.0,
        "upload_delay": 0.0,
    }
    defaults.update(overrides)
    return FerryConfig(**defaults)


def _message(message_id: str, content: str, **overrides: object) -> DCEMessage:
    defaults: dict[str, object] = {
        "id": message_id,
        "type": "Default",
        "timestamp": "2024-01-15T12:00:00+00:00",
        "content": content,
        "author": DCEAuthor(id="author", name="Author"),
    }
    defaults.update(overrides)
    return DCEMessage(**defaults)


def _export(
    channel_id: str,
    name: str,
    messages: list[DCEMessage],
    *,
    is_thread: bool = False,
    parent_channel_name: str = "",
) -> DCEExport:
    return DCEExport(
        guild=DCEGuild(id="guild", name="Guild"),
        channel=DCEChannel(id=channel_id, type=0, name=name),
        messages=messages,
        is_thread=is_thread,
        parent_channel_name=parent_channel_name,
    )


@pytest.mark.usefixtures("publication_permission_collaborator")
async def test_html_conflict_recovers_exact_nonce_and_persists_message_bookkeeping(
    tmp_path: Path,
) -> None:
    """An HTML 409 is uncertain delivery, then exact nonce history recovers normal bookkeeping."""
    destination = stoat_id(501)
    state = MigrationState(
        stoat_server_id=stoat_id(500),
        channel_map={"100": destination},
        autumn_url=AUTUMN_URL,
    )
    source = _message(
        "200",
        "recover me",
        is_pinned=True,
        reactions=[DCEReaction(emoji=DCEEmoji(id="", name="👍"), count=1)],
    )
    rows: list[dict[str, str]] = []
    sent_keys: list[str] = []
    url = f"{BASE_URL}/channels/{destination}/messages"

    def conflict(url, **kwargs):
        key = kwargs["headers"]["Idempotency-Key"]
        sent_keys.append(key)
        rows.append({"_id": stoat_id(502), "channel": destination, "nonce": key})
        return CallbackResult(
            status=409,
            body="<html><body>DuplicateNonce</body></html>",
            headers={"Content-Type": "text/html"},
        )

    def history(url, **kwargs):
        return CallbackResult(payload=rows)

    with aioresponses() as http:
        http.post(url, callback=conflict)
        http.get(f"{url}?limit=100&sort=Latest", callback=history)
        await run_messages(
            _config(tmp_path, reaction_mode="native"),
            state,
            [_export("100", "general", [source])],
            lambda event: None,
        )

    assert len(sent_keys) == 1
    assert state.message_map == {"200": stoat_id(502)}
    assert state.pending_pins == [(destination, stoat_id(502))]
    assert state.pending_reactions == [
        {"channel_id": destination, "message_id": stoat_id(502), "emoji": "👍"}
    ]
    assert not state.failed_messages
    restarted = load_state(tmp_path)
    assert restarted.message_map == state.message_map
    assert restarted.pending_pins == state.pending_pins
    assert restarted.pending_reactions == state.pending_reactions


@pytest.mark.usefixtures("publication_permission_collaborator")
async def test_recovered_parent_suppresses_merged_origin_after_restart_incremental(
    tmp_path: Path,
) -> None:
    """A recovered parent map persists and suppresses the duplicated thread origin on later runs."""
    destination = stoat_id(601)
    state = MigrationState(
        stoat_server_id=stoat_id(600),
        channel_map={"100": destination},
        autumn_url=AUTUMN_URL,
    )
    origin = _message("200", "parent body")
    parent = _export("100", "general", [origin])
    thread = _export(
        "200",
        "thread",
        [origin, _message("300", "thread body")],
        is_thread=True,
        parent_channel_name="general",
    )
    historical: list[dict[str, str]] = []
    url = f"{BASE_URL}/channels/{destination}/messages"

    def parent_conflict(url, **kwargs):
        key = kwargs["headers"]["Idempotency-Key"]
        historical.append({"_id": stoat_id(602), "channel": destination, "nonce": key})
        return CallbackResult(
            status=409,
            body="<html><body>cached key</body></html>",
            headers={"Content-Type": "text/html"},
        )

    def history(url, **kwargs):
        return CallbackResult(payload=historical)

    with aioresponses() as http:
        http.post(url, callback=parent_conflict)
        http.get(f"{url}?limit=100&sort=Latest", callback=history)
        await run_messages(
            _config(tmp_path),
            state,
            [parent],
            lambda event: None,
        )

    assert state.message_map["200"] == stoat_id(602)
    restarted = load_state(tmp_path)
    assert restarted.message_map["200"] == stoat_id(602)
    thread_posts: list[str] = []

    def thread_post(url, **kwargs):
        thread_posts.append(kwargs["headers"]["Idempotency-Key"])
        return CallbackResult(payload={"_id": stoat_id(603 + len(thread_posts))})

    with aioresponses() as http:
        http.post(url, callback=thread_post, repeat=True)
        await run_messages(
            _config(tmp_path, thread_strategy="merge", incremental=True),
            restarted,
            [parent, thread],
            lambda event: None,
        )
    assert len(thread_posts) == 2
    assert restarted.merge_duplicates_suppressed == 1
    with aioresponses() as http:
        await run_messages(
            _config(tmp_path, thread_strategy="merge", incremental=True),
            restarted,
            [parent, thread],
            lambda event: None,
        )
        assert not http.requests
