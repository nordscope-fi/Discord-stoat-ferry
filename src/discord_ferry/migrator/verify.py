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

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, get_args

from discord_ferry.core.events import MigrationEvent
from discord_ferry.core.http import new_session
from discord_ferry.errors import CheckError, FerryError
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
#: intact. It has exactly **three** producers, all name comparisons, and they are
#: enumerated here rather than left implicit because a design review once found
#: ``warn`` promised in two acceptance criteria and produced by no code path at
#: all:
#:
#: * ``category_title_mismatch``, since v2.16.0
#: * ``channel_renamed`` and ``role_renamed``, since 2.17.0, once
#:   ``created_channel_names`` and ``created_role_names`` gave the check an
#:   expected name to compare against
#:
#: The tail check emits no ``warn`` at all, which
#: ``test_the_tail_check_never_emits_warn`` pins across every tail scenario.
STATUSES: tuple[CheckStatus, ...] = get_args(CheckStatus)


@dataclass
class CheckResult:
    """One entity's verdict.

    Carries more than :class:`~discord_ferry.migrator.probe.ProbeCheck` on
    purpose. This report is the contract the repair tool consumes, and a status
    plus a sentence is not actionable: a repair needs the entity's ids and an
    enumerable defect category it can dispatch on, not prose to parse.

    The complete ``kind`` vocabulary lives here rather than only in the design
    document, because that directory is gitignored and this is the contract
    another batch will be written against:

    ==================  ====================================================
    Family              Kinds
    ==================  ====================================================
    Channel identity    ``channel_present``, ``channel_missing``,
                        ``channel_not_visible``, ``channel_renamed``
    Role identity       ``role_present``, ``role_missing``, ``role_renamed``
    Category identity   ``category_present``, ``category_missing``,
                        ``category_title_mismatch``, ``category_title_unknown``
    Emoji identity      ``emoji_present``, ``emoji_missing``
    Tail                ``nothing_expected``, ``tail_present``,
                        ``channel_empty``,
                        ``tail_absent``, ``tail_and_after_absent``,
                        ``tail_not_recorded``, ``tail_window_exhausted``
    Failure to look     ``check_error``
    ==================  ====================================================

    Two pairs are deliberately NOT collapsed, both for the same reason.
    ``tail_absent`` against ``tail_and_after_absent``: one message gone, against
    everything from that point on. ``tail_not_recorded`` against
    ``tail_window_exhausted``: Ferry recorded no id and can never confirm it,
    against the id being merely out of a 100-message window's reach, which a
    tool willing to page further back could resolve. Collapsing either pair
    forces a repair to parse the prose detail, which is what a ``kind`` exists
    to avoid.
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
        verified: set[str] = set()
        await _check_structure(sess, stoat_url, token, state, report, on_event, verified)
        await _check_tails(sess, stoat_url, token, state, report, on_event, verified)
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
    verified: set[str],
) -> None:
    """Compare every recorded entity against the server, by id.

    ``verified`` is filled with the channel keys that came back readable. The
    tail check consults it and skips the rest, so one deleted channel yields one
    finding rather than two.

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
    # An id-to-name dict, mirroring `found_titles` in the category branch below.
    # The name is needed for the rename comparison and is already in this
    # response, so keeping it costs no request and the two-request budget for the
    # whole structure family is unchanged.
    #
    # The isinstance and `"_id" in c` guards are unchanged, so `visible_ids`
    # derived from this dict has exactly the membership the previous set
    # comprehension produced, malformed entries dropped identically.
    visible_names = {
        c["_id"]: _readable_name(c)
        for c in (payload.get("channels") or [])
        if isinstance(c, dict) and "_id" in c
    }
    visible_ids = set(visible_names)

    for discord_id, stoat_id in state.channel_map.items():
        # `discord_id` is not always a Discord snowflake: the forum index writer
        # stores a synthetic `forum-index-{key}`. Only the VALUE is sent to the
        # server, so identity checking is valid for those entries too.
        if stoat_id in visible_ids:
            verified.add(discord_id)
            # The rename comparison lives INSIDE this arm, which has already
            # decided the channel is present. That satisfies "one cause, one
            # result" structurally rather than by a guard: a channel taking
            # either arm below cannot reach this code, so a missing or invisible
            # channel can never also be reported as renamed.
            #
            # A channel with no recorded name skips the comparison entirely and
            # keeps `channel_present`. That is what makes a state file written
            # before 2.17.0 honest rather than noisy, and it is a documented
            # limit rather than a missing check.
            recorded = state.created_channel_names.get(discord_id)
            found_name = visible_names.get(stoat_id)
            # `found_name is not None` is doing real work: an object Ferry could
            # not read a name from must not be compared, or every such channel
            # reports a rename nobody made.
            if recorded is not None and found_name is not None and recorded != found_name:
                report.add(
                    name=f"channel:{discord_id}",
                    status="warn",
                    kind="channel_renamed",
                    detail=(
                        "the channel exists and its content is intact, but it has "
                        "been renamed on the server since the migration"
                    ),
                    discord_id=discord_id,
                    stoat_id=stoat_id,
                    expected=recorded,
                    found=found_name,
                )
                continue
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
    #
    # Kept whole rather than collapsed straight to its keys, because the rename
    # comparison needs the Role objects the values hold. `set()` over a dict
    # takes its keys, so `role_ids` is exactly what the previous one-liner
    # produced.
    roles_map = server.get("roles") or {}
    role_ids = set(roles_map)
    for discord_id, stoat_id in state.role_map.items():
        present = stoat_id in role_ids
        if present:
            recorded_role = state.created_role_names.get(discord_id)
            # Guard the VALUE, matching what the channel and category branches
            # already do. Until this comparison existed the roles map was
            # consumed with set(), which takes the KEYS and never touches a
            # value, so a None or a bare string was harmless. Reading a name off
            # one raises AttributeError, and mypy --strict cannot catch it
            # because the payload is typed dict[str, Any]. A malformed value
            # degrades to no-name-found rather than aborting the whole check.
            found_role_name = _readable_name(roles_map.get(stoat_id))
            if (
                recorded_role is not None
                and found_role_name is not None
                and recorded_role != found_role_name
            ):
                report.add(
                    name=f"role:{discord_id}",
                    status="warn",
                    kind="role_renamed",
                    detail=(
                        "the role exists and its permissions are intact, but it "
                        "has been renamed on the server since the migration"
                    ),
                    discord_id=discord_id,
                    stoat_id=stoat_id,
                    expected=recorded_role,
                    found=found_role_name,
                )
                continue
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


