"""Verify a finished migration against a live Stoat server.

Modelled on :mod:`discord_ferry.migrator.probe`: read-only, reports through an
``on_event`` callback so nothing here imports a shell, and takes an injectable
session so tests drive it without a network.

Two check families. Structure compares every entity recorded in
:class:`~discord_ferry.state.MigrationState` against the server **by id**, in
two requests regardless of how many entities exist. The tail check asks each
channel for its newest 100 messages and confirms the message state records as
that channel's last is still present among them.

The existing ``validate_after`` block in ``run_migration`` compares two
cardinalities and sets ``passed`` on them alone, so two channels created with
each other's names pass it. It also swallows every exception into a warning and
leaves ``validation_results`` empty, which makes a check that failed
indistinguishable from one that passed. Neither is repeated here: a failure to
check is a recorded result, not the absence of one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from discord_ferry.errors import ValidationError

if TYPE_CHECKING:
    import aiohttp

    from discord_ferry.core.events import EventCallback
    from discord_ferry.state import MigrationState

#: Every status the tool can report, in the order a summary line names them.
#:
#: ``warn`` exists for a cosmetic difference on an entity whose content is
#: intact. Today the only such difference is a category title, because
#: ``category_names`` is the only expected name ``MigrationState`` records. A
#: design review found ``warn`` promised in two acceptance criteria and produced
#: by no code path at all, so its single legitimate home is stated here rather
#: than left implicit.
STATUSES: tuple[str, ...] = ("ok", "warn", "fail", "unverifiable")


@dataclass
class CheckResult:
    """One entity's verdict.

    Carries more than :class:`~discord_ferry.migrator.probe.ProbeCheck` on
    purpose. This report is the contract the repair tool consumes, and a status
    plus a sentence is not actionable: a repair needs the entity's ids and an
    enumerable defect category it can dispatch on, not prose to parse.
    """

    name: str
    status: str
    kind: str
    detail: str
    discord_id: str | None = None
    stoat_id: str | None = None
    expected: str | None = None
    found: str | None = None


@dataclass
class CheckReport:
    """Every verdict from one run."""

    results: list[CheckResult] = field(default_factory=list)

    def add(
        self,
        *,
        name: str,
        status: str,
        kind: str,
        detail: str,
        discord_id: str | None = None,
        stoat_id: str | None = None,
        expected: str | None = None,
        found: str | None = None,
    ) -> None:
        """Append a verdict. Mirrors ``ProbeReport.add``."""
        self.results.append(
            CheckResult(
                name=name,
                status=status,
                kind=kind,
                detail=detail,
                discord_id=discord_id,
                stoat_id=stoat_id,
                expected=expected,
                found=found,
            )
        )

    def counts(self) -> dict[str, int]:
        """Count results per status, with **every** status present.

        Seeded from :data:`STATUSES` rather than counting what happens to be
        there, so a summary line can always say "0 failed" out loud. A reader
        most wants that stated when it is zero.
        """
        tally = dict.fromkeys(STATUSES, 0)
        for result in self.results:
            tally[result.status] = tally.get(result.status, 0) + 1
        return tally

    @property
    def has_failures(self) -> bool:
        """True only when something is genuinely wrong.

        ``warn`` and ``unverifiable`` do not count. Treating either as a failure
        would exit non-zero on every merge migration and on every renamed
        category, which is the fastest way to teach people to ignore the tool.
        """
        return any(r.status == "fail" for r in self.results)


async def run_check(
    stoat_url: str,
    token: str,
    state: MigrationState,
    on_event: EventCallback,
    *,
    session: aiohttp.ClientSession | None = None,
) -> CheckReport:
    """Verify a finished migration against a live Stoat server.

    Takes an already-loaded :class:`~discord_ferry.state.MigrationState` rather
    than an output directory, so every test drives it with no filesystem. Takes
    ``stoat_url`` and ``token`` directly and reports through ``on_event``,
    matching ``run_probe``, so nothing here needs a ``FerryConfig`` and nothing
    under ``migrator/`` imports a shell.

    Both preconditions raise before any request is made. That ordering is the
    behaviour under test: checking after the first fetch would spend a request
    and, on a dry-run state, would go looking for ``dry-ch-`` sentinels that
    name channels nobody ever created.

    Raises:
        ValidationError: the state is from a dry run, or records no server.
    """
    if state.is_dry_run:
        raise ValidationError(
            "Cannot check a dry-run state. A dry run records placeholder ids for "
            "channels and messages that were never created, so there is nothing on "
            "the server to verify against. Run a real migration first."
        )
    if not state.stoat_server_id:
        raise ValidationError(
            "This state records no Stoat server id, so there is nothing to check "
            "against. The migration may not have reached the structure phase."
        )

    report = CheckReport()
    _ = (stoat_url, token, session, on_event)  # wired up in the next chunks
    return report
