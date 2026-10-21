"""Pure permission facts and proposed values, separate from operation readiness."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

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

    def __post_init__(self) -> None:
        if self.server_id:
            _identity(self.server_id)
        _integer(self.server_default, "server default", unknown=True)
        _records(self.roles, TargetRole)
        _records(self.channels, TargetChannel)
        _pairs(self.planned_roles)
        _pairs(self.planned_channels)
        for source, target in (*self.planned_roles, *self.planned_channels):
            _identity(source)
            if target is not None:
                _identity(target)


@dataclass(frozen=True)
class BitPredicate:
    """Immutable source predicate masks; formation and evaluation belong to T05."""

    P: int
    N: int
    G: int
    c: bool

    def __post_init__(self) -> None:
        for name, value in (("P", self.P), ("N", self.N), ("G", self.G)):
            _integer(value, name)
        if type(self.c) is not bool:
            raise ValueError("Predicate constant must be Boolean")


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


@dataclass(frozen=True)
class PermissionReadback:
    """Actual context and decoding problems, never expected-value fallback."""

    layout: TargetLayout = field(default_factory=TargetLayout)
    problems: tuple[PermissionProblem, ...] = ()

    def __post_init__(self) -> None:
        _fact(self.layout, TargetLayout)
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
