"""Pure parser for DiscordChatExporter (DCE) stdout lines.

Public surface:
  - parse_dce_line(line: str) -> ParsedDceLine  (total, no I/O)
  - ParsedDceLine = PerChannel | Phase | Success | Banner | StatusDot | Error | Raw

This module has zero side effects and zero subprocess interaction. The runner
calls parse_dce_line on each decoded stdout line and dispatches on the union.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class PerChannel:
    """A `<channel>: NN%` progress line from DCE's Spectre.Console fallback renderer."""

    channel: str
    pct: int  # 0-100, integer (DCE emits milestones {25, 50, 75, 95, 96, 97, 98, 99, 100})


PhaseKind = Literal[
    "fetching_channels",
    "fetched_channels",
    "fetching_threads",
    "fetched_threads",
    "exporting_header",
]


@dataclass(frozen=True, slots=True)
class Phase:
    """Headline phase line from DCE.

    `count` is None for `fetching_*` (no integer in the line); int for
    `fetched_*` and `exporting_header`.
    """

    kind: PhaseKind
    count: int | None
    message: str


@dataclass(frozen=True, slots=True)
class Success:
    """Final `Successfully exported N channel(s).` line."""

    count: int
    message: str


@dataclass(frozen=True, slots=True)
class Banner:
    """One line of the Ukraine-support banner DCE prints first."""

    message: str


@dataclass(frozen=True, slots=True)
class StatusDot:
    """Spectre status-ticker fallback: a line of `...`."""

    message: str  # always "..." but preserved for log faithfulness


@dataclass(frozen=True, slots=True)
class Error:
    """Reserved for future use; parser does not currently emit Error.

    DCE writes errors to stderr (handled by _read_stderr in runner). Kept in
    the union so error-path lines added later are non-breaking for callers.
    """

    message: str


@dataclass(frozen=True, slots=True)
class Raw:
    """Fallthrough: any line that did not match a more specific kind."""

    message: str


ParsedDceLine = PerChannel | Phase | Success | Banner | StatusDot | Error | Raw


# GREEDY (.+, not .+?) + anchored: backtracking lets the channel match the
# largest substring that still leaves ': <digits>%$' at the end. This is what
# makes `category: subname / channel: 50%` parse correctly.
_PER_CHANNEL_RE = re.compile(r"^(?P<channel>.+):\s(?P<pct>\d+)%$")

_PHASE_PATTERNS: tuple[tuple[PhaseKind, re.Pattern[str]], ...] = (
    ("fetching_channels", re.compile(r"^Fetching channels\.\.\.$")),
    ("fetching_threads", re.compile(r"^Fetching threads\.\.\.$")),
    ("fetched_channels", re.compile(r"^Fetched (?P<n>\d+) channel\(s\)\.$")),
    ("fetched_threads", re.compile(r"^Fetched (?P<n>\d+) thread\(s\)\.$")),
    ("exporting_header", re.compile(r"^Exporting (?P<n>\d+) channel\(s\)\.\.\.$")),
)

_SUCCESS_RE = re.compile(r"^Successfully exported (?P<n>\d+) channel\(s\)\.$")

_STATUS_DOT_RE = re.compile(r"^\.{3,}$")

# Ukraine banner box-drawing characters: corners, edges, vertical bar.
# Box edges and content rows all start with one of these.
_BANNER_RE = re.compile(r"^[┌┐└┘─│]")


def parse_dce_line(line: str) -> ParsedDceLine:
    """Map one DCE stdout line to a typed ParsedDceLine.

    Total function: every input maps to some result; never raises.

    Patterns tried in order:
      1. PerChannel  -- most common at runtime
      2. Phase       -- headline lines
      3. Success     -- final line
      4. StatusDot   -- Spectre fallback `...`
      5. Banner      -- Ukraine banner box-drawing
      6. Raw         -- fallthrough
    """
    pc_match = _PER_CHANNEL_RE.match(line)
    if pc_match:
        return PerChannel(
            channel=pc_match.group("channel"),
            pct=int(pc_match.group("pct")),
        )

    for kind, pattern in _PHASE_PATTERNS:
        match = pattern.match(line)
        if match:
            count = int(match.group("n")) if "n" in match.groupdict() else None
            return Phase(kind=kind, count=count, message=line)

    success_match = _SUCCESS_RE.match(line)
    if success_match:
        return Success(count=int(success_match.group("n")), message=line)

    if _STATUS_DOT_RE.match(line):
        return StatusDot(message=line)

    if _BANNER_RE.match(line):
        return Banner(message=line)

    return Raw(message=line)
