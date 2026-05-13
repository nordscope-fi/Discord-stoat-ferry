"""Tests for pre-creation review summary."""

from __future__ import annotations

from discord_ferry.discord.metadata import ChannelMeta, DiscordMetadata, PermissionPair
from discord_ferry.parser.models import (
    DCEAuthor,
    DCEChannel,
    DCEExport,
    DCEGuild,
    DCEMessage,
    DCERole,
)
from discord_ferry.review import (
    ReviewSummary,
    UntrackedSuspectChannel,
    _decode_ulid_timestamp,
    build_review_summary,
    build_rollback_summary,
)
from discord_ferry.state import (
    MigrationState,
    RollbackFailure,
    RollbackProgress,
)


def _make_export(
    guild_id: str = "111",
    guild_name: str = "Test",
    channel_id: str = "ch1",
    channel_name: str = "general",
    channel_type: int = 0,
    category_id: str = "cat1",
    is_thread: bool = False,
    message_count: int = 10,
    messages: list[DCEMessage] | None = None,
) -> DCEExport:
    guild = DCEGuild(id=guild_id, name=guild_name, icon_url="")
    channel = DCEChannel(
        id=channel_id,
        type=channel_type,
        name=channel_name,
        category_id=category_id,
        category="General",
    )
    return DCEExport(
        guild=guild,
        channel=channel,
        messages=messages or [],
        message_count=message_count,
        is_thread=is_thread,
    )


def test_basic_summary() -> None:
    exports = [
        _make_export(channel_id="ch1", message_count=100),
        _make_export(channel_id="ch2", message_count=50),
    ]
    summary = build_review_summary(exports)
    assert summary.server_name == "Test"
    assert summary.channel_count == 2
    assert summary.message_count == 150
    assert summary.has_permissions is False
    assert "permissions" in summary.warnings[0].lower()


def test_with_metadata() -> None:
    exports = [_make_export()]
    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={"r1": PermissionPair(allow=1, deny=0)},
        channel_metadata={
            "ch1": ChannelMeta(nsfw=True),
            "ch2": ChannelMeta(nsfw=False),
        },
    )
    summary = build_review_summary(exports, discord_metadata=meta)
    assert summary.has_permissions is True
    assert summary.nsfw_channel_count == 1
    assert not any("permissions" in w.lower() for w in summary.warnings)


def test_empty_exports() -> None:
    summary = build_review_summary([])
    assert summary.server_name == "(empty)"
    assert summary.channel_count == 0
    assert "No exports found" in summary.warnings


def test_thread_counting() -> None:
    exports = [
        _make_export(channel_id="ch1", is_thread=False),
        _make_export(channel_id="th1", is_thread=True),
        _make_export(channel_id="th2", is_thread=True),
    ]
    summary = build_review_summary(exports)
    assert summary.thread_count == 2
    assert summary.channel_count == 3


def test_role_counting_excludes_everyone() -> None:
    role = DCERole(id="r1", name="Admin")
    everyone = DCERole(id="111", name="@everyone")
    msg = DCEMessage(
        id="m1",
        type="Default",
        timestamp="t",
        content="hi",
        author=DCEAuthor(id="u1", name="User", roles=[role, everyone]),
    )
    exports = [_make_export(guild_id="111", messages=[msg])]
    summary = build_review_summary(exports)
    assert summary.role_count == 1  # @everyone excluded


def test_returns_review_summary_type() -> None:
    exports = [_make_export()]
    summary = build_review_summary(exports)
    assert isinstance(summary, ReviewSummary)


def test_category_counting() -> None:
    exports = [
        _make_export(channel_id="ch1", category_id="cat1"),
        _make_export(channel_id="ch2", category_id="cat1"),  # same category
        _make_export(channel_id="ch3", category_id="cat2"),  # different category
    ]
    summary = build_review_summary(exports)
    assert summary.category_count == 2
    assert summary.channel_count == 3


def test_category_type_channel_excluded_from_channel_count() -> None:
    exports = [
        _make_export(channel_id="ch1", channel_type=0),  # text channel
        _make_export(channel_id="cat1", channel_type=4),  # category — should NOT count
    ]
    summary = build_review_summary(exports)
    assert summary.channel_count == 1


def test_channel_count_warning_at_limit() -> None:
    # Create 201 unique channels
    exports = [_make_export(channel_id=f"ch{i}", category_id="") for i in range(201)]
    summary = build_review_summary(exports)
    assert any("200" in w for w in summary.warnings)


