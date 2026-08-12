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
from typing import TYPE_CHECKING, Literal, get_args

from discord_ferry.core.events import MigrationEvent
from discord_ferry.core.http import new_session
from discord_ferry.errors import CheckError
from discord_ferry.migrator.api import (
    api_fetch_emoji_list,
    api_fetch_messages,
    api_fetch_server_with_channels,
)

if TYPE_CHECKING:
    import aiohttp

    from discord_ferry.core.events import EventCallback
    from discord_ferry.state import MigrationState

#: Every status the tool can report.
#:
#: A ``Literal`` rather than a bare ``str`` so ``mypy --strict`` rejects a typo at
#: the call site. This is not fussiness: a status of ``"faild"`` would leave
#: :attr:`CheckReport.has_failures` False and the command exiting 0 while a real
#: failure sat in the report. A runtime guard would catch it too, but only once
#: the line actually runs, and the wrong branch may be the rare one.
CheckStatus = Literal["ok", "warn", "fail", "unverifiable"]

#: The same four, as a tuple, DERIVED from the type rather than restated beside
#: it. A hand-maintained parallel list is how this project once let every
#: extension of a permission map leave its expansion behind.
#:
#: ``warn`` exists for a cosmetic difference on an entity whose content is
#: intact. Today the only such difference is a category title, because
#: ``category_names`` is the only expected name ``MigrationState`` records. A
#: design review found ``warn`` promised in two acceptance criteria and produced
#: by no code path at all, so its single legitimate home is stated here rather
#: than left implicit.
STATUSES: tuple[CheckStatus, ...] = get_args(CheckStatus)


