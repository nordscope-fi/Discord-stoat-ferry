"""Tests for string sanitization helpers."""

from discord_ferry.migrator.sanitize import sanitize_emoji_name, sanitize_filename, truncate_name

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


# ---------------------------------------------------------------------------
# Batch 9 — S4 collision-correct emoji de-dup
# ---------------------------------------------------------------------------


def test_sanitize_emoji_registers_emitted_name() -> None:
    """SC-23: two 'fire' → fire, fire_2; both registered in used_names."""
    used: dict[str, int] = {}
    a = sanitize_emoji_name("fire", used)
    b = sanitize_emoji_name("fire", used)
    assert (a, b) == ("fire", "fire_2")
    assert "fire" in used and "fire_2" in used


def test_sanitize_emoji_emitted_recollision() -> None:
    """SC-24: a third emoji whose raw name sanitizes to 'fire_2' becomes a distinct fire_2_2."""
    used: dict[str, int] = {}
    sanitize_emoji_name("fire", used)
    sanitize_emoji_name("fire", used)  # → fire_2
    third = sanitize_emoji_name("fire_2", used)  # collides with the emitted fire_2
    assert third not in ("fire", "fire_2")
    assert third == "fire_2_2"


def test_sanitize_emoji_near_limit_keeps_suffix() -> None:
    """SC-25: two 32-char-identical names → distinct (suffix preserved, base truncated, ≤32)."""
    used: dict[str, int] = {}
    name = "a" * 40  # sanitizes/truncates to 32 'a'
    a = sanitize_emoji_name(name, used)
    b = sanitize_emoji_name(name, used)
    assert a != b
    assert len(a) <= 32 and len(b) <= 32


def test_sanitize_emoji_non_colliding_unchanged() -> None:
    """SC-26: a non-colliding name is unchanged."""
    used: dict[str, int] = {}
    assert sanitize_emoji_name("unique", used) == "unique"


# ---------------------------------------------------------------------------
# sanitize_filename
# ---------------------------------------------------------------------------


def test_sanitize_filename_safe_name_unchanged() -> None:
    """A name with no illegal characters passes through."""
    assert sanitize_filename("my-thread") == "my-thread"


def test_sanitize_filename_replaces_windows_illegal_chars() -> None:
    """Characters illegal on Windows are replaced with underscores."""
    assert sanitize_filename('bug: crash? "yes"') == "bug_ crash_ _yes_"


def test_sanitize_filename_replaces_slash() -> None:
    """Forward slash would create a subdirectory on POSIX."""
    assert sanitize_filename("a/b") == "a_b"


def test_sanitize_filename_replaces_backslash() -> None:
    """Backslash is illegal on Windows."""
    assert sanitize_filename("a\\b") == "a_b"


def test_sanitize_filename_strips_dots_and_spaces() -> None:
    """Leading/trailing dots and spaces are invalid on Windows."""
    assert sanitize_filename("...hidden...") == "hidden"
    assert sanitize_filename("  spaced  ") == "spaced"


def test_sanitize_filename_empty_fallback() -> None:
    """Empty or whitespace-only input returns underscore."""
    assert sanitize_filename("") == "_"
    assert sanitize_filename("   ") == "_"


def test_sanitize_filename_reserved_device_name() -> None:
    """Win32 reserved names are prefixed with underscore."""
    assert sanitize_filename("CON") == "_CON"
    assert sanitize_filename("com1") == "_com1"
    assert sanitize_filename("NUL.txt") == "_NUL.txt"


def test_sanitize_filename_truncates() -> None:
    """Long names are truncated to max_length."""
    name = "a" * 300
    assert len(sanitize_filename(name)) == 200
    assert len(sanitize_filename(name, max_length=50)) == 50


def test_sanitize_filename_control_characters() -> None:
    """Control characters (0x00-0x1f) are replaced."""
    assert sanitize_filename("hello\x00world\x1f") == "hello_world_"


def test_sanitize_filename_preserves_unicode() -> None:
    """Emoji, CJK, and other Unicode characters pass through unchanged."""
    assert sanitize_filename("thread-\U0001f680-launch") == "thread-\U0001f680-launch"
    assert sanitize_filename("测试线程") == "测试线程"