#: How many channels the tail check reads at once.
#:
#: Matches the default `init_request_semaphore` uses for a migration. The
#: per-channel bucket allows 15 requests per 10 seconds EACH, so this is
#: nowhere near a limit; it exists to avoid opening 200 sockets at once.
_MAX_CONCURRENT_TAILS = 5


#: The prefix `structure.py` puts on a forum index channel's synthetic
#: `channel_map` key. Its message id lives under the BARE key in
#: `forum_index_message_ids`, so one of the two has to be translated.
_FORUM_INDEX_PREFIX = "forum-index-"


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
    verified: set[str],
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
    # Fanned out, not sequential. The rate-limit bucket is keyed
    # ("channels", channel_id), so separate channels never contend with each
    # other, and 200 channels in series would be 200 round trips of pure
    # latency for no reason.
    #
    # Bounded by a LOCAL semaphore rather than the module-level one in api.py.
    # That one is initialised by whoever drives a migration, and a caller using
    # this module directly may not have done so, in which case it is None and
    # imposes no limit at all. A local bound cannot be forgotten.
    #
    # `_one_tail` is written so it CANNOT raise, which is what makes gather safe
    # here. The recorded hazard is `gather(..., return_exceptions=True)`
    # discarding what it collected; a coroutine with nothing to throw leaves
    # gather nothing to swallow, and the ordinary form preserves input order so
    # the report stays deterministic.
    limiter = asyncio.Semaphore(_MAX_CONCURRENT_TAILS)
    targets = [
        (discord_id, stoat_id)
        for discord_id, stoat_id in state.channel_map.items()
        # The structure pass could not read these, and has already said so.
        # Fetching them would fail for the same cause and report it twice,
        # doubling the apparent damage and sending a repair after a channel
        # that may not exist.
        if discord_id in verified
    ]
    results = await asyncio.gather(
        *(
            _one_tail(sess, stoat_url, token, state, discord_id, stoat_id, limiter)
            for discord_id, stoat_id in targets
        )
    )
    for result in results:
        report.results.append(result)


