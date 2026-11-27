"""Tests for the avatars phase (Phase 7.5): one picture link per author."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import aiohttp
import pytest
from aioresponses import aioresponses

from discord_ferry.config import FerryConfig
from discord_ferry.migrator.avatars import (
    AVATAR_LINK_MAX_LENGTH,
    classify_export_link,
    lookup_avatar_link,
    run_avatars,
)
from discord_ferry.parser.models import (
    DCEAuthor,
    DCEChannel,
    DCEExport,
    DCEGuild,
    DCEMessage,
)
from discord_ferry.state import MigrationState

if TYPE_CHECKING:
    from pathlib import Path

    from discord_ferry.core.events import MigrationEvent

BASE_URL = "https://stoat.test"
AUTUMN_URL = "https://autumn.test"
TOKEN = "test-token"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path, **overrides: Any) -> FerryConfig:
    defaults: dict[str, Any] = {
        "export_dir": tmp_path,
        "stoat_url": BASE_URL,
        "token": TOKEN,
        "upload_delay": 0.0,
        "output_dir": tmp_path / "output",
    }
    defaults.update(overrides)
    return FerryConfig(**defaults)


def _make_state() -> MigrationState:
    state = MigrationState()
    state.stoat_server_id = "srv1"
    state.autumn_url = AUTUMN_URL
    return state


def _make_author(
    author_id: str = "user1",
    name: str = "Alice",
    avatar_url: str = "",
) -> DCEAuthor:
    return DCEAuthor(id=author_id, name=name, avatar_url=avatar_url)


def _make_message(
    msg_id: str = "msg1",
    author: DCEAuthor | None = None,
) -> DCEMessage:
    return DCEMessage(
        id=msg_id,
        type="Default",
        timestamp="2024-01-01T00:00:00Z",
        content="hello",
        author=author or _make_author(),
    )


def _make_export(messages: list[DCEMessage] | None = None) -> DCEExport:
    return DCEExport(
        guild=DCEGuild(id="guild1", name="Test"),
        channel=DCEChannel(id="ch1", type=0, name="general"),
        messages=messages or [],
    )


DISCORD_API = "https://discord.com/api/v10"
DISCORD_TOKEN = "Bot TESTTOKEN-SENTINEL-1208"


def _user_url(user_id: str) -> str:
    return f"{DISCORD_API}/users/{user_id}"


def _export_of(*authors: DCEAuthor) -> DCEExport:
    return _make_export([_make_message(f"m-{author.id}", author) for author in authors])


def _request_count(mock: aioresponses) -> int:
    return sum(len(calls) for calls in mock.requests.values())


def _methods(mock: aioresponses) -> set[str]:
    return {method for (method, _url) in mock.requests}


# ---------------------------------------------------------------------------
# Pure link functions (#1208)
# ---------------------------------------------------------------------------

_CDN = "https://cdn.discordapp.com"
_HASH = "4551a51a7078abcdef0123456789abcd"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # SC-3.1: the query string is removed.
        (f"{_CDN}/avatars/1/abc.png?size=128", f"{_CDN}/avatars/1/abc.png"),
        # SC-3.2: the second Discord picture host, with no query to remove.
        (
            "https://media.discordapp.net/avatars/1/abc.webp",
            "https://media.discordapp.net/avatars/1/abc.webp",
        ),
        # Host case, an explicit default port and a fragment all normalise away.
        ("https://CDN.DiscordApp.com:443/avatars/1/abc.gif#x", f"{_CDN}/avatars/1/abc.gif"),
        # Animated hashes keep their a_ prefix.
        (f"{_CDN}/avatars/1/a_0f1e.gif", f"{_CDN}/avatars/1/a_0f1e.gif"),
        # The two other documented picture paths.
        (f"{_CDN}/embed/avatars/3.png", f"{_CDN}/embed/avatars/3.png"),
        (
            f"{_CDN}/guilds/5/users/1/avatars/abc.png?size=64",
            f"{_CDN}/guilds/5/users/1/avatars/abc.png",
        ),
    ],
)
def test_classify_export_link_accepts_documented_picture_paths(url: str, expected: str) -> None:
    assert classify_export_link(url) == ("link", expected)


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.discordapp.com/avatars/1/a.png",  # not https
        "https://evil.example/avatars/1/a.png",  # not a Discord host
        "https://user:pw@cdn.discordapp.com/avatars/1/a.png",  # carries credentials
        "https://cdn.discordapp.com:8443/avatars/1/a.png",  # non-default port
        "https://cdn.discordapp.com.evil.example/avatars/1/a.png",  # lookalike host
        "https://127.0.0.1/avatars/1/a.png",  # literal address
    ],
)
def test_classify_export_link_refuses_what_remote_media_refusal_refuses(url: str) -> None:
    assert classify_export_link(url) == ("unusable", "")


@pytest.mark.parametrize(
    "url",
    [
        "",  # no picture in the export
        "media/avatars/alice.png",  # the --media case: a local path, never read
        "../../etc/x.png",  # a local path that escapes the export folder
        "/etc/passwd",
        f"{_CDN}/avatars/webhook.png",  # SC-3.4: Discord host, undocumented path
        f"{_CDN}/attachments/1/2/file.png",  # a Discord file that is not a picture path
        f"{_CDN}/avatars/1/a%2Fb.png",  # an encoded separator never matches
        f"{_CDN}/avatars/1/abc.exe",  # extension outside the picture set
        f"{_CDN}/embed/avatars/12.png",  # two digits
        f"{_CDN}/",
    ],
)
def test_classify_export_link_falls_through_when_there_is_nothing_usable(url: str) -> None:
    assert classify_export_link(url) == ("skip", "")


def test_classify_export_link_does_not_decide_the_length() -> None:
    """The caller applies the 256 limit, so an over-long accepted link still comes back."""
    prefix = f"{_CDN}/avatars/1/"
    url = prefix + "a" * (AVATAR_LINK_MAX_LENGTH + 1 - len(prefix) - len(".png")) + ".png"
    kind, link = classify_export_link(url)
    assert kind == "link"
    assert len(link) == AVATAR_LINK_MAX_LENGTH + 1


@pytest.mark.parametrize(
    ("user_id", "avatar_hash", "expected"),
    [
        # SC-2.1
        ("1505988963628879902", _HASH, f"{_CDN}/avatars/1505988963628879902/{_HASH}.png"),
        # SC-2.2: an animated hash gets .gif
        ("42", f"a_{_HASH}", f"{_CDN}/avatars/42/a_{_HASH}.gif"),
    ],
)
def test_lookup_avatar_link_builds_the_documented_cdn_path(
    user_id: str, avatar_hash: str, expected: str
) -> None:
    assert lookup_avatar_link(user_id, avatar_hash) == expected


@pytest.mark.parametrize(
    ("user_id", "avatar_hash"),
    [
        ("42", "../../x?y"),  # SC-2.7
        ("42", "ABCDEF"),  # upper case is not what Discord returns
        ("42", "abc.png"),
        ("42", "a_"),
        ("42", ""),
        ("42", "abc\n"),  # fullmatch, not $, so a trailing newline is refused
        ("42", None),
        ("42", 7),
        ("abc/../1", _HASH),  # SC-2.8
        ("", _HASH),
        ("٣", _HASH),
        ("1" * 21, _HASH),
    ],
)
def test_lookup_avatar_link_refuses_anything_that_could_change_the_url(
    user_id: str, avatar_hash: object
) -> None:
    assert lookup_avatar_link(user_id, avatar_hash) is None


def test_a_built_lookup_link_always_fits_stoats_limit() -> None:
    link = lookup_avatar_link("9" * 20, "a_" + "f" * 64)
    assert link is not None
    assert len(link) <= AVATAR_LINK_MAX_LENGTH


# ---------------------------------------------------------------------------
# One lookup per author (S2)
# ---------------------------------------------------------------------------


async def test_one_lookup_builds_a_static_link(tmp_path: Path) -> None:
    """SC-2.1."""
    author = _make_author("1505988963628879902")
    config = _make_config(tmp_path, discord_token=DISCORD_TOKEN)
    state = _make_state()
    with aioresponses() as mock:
        mock.get(_user_url(author.id), payload={"id": author.id, "avatar": _HASH})
        await run_avatars(config, state, [_export_of(author)], lambda e: None)
        assert _request_count(mock) == 1
    assert state.author_avatar_links == {author.id: f"{_CDN}/avatars/{author.id}/{_HASH}.png"}
    assert state.author_avatar_reasons == {}


async def test_the_lookup_sends_the_token_and_nothing_goes_to_stoat(tmp_path: Path) -> None:
    author = _make_author("1505988963628879902")
    config = _make_config(tmp_path, discord_token=DISCORD_TOKEN)
    with aioresponses() as mock:
        mock.get(_user_url(author.id), payload={"id": author.id, "avatar": _HASH})
        await run_avatars(config, _make_state(), [_export_of(author)], lambda e: None)
        request = next(iter(mock.requests.values()))[0]
        assert request.kwargs["headers"]["Authorization"] == DISCORD_TOKEN
        assert {url.host for (_method, url) in mock.requests} == {"discord.com"}


async def test_animated_hash_gets_a_gif_link(tmp_path: Path) -> None:
    """SC-2.2."""
    author = _make_author("42")
    config = _make_config(tmp_path, discord_token=DISCORD_TOKEN)
    state = _make_state()
    with aioresponses() as mock:
        mock.get(_user_url("42"), payload={"id": "42", "avatar": f"a_{_HASH}"})
        await run_avatars(config, state, [_export_of(author)], lambda e: None)
    assert state.author_avatar_links["42"] == f"{_CDN}/avatars/42/a_{_HASH}.gif"


async def test_one_lookup_per_author_across_messages_and_exports(tmp_path: Path) -> None:
    """SC-2.3: 50 messages by one author across two exports cost one request."""
    author = _make_author("77")
    first = _make_export([_make_message(f"a{i}", author) for i in range(25)])
    second = _make_export([_make_message(f"b{i}", author) for i in range(25)])
    config = _make_config(tmp_path, discord_token=DISCORD_TOKEN)
    state = _make_state()
    with aioresponses() as mock:
        mock.get(_user_url("77"), payload={"id": "77", "avatar": _HASH})
        await run_avatars(config, state, [first, second], lambda e: None)
        assert _request_count(mock) == 1
    assert list(state.author_avatar_links) == ["77"]


async def test_a_rate_limit_is_waited_out(tmp_path: Path, recorded_sleeps: list[float]) -> None:
    """SC-2.4: Discord's Retry-After is in seconds and is honoured."""
    author = _make_author("42")
    config = _make_config(tmp_path, discord_token=DISCORD_TOKEN)
    state = _make_state()
    with aioresponses() as mock:
        mock.get(_user_url("42"), status=429, payload={}, headers={"Retry-After": "2"})
        mock.get(_user_url("42"), payload={"id": "42", "avatar": _HASH})
        await run_avatars(config, state, [_export_of(author)], lambda e: None)
        assert _request_count(mock) == 2
    assert 2 in recorded_sleeps
    assert "42" in state.author_avatar_links