def test_no_warnings_when_metadata_provided() -> None:
    exports = [_make_export()]
    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
    )
    summary = build_review_summary(exports, discord_metadata=meta)
    assert summary.has_permissions is True
    # No "No Discord token" warning
    assert not any("permissions" in w.lower() for w in summary.warnings)


def test_user_override_count_from_metadata() -> None:
    """ReviewSummary.user_override_count populated from metadata."""
    exports = [_make_export()]
    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
        user_override_channels=[
            {"channel_id": "ch1", "channel_name": "general", "override_count": 3},
            {"channel_id": "ch2", "channel_name": "mods", "override_count": 1},
        ],
    )
    summary = build_review_summary(exports, discord_metadata=meta)
    assert summary.user_override_count == 2


def test_user_override_count_zero_without_metadata() -> None:
    """ReviewSummary.user_override_count is 0 when no metadata provided."""
    exports = [_make_export()]
    summary = build_review_summary(exports)
    assert summary.user_override_count == 0


def test_user_override_count_zero_when_no_overrides() -> None:
    """ReviewSummary.user_override_count is 0 when metadata has no user overrides."""
    exports = [_make_export()]
    meta = DiscordMetadata(
        guild_id="111",
        fetched_at="t",
        server_default_permissions=0,
        role_permissions={},
        channel_metadata={},
    )
    summary = build_review_summary(exports, discord_metadata=meta)
    assert summary.user_override_count == 0


def test_review_summary_threads_filtered() -> None:
    """ReviewSummary.threads_filtered defaults to 0 and is settable."""
    exports = [_make_export()]
    summary = build_review_summary(exports)
    # Default is 0
    assert summary.threads_filtered == 0
    # Engine sets it after building the summary
    summary.threads_filtered = 3
    assert summary.threads_filtered == 3


# ---------------------------------------------------------------------------
# Rollback summary + ULID decoder (issue #10)
# ---------------------------------------------------------------------------


def test_decode_ulid_timestamp_known_value() -> None:
    """Unit: a hand-crafted ULID decodes to the expected ISO 8601 string."""
    # 01KPTJT1G0 encodes 1776861120000 ms = 2026-04-22T12:32:00+00:00
    ulid = "01KPTJT1G00123456789ABCDEF"
    assert _decode_ulid_timestamp(ulid) == "2026-04-22T12:32:00+00:00"


def test_decode_ulid_timestamp_wrong_length() -> None:
    """Edge: <26 or >26 chars returns None."""
    assert _decode_ulid_timestamp("01KPTJT1G0") is None
    assert _decode_ulid_timestamp("01KPTJT1G00123456789ABCDEFG") is None
    assert _decode_ulid_timestamp("") is None


def test_decode_ulid_timestamp_invalid_chars() -> None:
    """Edge: non-Crockford chars (e.g. U, special chars) return None."""
    # 'U' is the only non-aliased excluded char in the Crockford alphabet.
    assert _decode_ulid_timestamp("U" + "1" * 25) is None
    # Special char.
    assert _decode_ulid_timestamp("!" + "1" * 25) is None


def test_decode_ulid_timestamp_crockford_aliases() -> None:
    """Edge: I/L/O are accepted as aliases for 1/1/0."""
    # 01KPTJT1G0 -> same value when 'O' replaces the leading '0' (O = 0).
    aliased = "O1KPTJT1GO" + "0123456789ABCDEF"
    assert _decode_ulid_timestamp(aliased) == "2026-04-22T12:32:00+00:00"


def test_build_rollback_summary_basic() -> None:
    """build_rollback_summary assembles counts from state + server response."""
    state = MigrationState(
        stoat_server_id="srv01",
        channel_map={"d1": "ch01", "d2": "ch02"},
        role_map={"d_role": "role01"},
        emoji_map={"d_em": "em01"},
        category_map={"d_cat": "cat01"},
    )
    server = {
        "_id": "srv01",
        "name": "Target Server",
        "channels": ["ch01", "ch02"],
        "roles": {"role01": {"name": "Mod"}},
    }
    summary = build_rollback_summary(state, server, untracked_ferry_suspect=[])

    assert summary.stoat_server_id == "srv01"
    assert summary.stoat_server_name == "Target Server"
    assert sorted(summary.channels_to_delete) == [("ch01", ""), ("ch02", "")]
    assert summary.roles_to_delete == [("role01", "Mod")]
    assert summary.emoji_to_delete == [("em01", "")]
    assert summary.categories_to_clean == 1
    assert summary.has_failures_from_prior_run is False