@dataclass
class CheckResult:
    """One entity's verdict.

    Carries more than :class:`~discord_ferry.migrator.probe.ProbeCheck` on
    purpose. This report is the contract the repair tool consumes, and a status
    plus a sentence is not actionable: a repair needs the entity's ids and an
    enumerable defect category it can dispatch on, not prose to parse.
    """

    name: str
    status: CheckStatus
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
        status: CheckStatus,
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
        # Annotated dict[str, int] rather than inferred: dict.fromkeys over a
        # Literal tuple infers dict[CheckStatus, int], which callers formatting a
        # summary line would have to satisfy with literals. CheckStatus is a str,
        # so the widening is free and the increment below still type-checks.
        tally: dict[str, int] = dict.fromkeys(STATUSES, 0)
        for result in self.results:
            tally[result.status] += 1
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
        CheckError: the state is from a dry run, or records no server.
    """
    if state.is_dry_run:
        raise CheckError(
            "Cannot check a dry-run state. A dry run records placeholder ids for "
            "channels and messages that were never created, so there is nothing on "
            "the server to verify against. Run a real migration first."
        )
    if not state.stoat_server_id:
        raise CheckError(
            "This state records no Stoat server id, so there is nothing to check "
            "against. The migration may not have reached the structure phase."
        )

    report = CheckReport()
    own_session = session is None
    sess = session or new_session()
    try:
        await _check_structure(sess, stoat_url, token, state, report, on_event)
        await _check_tails(sess, stoat_url, token, state, report, on_event)
    finally:
        if own_session:
            await sess.close()
    return report


async def _check_structure(
    sess: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    state: MigrationState,
    report: CheckReport,
    on_event: EventCallback,
) -> None:
    """Compare every recorded entity against the server, by id.

    Two requests for the whole family, whatever the entity count: the server
    fetch carries roles and categories as full objects alongside the channels,
    and emoji need one more because ``Server`` has no emoji field.
    """
    on_event(
        MigrationEvent(
            phase="check",
            status="started",
            message="Checking server structure...",
        )
    )
    payload = await api_fetch_server_with_channels(sess, stoat_url, token, state.stoat_server_id)
    emoji = await api_fetch_emoji_list(sess, stoat_url, token, state.stoat_server_id)

    # Three id conventions arrive in this ONE response, verified against
    # upstream rather than assumed, because they do not agree with each other:
    #
    #   channel objects   ->  "_id"   (Channel carries serde(rename = "_id"))
    #   category objects  ->  "id"    (Category has NO rename)
    #   roles             ->  the KEY of a HashMap<String, Role>, so the map's
    #                         keys are read, not each Role's inner "_id"
    #
    # A later pass that "makes these consistent" breaks two of the three. The
    # sibling hazard is already recorded for messages, which use "_id" while
    # Ferry's webhook id field uses "id".
    server = payload.get("server") or {}
    # Two lists, and the pair is the discriminator. `server.channels` comes back
    # UNFILTERED and names every channel id; the sibling array holds only the
    # objects this token may ViewChannel. Consulting the objects alone cannot
    # tell a deleted channel from one merely hidden, which would make
    # `channel_missing` unreachable and lose the point of the tool.
    all_channel_ids = set(server.get("channels") or [])
    visible_ids = {
        c["_id"] for c in (payload.get("channels") or []) if isinstance(c, dict) and "_id" in c
    }

    for discord_id, stoat_id in state.channel_map.items():
        # `discord_id` is not always a Discord snowflake: the forum index writer
        # stores a synthetic `forum-index-{key}`. Only the VALUE is sent to the
        # server, so identity checking is valid for those entries too.
        if stoat_id in visible_ids:
            report.add(
                name=f"channel:{discord_id}",
                status="ok",
                kind="channel_present",
                detail="channel exists under its recorded id",
                discord_id=discord_id,
                stoat_id=stoat_id,
            )
        elif stoat_id in all_channel_ids:
            report.add(
                name=f"channel:{discord_id}",
                status="unverifiable",
                kind="channel_not_visible",
                detail=(
                    "the server lists this channel but did not return it, which "
                    "means this token cannot view it. Its contents cannot be checked."
                ),
                discord_id=discord_id,
                stoat_id=stoat_id,
            )
        else:
            report.add(
                name=f"channel:{discord_id}",
                status="fail",
                kind="channel_missing",
                detail="the server does not list this channel at all",
                discord_id=discord_id,
                stoat_id=stoat_id,
            )

    # No branch for a `dry-ch-` value. It is written only under config.dry_run,
    # and the same run sets state.is_dry_run, which run_check refuses above. The
    # branch is unreachable by construction, so it is not written.

    # Roles arrive as a map keyed by role id, so membership is a key lookup.
    # There is no second list and no permission filter here, unlike channels, so
    # an absence is unambiguous and reports fail rather than unverifiable.
    # set() over the MAP, so these are its KEYS. Roles are keyed by id upstream;
    # each Role object also carries an inner "_id", which is deliberately not
    # what is read here. See the three-conventions note above.
    role_ids = set(server.get("roles") or {})
    for discord_id, stoat_id in state.role_map.items():
        present = stoat_id in role_ids
        report.add(
            name=f"role:{discord_id}",
            status="ok" if present else "fail",
            kind="role_present" if present else "role_missing",
            detail=(
                "role exists under its recorded id"
                if present
                else "the server does not list this role"
            ),
            discord_id=discord_id,
            stoat_id=stoat_id,
        )

    # Categories are the ONE entity whose expected name Ferry records, in
    # state.category_names, so this is the only name comparison in the tool. A
    # channel or role rename is undetectable, which is stated as a limit rather
    # than approximated (spec P2 S11).
    #
    # `categories` is Optional upstream: the key can be absent entirely on a
    # server that never had one, not merely an empty list.
    found_titles = {
        c["id"]: c.get("title", "")
        for c in (server.get("categories") or [])
        if isinstance(c, dict) and "id" in c
    }
    for discord_id, stoat_id in state.category_map.items():
        if stoat_id not in found_titles:
            report.add(
                name=f"category:{discord_id}",
                status="fail",
                kind="category_missing",
                detail="the server does not list this category",
                discord_id=discord_id,
                stoat_id=stoat_id,
            )
            continue
        expected_title = state.category_names.get(discord_id)
        actual_title = found_titles[stoat_id]
        if expected_title is None:
            # The category exists, but nothing records what it should be called,
            # so the title half of this check cannot be answered. Reporting ok
            # would claim more than was verified.
            #
            # Unreachable for a state written by a current Ferry: structure.py
            # writes category_map and category_names on the same line, twice
            # over, and the only unpaired writes are the dry-run ones the
            # precondition refuses. It IS reachable for a state.json written
            # before category_names existed, which is exactly the population
            # the degrade-rather-than-refuse rule exists for.
            report.add(
                name=f"category:{discord_id}",
                status="unverifiable",
                kind="category_title_unknown",
                detail=(
                    "the category exists, but this state records no expected title "
                    "for it, so the title cannot be compared"
                ),
                discord_id=discord_id,
                stoat_id=stoat_id,
                found=actual_title,
            )
        elif expected_title != actual_title:
            # warn, not fail. The category exists and its channels are intact;
            # only a heading differs. This is the single reachable warn in the
            # tool, and it is why the status exists at all: a design review
            # found warn promised in two acceptance criteria and produced by no
            # code path.
            report.add(
                name=f"category:{discord_id}",
                status="warn",
                kind="category_title_mismatch",
                detail="the category exists but its title differs from the recorded one",
                discord_id=discord_id,
                stoat_id=stoat_id,
                expected=expected_title,
                found=actual_title,
            )
        else:
            report.add(
                name=f"category:{discord_id}",
                status="ok",
                kind="category_present",
                detail="category exists under its recorded id",
                discord_id=discord_id,
                stoat_id=stoat_id,
            )

    emoji_ids = {e["_id"] for e in emoji if isinstance(e, dict) and "_id" in e}
    for discord_id, stoat_id in state.emoji_map.items():
        present = stoat_id in emoji_ids
        report.add(
            name=f"emoji:{discord_id}",
            status="ok" if present else "fail",
            kind="emoji_present" if present else "emoji_missing",
            detail=(
                "emoji exists under its recorded id"
                if present
                else "the server does not list this emoji"
            ),
            discord_id=discord_id,
            stoat_id=stoat_id,
        )


#: How many messages the tail check asks for per channel.
#:
#: 100 is the upstream maximum and costs exactly what 1 costs, because the rate
#: bucket counts REQUESTS. Asking for the whole window rather than the single
#: newest message is what lets a merge parent, a forum index channel, and a
#: channel someone posted in since all report ok instead of unverifiable.
_WINDOW = 100


async def _check_tails(
    sess: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    state: MigrationState,
    report: CheckReport,
    on_event: EventCallback,
) -> None:
    """Confirm each channel still holds the last message Ferry recorded sending.

    One request per channel, whatever the channel's size. Reads land in the
    ``channels`` bucket, 15 per 10 seconds keyed per channel id, because
    upstream routes to ``messaging`` only on POST, so separate channels do not
    contend with each other.
    """
    on_event(
        MigrationEvent(
            phase="check",
            status="started",
            message="Checking each channel's most recent messages...",
        )
    )
    for discord_id, stoat_id in state.channel_map.items():
        window = await api_fetch_messages(
            sess, stoat_url, token, stoat_id, limit=_WINDOW, sort="Latest"
        )
        window_ids = [m["_id"] for m in window if isinstance(m, dict) and "_id" in m]
        status, kind, detail = _classify_tail(
            expected=_expected_tail(state, discord_id),
            window_ids=window_ids,
            recorded_count=state.channel_message_counts.get(discord_id, 0),
        )
        report.add(
            name=f"tail:{discord_id}",
            status=status,
            kind=kind,
            detail=detail,
            discord_id=discord_id,
            stoat_id=stoat_id,
            expected=_expected_tail(state, discord_id),
        )


def _expected_tail(state: MigrationState, discord_id: str) -> str | None:
    """The Stoat id of the last message Ferry believes it sent to this channel.

    TWO hops, and both can miss without either being an error.

    ``channel_high_water`` is keyed by DISCORD channel id and holds a DISCORD
    message id, so it must be resolved through ``message_map`` to become a Stoat
    id. Comparing without that hop compares a Discord id against a Stoat one and
    never matches.

    A missing high-water entry means the channel sent nothing, which is ordinary
    for an empty Discord channel. A missing map entry means the send returned
    409 DuplicateNonce, where batch 7 deliberately records no id. Neither may
    raise: an unconditional lookup reports the commonest correct case as a
    crash.
    """
    high_water = state.channel_high_water.get(discord_id)
    if high_water is None:
        return None
    return state.message_map.get(high_water)


def _classify_tail(
    *,
    expected: str | None,
    window_ids: list[str],
    recorded_count: int,
) -> tuple[CheckStatus, str, str]:
    """Decide one channel's verdict. A pure function, deliberately.

    Order matters and is not arbitrary. The zero-count rule runs FIRST so a
    channel that legitimately received nothing is never dragged through the
    lookups below it.
    """
    if recorded_count == 0:
        return (
            "ok",
            "nothing_expected",
            "no messages were migrated to this channel, and none were expected",
        )
    if not window_ids:
        return (
            "fail",
            "channel_empty",
            "state records messages for this channel but the server returned none",
        )
    if expected is None:
        return (
            "unverifiable",
            "tail_not_recorded",
            "Ferry recorded no id for this channel's last message, which happens "
            "when a send was accepted as a duplicate. Its delivery cannot be "
            "confirmed here (see issue #240).",
        )
    if expected in set(window_ids):
        return ("ok", "tail_present", "the last message Ferry recorded is still present")
    return ("unverifiable", "tail_not_recorded", "placeholder, refined in task #257")