async def test_a_null_hash_records_no_picture(tmp_path: Path) -> None:
    """SC-2.5: the shape the live probe returned."""
    config = _make_config(tmp_path, discord_token=DISCORD_TOKEN)
    state = _make_state()
    with aioresponses() as mock:
        mock.get(_user_url("42"), payload={"id": "42", "avatar": None})
        await run_avatars(config, state, [_export_of(_make_author("42"))], lambda e: None)
    assert state.author_avatar_links == {}
    assert state.author_avatar_reasons == {"42": "no_picture"}


async def test_an_unknown_user_costs_only_that_author(tmp_path: Path) -> None:
    """SC-2.6."""
    config = _make_config(tmp_path, discord_token=DISCORD_TOKEN)
    state = _make_state()
    events: list[MigrationEvent] = []
    with aioresponses() as mock:
        mock.get(_user_url("101"), status=404, payload={"code": 10013, "message": "Unknown User"})
        mock.get(_user_url("102"), payload={"id": "102", "avatar": _HASH})
        await run_avatars(
            config,
            state,
            [_export_of(_make_author("101"), _make_author("102"))],
            events.append,
        )
    assert state.author_avatar_reasons == {"101": "lookup_failed"}
    assert state.author_avatar_links == {"102": f"{_CDN}/avatars/102/{_HASH}.png"}
    assert events[-1].status == "completed"