async def _one_tail(
    sess: aiohttp.ClientSession,
    stoat_url: str,
    token: str,
    state: MigrationState,
    discord_id: str,
    stoat_id: str,
    limiter: asyncio.Semaphore,
) -> CheckResult:
    """One channel's tail verdict. Never raises, by construction.

    Every failure becomes a result. That is what lets the caller use a plain
    ``gather`` without ``return_exceptions``, and it is the deliberate contrast
    with the ``validate_after`` block in ``run_migration``, whose blanket
    handler leaves its result dict empty so a check that failed reads exactly
    like one that passed.
    """
    expected = _expected_tail(state, discord_id)
    async with limiter:
        try:
            window = await api_fetch_messages(
                sess, stoat_url, token, stoat_id, limit=_WINDOW, sort="Latest"
            )
        except FerryError as exc:
            # unverifiable, not fail: Ferry could not look, which is not the
            # same as finding something wrong. Only FerryError is caught, so a
            # programming error still surfaces as a crash rather than being
            # filed as a server problem.
            return CheckResult(
                name=f"tail:{discord_id}",
                status="unverifiable",
                kind="check_error",
                detail=f"could not read this channel's messages: {exc}",
                discord_id=discord_id,
                stoat_id=stoat_id,
            )
    window_ids = [m["_id"] for m in window if isinstance(m, dict) and "_id" in m]
    status, kind, detail = _classify_tail(
        expected=expected,
        window_ids=window_ids,
        recorded_count=state.channel_message_counts.get(discord_id, 0),
        thread_strategy=state.thread_strategy,
    )
    return CheckResult(
        name=f"tail:{discord_id}",
        status=status,
        kind=kind,
        detail=detail,
        discord_id=discord_id,
        stoat_id=stoat_id,
        expected=expected,
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
    # A forum index channel's newest message is the index itself, posted by
    # _rebuild_forum_indexes AFTER the messages phase, and recorded in its own
    # map rather than in message_map.
    #
    # The two maps are keyed differently, which is the trap: channel_map holds
    # `forum-index-{key}` while forum_index_message_ids holds the bare `{key}`.
    # Looking the prefixed key up in the second map never matches and drops
    # every forum index channel into unverifiable.
    if discord_id.startswith(_FORUM_INDEX_PREFIX):
        forum_key = discord_id[len(_FORUM_INDEX_PREFIX) :]
        return state.forum_index_message_ids.get(forum_key)

    high_water = state.channel_high_water.get(discord_id)
    if high_water is None:
        return None
    return state.message_map.get(high_water)


def _readable_name(obj: object) -> str | None:
    """The ``name`` of an entity object, or None when it cannot be read.

    None means "no name found", which is NOT the same as an empty name and must
    not be compared against a recorded one. A malformed entry, or an object with
    no ``name`` key, would otherwise read as ``""`` and differ from every
    recorded name, reporting a rename nobody made on a response Ferry simply
    could not parse.

    The isinstance guards are hand-written because ``mypy --strict`` cannot help
    here: the payload arrives as ``dict[str, Any]``, so every value off it is
    ``Any``. The channel and category branches guard the same way.
    """
    if not isinstance(obj, dict):
        return None
    name = obj.get("name")
    return name if isinstance(name, str) else None


def _tail_not_recorded_detail(thread_strategy: str) -> str:
    """Word the one unverifiable case whose cause Ferry can sometimes name.

    ``thread_strategy`` is ``""`` for every migration that predates the field,
    and that is the only case where the possibilities have to be listed rather
    than named. Keeping the v2.16.0 wording there is deliberate: naming an empty
    or unknown strategy would read as a defect rather than as an old state file.

    Under ``merge`` the cause is usually structural rather than a failure.
    ``_merge_threads`` never writes ``message_map``, so a parent channel that
    absorbed thread content legitimately has no recorded last message.
    """
    if not thread_strategy:
        return (
            "Ferry recorded no id for this channel's last message, which happens "
            "when a send was accepted as a duplicate. Its delivery cannot be "
            "confirmed here (see issue #240)."
        )
    if thread_strategy == "merge":
        return (
            "Ferry recorded no id for this channel's last message. The migration "
            "ran with --thread-strategy=merge, under which a parent channel that "
            "absorbed thread content records no id for what it sent, so this is "
            "expected rather than a failure. A duplicate send produces the same "
            "result (see issue #240)."
        )
    return (
        "Ferry recorded no id for this channel's last message. The migration ran "
        f"with --thread-strategy={thread_strategy}, which does not produce this "
        "on its own, so a send was accepted as a duplicate (see issue #240)."
    )


def _classify_tail(
    *,
    expected: str | None,
    window_ids: list[str],
    recorded_count: int,
    thread_strategy: str,
) -> tuple[CheckStatus, str, str]:
    """Decide one channel's verdict. A pure function, deliberately.

    Order matters and is not arbitrary. The zero-count rule runs FIRST so a
    channel that legitimately received nothing is never dragged through the
    lookups below it.
    """
    if recorded_count == 0 and expected is None:
        return (
            "ok",
            "nothing_expected",
            "no messages were migrated to this channel, and none were expected",
        )
    # The `and expected is None` half is not belt-and-braces. A forum index
    # channel has NO channel_message_counts entry, because that map is keyed by
    # real export channels and the index channel is synthetic. Its expected
    # message is nonetheless known, from forum_index_message_ids. Firing the
    # zero-count rule on count alone would report "nothing expected" for a
    # channel whose one message Ferry can actually verify, and would leave the
    # forum branch of _expected_tail permanently unreachable.
    if not window_ids:
        return (
            "fail",
            "channel_empty",
            "state records messages for this channel but the server returned none",
        )
    if expected is None:
        return ("unverifiable", "tail_not_recorded", _tail_not_recorded_detail(thread_strategy))
    if expected in set(window_ids):
        return ("ok", "tail_present", "the last message Ferry recorded is still present")

    # The tail is absent. Two very different reasons, and telling them apart is
    # what makes this check worth running.
    #
    # Stoat message ids are ULIDs, whose leading 48 bits are a millisecond
    # timestamp in Crockford base32, so lexicographic order on equal-length ids
    # IS time order. That lets the window's position be compared against the
    # expected tail without any timestamps.
    #
    # min() and max(), never window[0] or window[-1]. MessageSort::Latest is
    # doc!{"_id": -1} upstream and the vector is not reversed, so element zero
    # is the NEWEST. Reading it as the oldest inverts both verdicts below: real
    # loss would report unverifiable while ordinary post-migration activity
    # would report fail. Taking the extremes by value also makes this
    # independent of whatever order the server chooses to send.
    newest = max(window_ids)
    oldest = min(window_ids)
    if newest < expected:
        # Everything on the server predates the tail, so the tail and every
        # message after it is gone while older content survives. Stronger
        # evidence of loss than the branch below, where only the tail itself is
        # missing, and separated because a repair would treat them differently.
        return (
            "fail",
            "tail_and_after_absent",
            "every recent message predates the last one Ferry recorded, so that "
            "message and everything after it is missing",
        )
    if oldest < expected:
        # The window spans the tail's own position in time and the tail is not
        # in it. It was deleted.
        return (
            "fail",
            "tail_absent",
            "the last message Ferry recorded is missing, though messages from "
            "both before and after it are present",
        )
    # The whole window is newer than the tail: more than the window's worth of
    # messages arrived since. Not evidence of loss. This is the branch that
    # keeps a large merge, and a busy channel, off the failure list.
    # A DIFFERENT kind from the `expected is None` branch above, and the two are
    # separated for the same reason the two fail kinds are. That one is
    # permanently unanswerable: Ferry recorded no id, so no amount of looking
    # will ever confirm it. This one is merely out of reach of a 100-message
    # window, and a repair tool willing to page further back could resolve it.
    # Collapsing them would force that tool to parse the prose detail, which is
    # what having a kind exists to avoid.
    return (
        "unverifiable",
        "tail_window_exhausted",
        f"more than {_WINDOW} messages have arrived since the last one Ferry "
        "recorded, so the window does not reach far enough back to confirm it",
    )
