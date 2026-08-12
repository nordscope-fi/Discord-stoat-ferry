"""Tests for the migration check tool (#107 batch 9).

Fixture rule for this whole module: Discord ids and Stoat ids are LITERAL and
visibly different, ``"d-100"`` against ``"01JSTOAT..."``. They are never derived
from one variable and never equal. Seeding both sides of a comparison from a
single value is what lets a check test pass against an implementation comparing
the wrong one, and this project has shipped five assertions that could not fail
against what they guarded.
"""

from __future__ import annotations

from discord_ferry.migrator.verify import CheckReport, CheckResult

# ---------------------------------------------------------------------------
# CheckResult / CheckReport (task #250)
# ---------------------------------------------------------------------------


def test_a_report_of_only_unverifiable_counts_zero_ok() -> None:
    """Kills an implementation that folds unverifiable into ok.

    The exit code cannot separate those two, because a report of only
    unverifiable and a report of all ok BOTH exit 0. Only the count can tell
    them apart, which is why the count is asserted here rather than only the
    command's status.
    """
    report = CheckReport()
    report.add(
        name="channel:d-100",
        status="unverifiable",
        kind="tail_not_recorded",
        detail="newest message was not recorded by Ferry",
    )
    counts = report.counts()
    assert counts["ok"] == 0
    assert counts["unverifiable"] == 1
    assert report.has_failures is False


def test_counts_reports_every_status_even_at_zero() -> None:
    """A caller rendering a summary line needs all four keys present.

    Kills a Counter-based implementation that omits statuses with no results,
    which would make the summary line drop "0 failed" exactly when a reader
    most wants to see it stated.
    """
    report = CheckReport()
    report.add(name="a", status="ok", kind="tail_present", detail="")
    counts = report.counts()
    assert set(counts) == {"ok", "warn", "fail", "unverifiable"}
    assert counts["fail"] == 0
    assert counts["warn"] == 0


def test_has_failures_is_true_only_for_a_fail() -> None:
    """warn and unverifiable must NOT make the command exit non-zero.

    Kills an implementation treating anything non-ok as a failure, which would
    exit non-zero on every merge migration and on every renamed category.
    """
    report = CheckReport()
    report.add(name="a", status="warn", kind="category_title_mismatch", detail="")
    report.add(name="b", status="unverifiable", kind="tail_not_recorded", detail="")
    assert report.has_failures is False
    report.add(name="c", status="fail", kind="channel_missing", detail="")
    assert report.has_failures is True


def test_a_result_carries_the_entity_ids_a_repair_would_need() -> None:
    """The report is the contract batch 10's repair tool consumes.

    A status plus prose is not actionable. Kills a shape carrying only the
    ProbeCheck fields (name, status, detail), which would force a repair tool
    to parse ids back out of a sentence.
    """
    result = CheckResult(
        name="channel:d-100",
        status="fail",
        kind="channel_missing",
        detail="recorded channel is absent from the server",
        discord_id="d-100",
        stoat_id="01JSTOATCH00000000000AAA",
    )
    assert result.discord_id == "d-100"
    assert result.stoat_id == "01JSTOATCH00000000000AAA"
    assert result.expected is None
    assert result.found is None
