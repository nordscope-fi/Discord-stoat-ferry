"""Replay test: parser handles every line shape in the captured fixture.

Catches accidental regex regression even when DCE itself hasn't changed.
Complementary to the contract test (which catches DCE-side drift).

Design choice: explicit per-line (line_number, expected_kind) assertions
rather than an allowlist of "expected raw" strings. The allowlist pattern
would let new fixture lines silently fall into Raw -- exactly the
judgment-call drift that caused #23.

`#`-prefixed lines are fixture metadata (not DCE output the parser would
ever see) and are filtered out before parsing. EXPECTED_LINES covers only
the real-content lines.

Spec-vs-implementation note: the spec assumed `parse_dce_line` returns a
value with a `.kind` string attribute. The actual v2.2.0 implementation
returns a typed union (PerChannel | Phase | Success | Banner | StatusDot |
Error | Raw) with no shared `.kind` field. This test adapts by using
`type(parsed).__name__` (yielding strings like "PerChannel", "Phase",
"Success") as the expected-kind string.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from discord_ferry.exporter.dce_output import parse_dce_line

# Each tuple is (1-indexed line number in fixture, expected type name).
# When the fixture changes, this list MUST change in lockstep -- that is the
# point. Adding a fixture line without claiming what it parses to will fail
# the length assertion below.
EXPECTED_LINES: list[tuple[int, str]] = [
    (15, "Banner"),  # '┌────────────────────────────────────────────────────────────────────┐'
    (16, "Banner"),  # '│   Thank you for supporting Ukraine <3                              │'
    (17, "Banner"),  # '│                                                                    │'
    (18, "Banner"),  # '│   As Russia wages a genocidal war against my country,              │'
    (19, "Banner"),  # "│   I'm grateful to everyone who continues to                        │"
    (20, "Banner"),  # '│   stand with Ukraine in our fight for freedom.                     │'
    (21, "Banner"),  # '│                                                                    │'
    (22, "Banner"),  # '│   Learn more: https://tyrrrz.me/ukraine                            │'
    (23, "Banner"),  # '└────────────────────────────────────────────────────────────────────┘'
    (24, "Raw"),  # '' (blank line -- runner skips empty lines; parser sees empty string -> Raw)
    (25, "Phase"),  # 'Fetching channels...'
    (26, "StatusDot"),  # '...'
    (28, "Phase"),  # 'Fetched 3 channel(s).'
    (29, "Phase"),  # 'Fetching threads...'
    (30, "Phase"),  # 'Fetched 0 thread(s).'
    (31, "Phase"),  # 'Exporting 3 channel(s)...'
    (32, "PerChannel"),  # 'general: 25%'
    (33, "PerChannel"),  # 'general: 50%'
    (34, "PerChannel"),  # 'general: 75%'
    (35, "PerChannel"),  # 'general: 95%'
    (36, "PerChannel"),  # 'general: 100%'
    (37, "PerChannel"),  # 'announcements: 25%'
    (38, "PerChannel"),  # 'announcements: 50%'
    (39, "PerChannel"),  # 'announcements: 75%'
    (40, "PerChannel"),  # 'announcements: 95%'
    (41, "PerChannel"),  # 'announcements: 100%'
    (42, "PerChannel"),  # 'memes: 25%'
    (43, "PerChannel"),  # 'memes: 50%'
    (44, "PerChannel"),  # 'memes: 75%'
    (45, "PerChannel"),  # 'memes: 95%'
    (46, "PerChannel"),  # 'memes: 100%'
    (47, "Success"),  # 'Successfully exported 3 channel(s).'
]


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


def test_parse_dce_line_matches_expected_kinds_per_line(fixtures_dir: Path) -> None:
    """Every fixture line (excluding `#` comments) parses to the kind explicitly claimed."""
    sample_path = fixtures_dir / "dce_stdout_sample.txt"
    raw_lines = sample_path.read_text(encoding="utf-8").splitlines()
    content_lines = [(i + 1, line) for i, line in enumerate(raw_lines) if not line.startswith("#")]

    # Length guard: forces the test to be updated when the fixture grows or
    # shrinks. Without this, new lines could silently go unverified.
    assert len(EXPECTED_LINES) == len(content_lines), (
        f"EXPECTED_LINES has {len(EXPECTED_LINES)} entries but fixture has "
        f"{len(content_lines)} non-comment lines. Update EXPECTED_LINES in "
        f"lockstep with the fixture."
    )

    mismatches: list[str] = []
    for (expected_line_no, expected_kind), (actual_line_no, line) in zip(
        EXPECTED_LINES, content_lines, strict=True
    ):
        assert expected_line_no == actual_line_no, (
            f"Line number drift: EXPECTED_LINES claims line {expected_line_no} "
            f"but the {len(EXPECTED_LINES) - len(content_lines) + actual_line_no}-th "
            f"non-comment line is at fixture line {actual_line_no}. Regenerate "
            f"EXPECTED_LINES."
        )
        parsed = parse_dce_line(line)
        actual_kind = type(parsed).__name__
        if actual_kind != expected_kind:
            mismatches.append(
                f"  line {actual_line_no}: expected kind={expected_kind!r}, "
                f"got kind={actual_kind!r} for: {line!r}"
            )

    assert not mismatches, "Parser kind mismatches:\n" + "\n".join(mismatches)
