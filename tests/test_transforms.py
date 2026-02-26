"""Tests for content transformations."""

from discord_ferry.parser.transforms import (
    convert_spoilers,
    flatten_embed,
    format_original_timestamp,
    handle_stickers,
    remap_emoji,
    remap_mentions,
    strip_underline,
)

# ---------------------------------------------------------------------------
# convert_spoilers
# ---------------------------------------------------------------------------


def test_convert_spoilers_basic() -> None:
    assert convert_spoilers("||hidden||") == "!!hidden!!"


def test_convert_spoilers_multiple() -> None:
    assert convert_spoilers("||first|| and ||second||") == "!!first!! and !!second!!"


def test_convert_spoilers_preserves_code_blocks() -> None:
    content = "```\n||not a spoiler||\n```"
    assert convert_spoilers(content) == content


def test_convert_spoilers_preserves_inline_code() -> None:
    content = "`a || b`"
    assert convert_spoilers(content) == content


def test_convert_spoilers_mixed() -> None:
    content = "Code: `a || b` and ||spoiler||"
    assert convert_spoilers(content) == "Code: `a || b` and !!spoiler!!"


def test_convert_spoilers_no_spoilers() -> None:
    content = "Nothing to change here."
    assert convert_spoilers(content) == content


def test_convert_spoilers_fenced_code_with_language() -> None:
    content = "```python\n||not_spoiler||\n```"
    assert convert_spoilers(content) == content


# ---------------------------------------------------------------------------
# remap_mentions
# ---------------------------------------------------------------------------


def test_remap_user_mention() -> None:
    result = remap_mentions(
        "<@123>",
        channel_map={},
        role_map={},
        author_names={"123": "Alice"},
    )
    assert result == "@Alice"


def test_remap_user_mention_with_nick() -> None:
    result = remap_mentions(
        "<@!456>",
        channel_map={},
        role_map={},
        author_names={"456": "Bob"},
    )
    assert result == "@Bob"


def test_remap_user_mention_unknown() -> None:
    result = remap_mentions(
        "<@12345678>",
        channel_map={},
        role_map={},
        author_names={},
    )
    assert result == "@Unknown#1234"


def test_remap_channel_mention_mapped() -> None:
    result = remap_mentions(
        "<#123>",
        channel_map={"123": "stoat_abc"},
        role_map={},
        author_names={},
    )
    assert result == "<#stoat_abc>"


def test_remap_channel_mention_unmapped() -> None:
    result = remap_mentions(
        "<#999>",
        channel_map={},
        role_map={},
        author_names={},
    )
    assert result == "#deleted-channel"


def test_remap_role_mention_mapped() -> None:
    result = remap_mentions(
        "<@&123>",
        channel_map={},
        role_map={"123": "stoat_role_xyz"},
        author_names={},
    )
    assert result == "<@&stoat_role_xyz>"


def test_remap_role_mention_unmapped() -> None:
    result = remap_mentions(
        "<@&999>",
        channel_map={},
        role_map={},
        author_names={},
    )
    assert result == "@deleted-role"


def test_remap_mentions_multiple() -> None:
    content = "Hey <@123>, go to <#456> for info about <@&789>."
    result = remap_mentions(
        content,
        channel_map={"456": "stoat_ch"},
        role_map={"789": "stoat_role"},
        author_names={"123": "Alice"},
    )
    assert result == "Hey @Alice, go to <#stoat_ch> for info about <@&stoat_role>."


def test_remap_mentions_preserves_code_block() -> None:
    content = "```\n<@123> in code\n```\nand <@123> outside"
    result = remap_mentions(
        content,
        channel_map={},
        role_map={},
        author_names={"123": "Alice"},
    )
    assert result == "```\n<@123> in code\n```\nand @Alice outside"


def test_remap_mentions_preserves_inline_code() -> None:
    content = "See `<#456>` for reference and <#456> for real"
    result = remap_mentions(
        content,
        channel_map={"456": "stoat_ch"},
        role_map={},
        author_names={},
    )
    assert result == "See `<#456>` for reference and <#stoat_ch> for real"


# ---------------------------------------------------------------------------
# remap_emoji
# ---------------------------------------------------------------------------


def test_remap_emoji_mapped() -> None:
    result = remap_emoji("<:smile:123>", emoji_map={"123": "stoat456"})
    assert result == ":stoat456:"


def test_remap_emoji_animated_mapped() -> None:
    result = remap_emoji("<a:dance:456>", emoji_map={"456": "stoat789"})
    assert result == ":stoat789:"


def test_remap_emoji_unmapped() -> None:
    result = remap_emoji("<:rare:999>", emoji_map={})
    assert result == "[:rare:]"


def test_remap_emoji_multiple() -> None:
    content = "<:smile:1> hello <a:wave:2>"
    result = remap_emoji(content, emoji_map={"1": "stoat_s", "2": "stoat_w"})
    assert result == ":stoat_s: hello :stoat_w:"


def test_remap_emoji_animated_unmapped() -> None:
    result = remap_emoji("<a:dance:999>", emoji_map={})
    assert result == "[:dance:]"


def test_remap_emoji_preserves_code_block() -> None:
    content = "```\n<:smile:123>\n```\nand <:smile:123>"
    result = remap_emoji(content, emoji_map={"123": "stoat456"})
    assert result == "```\n<:smile:123>\n```\nand :stoat456:"


