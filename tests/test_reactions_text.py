"""Tests for the _build_reaction_text helper."""

from discord_ferry.migrator.messages import _build_reaction_text
from discord_ferry.parser.models import DCEEmoji, DCEReaction


def _r(name: str, count: int, eid: str = "") -> DCEReaction:
    return DCEReaction(emoji=DCEEmoji(id=eid, name=name), count=count)


def test_basic_formatting() -> None:
    result = _build_reaction_text([_r("thumbsup", 12), _r("tada", 5)], 500)
    assert "thumbsup 12" in result and "tada 5" in result and "·" in result


def test_single_reaction() -> None:
    result = _build_reaction_text([_r("heart", 1)], 500)
    assert "heart 1" in result and "·" not in result


def test_empty_reactions() -> None:
    assert _build_reaction_text([], 500) == ""


def test_zero_count_excluded() -> None:
    result = _build_reaction_text([_r("thumbsup", 5), _r("ghost", 0)], 500)
    assert "thumbsup" in result and "ghost" not in result


def test_zero_budget() -> None:
    assert _build_reaction_text([_r("thumbsup", 5)], 0) == ""


def test_truncation_on_tight_budget() -> None:
    reactions = [_r(f"emoji{i}", i + 1) for i in range(20)]
    result = _build_reaction_text(reactions, 50)
    assert len(result) <= 50 and "..." in result


def test_all_included_with_large_budget() -> None:
    reactions = [_r(f"e{i}", i + 1) for i in range(25)]
    result = _build_reaction_text(reactions, 2000)
    for i in range(25):
        assert f"e{i}" in result


def test_custom_emoji_mapped_uses_stoat_id() -> None:
    emoji_map = {"12345": "stoat_abc"}
    result = _build_reaction_text([_r("pepe", 5, eid="12345")], 500, emoji_map)
    assert ":stoat_abc:" in result
    assert "pepe" not in result


def test_custom_emoji_unmapped_uses_bracketed_name() -> None:
    result = _build_reaction_text([_r("pepe", 5, eid="12345")], 500, {})
    assert "[:pepe:]" in result
    assert "12345" not in result


def test_mixed_unicode_and_custom_emoji() -> None:
    emoji_map = {"111": "stoat_fire"}
    reactions = [
        _r("\U0001f44d", 3),
        _r("fire", 2, eid="111"),
        _r("catjam", 1, eid="999"),
    ]
    result = _build_reaction_text(reactions, 500, emoji_map)
    assert "\U0001f44d 3" in result
    assert ":stoat_fire: 2" in result
    assert "[:catjam:] 1" in result
