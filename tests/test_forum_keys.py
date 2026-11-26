"""#1640: the key each forum's posts are filed under."""

from __future__ import annotations

from discord_ferry.discord import client
from discord_ferry.discord.metadata import (
    DiscordMetadata,
    capture_permission_source,
    forum_group_key,
    forum_key_table,
    forum_parent_ids,
)
from discord_ferry.parser.models import DCEChannel, DCEExport, DCEGuild
from discord_ferry.state import MigrationState

PLAIN = "forum-Ideas & help"


def _meta(*forum_ids: str) -> DiscordMetadata:
    """Bound metadata that lists forums only, as a later run's metadata does."""
    forums = [
        client._parse_channel({"id": fid, "name": "forum", "type": 15, "guild_id": "111"})
        for fid in forum_ids
    ]
    return DiscordMetadata(
        "111", "now", 0, {}, {}, permission_source=capture_permission_source("111", [], forums)
    )


def _post(post_id: str, forum_id: str, name: str = "Ideas & help") -> DCEExport:
    return DCEExport(
        DCEGuild("111", "source"),
        DCEChannel(post_id, 11, "post", category_id=forum_id, category=name),
        is_thread=True,
        parent_channel_name=name,
    )


def test_a_unique_forum_name_keeps_the_plain_key() -> None:
    """SC-2.1."""
    table = forum_key_table(_meta("300"), MigrationState(), [_post("222", "300")])
    assert table == {"300": PLAIN}


def test_a_shared_name_on_a_fresh_run_gives_the_plain_key_to_the_lowest_id() -> None:
    """SC-1.1. The exports arrive highest id first, so list order cannot be the rule."""
    exports = [_post("333", "301"), _post("222", "300")]
    table = forum_key_table(_meta("300", "301"), MigrationState(), exports)
    assert table == {"300": PLAIN, "301": f"{PLAIN}~301"}


def test_ids_are_ordered_numerically_not_alphabetically() -> None:
    exports = [_post("a", "1000"), _post("b", "999")]
    table = forum_key_table(_meta("999", "1000"), MigrationState(), exports)
    assert table == {"999": PLAIN, "1000": f"{PLAIN}~1000"}


def test_a_saved_owner_keeps_the_plain_key_even_when_it_has_the_higher_id() -> None:
    state = MigrationState(category_map={PLAIN: "cat-a"}, forum_key_forums={PLAIN: ["301"]})
    exports = [_post("222", "300"), _post("333", "301")]
    table = forum_key_table(_meta("300", "301"), state, exports)
    assert table == {"300": f"{PLAIN}~300", "301": PLAIN}


def test_an_entry_naming_two_forums_keeps_every_forum_on_the_plain_key() -> None:
    """A merged state is not split, and a third forum of that name joins the merge."""
    state = MigrationState(category_map={PLAIN: "cat-a"}, forum_key_forums={PLAIN: ["300", "301"]})
    exports = [_post("222", "300"), _post("333", "301"), _post("444", "302")]
    table = forum_key_table(_meta("300", "301", "302"), state, exports)
    assert table == {"300": PLAIN, "301": PLAIN, "302": PLAIN}


def test_a_plain_key_with_no_entry_is_a_state_from_before_the_change() -> None:
    """SC-3.1 (table half). Nobody can say who owns it, so every forum keeps it."""
    state = MigrationState(category_map={PLAIN: "cat-a"})
    exports = [_post("222", "300"), _post("333", "301")]
    table = forum_key_table(_meta("300", "301"), state, exports)
    assert table == {"300": PLAIN, "301": PLAIN}


def test_a_later_run_holding_only_the_second_forum_reads_the_first_from_state() -> None:
    """SC-1.6 (table half). Forum 300 is not in the exports and its name is not in the metadata."""
    state = MigrationState(category_map={PLAIN: "cat-a"}, forum_key_forums={PLAIN: ["300"]})
    table = forum_key_table(_meta("300", "301"), state, [_post("444", "301")])
    assert table["301"] == f"{PLAIN}~301"
    assert table["300"] == PLAIN


