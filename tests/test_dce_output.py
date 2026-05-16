"""Tests for DCE stdout parser."""

from __future__ import annotations

import dataclasses

import pytest

from discord_ferry.exporter.dce_output import (
    Banner,
    PerChannel,
    Phase,
    Raw,
    StatusDot,
    Success,
    parse_dce_line,
)


class TestModuleShape:
    def test_dataclasses_are_frozen(self) -> None:
        pc = PerChannel(channel="general", pct=50)
        with pytest.raises(dataclasses.FrozenInstanceError):
            pc.pct = 99  # type: ignore[misc]

    def test_parse_dce_line_returns_raw_for_unknown(self) -> None:
        result = parse_dce_line("totally unrecognized line")
        assert isinstance(result, Raw)
        assert result.message == "totally unrecognized line"

    def test_parse_dce_line_is_total_on_empty_string(self) -> None:
        result = parse_dce_line("")
        assert isinstance(result, Raw)


class TestPhaseParsing:
    def test_phase_fetching_channels(self) -> None:
        result = parse_dce_line("Fetching channels...")
        assert isinstance(result, Phase)
        assert result.kind == "fetching_channels"
        assert result.count is None
        assert result.message == "Fetching channels..."

    def test_phase_fetched_channels(self) -> None:
        result = parse_dce_line("Fetched 142 channel(s).")
        assert isinstance(result, Phase)
        assert result.kind == "fetched_channels"
        assert result.count == 142

    def test_phase_fetching_threads(self) -> None:
        result = parse_dce_line("Fetching threads...")
        assert isinstance(result, Phase)
        assert result.kind == "fetching_threads"
        assert result.count is None

    def test_phase_fetched_threads(self) -> None:
        result = parse_dce_line("Fetched 87 thread(s).")
        assert isinstance(result, Phase)
        assert result.kind == "fetched_threads"
        assert result.count == 87

    def test_phase_exporting_header(self) -> None:
        result = parse_dce_line("Exporting 229 channel(s)...")
        assert isinstance(result, Phase)
        assert result.kind == "exporting_header"
        assert result.count == 229
        assert result.message == "Exporting 229 channel(s)..."


class TestSuccessParsing:
    def test_success_typical(self) -> None:
        result = parse_dce_line("Successfully exported 229 channel(s).")
        assert isinstance(result, Success)
        assert result.count == 229
        assert result.message == "Successfully exported 229 channel(s)."

    def test_success_count_zero(self) -> None:
        # Edge case: empty server or all channels filtered out.
        result = parse_dce_line("Successfully exported 0 channel(s).")
        assert isinstance(result, Success)
        assert result.count == 0


class TestPerChannelParsing:
    """Per-channel `<name>: NN%` lines from DCE's Spectre fallback renderer.

    The regex is intentionally greedy + anchored: ^(?P<channel>.+):\\s(?P<pct>\\d+)%$.
    A non-greedy .+? would match only the prefix up to the first `:` and orphan
    the rest of channels named like `category: subname / channel: 50%`. Backtracking
    on greedy match resolves to the largest channel that still leaves : <digits>%$.
    """

    def test_per_channel_25(self) -> None:
        result = parse_dce_line("general: 25%")
        assert isinstance(result, PerChannel)
        assert result.channel == "general"
        assert result.pct == 25

    def test_per_channel_hierarchical(self) -> None:
        # DCE joins forum/thread hierarchy with " / ", NOT Discord's "#" prefix.
        result = parse_dce_line("Information / general / my-thread: 50%")
        assert isinstance(result, PerChannel)
        assert result.channel == "Information / general / my-thread"
        assert result.pct == 50

    def test_per_channel_100(self) -> None:
        result = parse_dce_line("announcements: 100%")
        assert isinstance(result, PerChannel)
        assert result.channel == "announcements"
        assert result.pct == 100

    def test_per_channel_with_colon_in_name(self) -> None:
        # Adversarial: channel name itself contains ': '. Greedy match required.
        # Non-greedy would yield channel="category" and orphan the rest.
        result = parse_dce_line("category: subname / channel: 50%")
        assert isinstance(result, PerChannel)
        assert result.channel == "category: subname / channel"
        assert result.pct == 50

    def test_per_channel_no_percent_falls_through(self) -> None:
        # Bare "25" might be a count or part of a name -- require the % suffix.
        result = parse_dce_line("general: 25")
        assert isinstance(result, Raw)

    def test_per_channel_no_space_falls_through(self) -> None:
        # Spectre always emits ": " (colon-space). Be strict about the separator.
        result = parse_dce_line("general:25%")
        assert isinstance(result, Raw)

    def test_per_channel_decimal_pct_falls_through(self) -> None:
        # DCE per-source emits integer-only milestone percents. Reject decimals.
        result = parse_dce_line("general: 25.5%")
        assert isinstance(result, Raw)


class TestStatusDotParsing:
    def test_status_dot_three(self) -> None:
        result = parse_dce_line("...")
        assert isinstance(result, StatusDot)
        assert result.message == "..."

    def test_status_dot_more(self) -> None:
        # Spectre may emit longer dot runs.
        result = parse_dce_line("......")
        assert isinstance(result, StatusDot)


class TestBannerParsing:
    def test_banner_top_edge(self) -> None:
        # Box-drawing top edge of Ukraine banner.
        line = "┌" + "─" * 68 + "┐"
        result = parse_dce_line(line)
        assert isinstance(result, Banner)

    def test_banner_text_inside(self) -> None:
        # Vertical bar + text + vertical bar = Banner content row.
        line = "│   Thank you for supporting Ukraine <3" + " " * 30 + "│"
        result = parse_dce_line(line)
        assert isinstance(result, Banner)


class TestRawFallthrough:
    def test_raw_unknown_text(self) -> None:
        result = parse_dce_line("Some unknown DCE line we have not seen before")
        assert isinstance(result, Raw)
        assert result.message == "Some unknown DCE line we have not seen before"

    def test_raw_empty_string(self) -> None:
        result = parse_dce_line("")
        assert isinstance(result, Raw)
