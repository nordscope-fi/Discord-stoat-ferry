"""Tests for DCE stdout parser."""

from __future__ import annotations

import dataclasses

import pytest

from discord_ferry.exporter.dce_output import (
    Banner,
    Error,
    ParsedDceLine,
    PerChannel,
    Phase,
    Raw,
    StatusDot,
    Success,
    parse_dce_line,
)


class TestModuleShape:
    def test_dataclasses_are_frozen(self):
        pc = PerChannel(channel="general", pct=50)
        with pytest.raises(dataclasses.FrozenInstanceError):
            pc.pct = 99  # type: ignore[misc]

    def test_parse_dce_line_returns_raw_for_unknown(self):
        result = parse_dce_line("totally unrecognized line")
        assert isinstance(result, Raw)
        assert result.message == "totally unrecognized line"

    def test_parse_dce_line_is_total_on_empty_string(self):
        result = parse_dce_line("")
        assert isinstance(result, Raw)
