"""Tests for the migration check tool (#107 batch 9).

Fixture rule for this whole module: Discord ids and Stoat ids are LITERAL and
visibly different, ``"d-100"`` against ``"01JSTOAT..."``. They are never derived
from one variable and never equal. Seeding both sides of a comparison from a
single value is what lets a check test pass against an implementation comparing
the wrong one, and this project has shipped five assertions that could not fail
against what they guarded.
"""

from __future__ import annotations

import pytest
from aioresponses import aioresponses

from discord_ferry.errors import CheckError
from discord_ferry.migrator.verify import CheckReport, CheckResult, run_check
from discord_ferry.state import MigrationState

BASE_URL = "https://api.test"
TOKEN = "test-session-token"


@pytest.fixture()
def mock_aiohttp() -> object:
    with aioresponses() as m:
        yield m


def _noop_event(_event: object) -> None:
    return None


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


# ---------------------------------------------------------------------------
# run_check preconditions (task #251)
# ---------------------------------------------------------------------------


async def test_a_dry_run_state_is_refused_before_any_request() -> None:
    """A dry run records dry-ch- and dry-msg- sentinels for entities that were
    never created, so there is nothing on a server to check against.

    --resume has refused a dry-run state since it was written and --incremental
    gained the same refusal in v2.15.0. This is the third sibling.

    No route is registered, so the refusal is proven by the absence of a
    request rather than only by the exception: an implementation checking the
    flag AFTER its first fetch would raise a different error here.
    """
    state = MigrationState()
    state.is_dry_run = True
    state.stoat_server_id = "01JSTOATSRV0000000000AAA"
    with aioresponses(), pytest.raises(CheckError, match="dry-run"):
        await run_check(BASE_URL, TOKEN, state, _noop_event)


async def test_an_empty_server_id_is_refused_before_any_request() -> None:
    """Without a server id there is nothing to check against, and the URL would
    be built with an empty path segment rather than failing honestly."""
    state = MigrationState()
    state.stoat_server_id = ""
    with aioresponses(), pytest.raises(CheckError, match="server"):
        await run_check(BASE_URL, TOKEN, state, _noop_event)


async def test_a_state_predating_this_feature_is_not_refused() -> None:
    """An older state.json loads with its newer optional fields defaulted,
    because load_state reads every one through data.get.

    Kills an implementation that hard-requires a field a released state file
    does not carry, which would make the tool useless for exactly the
    migrations it exists to inspect.

    Scoped to what this layer can actually prove: the preconditions do not
    reject it. The fuller claim, that the CHECKS degrade and report what they
    cannot determine, needs checks to exist and is pinned in chunk 3 and chunk
    4. Registering mock routes here would have made this look like an
    integration test while asserting nothing, because the skeleton makes no
    requests yet.
    """
    state = MigrationState()
    state.stoat_server_id = "01JSTOATSRV0000000000AAA"
    # No channel_map, no message_map, no channel_high_water, no
    # channel_message_counts: the shape an early state.json presents.
    report = await run_check(BASE_URL, TOKEN, state, _noop_event)
    assert report.counts()["fail"] == 0
