"""Tests for string sanitization helpers."""

from discord_ferry.migrator.sanitize import sanitize_emoji_name, truncate_name

# ---------------------------------------------------------------------------
# truncate_name
# ---------------------------------------------------------------------------


def test_truncate_name_under_limit() -> None:
    """Short names are returned unchanged."""
    assert truncate_name("general") == "general"


def test_truncate_name_at_limit() -> None:
    """Names at exactly 32 chars are returned unchanged."""
    name = "a" * 32
    assert truncate_name(name) == name


def test_truncate_name_over_limit() -> None:
    """Names over 32 chars are truncated."""
    name = "a" * 50
    assert truncate_name(name) == "a" * 32


def test_truncate_name_custom_limit() -> None:
    """Custom max_length is respected."""
    assert truncate_name("abcdefgh", max_length=5) == "abcde"


def test_truncate_name_empty() -> None:
    """Empty string stays empty."""
    assert truncate_name("") == ""


def test_truncate_name_with_discriminator() -> None:
    """40-char name with author_id is truncated to 32 chars ending with #XXXX suffix."""
    name = "A" * 40
    author_id = "1234567890123456789"
    result = truncate_name(name, author_id=author_id)
    assert len(result) == 32
    assert result.endswith("#6789")


def test_truncate_name_without_author_id() -> None:
    """40-char name without author_id is plain-truncated to 32 chars with no # suffix."""
    name = "B" * 40
    result = truncate_name(name)
    assert len(result) == 32
    assert "#" not in result


def test_truncate_name_short_name_no_truncation() -> None:
    """20-char name is returned unchanged regardless of author_id."""
    name = "C" * 20
    result = truncate_name(name, author_id="999")
    assert result == name


# ---------------------------------------------------------------------------
# sanitize_emoji_name
# ---------------------------------------------------------------------------


def test_sanitize_emoji_name_lowercase() -> None:
    """Uppercase is lowercased."""
    assert sanitize_emoji_name("PartyTime") == "partytime"


def test_sanitize_emoji_name_replaces_invalid_chars() -> None:
    """Non-alphanumeric non-underscore chars become underscores."""
    assert sanitize_emoji_name("my-emoji!") == "my_emoji"


def test_sanitize_emoji_name_strips_edge_underscores() -> None:
    """Leading/trailing underscores from replacements are stripped."""
    assert sanitize_emoji_name("-hello-") == "hello"


def test_sanitize_emoji_name_truncates() -> None:
    """Names over 32 chars are truncated after sanitization."""
    name = "a" * 50
    result = sanitize_emoji_name(name)
    assert len(result) == 32


def test_sanitize_emoji_name_empty_fallback() -> None:
    """Entirely invalid names fall back to 'emoji'."""
    assert sanitize_emoji_name("!!!") == "emoji"


def test_sanitize_emoji_name_already_valid() -> None:
    """Already valid names pass through unchanged."""
    assert sanitize_emoji_name("cool_emoji_42") == "cool_emoji_42"


def test_sanitize_emoji_name_spaces() -> None:
    """Spaces are replaced with underscores."""
    assert sanitize_emoji_name("my emoji") == "my_emoji"


# ---------------------------------------------------------------------------
# sanitize_emoji_name — collision tracking
# ---------------------------------------------------------------------------


def test_emoji_name_no_collision_without_tracking() -> None:
    """Without used_names, no suffix is appended even for repeated calls."""
    assert sanitize_emoji_name("wave") == "wave"
    assert sanitize_emoji_name("wave") == "wave"


def test_emoji_name_collision_suffix() -> None:
    """Second emoji sanitizing to the same name receives a _2 suffix."""
    used: dict[str, int] = {}
    first = sanitize_emoji_name("wave", used)
    second = sanitize_emoji_name("wave", used)
    assert first == "wave"
    assert second == "wave_2"


def test_emoji_name_triple_collision() -> None:
    """Third collision gets _3 suffix."""
    used: dict[str, int] = {}
    first = sanitize_emoji_name("star", used)
    second = sanitize_emoji_name("star", used)
    third = sanitize_emoji_name("star", used)
    assert first == "star"
    assert second == "star_2"
    assert third == "star_3"


def test_emoji_name_collision_different_raw_names_same_sanitized() -> None:
    """Two different raw names that sanitize identically still get deduplicated."""
    used: dict[str, int] = {}
    first = sanitize_emoji_name("My-Emoji", used)
    second = sanitize_emoji_name("My_Emoji", used)
    assert first == "my_emoji"
    assert second == "my_emoji_2"


def test_emoji_name_collision_suffix_truncated() -> None:
    """Suffixed name is truncated to 32 chars if needed."""
    used: dict[str, int] = {}
    long_name = "a" * 32
    first = sanitize_emoji_name(long_name, used)
    second = sanitize_emoji_name(long_name, used)
    assert first == "a" * 32
    assert len(second) <= 32