async def test_a_hash_with_unexpected_characters_is_not_put_in_a_link(tmp_path: Path) -> None:
    """SC-2.7."""
    config = _make_config(tmp_path, discord_token=DISCORD_TOKEN)
    state = _make_state()
    with aioresponses() as mock:
        mock.get(_user_url("42"), payload={"id": "42", "avatar": "../../x?y"})
        await run_avatars(config, state, [_export_of(_make_author("42"))], lambda e: None)
    assert state.author_avatar_links == {}
    assert state.author_avatar_reasons == {"42": "lookup_failed"}


async def test_a_non_digit_author_id_is_never_looked_up(tmp_path: Path) -> None:
    """SC-2.8: no request at all, so the id cannot alter a Discord path."""
    config = _make_config(tmp_path, discord_token=DISCORD_TOKEN)
    state = _make_state()
    with aioresponses() as mock:
        await run_avatars(config, state, [_export_of(_make_author("abc/../1"))], lambda e: None)
        assert _request_count(mock) == 0
    assert state.author_avatar_reasons == {"abc/../1": "lookup_failed"}


async def test_a_401_stops_lookups_and_export_links_still_count(tmp_path: Path) -> None:
    """SC-2.9: 101 and 102 succeed, 103 is rejected, 104 has an export link, 105 never asks."""
    export_link = f"{_CDN}/avatars/104/abc.png?size=64"
    authors = [
        _make_author("101"),
        _make_author("102"),
        _make_author("103"),
        _make_author("104", avatar_url=export_link),
        _make_author("105"),
    ]
    config = _make_config(tmp_path, discord_token=DISCORD_TOKEN)
    state = _make_state()
    with aioresponses() as mock:
        mock.get(_user_url("101"), payload={"id": "101", "avatar": _HASH})
        mock.get(_user_url("102"), payload={"id": "102", "avatar": _HASH})
        mock.get(_user_url("103"), status=401, body="401: Unauthorized")
        await run_avatars(config, state, [_export_of(*authors)], lambda e: None)
        assert _request_count(mock) == 3
    assert state.author_avatar_reasons == {"103": "token_rejected", "105": "token_rejected"}
    assert state.author_avatar_links == {
        "101": f"{_CDN}/avatars/101/{_HASH}.png",
        "102": f"{_CDN}/avatars/102/{_HASH}.png",
        "104": f"{_CDN}/avatars/104/abc.png",
    }