def test_a_saved_suffixed_key_gives_its_name_back_to_the_group() -> None:
    """Forum 301 was filed under its own key; 302 of the same name must not take the plain key."""
    state = MigrationState(
        category_map={f"{PLAIN}~301": "cat-b"}, forum_key_forums={f"{PLAIN}~301": ["301"]}
    )
    table = forum_key_table(_meta("301", "302"), state, [_post("555", "302")])
    assert table == {"301": f"{PLAIN}~301", "302": PLAIN}


def test_without_metadata_the_table_is_empty() -> None:
    """SC-2.2."""
    assert forum_key_table(None, MigrationState(), [_post("222", "300")]) == {}


def test_a_forum_named_like_a_suffixed_key_does_not_collide() -> None:
    """SC-1.8, the design's own example: forum 150 is literally named `Questions-200`."""
    exports = [
        _post("a", "100", "Questions"),
        _post("b", "200", "Questions"),
        _post("c", "150", "Questions-200"),
    ]
    table = forum_key_table(_meta("100", "150", "200"), MigrationState(), exports)
    assert table == {
        "100": "forum-Questions",
        "200": "forum-Questions~200",
        "150": "forum-Questions-200",
    }
    assert len(set(table.values())) == 3


def test_a_forum_named_like_a_suffixed_key_falls_back_to_plain_name_keys() -> None:
    """SC-1.8, the case the scenario text gets wrong: `Q~301` makes the duplicate check fire.

    Forums 300 and 301 are both `Q`, so 301 would get `forum-Q~301`, which is also the
    plain key of forum 302 named `Q~301`. The design's rule is that both colliding forums
    take their plain name keys, which merges 301 into 300 (today's behaviour) and keeps
    302 apart.
    """
    exports = [_post("a", "300", "Q"), _post("b", "301", "Q"), _post("c", "302", "Q~301")]
    table = forum_key_table(_meta("300", "301", "302"), MigrationState(), exports)
    assert table == {"300": "forum-Q", "301": "forum-Q", "302": "forum-Q~301"}


def test_a_non_forum_export_has_no_group_key() -> None:
    plain_channel = DCEExport(DCEGuild("111", "source"), DCEChannel("5", 0, "general"))
    assert forum_group_key(plain_channel, frozenset({"300"}), {}, {}) is None


def test_a_real_post_takes_its_forums_table_key() -> None:
    forum_ids = forum_parent_ids(_meta("300", "301"))
    table = {"300": PLAIN, "301": f"{PLAIN}~301"}
    assert forum_group_key(_post("333", "301"), forum_ids, table, {}) == f"{PLAIN}~301"


def test_a_saved_category_key_wins_over_the_table() -> None:
    """SC-3.2: a post filed before this change keeps its key whatever the table says."""
    forum_ids = frozenset({"300", "301"})
    table = {"300": PLAIN, "301": f"{PLAIN}~301"}
    saved = {"333": PLAIN}
    assert forum_group_key(_post("333", "301"), forum_ids, table, saved) == PLAIN


def test_a_saved_key_that_is_not_a_forum_key_is_ignored() -> None:
    forum_ids = frozenset({"300"})
    assert forum_group_key(_post("222", "300"), forum_ids, {"300": PLAIN}, {"222": "cat1"}) == PLAIN


def test_a_legacy_export_without_a_forum_id_uses_the_name_key() -> None:
    """SC-2.2: a type 15 export whose category id is not a known forum."""
    legacy = DCEExport(
        DCEGuild("111", "source"),
        DCEChannel("9", 15, "post", category_id="999"),
        is_thread=True,
        parent_channel_name="Questions",
    )
    assert forum_group_key(legacy, frozenset({"300"}), {"300": PLAIN}, {}) == "forum-Questions"


def test_without_metadata_a_forum_post_uses_the_name_key() -> None:
    """SC-2.2: an empty forum id set and an empty table give today's key."""
    legacy = DCEExport(
        DCEGuild("111", "source"),
        DCEChannel("9", 15, "post", category_id="999"),
        is_thread=True,
        parent_channel_name="Questions",
    )
    assert forum_group_key(legacy, frozenset(), {}, {}) == "forum-Questions"