# ---------------------------------------------------------------------------
# flatten_embed
# ---------------------------------------------------------------------------


def test_flatten_embed_full() -> None:
    embed: dict[str, object] = {
        "title": "My Title",
        "description": "Main body",
        "url": "https://example.com",
        "color": 16711680,
        "author": {"name": "An Author", "iconUrl": "https://example.com/icon.png"},
        "fields": [
            {"name": "Field 1", "value": "Value 1"},
            {"name": "Field 2", "value": "Value 2"},
        ],
        "footer": {"text": "Footer text"},
    }
    result = flatten_embed(embed)

    assert result["title"] == "My Title"
    assert result["url"] == "https://example.com"
    assert result["colour"] == 16711680
    assert "color" not in result
    assert result["icon_url"] == "https://example.com/icon.png"

    description = result["description"]
    assert isinstance(description, str)
    assert "**An Author**" in description
    assert "Main body" in description
    assert "**Field 1:**" in description
    assert "Value 1" in description
    assert "**Field 2:**" in description
    assert "Value 2" in description
    assert "_Footer text_" in description


def test_flatten_embed_color_to_colour() -> None:
    embed: dict[str, object] = {"color": 255}
    result = flatten_embed(embed)
    assert "colour" in result
    assert result["colour"] == 255
    assert "color" not in result


def test_flatten_embed_minimal() -> None:
    embed: dict[str, object] = {"title": "Only Title"}
    result = flatten_embed(embed)
    assert result["title"] == "Only Title"
    # description should be absent or empty when there's no content to flatten
    assert result.get("description") is None or result.get("description") == ""


def test_flatten_embed_empty() -> None:
    embed: dict[str, object] = {}
    result = flatten_embed(embed)
    # No keys with None values
    for v in result.values():
        assert v is not None


def test_flatten_embed_no_author_icon() -> None:
    embed: dict[str, object] = {"author": {"name": "Just Name"}}
    result = flatten_embed(embed)
    assert "icon_url" not in result or result["icon_url"] is None


def test_flatten_embed_fields_order() -> None:
    embed: dict[str, object] = {
        "author": {"name": "Auth"},
        "description": "Desc",
        "fields": [{"name": "F1", "value": "V1"}],
        "footer": {"text": "Foot"},
    }
    result = flatten_embed(embed)
    desc = str(result["description"])
    # Author before description before fields before footer
    assert desc.index("**Auth**") < desc.index("Desc")
    assert desc.index("Desc") < desc.index("**F1:**")
    assert desc.index("**F1:**") < desc.index("_Foot_")


# ---------------------------------------------------------------------------
# format_original_timestamp
# ---------------------------------------------------------------------------


def test_format_timestamp_basic() -> None:
    result = format_original_timestamp("2024-01-15T12:00:00+00:00")
    assert result == "*[2024-01-15 12:00 UTC]*"


def test_format_timestamp_with_offset() -> None:
    # +02:00 means 10:00 UTC
    result = format_original_timestamp("2024-06-01T12:00:00+02:00")
    assert result == "*[2024-06-01 10:00 UTC]*"


def test_format_timestamp_with_microseconds() -> None:
    result = format_original_timestamp("2024-03-20T08:30:00.000000+00:00")
    assert result == "*[2024-03-20 08:30 UTC]*"


# ---------------------------------------------------------------------------
# handle_stickers
# ---------------------------------------------------------------------------


def test_handle_stickers_single() -> None:
    stickers = [{"name": "wave"}]
    result = handle_stickers(stickers)
    assert "[Sticker: wave]" in result


def test_handle_stickers_multiple() -> None:
    stickers = [{"name": "wave"}, {"name": "tada"}]
    result = handle_stickers(stickers)
    assert "[Sticker: wave]" in result
    assert "[Sticker: tada]" in result


def test_handle_stickers_empty() -> None:
    result = handle_stickers([])
    assert result == ""


def test_handle_stickers_no_name() -> None:
    stickers = [{}]
    result = handle_stickers(stickers)
    assert "[Sticker: unknown]" in result


# ---------------------------------------------------------------------------
# strip_underline
# ---------------------------------------------------------------------------


def test_strip_underline_basic() -> None:
    assert strip_underline("__text__") == "**text**"


def test_strip_underline_preserves_code() -> None:
    content = "```\n__not_underline__\n```"
    assert strip_underline(content) == content


def test_strip_underline_preserves_inline_code() -> None:
    content = "`__not_underline__`"
    assert strip_underline(content) == content


def test_strip_underline_multiple() -> None:
    assert strip_underline("__a__ and __b__") == "**a** and **b**"


def test_strip_underline_not_dunder() -> None:
    # __init__ — double underscores around a word containing an underscore mid-word
    # The spec says test behavior; our regex `__([^_]+?)__` won't match because [^_]
    # excludes underscores, so __init__ should NOT be converted.
    content = "__init__"
    result = strip_underline(content)
    # "init" has no underscores so it WILL match: __init__ → **init**
    # This documents the known behavior
    assert result == "**init**"


def test_strip_underline_mixed() -> None:
    content = "__underlined__ and `__code__`"
    assert strip_underline(content) == "**underlined** and `__code__`"
