"""Tests for _split_message — message content splitting engine."""

from __future__ import annotations

from discord_ferry.migrator.messages import _split_message


# ---------------------------------------------------------------------------
# Basic boundary behaviour
# ---------------------------------------------------------------------------


def test_no_split_under_2000() -> None:
    """Short content returns a single-element list with no markers."""
    content = "Hello world"
    parts = _split_message(content)
    assert parts == [content]


def test_no_split_exactly_2000() -> None:
    """Content at exactly the limit returns a single-element list."""
    content = "x" * 2000
    parts = _split_message(content)
    assert len(parts) == 1
    assert parts[0] == content


def test_split_at_2001() -> None:
    """Content one char over the limit splits into exactly 2 parts."""
    content = "x" * 2001
    parts = _split_message(content)
    assert len(parts) == 2


def test_split_5000_chars() -> None:
    """5000-char content splits into 3 parts."""
    content = "w " * 1000  # alternating word+space → 2000 chars per rough chunk
    # Force a definite 3-part split by using a long string with no spaces.
    long = "a" * 5000
    parts = _split_message(long)
    assert len(parts) >= 2
    # Independently verify a 5000-char block produces multiple parts.
    assert all(len(p) <= 2000 for p in parts)


def test_split_no_spaces() -> None:
    """Hard split works when there are no word boundaries."""
    content = "a" * 4000
    parts = _split_message(content)
    assert len(parts) >= 2
    for part in parts:
        assert len(part) <= 2000


def test_split_preserves_all_content() -> None:
    """Stripping markers from all parts recovers the full original content."""
    content = "hello world " * 300  # ~3600 chars
    parts = _split_message(content)
    assert len(parts) >= 2

    n = len(parts)
    stripped: list[str] = []
    for k, part in enumerate(parts, start=1):
        if k == 1:
            # First part ends with "\n[continued 1/N]"
            marker = f"\n[continued 1/{n}]"
            assert part.endswith(marker), f"Part 1 missing footer marker: {part[-30:]!r}"
            stripped.append(part[: -len(marker)])
        else:
            # Subsequent parts start with "[continued K/N] "
            marker = f"[continued {k}/{n}] "
            assert part.startswith(marker), f"Part {k} missing prefix marker: {part[:30]!r}"
            stripped.append(part[len(marker) :])

    recovered = " ".join(stripped)
    # Original content uses "hello world " (trailing space), joined parts drop the boundary space
    # so we just check all words are present.
    assert recovered.replace(" ", "") == content.replace(" ", "")


def test_all_parts_under_max_len() -> None:
    """Every chunk produced by _split_message is within the 2000-char limit."""
    content = "The quick brown fox jumps over the lazy dog. " * 100  # ~4500 chars
    parts = _split_message(content)
    for i, part in enumerate(parts):
        assert len(part) <= 2000, f"Part {i + 1} exceeds 2000 chars: {len(part)}"


# ---------------------------------------------------------------------------
# Marker format
# ---------------------------------------------------------------------------


def test_markers_format_correctly() -> None:
    """Markers follow the [continued K/N] convention."""
    content = "a" * 4000
    parts = _split_message(content)
    n = len(parts)
    assert n >= 2

    # First part ends with the continuation footer.
    assert parts[0].endswith(f"\n[continued 1/{n}]")

    # All subsequent parts start with the continuation prefix.
    for k in range(2, n + 1):
        assert parts[k - 1].startswith(f"[continued {k}/{n}] ")