async def test_a_403_on_the_first_lookup_stops_lookups(tmp_path: Path) -> None:
    """SC-2.10: the token cannot use the route, so authors 1 and 3 are not asked again."""
    authors = [
        _make_author("201"),
        _make_author("202", avatar_url=f"{_CDN}/avatars/202/abc.png"),
        _make_author("203"),
    ]
    config = _make_config(tmp_path, discord_token=DISCORD_TOKEN)
    state = _make_state()
    with aioresponses() as mock:
        mock.get(_user_url("201"), status=403, body="Forbidden")
        await run_avatars(config, state, [_export_of(*authors)], lambda e: None)
        assert _request_count(mock) == 1
    assert state.author_avatar_reasons == {
        "201": "lookup_not_allowed",
        "203": "lookup_not_allowed",
    }
    assert state.author_avatar_links == {"202": f"{_CDN}/avatars/202/abc.png"}


async def test_a_later_403_costs_only_that_author(tmp_path: Path) -> None:
    """SC-2.11: once one lookup has worked, a 403 is about that author."""
    authors = [_make_author("301"), _make_author("302"), _make_author("303")]
    config = _make_config(tmp_path, discord_token=DISCORD_TOKEN)
    state = _make_state()
    with aioresponses() as mock:
        mock.get(_user_url("301"), payload={"id": "301", "avatar": _HASH})
        mock.get(_user_url("302"), status=403, body="Forbidden")
        mock.get(_user_url("303"), payload={"id": "303", "avatar": _HASH})
        await run_avatars(config, state, [_export_of(*authors)], lambda e: None)
        assert _request_count(mock) == 3
    assert state.author_avatar_reasons == {"302": "lookup_failed"}
    assert set(state.author_avatar_links) == {"301", "303"}


async def test_a_network_failure_after_retries_costs_only_that_author(
    tmp_path: Path, recorded_sleeps: list[float]
) -> None:
    """SC-2.12."""
    authors = [_make_author("401"), _make_author("402")]
    config = _make_config(tmp_path, discord_token=DISCORD_TOKEN)
    state = _make_state()
    events: list[MigrationEvent] = []
    with aioresponses() as mock:
        for _ in range(3):
            mock.get(_user_url("401"), exception=aiohttp.ClientConnectionError("down"))
        mock.get(_user_url("402"), payload={"id": "402", "avatar": _HASH})
        await run_avatars(config, state, [_export_of(*authors)], events.append)
    assert state.author_avatar_reasons == {"401": "lookup_failed"}
    assert set(state.author_avatar_links) == {"402"}
    assert events[-1].status == "completed"
    assert recorded_sleeps  # the retry waits ran through the recorder, not the clock


async def test_a_lookup_timeout_costs_only_that_author(tmp_path: Path) -> None:
    """A plain TimeoutError (session total timeout) must not stop the phase."""
    authors = [_make_author("451"), _make_author("452")]
    config = _make_config(tmp_path, discord_token=DISCORD_TOKEN)
    state = _make_state()
    events: list[MigrationEvent] = []
    with aioresponses() as mock:
        mock.get(_user_url("451"), exception=TimeoutError())
        mock.get(_user_url("452"), payload={"id": "452", "avatar": _HASH})
        await run_avatars(config, state, [_export_of(*authors)], events.append)
    assert state.author_avatar_reasons == {"451": "lookup_failed"}
    assert set(state.author_avatar_links) == {"452"}
    assert events[-1].status == "completed"


async def test_a_dry_run_looks_up_too_and_sends_nothing_to_stoat(tmp_path: Path) -> None:
    """SC-2.14 (phase level): lookups run, the maps are filled, only Discord is contacted."""
    config = _make_config(tmp_path, discord_token=DISCORD_TOKEN, dry_run=True)
    state = _make_state()
    with aioresponses() as mock:
        mock.get(_user_url("42"), payload={"id": "42", "avatar": _HASH})
        await run_avatars(config, state, [_export_of(_make_author("42"))], lambda e: None)
        assert {url.host for (_method, url) in mock.requests} == {"discord.com"}
    assert state.author_avatar_links == {"42": f"{_CDN}/avatars/42/{_HASH}.png"}


# ---------------------------------------------------------------------------
# Links already in the export (S3)
# ---------------------------------------------------------------------------


async def test_an_export_link_is_used_without_a_token_or_a_request(tmp_path: Path) -> None:
    """SC-3.1."""
    author = _make_author("1", avatar_url=f"{_CDN}/avatars/1/abc.png?size=128")
    config = _make_config(tmp_path)
    state = _make_state()
    with aioresponses() as mock:
        await run_avatars(config, state, [_export_of(author)], lambda e: None)
        assert _request_count(mock) == 0
    assert state.author_avatar_links == {"1": f"{_CDN}/avatars/1/abc.png"}


