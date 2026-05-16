"""Tests for DCE stdout parser."""

from __future__ import annotations

import dataclasses

import pytest

from discord_ferry.exporter.dce_output import (
    PerChannel,
    Phase,
    Raw,
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