def test_build_rollback_summary_with_suspect_channels() -> None:
    """SC-9: untracked-suspect channels appear in the summary with decoded timestamps."""
    state = MigrationState(
        stoat_server_id="srv01",
        channel_map={"d1": "s1", "d2": "s2"},
    )
    server = {
        "_id": "srv01",
        "name": "T",
        "channels": ["s1", "s2", "s3", "s4"],
        "roles": {},
    }
    # ULID-shaped suspect IDs with decoded timestamps.
    suspects = [
        UntrackedSuspectChannel(
            stoat_id="01KPTJT1G00123456789ABCDEF",
            name="orphan-1",
            created_at_iso="2026-04-22T12:32:00+00:00",
        ),
        UntrackedSuspectChannel(
            stoat_id="01KPTJT1G00123456789ABCDEG",
            name="orphan-2",
            created_at_iso="2026-04-22T12:32:00+00:00",
        ),
    ]
    summary = build_rollback_summary(state, server, untracked_ferry_suspect=suspects)

    assert len(summary.untracked_ferry_suspect) == 2
    assert summary.untracked_ferry_suspect[0].stoat_id == "01KPTJT1G00123456789ABCDEF"
    assert summary.untracked_ferry_suspect[0].opted_in is False
    assert summary.untracked_ferry_suspect[0].created_at_iso is not None
    # channels_to_delete only contains the mapped ones (s1, s2), not the suspects.
    assert sorted(s for s, _ in summary.channels_to_delete) == ["s1", "s2"]


def test_build_rollback_summary_with_non_ulid_id() -> None:
    """SC-10: a hand-typed non-ULID suspect ID has created_at_iso = None."""
    bad_id = "not-a-ulid"
    suspect = UntrackedSuspectChannel(
        stoat_id=bad_id,
        name="weird",
        created_at_iso=_decode_ulid_timestamp(bad_id),
    )
    state = MigrationState(stoat_server_id="srv01")
    server = {"_id": "srv01", "name": "T", "channels": [], "roles": {}}
    summary = build_rollback_summary(state, server, untracked_ferry_suspect=[suspect])

    assert summary.untracked_ferry_suspect[0].created_at_iso is None


def test_build_rollback_summary_skips_already_deleted() -> None:
    """Channels in state.channel_map but missing from server.channels are skipped."""
    state = MigrationState(
        stoat_server_id="srv01",
        channel_map={"d1": "ch01", "d2": "ch_gone"},
    )
    server = {
        "_id": "srv01",
        "name": "T",
        "channels": ["ch01"],  # ch_gone is missing — already deleted
        "roles": {},
    }
    summary = build_rollback_summary(state, server, untracked_ferry_suspect=[])
    assert [s for s, _ in summary.channels_to_delete] == ["ch01"]


def test_build_rollback_summary_skips_rolled_back_ids() -> None:
    """IDs in state.rollback_progress.rolled_back_ids are skipped on re-run."""
    state = MigrationState(
        stoat_server_id="srv01",
        channel_map={"d1": "ch01", "d2": "ch02"},
        rollback_progress=RollbackProgress(rolled_back_ids={"ch01"}),
    )
    server = {
        "_id": "srv01",
        "name": "T",
        "channels": ["ch01", "ch02"],
        "roles": {},
    }
    summary = build_rollback_summary(state, server, untracked_ferry_suspect=[])
    assert [s for s, _ in summary.channels_to_delete] == ["ch02"]


def test_build_rollback_summary_carries_prior_failures_flag() -> None:
    """has_failures_from_prior_run is True when rollback_progress.failures is non-empty."""
    state = MigrationState(
        stoat_server_id="srv01",
        rollback_progress=RollbackProgress(
            failures=[
                RollbackFailure(
                    entity_type="role", stoat_id="r1", error="x", http_status=403
                )
            ]
        ),
    )
    server = {"_id": "srv01", "name": "T", "channels": [], "roles": {}}
    summary = build_rollback_summary(state, server, untracked_ferry_suspect=[])
    assert summary.has_failures_from_prior_run is True


def test_build_rollback_summary_autumn_orphan_count() -> None:
    """autumn_orphan_count = uploads not in referenced_autumn_ids."""
    state = MigrationState(
        stoat_server_id="srv01",
        autumn_uploads={"a1": "src1", "a2": "src2", "a3": "src3"},
        referenced_autumn_ids={"a1"},
    )
    server = {"_id": "srv01", "name": "T", "channels": [], "roles": {}}
    summary = build_rollback_summary(state, server, untracked_ferry_suspect=[])
    assert summary.autumn_orphan_count == 2