async def test_refused_export_links_are_reported_and_never_fetched_or_looked_up(
    tmp_path: Path,
) -> None:
    """SC-3.3: four hostile links, token set, so a lookup would be possible and must not happen."""
    links = [
        "http://cdn.discordapp.com/avatars/1/a.png",
        "https://evil.example/a.png",
        "https://user:pw@cdn.discordapp.com/avatars/1/a.png",
        "https://cdn.discordapp.com:8443/avatars/1/a.png",
    ]
    authors = [_make_author(str(500 + i), avatar_url=link) for i, link in enumerate(links)]
    config = _make_config(tmp_path, discord_token=DISCORD_TOKEN)
    state = _make_state()
    with aioresponses() as mock:
        await run_avatars(config, state, [_export_of(*authors)], lambda e: None)
        assert _request_count(mock) == 0
    assert state.author_avatar_links == {}
    assert set(state.author_avatar_reasons.values()) == {"export_link_unusable"}
    assert len(state.author_avatar_reasons) == 4


async def test_a_discord_host_with_an_undocumented_path_falls_through_to_a_lookup(
    tmp_path: Path,
) -> None:
    """SC-3.4: the shape in tests/fixtures/edge_cases.json."""
    author = _make_author("42", avatar_url=f"{_CDN}/avatars/webhook.png")
    config = _make_config(tmp_path, discord_token=DISCORD_TOKEN)
    state = _make_state()
    with aioresponses() as mock:
        mock.get(_user_url("42"), payload={"id": "42", "avatar": _HASH})
        await run_avatars(config, state, [_export_of(author)], lambda e: None)
        assert _request_count(mock) == 1
    assert state.author_avatar_links == {"42": f"{_CDN}/avatars/42/{_HASH}.png"}


async def test_an_over_long_export_link_records_link_too_long(tmp_path: Path) -> None:
    """SC-3.5: 257 characters is refused, 256 is kept."""
    prefix = f"{_CDN}/avatars/1/"

    def link_of(length: int) -> str:
        return prefix + "a" * (length - len(prefix) - len(".png")) + ".png"

    authors = [
        _make_author("1", avatar_url=link_of(AVATAR_LINK_MAX_LENGTH + 1)),
        _make_author("2", avatar_url=link_of(AVATAR_LINK_MAX_LENGTH)),
    ]
    config = _make_config(tmp_path)
    state = _make_state()
    await run_avatars(config, state, [_export_of(*authors)], lambda e: None)
    assert state.author_avatar_reasons == {"1": "link_too_long"}
    assert list(state.author_avatar_links) == ["2"]
    assert len(state.author_avatar_links["2"]) == AVATAR_LINK_MAX_LENGTH


async def test_an_empty_avatar_url_without_a_token_records_no_discord_token(
    tmp_path: Path,
) -> None:
    """SC-3.6."""
    config = _make_config(tmp_path)
    state = _make_state()
    with aioresponses() as mock:
        await run_avatars(config, state, [_export_of(_make_author("42"))], lambda e: None)
        assert _request_count(mock) == 0
    assert state.author_avatar_reasons == {"42": "no_discord_token"}


# ---------------------------------------------------------------------------
# Nothing is uploaded and no local file is read (S1)
# ---------------------------------------------------------------------------


async def test_no_author_picture_is_uploaded_to_autumn(tmp_path: Path) -> None:
    """SC-1.2: three authors with local picture files that exist, and a token."""
    authors = []
    for i in range(3):
        local = tmp_path / f"avatar_{i}.png"
        local.write_bytes(b"\x89PNG\r\n\x1a\n")
        authors.append(_make_author(str(600 + i), avatar_url=local.name))
    config = _make_config(tmp_path, discord_token=DISCORD_TOKEN)
    state = _make_state()
    with aioresponses() as mock:
        for author in authors:
            mock.get(_user_url(author.id), payload={"id": author.id, "avatar": _HASH})
        await run_avatars(config, state, [_export_of(*authors)], lambda e: None)
        assert _methods(mock) == {"GET"}
        assert {url.host for (_method, url) in mock.requests} == {"discord.com"}
    assert state.autumn_uploads == {}
    assert state.referenced_autumn_ids == set()
    assert state.upload_cache == {}
    assert len(state.author_avatar_links) == 3


