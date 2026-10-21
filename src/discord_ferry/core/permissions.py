"""Pure permission facts and proposed values, separate from operation readiness."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from functools import partial
from hashlib import sha256
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

ProblemReason = Literal[
    "unsupported",
    "unverified",
    "write_rejected",
    "write_uncertain",
    "readback_mismatch",
    "interrupted",
]
OutcomeReason = Literal["verified", ProblemReason]
PermissionStage = Literal["preflight", "application", "recheck"]
PermissionEffects = Literal["none_confirmed", "changes_confirmed", "changes_possible"]
_PROBLEM_REASONS = frozenset(
    {
        "unsupported",
        "unverified",
        "write_rejected",
        "write_uncertain",
        "readback_mismatch",
        "interrupted",
    }
)


def _integer(value: int | None, name: str, *, unknown: bool = False) -> None:
    if value is None and unknown:
        return
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


def _identity(value: str) -> None:
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise ValueError("Structural identity must be a nonempty string without whitespace")


def _immutable(value: object, expected: type = tuple) -> None:
    if not isinstance(value, expected):
        raise ValueError("Permission fact collections must be immutable")


def _ids(values: tuple[str, ...] | frozenset[str]) -> None:
    _immutable(values, frozenset if isinstance(values, frozenset) else tuple)
    for value in values:
        _identity(value)


def _pairs(values: tuple[tuple[object, object], ...]) -> None:
    _immutable(values)
    for pair in values:
        _immutable(pair)
        if len(pair) != 2:
            raise ValueError("Permission fact pairs need exactly two entries")


def _fact(value: object, expected: type, *, unknown: bool = False) -> None:
    if not (value is None and unknown) and not isinstance(value, expected):
        raise ValueError(f"Expected immutable {expected.__name__} fact")


def _records(values: tuple[object, ...], expected: type) -> None:
    _immutable(values)
    for value in values:
        _fact(value, expected)


@dataclass(frozen=True)
class PermissionProblem:
    """A structural failure and operator guidance, without source content."""

    reason: ProblemReason
    entity_ids: tuple[str, ...]
    bit: int | None
    guidance: str

    def __post_init__(self) -> None:
        if self.reason not in _PROBLEM_REASONS:
            raise ValueError("Unknown permission problem reason")
        _ids(self.entity_ids)
        _integer(self.bit, "bit", unknown=True)


@dataclass(frozen=True)
class PermissionCell:
    """One explicit allow/deny pair; None at the owner means unobserved."""

    allow: int
    deny: int

    def __post_init__(self) -> None:
        _integer(self.allow, "allow")
        _integer(self.deny, "deny")


@dataclass(frozen=True)
class SourceRole:
    """Raw role facts, including presence before translation or filtering."""

    id: str
    permissions: int | None = None
    position: int | None = None
    managed: bool | None = None
    capture_fields: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        _identity(self.id)
        _integer(self.permissions, "permissions", unknown=True)
        _integer(self.position, "position", unknown=True)
        if self.managed is not None and type(self.managed) is not bool:
            raise ValueError("managed must be Boolean or unknown")
        _immutable(self.capture_fields, frozenset)
        _ids(self.capture_fields)


@dataclass(frozen=True)
class SourceOverwrite:
    """Raw everyone/role overwrite; member identities are not retained."""

    role_id: str
    allow: int
    deny: int

    def __post_init__(self) -> None:
        _identity(self.role_id)
        _integer(self.allow, "allow")
        _integer(self.deny, "deny")


@dataclass(frozen=True)
class SourceChannel:
    """Raw channel facts; missing collections differ from explicit empty ones."""

    id: str
    type: int | None = None
    overwrites: tuple[SourceOverwrite, ...] | None = None
    member_overwrite_count: int | None = None
    capture_fields: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        _identity(self.id)
        _integer(self.type, "channel type", unknown=True)
        _integer(self.member_overwrite_count, "member overwrite count", unknown=True)
        if self.overwrites is not None:
            _records(self.overwrites, SourceOverwrite)
        _immutable(self.capture_fields, frozenset)
        _ids(self.capture_fields)


@dataclass(frozen=True)
class PermissionSourceEvidence:
    """Versioned raw source snapshot, never proof of target application."""

    guild_id: str
    translation_version: str
    roles: tuple[SourceRole, ...] = ()
    channels: tuple[SourceChannel, ...] = ()
    covered_channel_ids: frozenset[str] = frozenset()
    fingerprint: str = ""
    schema_version: int = 1

    def __post_init__(self) -> None:
        _identity(self.guild_id)
        _identity(self.translation_version)
        _integer(self.schema_version, "schema version")
        _records(self.roles, SourceRole)
        _records(self.channels, SourceChannel)
        _immutable(self.covered_channel_ids, frozenset)
        _ids(self.covered_channel_ids)


@dataclass(frozen=True)
class TargetRole:
    """Observed role identity, grants and rank, without proposed substitutes."""

    id: str
    permissions: PermissionCell | None = None
    rank: int | None = None

    def __post_init__(self) -> None:
        _identity(self.id)
        _fact(self.permissions, PermissionCell, unknown=True)
        _integer(self.rank, "rank", unknown=True)


@dataclass(frozen=True)
class PlannedRole:
    """Proposed role facts bound to source identity, before target creation."""

    source_id: str
    permissions: PermissionCell
    rank: int

    def __post_init__(self) -> None:
        _identity(self.source_id)
        _fact(self.permissions, PermissionCell)
        _integer(self.rank, "planned rank")


@dataclass(frozen=True)
class TargetChannel:
    """Observed channel cells; missing role inventory is not an empty one."""

    id: str
    default: PermissionCell | None = None
    roles: tuple[tuple[str, PermissionCell], ...] | None = None

    def __post_init__(self) -> None:
        _identity(self.id)
        _fact(self.default, PermissionCell, unknown=True)
        if self.roles is not None:
            _pairs(self.roles)
            for role_id, cell in self.roles:
                _identity(role_id)
                _fact(cell, PermissionCell)


@dataclass(frozen=True)
class TargetLayout:
    """Observed context and separate planned mappings, unknown by default."""

    service: str = ""
    server_id: str = ""
    roles: tuple[TargetRole, ...] = ()
    server_default: int | None = None
    channels: tuple[TargetChannel, ...] = ()
    planned_roles: tuple[tuple[str, str | None], ...] = ()
    planned_channels: tuple[tuple[str, str | None], ...] = ()
    fingerprint: str = ""
    planned_role_facts: tuple[PlannedRole, ...] = ()
    planned_server_default: int | None = None

    def __post_init__(self) -> None:
        if self.server_id:
            _identity(self.server_id)
        _integer(self.server_default, "server default", unknown=True)
        _records(self.roles, TargetRole)
        _records(self.channels, TargetChannel)
        _records(self.planned_role_facts, PlannedRole)
        _integer(self.planned_server_default, "planned server default", unknown=True)
        _pairs(self.planned_roles)
        _pairs(self.planned_channels)
        for source, target in (*self.planned_roles, *self.planned_channels):
            _identity(source)
            if target is not None:
                _identity(target)


@dataclass(frozen=True)
class BitPredicate:
    """Source result P OR (NOT N AND (c OR G)) over assigned role indices."""

    P: int
    N: int
    G: int
    c: bool

    def __post_init__(self) -> None:
        for name, value in (("P", self.P), ("N", self.N), ("G", self.G)):
            _integer(value, name)
        if type(self.c) is not bool:
            raise ValueError("Predicate constant must be Boolean")


def evaluate_source_bit(predicate: BitPredicate, members: int) -> bool:
    """Evaluate explicit allows/admin before aggregated denies and base grants."""
    _integer(members, "members")
    return bool(
        (predicate.P & members)
        or (not (predicate.N & members) and (predicate.c or predicate.G & members))
    )


def constant_result(p: BitPredicate, all_roles: int, zeros: int, ones: int = 0) -> bool | None:
    """Return a source constant for every membership satisfying the constraints."""
    for name, value in (("all_roles", all_roles), ("zeros", zeros), ("ones", ones)):
        _integer(value, name)
    if (p.P | p.N | p.G | zeros | ones) & ~all_roles or zeros & ones:
        raise ValueError("Predicate and disjoint constraints must share the role universe")
    unknown = all_roles & ~(zeros | ones)
    if p.P & ones:
        return True
    if not (p.N & ones) and not (p.N & unknown & ~p.P) and (p.c or p.G & ones):
        return True
    if p.P & unknown:
        return None
    if p.N & ones:
        return False
    if not p.c and not (p.G & ones) and not (p.G & unknown & ~p.N):
        return False
    return None


def synthesize_bit(
    role_count: int,
    query: Callable[[int, int], bool | None],
    target_grants: int,
    base: bool,
) -> tuple[bool | None, ...] | None:
    """Return exact low-to-high-priority overrides, or None when unsupported.

    A tuple entry is an explicit allow/deny Boolean or None for neutral.
    Query answers describe all memberships satisfying its zero/one masks.
    The fixed fallback is base OR assigned target grants. Higher-priority
    roles are visited first; neutral roles stay free in subsequent queries.
    """
    _integer(role_count, "role_count")
    _integer(target_grants, "target_grants")
    all_roles = (1 << role_count) - 1
    if target_grants & ~all_roles or type(base) is not bool:
        raise ValueError("Fixed target grants and base must match the role universe")

    def ask(zeros: int, ones: int = 0) -> bool | None:
        result = query(zeros, ones)
        if result is not None and type(result) is not bool:
            raise ValueError("Constant queries must return Boolean or None")
        return result

    operations: list[bool | None] = [None] * role_count
    zeros = 0
    for index in range(role_count - 1, -1, -1):
        role = 1 << index
        result = ask(zeros, role)
        if result is not None:
            operations[index] = result
            zeros |= role
    if base:
        if ask(zeros) is not True:
            return None
    else:
        if ask(zeros | target_grants) is not False:
            return None
        for index in range(role_count):
            role = 1 << index
            if target_grants & role and not zeros & role and ask(zeros, role) is not True:
                return None
    return tuple(operations)


def _source_role_changes_bit(predicate: BitPredicate, role: int) -> bool:
    """Whether toggling one role can change the result for any other assignment."""
    if predicate.P & role:
        # Without this allow, all-zero membership requires c; a non-allow deny
        # supplies a counterexample even when c is true.
        return not predicate.c or bool(predicate.N & ~predicate.P)
    if predicate.N & role:
        # Only other grants not defeated by their own deny can survive.
        return predicate.c or bool(predicate.G & ~(predicate.P | predicate.N))
    return bool(predicate.G & role) and not predicate.c


def _form_source_bit(
    everyone: SourceRole,
    roles: tuple[SourceRole, ...],
    overwrites: dict[str, SourceOverwrite],
    bit: int,
) -> BitPredicate:
    """Form one raw bit after structural validation, before implicit dependencies."""
    assert everyone.permissions is not None
    if everyone.permissions & 8:  # Discord ADMINISTRATOR bypasses all overwrites.
        return BitPredicate(0, 0, 0, True)
    mask = 1 << bit
    default = overwrites.get(everyone.id, SourceOverwrite(everyone.id, 0, 0))
    c = bool(((everyone.permissions & ~default.deny) | default.allow) & mask)
    guild_survives = not bool((default.allow | default.deny) & mask)
    allows = denies = grants = 0
    for index, role in enumerate(roles):
        assert role.permissions is not None
        assigned = 1 << index
        overwrite = overwrites.get(role.id)
        if role.permissions & 8 or (overwrite is not None and overwrite.allow & mask):
            allows |= assigned
        if overwrite is not None and overwrite.deny & mask:
            denies |= assigned
        if guild_survives and role.permissions & mask:
            grants |= assigned
    return BitPredicate(allows, denies, grants, c)


def build_source_predicates(
    source: PermissionSourceEvidence,
    channel_id: str,
    role_ids: tuple[str, ...],
    bits: tuple[int, ...],
) -> tuple[tuple[tuple[int, BitPredicate], ...], tuple[PermissionProblem, ...]]:
    """Form raw source bits or refuse, without target encoding or readiness.

    The metadata owner validates snapshot binding before calculation. This
    boundary additionally requires complete structural inputs and an exact
    ordered universe of ordinary roles, excluding everyone and managed roles.
    Implicit dependencies and target compatibility belong to plan compilation.
    """
    entities: tuple[str, ...] = ()

    def refuse(
        reason: ProblemReason, guidance: str
    ) -> tuple[tuple[tuple[int, BitPredicate], ...], tuple[PermissionProblem, ...]]:
        return (), (PermissionProblem(reason, entities, None, guidance),)

    try:
        _identity(channel_id)
        entities = (channel_id,)
        _immutable(role_ids)
        _ids(role_ids)
        _immutable(bits)
        for bit in bits:
            _integer(bit, "bit")
    except ValueError:
        return refuse("unverified", "Supply valid channel, role and permission selections")
    if len(set(bits)) != len(bits) or len(set(role_ids)) != len(role_ids):
        return refuse("unverified", "Supply unique role and permission selections")
    roles_by_id = {role.id: role for role in source.roles}
    if len(roles_by_id) != len(source.roles) or source.guild_id not in roles_by_id:
        return refuse("unverified", "Refresh a unique role inventory including everyone")
    if any(
        role.permissions is None
        or role.position is None
        or role.managed is None
        or not {"permissions", "position", "managed"} <= role.capture_fields
        for role in source.roles
    ):
        return refuse("unverified", "Refresh complete raw role facts before calculation")
    ordinary = {role.id for role in source.roles if role.id != source.guild_id and not role.managed}
    if set(role_ids) != ordinary:
        return refuse("unverified", "Bind every ordinary source role exactly once")
    channels = tuple(channel for channel in source.channels if channel.id == channel_id)
    if len(channels) != 1 or channel_id not in source.covered_channel_ids:
        return refuse("unverified", "Refresh unique evidence covering the selected channel")
    channel = channels[0]
    if (
        channel.type is None
        or channel.overwrites is None
        or channel.member_overwrite_count is None
        or not {"type", "permission_overwrites"} <= channel.capture_fields
    ):
        return refuse("unverified", "Refresh complete raw channel permission facts")
    overwrites = {overwrite.role_id: overwrite for overwrite in channel.overwrites}
    if len(overwrites) != len(channel.overwrites) or not overwrites.keys() <= roles_by_id.keys():
        return refuse("unverified", "Refresh unique overwrites bound to known source roles")
    # Discord channel resource: ordinary guild overwrite scopes, not threads,
    # direct messages or directory channels. Never infer parent/member access.
    if channel.type not in {0, 2, 4, 5, 13, 15, 16}:
        return refuse("unsupported", "This source channel scope has no exact supported model")
    if channel.member_overwrite_count:
        return refuse("unsupported", "Member-specific source overwrites cannot be reproduced")
    managed = tuple(role for role in source.roles if role.id != source.guild_id and role.managed)
    roles = tuple(roles_by_id[role_id] for role_id in role_ids) + managed
    retained = (1 << len(role_ids)) - 1
    pairs = []
    for bit in bits:
        predicate = _form_source_bit(roles_by_id[source.guild_id], roles, overwrites, bit)
        for index, role in enumerate(managed, start=len(role_ids)):
            if _source_role_changes_bit(predicate, 1 << index):
                return (), (
                    PermissionProblem(
                        "unsupported",
                        (channel_id, role.id),
                        bit,
                        "A managed source role changes access without an exact target mapping",
                    ),
                )
        pairs.append(
            (
                bit,
                BitPredicate(
                    predicate.P & retained,
                    predicate.N & retained,
                    predicate.G & retained,
                    predicate.c,
                ),
            )
        )
    return tuple(pairs), ()


@dataclass(frozen=True)
class ChannelPermissionPlan:
    """Proposed exact cells and preserved masks, not content authority."""

    source_key: str
    source_ids: frozenset[str] = frozenset()
    default: PermissionCell | None = None
    roles: tuple[tuple[str, PermissionCell], ...] = ()
    affected_mask: int = 0
    preserved_mask: int = 0
    target_layout: TargetLayout = field(default_factory=TargetLayout)
    problems: tuple[PermissionProblem, ...] = ()
    source_predicates: tuple[tuple[int, BitPredicate], ...] = ()

    def __post_init__(self) -> None:
        _identity(self.source_key)
        _fact(self.default, PermissionCell, unknown=True)
        _fact(self.target_layout, TargetLayout)
        _immutable(self.source_ids, frozenset)
        _ids(self.source_ids)
        _pairs(self.roles)
        _records(self.problems, PermissionProblem)
        _pairs(self.source_predicates)
        for bit, predicate in self.source_predicates:
            _integer(bit, "predicate bit")
            _fact(predicate, BitPredicate)
        _integer(self.affected_mask, "affected mask")
        _integer(self.preserved_mask, "preserved mask")
        for role_id, cell in self.roles:
            _identity(role_id)
            _fact(cell, PermissionCell)

    @classmethod
    def unverified(cls, key: str, guidance: str) -> ChannelPermissionPlan:
        return cls(key, problems=(PermissionProblem("unverified", (key,), None, guidance),))


@dataclass(frozen=True)
class PermissionPlan:
    """Immutable calculation results bound to source and destination facts."""

    source_fingerprint: str = ""
    calculation_fingerprint: str = ""
    target_layout: TargetLayout = field(default_factory=TargetLayout)
    channels: tuple[ChannelPermissionPlan, ...] = ()
    problems: tuple[PermissionProblem, ...] = ()

    def __post_init__(self) -> None:
        _fact(self.target_layout, TargetLayout)
        _records(self.channels, ChannelPermissionPlan)
        _records(self.problems, PermissionProblem)

    @classmethod
    def refused(cls, problems: tuple[PermissionProblem, ...]) -> PermissionPlan:
        if not problems:
            raise ValueError("A refused plan needs a problem")
        return cls(problems=problems)


def _permission_mapping() -> dict[int, tuple[int, ...]]:
    # The mapping keeps its existing owner. A function-local import avoids the
    # package initialization cycle through the metadata domain-record imports.
    from discord_ferry.discord.permissions import DISCORD_TO_STOAT

    return {
        source: tuple(target) if isinstance(target, list) else (target,)
        for source, target in DISCORD_TO_STOAT.items()
    }


def _calculation_roles(layout: TargetLayout) -> tuple[str, ...]:
    actual = {role.id: role for role in layout.roles}
    future = {role.source_id: role for role in layout.planned_role_facts}
    priorities: dict[str, int] = {}
    for source, target in layout.planned_roles:
        role: TargetRole | PlannedRole = actual[target] if target is not None else future[source]
        assert role.rank is not None
        priorities[source] = role.rank
    return tuple(sorted(priorities, key=priorities.__getitem__, reverse=True))


def validate_calculation_inputs(
    source: PermissionSourceEvidence, layout: TargetLayout
) -> tuple[PermissionProblem, ...]:
    """Check operation scope, complete facts and the supported target fallback."""

    def refuse(reason: ProblemReason, guidance: str, *ids: str) -> tuple[PermissionProblem, ...]:
        return (PermissionProblem(reason, tuple(ids), None, guidance),)

    if (
        source.schema_version != 1
        or source.translation_version != "discord-to-stoat-v1"
        or not isinstance(source.fingerprint, str)
        or not source.fingerprint
    ):
        return refuse("unverified", "Refresh validated and bound source permission evidence")
    if (
        not isinstance(layout.service, str)
        or not layout.service
        or not isinstance(layout.fingerprint, str)
        or not layout.fingerprint
    ):
        return refuse("unverified", "Bind a complete target service and permission context")
    if layout.server_id:
        if layout.server_default is None or layout.planned_server_default is not None:
            return refuse(
                "unverified", "Read the actual server default separately from planned values"
            )
    elif (
        layout.planned_server_default is None
        or layout.server_default is not None
        or layout.roles
        or layout.channels
    ):
        return refuse("unverified", "Supply explicit planned context for the new server")
    raw_roles = {role.id: role for role in source.roles}
    if len(raw_roles) != len(source.roles) or source.guild_id not in raw_roles:
        return refuse("unverified", "Refresh a unique source role inventory including everyone")
    ordinary = {
        role.id for role in source.roles if role.id != source.guild_id and role.managed is False
    }
    mappings = dict(layout.planned_roles)
    if len(mappings) != len(layout.planned_roles) or set(mappings) != ordinary:
        return refuse("unverified", "Bind every ordinary source role exactly once")
    actual = {role.id: role for role in layout.roles}
    future = {role.source_id: role for role in layout.planned_role_facts}
    if len(actual) != len(layout.roles) or len(future) != len(layout.planned_role_facts):
        return refuse("unverified", "Read unique actual and planned role identities")
    targets = tuple(target for target in mappings.values() if target is not None)
    if len(set(targets)) != len(targets) or not set(targets) <= actual.keys():
        return refuse("unverified", "Resolve unique mapped target roles from actual evidence")
    if set(future) != {sid for sid, target in mappings.items() if target is None}:
        return refuse("unverified", "Supply separate facts for each uncreated mapped role")
    if any(role.permissions is None or role.rank is None for role in layout.roles):
        return refuse("unverified", "Read every target role grant, deny and rank")
    ranks = tuple(role.rank for role in layout.roles) + tuple(
        role.rank for role in layout.planned_role_facts
    )
    if len(set(ranks)) != len(ranks):
        return refuse("unverified", "Resolve target role priority without ambiguous rank ties")
    selected = dict(layout.planned_channels)
    channels = {channel.id: channel for channel in layout.channels}
    raw_channels = {channel.id: channel for channel in source.channels}
    if (
        not selected
        or len(selected) != len(layout.planned_channels)
        or len(channels) != len(layout.channels)
        or len(raw_channels) != len(source.channels)
        or not selected.keys() <= raw_channels.keys()
    ):
        return refuse("unverified", "Bind a unique complete selected channel scope")
    target_channels = tuple(target for target in selected.values() if target is not None)
    if (
        len(set(target_channels)) != len(target_channels)
        or not set(target_channels) <= channels.keys()
    ):
        return refuse("unverified", "Read unique actual target channel bindings")
    ordered = _calculation_roles(layout)
    mapping = _permission_mapping()
    affected = sum(1 << target for target in {bit for values in mapping.values() for bit in values})
    for role in layout.roles:
        assert role.permissions is not None
        if role.permissions.deny & affected:
            return refuse(
                "unsupported", "Server-role deny effects require an exact supported model", role.id
            )
        if role.id not in targets and role.permissions.allow & affected:
            return refuse(
                "unsupported", "Native target role access has no source intent binding", role.id
            )
    for sid, target in selected.items():
        _, problems = build_source_predicates(source, sid, ordered, (10,))
        if problems:
            return problems
        if raw_channels[sid].type in {2, 4, 13}:
            return refuse(
                "unsupported",
                "This source scope needs additional category or live voice evidence",
                sid,
            )
        if target is None:
            continue
        channel = channels[target]
        if channel.default is None or channel.roles is None:
            return refuse("unverified", "Read complete channel defaults and role cells", target)
        overrides = dict(channel.roles)
        if len(overrides) != len(channel.roles):
            return refuse("unverified", "Read unique channel role cells", target)
        # The bound inventory is complete. The backend ignores cells for absent roles.
        # Preserve those observed cells, including their mapped bits, in the layout.
        if any(
            rid in actual and rid not in targets and (cell.allow | cell.deny) & affected
            for rid, cell in overrides.items()
        ):
            return refuse(
                "unsupported", "Native channel-role effects have no source intent binding", target
            )
    from discord_ferry.discord.permissions import translate_permissions

    for sid, planned_role in future.items():
        raw = raw_roles[sid].permissions
        if raw is None or planned_role.permissions != PermissionCell(translate_permissions(raw), 0):
            return refuse(
                "unverified", "Bind planned grants to the existing role creation owner", sid
            )
    everyone = raw_roles[source.guild_id].permissions
    if not layout.server_id and (
        everyone is None or layout.planned_server_default != translate_permissions(everyone)
    ):
        return refuse(
            "unverified", "Bind the planned server default to its existing creation owner"
        )
    return ()


def _predicate_terms(predicate: BitPredicate, wanted: bool) -> tuple[tuple[int, int], ...]:
    """Expand role literals, never member subsets, into zero/one constraint cubes."""
    terms: list[tuple[int, int]] = []
    candidates = predicate.P if wanted else predicate.N & ~predicate.P
    while candidates:
        role = candidates & -candidates
        terms.append((0 if wanted else predicate.P, role))
        candidates ^= role
    if wanted:
        if predicate.c:
            terms.append((predicate.N, 0))
        else:
            candidates = predicate.G & ~predicate.N
            while candidates:
                role = candidates & -candidates
                terms.append((predicate.N, role))
                candidates ^= role
    elif not predicate.c:
        terms.append((predicate.P | predicate.G, 0))
    return tuple(terms)


def _conjunction_possible(
    conditions: tuple[tuple[BitPredicate, bool], ...], all_roles: int, zeros: int, ones: int
) -> bool:
    if not conditions:
        return True
    predicate, wanted = conditions[0]
    constant = constant_result(predicate, all_roles, zeros, ones)
    if constant is not None:
        return constant is wanted and _conjunction_possible(conditions[1:], all_roles, zeros, ones)
    for absent, present in _predicate_terms(predicate, wanted):
        if not (zeros & present or ones & absent) and _conjunction_possible(
            conditions[1:], all_roles, zeros | absent, ones | present
        ):
            return True
    return False


def _visible_bit_query(
    view: BitPredicate,
    required: tuple[BitPredicate, ...],
    all_roles: int,
    grants: int,
    base: bool,
    zeros: int,
    ones: int,
) -> bool | None:
    visible = ((view, True),)
    can_allow = _conjunction_possible(
        visible + tuple((p, True) for p in required), all_roles, zeros, ones
    )
    can_deny = any(
        _conjunction_possible(visible + ((p, False),), all_roles, zeros, ones) for p in required
    )
    if can_allow and can_deny:
        return None
    if can_allow or can_deny:
        return can_allow
    # No visible source member satisfies this cube. Match the fixed fallback
    # there instead of imposing an additional restriction on hidden members.
    return base or bool(grants & ones)


def compile_supported_bits(
    source: PermissionSourceEvidence, layout: TargetLayout
) -> PermissionPlan:
    """Compile complete validated channel scope, preserving all unrelated cells."""
    problems = validate_calculation_inputs(source, layout)
    if problems:
        return PermissionPlan.refused(problems)
    mapping = _permission_mapping()
    affected = sum(
        1 << bit for bit in {target for targets in mapping.values() for target in targets}
    )
    source_roles = {role.id: role for role in source.roles}
    actual = {role.id: role for role in layout.roles}
    future = {role.source_id: role for role in layout.planned_role_facts}
    bindings = dict(layout.planned_roles)
    ordered = _calculation_roles(layout)
    fixed: list[PermissionCell] = []
    for role_id in ordered:
        actual_id = bindings[role_id]
        cell = (
            actual[actual_id].permissions if actual_id is not None else future[role_id].permissions
        )
        assert cell is not None
        fixed.append(cell)
    managed = tuple(
        sorted(
            (r for r in source.roles if r.id != source.guild_id and r.managed), key=lambda r: r.id
        )
    )
    roles = tuple(source_roles[sid] for sid in ordered) + managed
    all_roles = (1 << len(roles)) - 1
    observed = {c.id: c for c in layout.channels}
    raw_channels = {c.id: c for c in source.channels}
    channels: list[ChannelPermissionPlan] = []
    for sid, target in layout.planned_channels:
        raw_channel = raw_channels[sid]
        assert raw_channel.overwrites is not None
        overwrites = {o.role_id: o for o in raw_channel.overwrites}
        predicates = {
            bit: _form_source_bit(source_roles[source.guild_id], roles, overwrites, bit)
            for bit in mapping
        }
        view = predicates[10]
        old = (
            observed[target] if target is not None else TargetChannel(sid, PermissionCell(0, 0), ())
        )
        assert old.default is not None and old.roles is not None
        old_cells = dict(old.roles)
        cells = {
            role_id: old_cells[actual_id]
            if (actual_id := bindings[role_id]) is not None and actual_id in old_cells
            else PermissionCell(0, 0)
            for role_id in ordered
        }
        allow = old.default.allow & ~affected
        deny = old.default.deny & ~affected
        proposed = {
            role_id: PermissionCell(cell.allow & ~affected, cell.deny & ~affected)
            for role_id, cell in cells.items()
        }
        preserved = old.default.allow | old.default.deny
        for _, cell in old.roles:
            preserved |= cell.allow | cell.deny
        for bit, targets in mapping.items():
            required: tuple[BitPredicate, ...] = (predicates[bit],)
            if bit in {14, 15, 17}:
                required += (predicates[11],)
            preferred_base = all(p.c for p in required)
            for target_bit in targets:
                mask = 1 << target_bit
                grants = sum(1 << i for i, cell in enumerate(fixed) if cell.allow & mask)
                # With no source visibility for the default member, either
                # data-bit default is valid. Try both before declaring failure.
                bases = (
                    (preferred_base,)
                    if bit == 10 or view.c
                    else (preferred_base, not preferred_base)
                )
                operations = None
                for base in bases:
                    query: Callable[[int, int], bool | None]
                    if bit == 10:
                        query = partial(constant_result, view, all_roles)
                    else:
                        query = partial(_visible_bit_query, view, required, all_roles, grants, base)
                    operations = synthesize_bit(len(ordered), query, grants, base)
                    if operations is not None:
                        break
                if operations is None:
                    return PermissionPlan.refused(
                        (
                            PermissionProblem(
                                "unsupported",
                                (sid,),
                                target_bit,
                                "Existing grants and priority cannot express exact source access",
                            ),
                        )
                    )
                allow |= mask if base else 0
                deny |= 0 if base else mask
                for role_id, operation in zip(ordered, operations, strict=True):
                    cell = proposed[role_id]
                    proposed[role_id] = PermissionCell(
                        cell.allow | (mask if operation is True else 0),
                        cell.deny | (mask if operation is False else 0),
                    )
        channels.append(
            ChannelPermissionPlan(
                sid,
                frozenset({sid}),
                PermissionCell(allow, deny),
                tuple(proposed.items()),
                affected,
                preserved & ~affected,
                layout,
                source_predicates=tuple(sorted(predicates.items())),
            )
        )
    fingerprint = sha256(
        json.dumps(
            {
                "calculation_version": 1,
                "source": asdict(source),
                "layout": asdict(layout),
                "mapping": mapping,
            },
            default=lambda value: sorted(value) if isinstance(value, frozenset) else value,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return PermissionPlan(source.fingerprint, fingerprint, layout, tuple(channels))


def compile_permission_plan(
    source: PermissionSourceEvidence, layout: TargetLayout
) -> PermissionPlan:
    """Return one complete exact proposal or a whole-operation refusal."""
    return compile_supported_bits(source, layout)


@dataclass(frozen=True)
class PermissionReadback:
    """Actual context and decoding problems, never expected-value fallback."""

    layout: TargetLayout = field(default_factory=TargetLayout)
    problems: tuple[PermissionProblem, ...] = ()

    def __post_init__(self) -> None:
        _fact(self.layout, TargetLayout)
        if self.layout.planned_role_facts or self.layout.planned_server_default is not None:
            raise ValueError("Proposed role/default facts cannot stand in for observed readback")
        _records(self.problems, PermissionProblem)


@dataclass(frozen=True)
class PermissionOutcome:
    """Operation stage, verified status and independently retained effects."""

    stage: PermissionStage
    reason: OutcomeReason
    effects: PermissionEffects
    problems: tuple[PermissionProblem, ...] = ()

    def __post_init__(self) -> None:
        if self.stage not in {"preflight", "application", "recheck"}:
            raise ValueError("Unknown permission stage")
        if self.reason not in _PROBLEM_REASONS | {"verified"}:
            raise ValueError("Unknown permission outcome reason")
        if self.effects not in {"none_confirmed", "changes_confirmed", "changes_possible"}:
            raise ValueError("Unknown permission effects")
        _records(self.problems, PermissionProblem)

    @property
    def verified(self) -> bool:
        return self.reason == "verified" and not self.problems


@dataclass
class PermissionRuntime:
    """Run-local lifecycle data. Verification methods belong to T09."""

    plan: PermissionPlan | None = None
    ready: dict[str, PermissionReadback] = field(default_factory=dict)
    stopped: bool = False
    effects: PermissionEffects = "none_confirmed"
    outcome: PermissionOutcome = field(
        default_factory=lambda: PermissionOutcome("preflight", "unverified", "none_confirmed")
    )
