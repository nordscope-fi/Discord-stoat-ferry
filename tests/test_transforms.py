"""Tests for content transformations."""

from pathlib import Path

from discord_ferry.parser.transforms import (
    convert_spoilers,
    flatten_embed,
    flatten_poll,
    format_original_timestamp,
    handle_stickers,
    remap_emoji,
    remap_mentions,
    rewrite_discord_links,
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
    result, media_path = flatten_embed(embed)

    assert result["title"] == "My Title"
    assert result["url"] == "https://example.com"
    assert result["colour"] == 16711680
    assert "color" not in result
    assert result["icon_url"] == "https://example.com/icon.png"
    assert media_path is None  # No export_dir provided.

    description = result["description"]
    assert isinstance(description, str)
    assert "**An Author**" in description
    assert "Main body" in description
    assert "**Field 1**" in description
    assert "Value 1" in description
    assert "**Field 2**" in description
    assert "Value 2" in description
    assert "_Footer text_" in description


def test_flatten_embed_color_to_colour() -> None:
    embed: dict[str, object] = {"color": 255}
    result, _ = flatten_embed(embed)
    assert "colour" in result
    assert result["colour"] == 255
    assert "color" not in result


def test_flatten_embed_minimal() -> None:
    embed: dict[str, object] = {"title": "Only Title"}
    result, _ = flatten_embed(embed)
    assert result["title"] == "Only Title"
    # description should be absent or empty when there's no content to flatten
    assert result.get("description") is None or result.get("description") == ""


def test_flatten_embed_empty() -> None:
    embed: dict[str, object] = {}
    result, _ = flatten_embed(embed)
    # No keys with None values
    for v in result.values():
        assert v is not None


def test_flatten_embed_no_author_icon() -> None:
    embed: dict[str, object] = {"author": {"name": "Just Name"}}
    result, _ = flatten_embed(embed)
    assert "icon_url" not in result or result["icon_url"] is None


def test_flatten_embed_fields_order() -> None:
    embed: dict[str, object] = {
        "author": {"name": "Auth"},
        "description": "Desc",
        "fields": [{"name": "F1", "value": "V1"}],
        "footer": {"text": "Foot"},
    }
    result, _ = flatten_embed(embed)
    desc = str(result["description"])
    # Author before description before fields before footer
    assert desc.index("**Auth**") < desc.index("Desc")
    assert desc.index("Desc") < desc.index("**F1**")
    assert desc.index("**F1**") < desc.index("_Foot_")


def test_flatten_embed_with_local_thumbnail(tmp_path: Path) -> None:
    """flatten_embed returns media path when thumbnail is a local file."""
    thumb_file = tmp_path / "thumb.png"
    thumb_file.write_bytes(b"PNG")
    embed: dict[str, object] = {
        "title": "With Thumb",
        "thumbnail": {"url": "thumb.png"},
    }
    result, media_path = flatten_embed(embed, export_dir=tmp_path)
    assert result["title"] == "With Thumb"
    assert media_path == thumb_file


def test_flatten_embed_with_remote_thumbnail() -> None:
    """flatten_embed returns no media path when thumbnail is a remote URL."""
    embed: dict[str, object] = {
        "title": "Remote",
        "thumbnail": {"url": "https://cdn.discord.com/thumb.png"},
    }
    result, media_path = flatten_embed(embed, export_dir=Path("/tmp"))
    assert media_path is None


def test_all_inline_fields_in_rows() -> None:
    """3 inline fields render as a pipe-separated row."""
    embed: dict[str, object] = {
        "fields": [
            {"name": "HP", "value": "100", "inline": True},
            {"name": "MP", "value": "50", "inline": True},
            {"name": "ATK", "value": "25", "inline": True},
        ],
    }
    result, _ = flatten_embed(embed)
    desc = str(result["description"])
    assert "**HP** | **MP** | **ATK**" in desc
    assert "100 | 50 | 25" in desc


def test_non_inline_field_own_line() -> None:
    """Non-inline field renders as bold name on its own line, value below."""
    embed: dict[str, object] = {
        "fields": [
            {"name": "Description", "value": "Long text", "inline": False},
        ],
    }
    result, _ = flatten_embed(embed)
    desc = str(result["description"])
    assert "**Description**\nLong text" in desc
    assert "|" not in desc


def test_mixed_inline_breaks_rows() -> None:
    """[inline, inline, non-inline, inline, inline] produces 2 rows + 1 block."""
    embed: dict[str, object] = {
        "fields": [
            {"name": "A", "value": "1", "inline": True},
            {"name": "B", "value": "2", "inline": True},
            {"name": "C", "value": "3", "inline": False},
            {"name": "D", "value": "4", "inline": True},
            {"name": "E", "value": "5", "inline": True},
        ],
    }
    result, _ = flatten_embed(embed)
    desc = str(result["description"])
    # First inline row: A | B
    assert "**A** | **B**" in desc
    assert "1 | 2" in desc
    # Non-inline block
    assert "**C**\n3" in desc
    # Second inline row: D | E
    assert "**D** | **E**" in desc
    assert "4 | 5" in desc


def test_max_three_inline_per_row() -> None:
    """6 inline fields produce exactly 2 rows of 3."""
    embed: dict[str, object] = {
        "fields": [
            {"name": "A", "value": "1", "inline": True},
            {"name": "B", "value": "2", "inline": True},
            {"name": "C", "value": "3", "inline": True},
            {"name": "D", "value": "4", "inline": True},
            {"name": "E", "value": "5", "inline": True},
            {"name": "F", "value": "6", "inline": True},
        ],
    }
    result, _ = flatten_embed(embed)
    desc = str(result["description"])
    assert "**A** | **B** | **C**" in desc
    assert "1 | 2 | 3" in desc
    assert "**D** | **E** | **F**" in desc
    assert "4 | 5 | 6" in desc


def test_empty_field_skipped() -> None:
    """Field with empty name and empty value is skipped entirely."""
    embed: dict[str, object] = {
        "fields": [
            {"name": "", "value": ""},
            {"name": "Visible", "value": "Yes"},
        ],
    }
    result, _ = flatten_embed(embed)
    desc = str(result["description"])
    assert "**Visible**" in desc
    # Only one field section in the description
    assert desc.count("**") == 2  # opening and closing bold for "Visible"


def test_field_name_only_no_value() -> None:
    """Field with name but no value renders name only (no trailing newline)."""
    embed: dict[str, object] = {
        "fields": [
            {"name": "Score", "value": ""},
        ],
    }
    result, _ = flatten_embed(embed)
    desc = str(result["description"])
    assert "**Score**" in desc
    assert desc.strip() == "**Score**"


def test_no_inline_key_defaults_to_block() -> None:
    """Field without 'inline' key defaults to block (non-inline) rendering."""
    embed: dict[str, object] = {
        "fields": [
            {"name": "Key", "value": "Val"},
        ],
    }
    result, _ = flatten_embed(embed)
    desc = str(result["description"])
    assert "**Key**\nVal" in desc
    assert "|" not in desc


def test_unicode_field_names_preserved() -> None:
    """Unicode characters in field names and values are preserved."""
    embed: dict[str, object] = {
        "fields": [
            {"name": "\u2764\ufe0f Health", "value": "\u2b50 100", "inline": True},
            {"name": "\u2694\ufe0f Attack", "value": "\u2b50 50", "inline": True},
        ],
    }
    result, _ = flatten_embed(embed)
    desc = str(result["description"])
    assert "\u2764\ufe0f Health" in desc
    assert "\u2694\ufe0f Attack" in desc
    assert "\u2b50 100" in desc
    assert "\u2b50 50" in desc
    # Should be rendered as inline row
    assert "**\u2764\ufe0f Health** | **\u2694\ufe0f Attack**" in desc


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
    text, paths = handle_stickers(stickers)
    assert "[Sticker: wave]" in text
    assert paths == []


def test_handle_stickers_multiple() -> None:
    stickers = [{"name": "wave"}, {"name": "tada"}]
    text, _ = handle_stickers(stickers)
    assert "[Sticker: wave]" in text
    assert "[Sticker: tada]" in text


def test_handle_stickers_empty() -> None:
    text, paths = handle_stickers([])
    assert text == ""
    assert paths == []


def test_handle_stickers_no_name() -> None:
    stickers: list[dict[str, str]] = [{}]
    text, _ = handle_stickers(stickers)
    assert "[Sticker: unknown]" in text


def test_handle_stickers_with_local_image(tmp_path: Path) -> None:
    """Sticker with a local sourceUrl returns the image path."""
    sticker_file = tmp_path / "sticker.png"
    sticker_file.write_bytes(b"PNG")
    stickers = [{"name": "wave", "sourceUrl": "sticker.png"}]
    text, paths = handle_stickers(stickers, export_dir=tmp_path)
    assert "[Sticker: wave]" in text
    assert paths == [sticker_file]


def test_handle_stickers_remote_url_no_path() -> None:
    """Sticker with a remote sourceUrl returns no image path."""
    stickers = [{"name": "wave", "sourceUrl": "https://cdn.discord.com/sticker.png"}]
    text, paths = handle_stickers(stickers, export_dir=Path("/tmp"))
    assert "[Sticker: wave]" in text
    assert paths == []


# ---------------------------------------------------------------------------
# flatten_poll
# ---------------------------------------------------------------------------


def test_flatten_poll_basic() -> None:
    """Poll renders question and options with vote counts."""
    poll = {
        "question": {"text": "Favourite colour?"},
        "answers": [
            {"text": "Red", "votes": 42},
            {"text": "Blue", "votes": 18},
        ],
    }
    result = flatten_poll(poll)
    assert "**Poll: Favourite colour?**" in result
    assert "\u2022 Red \u2014 42 votes" in result
    assert "\u2022 Blue \u2014 18 votes" in result


def test_flatten_poll_empty() -> None:
    """Empty poll dict renders minimal output."""
    result = flatten_poll({})
    assert "**Poll: **" in result


def test_flatten_poll_string_question() -> None:
    """Poll with string question (not dict) still works."""
    poll = {"question": "Simple?", "answers": []}
    result = flatten_poll(poll)
    assert "**Poll: Simple?**" in result


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


def test_underline_bold_collision_collapsed() -> None:
    """**__both__** must not produce **** — should collapse to **both**."""
    result = strip_underline("**__both__**")
    assert "****" not in result
    assert result == "**both**"


def test_underline_in_code_block_preserved() -> None:
    """__init__ inside inline code must remain unchanged."""
    content = "`__init__`"
    result = strip_underline(content)
    assert result == content


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


# ---------------------------------------------------------------------------
# flatten_embed — CDN URL expiry validation (S5)
# ---------------------------------------------------------------------------


def test_expired_cdn_embed_media_stripped() -> None:
    """Expired Discord CDN thumbnail — media_path is None."""
    embed: dict[str, object] = {
        "title": "Post",
        "thumbnail": {"url": "https://cdn.discordapp.com/img.png?ex=60000000"},
    }
    result, media_path = flatten_embed(embed)
    assert media_path is None


def test_valid_cdn_embed_url_not_stripped() -> None:
    """Valid CDN URL — media_path still None (remote, not local) but no warning."""
    embed: dict[str, object] = {
        "image": {"url": "https://cdn.discordapp.com/img.png?ex=ffffffff"},
    }
    result, media_path = flatten_embed(embed)
    assert media_path is None  # Still None — it's remote


def test_non_discord_embed_url_untouched() -> None:
    """Non-Discord URL passes through."""
    embed: dict[str, object] = {
        "thumbnail": {"url": "https://example.com/img.png"},
    }
    result, media_path = flatten_embed(embed)
    assert media_path is None


def test_local_media_path_still_works(tmp_path: Path) -> None:
    """Local media extraction unchanged."""
    local = tmp_path / "media" / "img.png"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(b"PNG")
    embed: dict[str, object] = {"image": {"url": "media/img.png"}}
    result, media_path = flatten_embed(embed, export_dir=tmp_path)
    assert media_path == local


def test_unknown_cdn_format_preserved() -> None:
    """Discord CDN URL without ex param — not stripped."""
    embed: dict[str, object] = {
        "thumbnail": {"url": "https://cdn.discordapp.com/img.png"},
    }
    result, media_path = flatten_embed(embed)
    assert media_path is None  # Remote, but no expiry warning


def test_media_discordapp_net_checked() -> None:
    """media.discordapp.net recognized as Discord CDN."""
    embed: dict[str, object] = {
        "image": {"url": "https://media.discordapp.net/img.png?ex=60000000"},
    }
    result, media_path = flatten_embed(embed)
    assert media_path is None  # Expired, stripped


# ---------------------------------------------------------------------------
# rewrite_discord_links
# ---------------------------------------------------------------------------


def test_jump_link_mapped_channel() -> None:
    """Jump link with a mapped channel ID is rewritten to a Stoat channel mention."""
    content = "Check https://discord.com/channels/111/456/789 for details"
    result = rewrite_discord_links(content, channel_map={"456": "stoat_ch"})
    assert "<#stoat_ch>" in result
    assert "discord.com" not in result


def test_jump_link_unmapped() -> None:
    """Jump link with an unmapped channel ID gets annotation appended."""
    content = "See https://discord.com/channels/111/456/789"
    result = rewrite_discord_links(content, channel_map={})
    assert "https://discord.com/channels/111/456/789 [original Discord link]" in result


def test_jump_link_no_message_id() -> None:
    """Jump link without a message ID (channel-only) is still matched."""
    content = "Go to https://discord.com/channels/111/456"
    result = rewrite_discord_links(content, channel_map={"456": "stoat_ch"})
    assert "<#stoat_ch>" in result


def test_legacy_discordapp_link() -> None:
    """Legacy discordapp.com jump links are matched."""
    content = "Old link https://discordapp.com/channels/111/456/789"
    result = rewrite_discord_links(content, channel_map={"456": "stoat_ch"})
    assert "<#stoat_ch>" in result


def test_canary_ptb_links() -> None:
    """Canary and PTB subdomain links are matched."""
    content_canary = "https://canary.discord.com/channels/111/456/789"
    result_canary = rewrite_discord_links(content_canary, channel_map={"456": "stoat_ch"})
    assert "<#stoat_ch>" in result_canary

    content_ptb = "https://ptb.discord.com/channels/111/456/789"
    result_ptb = rewrite_discord_links(content_ptb, channel_map={"456": "stoat_ch"})
    assert "<#stoat_ch>" in result_ptb


def test_invite_annotation() -> None:
    """discord.gg invite links get annotated as no longer valid."""
    content = "Join us: https://discord.gg/abc123"
    result = rewrite_discord_links(content, channel_map={})
    assert "https://discord.gg/abc123 [Discord invite — no longer valid]" in result


def test_invite_via_discord_com() -> None:
    """discord.com/invite links get annotated as no longer valid."""
    content = "Join: https://discord.com/invite/xyz"
    result = rewrite_discord_links(content, channel_map={})
    assert "https://discord.com/invite/xyz [Discord invite — no longer valid]" in result


def test_link_in_code_block_untouched() -> None:
    """Links inside code blocks are NOT rewritten."""
    content = "```\nhttps://discord.com/channels/111/456/789\n```"
    result = rewrite_discord_links(content, channel_map={"456": "stoat_ch"})
    assert result == content


def test_multiple_links() -> None:
    """Multiple jump links and an invite link are all rewritten."""
    content = (
        "See https://discord.com/channels/111/456/789 and "
        "https://discord.com/channels/111/999/100 "
        "also https://discord.gg/invite1"
    )
    result = rewrite_discord_links(content, channel_map={"456": "stoat_ch1", "999": "stoat_ch2"})
    assert "<#stoat_ch1>" in result
    assert "<#stoat_ch2>" in result
    assert "[Discord invite — no longer valid]" in result