async def test_a_local_path_that_escapes_the_export_is_not_read_or_warned_about(
    tmp_path: Path,
) -> None:
    """SC-1.4: the old unsafe_media_path case now falls through to a lookup."""
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    (tmp_path / "x-marker.png").write_bytes(b"MARKER")
    author = _make_author("42", avatar_url="../x-marker.png")
    config = _make_config(export_dir, discord_token=DISCORD_TOKEN, output_dir=tmp_path / "out")
    state = _make_state()
    with aioresponses() as mock:
        mock.get(_user_url("42"), payload={"id": "42", "avatar": _HASH})
        await run_avatars(config, state, [_export_of(author)], lambda e: None)
        assert _methods(mock) == {"GET"}
    assert [w for w in state.warnings if w.get("type") == "unsafe_media_path"] == []
    assert state.author_avatar_links == {"42": f"{_CDN}/avatars/42/{_HASH}.png"}
    assert (tmp_path / "x-marker.png").read_bytes() == b"MARKER"


async def test_an_export_with_no_messages_completes(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    state = _make_state()
    events: list[MigrationEvent] = []
    await run_avatars(config, state, [_make_export([])], events.append)
    assert events[-1].status == "completed"
    assert state.author_avatar_links == {}
    assert state.author_avatar_reasons == {}


# ---------------------------------------------------------------------------
# Resume inside the phase (S5)
# ---------------------------------------------------------------------------


def _resume_state() -> MigrationState:
    """Seven authors, as the interrupted run left them, with the token problems unsettled."""
    state = _make_state()
    state.current_phase = "avatars"
    state.author_avatar_links = {
        "1": f"{_CDN}/avatars/1/{_HASH}.png",
        "2": f"{_CDN}/avatars/2/{_HASH}.png",
    }
    state.author_avatar_reasons = {
        "3": "no_picture",
        "4": "lookup_failed",
        "5": "token_rejected",
        "6": "token_rejected",
        "7": "no_discord_token",
    }
    return state


async def test_a_resumed_phase_asks_only_for_the_token_dependent_authors(tmp_path: Path) -> None:
    """SC-5.2: authors with a link, no_picture or lookup_failed are settled."""
    authors = [_make_author(str(n)) for n in range(1, 8)]
    config = _make_config(tmp_path, discord_token=DISCORD_TOKEN, resume=True)
    state = _resume_state()
    settled_links = dict(state.author_avatar_links)
    with aioresponses() as mock:
        for author_id in ("5", "6", "7"):
            mock.get(_user_url(author_id), payload={"id": author_id, "avatar": _HASH})
        await run_avatars(config, state, [_export_of(*authors)], lambda e: None)
        asked = sorted(url.path.rsplit("/", 1)[1] for (_method, url) in mock.requests)
    assert asked == ["5", "6", "7"]
    assert {k: state.author_avatar_links[k] for k in ("1", "2")} == settled_links
    assert set(state.author_avatar_links) == {"1", "2", "5", "6", "7"}
    assert state.author_avatar_reasons == {"3": "no_picture", "4": "lookup_failed"}


async def test_every_token_dependent_reason_is_retried(tmp_path: Path) -> None:
    authors = [_make_author(str(n)) for n in (11, 12, 13)]
    config = _make_config(tmp_path, discord_token=DISCORD_TOKEN, resume=True)
    state = _make_state()
    state.author_avatar_reasons = {
        "11": "no_discord_token",
        "12": "token_rejected",
        "13": "lookup_not_allowed",
    }
    with aioresponses() as mock:
        for author_id in ("11", "12", "13"):
            mock.get(_user_url(author_id), payload={"id": author_id, "avatar": _HASH})
        await run_avatars(config, state, [_export_of(*authors)], lambda e: None)
        assert _request_count(mock) == 3
    assert state.author_avatar_reasons == {}
    assert set(state.author_avatar_links) == {"11", "12", "13"}


async def test_reasons_that_do_not_depend_on_the_token_are_settled(tmp_path: Path) -> None:
    """An unusable export link or a long link comes out the same on every attempt."""
    authors = [_make_author("21"), _make_author("22")]
    config = _make_config(tmp_path, discord_token=DISCORD_TOKEN, resume=True)
    state = _make_state()
    state.author_avatar_reasons = {"21": "export_link_unusable", "22": "link_too_long"}
    with aioresponses() as mock:
        await run_avatars(config, state, [_export_of(*authors)], lambda e: None)
        assert _request_count(mock) == 0
    assert state.author_avatar_reasons == {"21": "export_link_unusable", "22": "link_too_long"}


async def test_a_retry_without_a_token_keeps_the_reason(tmp_path: Path) -> None:
    config = _make_config(tmp_path, resume=True)
    state = _make_state()
    state.author_avatar_reasons = {"31": "token_rejected"}
    await run_avatars(config, state, [_export_of(_make_author("31"))], lambda e: None)
    assert state.author_avatar_reasons == {"31": "no_discord_token"}
    assert state.author_avatar_links == {}


def _three_author_export() -> DCEExport:
    return _export_of(*(_make_author(f"90{i}") for i in range(3)))


async def test_the_phase_saves_state_once_the_clock_passes_five_seconds(tmp_path: Path) -> None:
    """SC-5.4: a short run checkpoints mid-loop, with the links settled so far."""
    import itertools
    from unittest.mock import Mock, patch

    snapshots: list[dict[str, str]] = []
    save_mock = Mock(
        side_effect=lambda state, _dir: snapshots.append(dict(state.author_avatar_links))
    )
    # Every clock reading jumps 100 s, so each check crosses the 5 s threshold even if other
    # code also reads the clock.
    monotonic = Mock(side_effect=itertools.count(0, 100))
    config = _make_config(tmp_path, discord_token=DISCORD_TOKEN)
    state = _make_state()
    with (
        aioresponses() as mock,
        patch("discord_ferry.migrator.avatars.save_state", save_mock),
        patch("discord_ferry.migrator.avatars.time.monotonic", monotonic),
    ):
        for i in range(3):
            mock.get(_user_url(f"90{i}"), payload={"id": f"90{i}", "avatar": _HASH})
        await run_avatars(config, state, [_three_author_export()], lambda e: None)
    assert save_mock.call_count >= 1
    assert snapshots[0], "the first save must already hold the first author's link"
    assert save_mock.call_args.args[1] == config.output_dir


async def test_the_phase_does_not_save_while_the_clock_stands_still(tmp_path: Path) -> None:
    """SC-5.4: no write storm on a fast run."""
    from unittest.mock import Mock, patch

    save_mock = Mock()
    config = _make_config(tmp_path, discord_token=DISCORD_TOKEN)
    state = _make_state()
    with (
        aioresponses() as mock,
        patch("discord_ferry.migrator.avatars.save_state", save_mock),
        patch("discord_ferry.migrator.avatars.time.monotonic", Mock(return_value=1.0)),
    ):
        for i in range(3):
            mock.get(_user_url(f"90{i}"), payload={"id": f"90{i}", "avatar": _HASH})
        await run_avatars(config, state, [_three_author_export()], lambda e: None)
    assert save_mock.call_count == 0
    assert len(state.author_avatar_links) == 3


# ---------------------------------------------------------------------------
# Reporting: warnings, notice and progress events (S4)
# ---------------------------------------------------------------------------


def _picture_warnings(state: MigrationState) -> list[dict[str, str]]:
    return [w for w in state.warnings if w.get("type") == "author_pictures"]


async def test_the_summary_event_carries_the_count_and_each_reason(tmp_path: Path) -> None:
    """SC-4.3 (event half): three pictures, two authors with none, one failed lookup."""
    authors = [_make_author(str(800 + i)) for i in range(6)]
    config = _make_config(tmp_path, discord_token=DISCORD_TOKEN)
    state = _make_state()
    events: list[MigrationEvent] = []
    with aioresponses() as mock:
        for author in authors[:3]:
            mock.get(_user_url(author.id), payload={"id": author.id, "avatar": _HASH})
        for author in authors[3:5]:
            mock.get(_user_url(author.id), payload={"id": author.id, "avatar": None})
        mock.get(_user_url(authors[5].id), status=404, payload={"code": 10013})
        await run_avatars(config, state, [_export_of(*authors)], events.append)

    completed = events[-1]
    assert completed.status == "completed"
    assert completed.message == (
        "Pictures set for 3 of 6 authors (no Discord picture: 2, lookup failed: 1)."
    )
    assert completed.detail == {
        "author_pictures": {"total": 6, "with_picture": 3, "no_picture": 2, "lookup_failed": 1}
    }


async def test_progress_events_count_the_authors_that_need_work(tmp_path: Path) -> None:
    authors = [_make_author(str(810 + i)) for i in range(3)]
    config = _make_config(tmp_path, discord_token=DISCORD_TOKEN)
    events: list[MigrationEvent] = []
    with aioresponses() as mock:
        for author in authors:
            mock.get(_user_url(author.id), payload={"id": author.id, "avatar": _HASH})
        await run_avatars(config, _make_state(), [_export_of(*authors)], events.append)

    progress = [e for e in events if e.status == "progress"]
    assert [e.message for e in progress] == [
        "Looking up picture 1 of 3",
        "Looking up picture 2 of 3",
        "Looking up picture 3 of 3",
    ]
    assert [(e.current, e.total) for e in progress] == [(1, 3), (2, 3), (3, 3)]


async def test_no_token_gives_one_notice_and_one_warning_not_one_per_author(
    tmp_path: Path,
) -> None:
    """SC-4.2: 40 authors with local picture paths and no token."""
    authors = [_make_author(str(900 + i), avatar_url=f"media/avatars/{i}.png") for i in range(40)]
    config = _make_config(tmp_path)
    state = _make_state()
    events: list[MigrationEvent] = []
    with aioresponses() as mock:
        await run_avatars(config, state, [_export_of(*authors)], events.append)
        assert _request_count(mock) == 0

    assert len(state.warnings) == 1
    warning = _picture_warnings(state)[0]
    assert warning["phase"] == "avatars"
    assert warning["count"] == "40"
    assert "need a Discord token" in warning["message"]
    notices = [e for e in events if e.status == "notice"]
    assert len(notices) == 1
    assert "need a Discord token" in notices[0].message
    assert events[-1].status == "completed"


async def test_a_run_with_a_token_gives_no_token_notice(tmp_path: Path) -> None:
    config = _make_config(tmp_path, discord_token=DISCORD_TOKEN)
    events: list[MigrationEvent] = []
    with aioresponses() as mock:
        mock.get(_user_url("42"), payload={"id": "42", "avatar": _HASH})
        await run_avatars(config, _make_state(), [_export_of(_make_author("42"))], events.append)
    assert [e for e in events if e.status == "notice"] == []


async def test_a_token_that_cannot_look_people_up_is_named_in_the_warning(
    tmp_path: Path,
) -> None:
    """SC-2.10 (warning half)."""
    authors = [_make_author("201"), _make_author("203")]
    config = _make_config(tmp_path, discord_token=DISCORD_TOKEN)
    state = _make_state()
    with aioresponses() as mock:
        mock.get(_user_url("201"), status=403, body="Forbidden")
        await run_avatars(config, state, [_export_of(*authors)], lambda e: None)
    (warning,) = _picture_warnings(state)
    assert warning["count"] == "2"
    assert "cannot look up other users" in warning["message"]


async def test_each_reason_gets_its_own_counted_warning(tmp_path: Path) -> None:
    authors = [
        _make_author("1", avatar_url="http://cdn.discordapp.com/avatars/1/a.png"),
        _make_author("2", avatar_url="https://evil.example/a.png"),
        _make_author("3"),
    ]
    config = _make_config(tmp_path, discord_token=DISCORD_TOKEN)
    state = _make_state()
    with aioresponses() as mock:
        mock.get(_user_url("3"), payload={"id": "3", "avatar": None})
        await run_avatars(config, state, [_export_of(*authors)], lambda e: None)
    by_count = {w["count"]: w["message"] for w in _picture_warnings(state)}
    assert sorted(by_count) == ["1", "2"]
    assert "were ignored" in by_count["2"]
    assert "no Discord picture" in by_count["1"]


async def test_a_resumed_phase_replaces_its_warnings_instead_of_repeating_them(
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path, resume=True)
    state = _make_state()
    export = _export_of(_make_author("42"))
    await run_avatars(config, state, [export], lambda e: None)
    await run_avatars(config, state, [export], lambda e: None)
    assert len(_picture_warnings(state)) == 1


async def test_a_run_with_nothing_to_do_says_so(tmp_path: Path) -> None:
    events: list[MigrationEvent] = []
    state = _make_state()
    await run_avatars(_make_config(tmp_path), state, [_make_export([])], events.append)
    assert events[-1].message == "No authors found, so there are no pictures to set."
    assert _picture_warnings(state) == []


def test_every_reason_code_has_warning_text() -> None:
    """A new reason code in state.py fails here until it has wording."""
    from discord_ferry.migrator.avatars import _WARNING_TEXT
    from discord_ferry.state import AVATAR_REASON_CODES

    assert set(_WARNING_TEXT) == set(AVATAR_REASON_CODES)
    assert all("{count}" in text for text in _WARNING_TEXT.values())


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, "token_rejected"),
        (403, "lookup_not_allowed"),
        (404, "lookup_failed"),
        (500, "lookup_failed"),
    ],
)
async def test_the_discord_token_never_reaches_a_log_a_document_or_an_event(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    recorded_sleeps: list[float],
    status: int,
    reason: str,
) -> None:
    """SC-2.13. The secret is deliberately NOT registered for masking, so a leak shows.

    Discord's error body echoes the token, so anything that stored or printed the error
    text would carry the sentinel into the checks below.
    """
    import json
    import logging

    from discord_ferry.reporter import generate_markdown_report, generate_report
    from discord_ferry.state import save_state

    sentinel = "TESTTOKEN-SENTINEL-1208"
    config = _make_config(tmp_path, discord_token=DISCORD_TOKEN)
    state = _make_state()
    export = _export_of(_make_author("42"))
    events: list[MigrationEvent] = []
    caplog.set_level(logging.DEBUG)
    with aioresponses() as mock:
        for _ in range(3 if status == 500 else 1):
            mock.get(_user_url("42"), status=status, body=f"rejected {DISCORD_TOKEN}")
        await run_avatars(config, state, [export], events.append)
    assert state.author_avatar_reasons == {"42": reason}

    save_state(state, config.output_dir)
    generate_report(config, state, [export])
    generate_markdown_report(config, state, [export])
    texts = [caplog.text]
    texts += [
        (config.output_dir / name).read_text(encoding="utf-8")
        for name in ("state.json", "migration_report.json", "migration_report.md")
    ]
    texts += [f"{e.message} {json.dumps(e.detail)} {e.channel_name}" for e in events]
    for text in texts:
        assert sentinel not in text
    assert (config.output_dir / "state.json").exists()
