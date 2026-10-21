"""Contracts for permission facts, proposed values and operation-local readiness."""

from dataclasses import FrozenInstanceError, replace
from importlib import import_module
from importlib.util import find_spec
from itertools import product
from types import ModuleType

import pytest


def permissions() -> ModuleType:
    assert find_spec("discord_ferry.core.permissions") is not None, (
        "Permission contracts are missing"
    )
    return import_module("discord_ferry.core.permissions")


def test_problem_retains_structural_identity() -> None:
    p = permissions()
    problem = p.PermissionProblem("unverified", ("111",), None, "Refresh source evidence")
    assert problem.entity_ids == ("111",)
    assert problem.reason == "unverified"
    with pytest.raises(FrozenInstanceError):
        problem.reason = "verified"


def test_evidence_keeps_large_raw_masks_and_missing_presence_distinct() -> None:
    p = permissions()
    role = p.SourceRole("111", 1 << 100, 0, False, frozenset({"permissions"}))
    overwrite = p.SourceOverwrite("111", 0, 1 << 100)
    channel = p.SourceChannel("222", 0, (overwrite,), 0, frozenset({"permission_overwrites"}))
    source = p.PermissionSourceEvidence(
        "111", "v1", (role,), (channel,), frozenset({"222"}), "source"
    )
    assert source.schema_version == 1
    assert source.roles[0].permissions == 1 << 100
    assert source.channels[0].overwrites[0].deny == 1 << 100
    assert p.SourceChannel("333").overwrites is None
    assert p.SourceChannel("333", overwrites=()).overwrites == ()
    assert p.SourceRole("111").capture_fields == frozenset()
    with pytest.raises(FrozenInstanceError):
        source.fingerprint = "changed"


def test_target_observation_never_inherits_proposed_values() -> None:
    p = permissions()
    pair = p.PermissionCell(1 << 100, 0)
    layout = p.TargetLayout()
    assert layout.server_default is None
    assert layout.service == layout.server_id == layout.fingerprint == ""
    observed = p.TargetChannel("target")
    assert observed.default is None
    assert observed.roles is None
    role = p.TargetRole("role", pair, 3)
    bound = p.TargetLayout(
        "https://example.test",
        "server",
        (role,),
        0,
        (observed,),
        (("111", None),),
        (("222", "target"),),
        "target-fingerprint",
    )
    readback = p.PermissionReadback(bound)
    assert readback.layout.channels[0].default is None
    assert readback.layout.planned_roles == (("111", None),)
    assert readback.layout.roles[0].permissions.allow == 1 << 100


def test_refused_plans_have_no_writable_cells_or_readiness() -> None:
    p = permissions()
    channel = p.ChannelPermissionPlan.unverified("222", "Refresh source evidence")
    assert channel.source_key == "222"
    assert channel.default is None and channel.roles == ()
    assert channel.problems[0].reason == "unverified"
    plan = p.PermissionPlan.refused(channel.problems)
    assert plan.channels == () and plan.problems == channel.problems
    runtime = p.PermissionRuntime(plan=plan)
    other = p.PermissionRuntime()
    assert runtime.ready == other.ready == {}
    assert not runtime.stopped
    assert runtime.effects == "none_confirmed"
    assert runtime.outcome.reason == "unverified" and not runtime.outcome.verified
    runtime.ready["target"] = p.PermissionReadback()
    assert other.ready == {}


@pytest.mark.parametrize(
    "reason",
    [
        "unsupported",
        "unverified",
        "write_rejected",
        "write_uncertain",
        "readback_mismatch",
        "interrupted",
    ],
)
def test_outcome_keeps_reason_stage_and_effects(reason: str) -> None:
    p = permissions()
    problem = p.PermissionProblem(reason, ("222",), 100, "Check permissions")
    for stage in ("preflight", "application", "recheck"):
        for effects in ("none_confirmed", "changes_confirmed", "changes_possible"):
            outcome = p.PermissionOutcome(stage, reason, effects, (problem,))
            assert not outcome.verified
            assert (outcome.reason, outcome.stage, outcome.effects) == (reason, stage, effects)
    assert p.PermissionOutcome("recheck", "verified", "none_confirmed").verified
    assert not p.PermissionOutcome("recheck", "verified", "none_confirmed", (problem,)).verified


@pytest.mark.parametrize(
    "record,args",
    [
        ("PermissionProblem", ("verified", (), None, "Check")),
        ("PermissionProblem", ("unverified", ("",), None, "Check")),
        ("PermissionProblem", ("unverified", (), True, "Check")),
        ("PermissionCell", (-1, 0)),
        ("PermissionCell", (True, 0)),
        ("SourceRole", ("111", -1)),
        ("SourceRole", ("111", 0, -1)),
        ("SourceRole", ("111", 0, 0, 1)),
        ("SourceRole", (" ",)),
        ("SourceOverwrite", ("111", 0, -1)),
        ("SourceChannel", ("222", True)),
        ("SourceChannel", ("222", 0, (), -1)),
        ("TargetRole", ("role", None, -1)),
        ("TargetChannel", ("",)),
        ("PermissionSourceEvidence", ("", "v1")),
        ("PermissionOutcome", ("bad-stage", "verified", "none_confirmed")),
        ("PermissionOutcome", ("preflight", "bad-reason", "none_confirmed")),
        ("PermissionOutcome", ("preflight", "unverified", "bad-effects")),
    ],
)
def test_structural_values_reject_invalid_integer_and_identity_inputs(
    record: str, args: tuple[object, ...]
) -> None:
    with pytest.raises(ValueError):
        getattr(permissions(), record)(*args)


def test_fact_containers_reject_mutable_collections() -> None:
    p = permissions()
    with pytest.raises(ValueError):
        p.PermissionProblem("unverified", ["111"], None, "Check")
    with pytest.raises(ValueError):
        p.SourceRole("111", capture_fields={"permissions"})
    with pytest.raises(ValueError):
        p.PermissionPlan(channels=[])
    with pytest.raises(ValueError):
        p.TargetLayout(planned_roles=(("111", ""),))


@pytest.mark.parametrize(
    "record,args",
    [
        ("PermissionCell", (None, 0)),
        ("SourceOverwrite", ("111", 0, None)),
        ("TargetLayout", ("", "", (), None, (), (["111", None],))),
        ("TargetChannel", ("target", None, (["role", None],))),
        ("ChannelPermissionPlan", ("222", frozenset(), None, (["role", None],))),
    ],
)
def test_required_masks_and_nested_mutable_pairs_are_rejected(
    record: str, args: tuple[object, ...]
) -> None:
    with pytest.raises(ValueError):
        getattr(permissions(), record)(*args)


def test_channel_plan_retains_immutable_predicate_facts_without_evaluating() -> None:
    p = permissions()
    predicate = p.BitPredicate(P=1 << 100, N=0, G=1, c=False)
    channel = p.ChannelPermissionPlan(
        "222", source_ids=frozenset({"222"}), source_predicates=((100, predicate),)
    )
    assert channel.source_predicates == ((100, predicate),)
    with pytest.raises(FrozenInstanceError):
        predicate.c = True
    with pytest.raises(ValueError):
        p.BitPredicate(P=True, N=0, G=0, c=False)
    with pytest.raises(ValueError):
        p.BitPredicate(P=0, N=0, G=0, c=1)
    with pytest.raises(ValueError):
        p.ChannelPermissionPlan("222", source_predicates=([100, predicate],))


@pytest.mark.parametrize(
    "record,kwargs",
    [
        (
            "PermissionSourceEvidence",
            {"guild_id": "111", "translation_version": "v1", "roles": ([],)},
        ),
        (
            "PermissionSourceEvidence",
            {"guild_id": "111", "translation_version": "v1", "channels": ([],)},
        ),
        ("SourceChannel", {"id": "222", "overwrites": ([],)}),
        ("TargetLayout", {"roles": ([],)}),
        ("TargetLayout", {"channels": ([],)}),
        ("TargetRole", {"id": "role", "permissions": []}),
        ("TargetChannel", {"id": "target", "default": []}),
        ("TargetChannel", {"id": "target", "roles": (("role", []),)}),
        ("ChannelPermissionPlan", {"source_key": "222", "roles": (("role", []),)}),
        ("ChannelPermissionPlan", {"source_key": "222", "source_predicates": ((0, []),)}),
        ("PermissionPlan", {"channels": ([],)}),
        ("PermissionReadback", {"problems": ([],)}),
    ],
)
def test_immutable_facts_cannot_hide_mutable_nested_values(
    record: str, kwargs: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        getattr(permissions(), record)(**kwargs)


def source_functions() -> ModuleType:
    p = permissions()
    assert callable(getattr(p, "evaluate_source_bit", None)), "Source evaluation is missing"
    assert callable(getattr(p, "build_source_predicates", None)), "Source formation is missing"
    return p


def source_evidence(
    grants: tuple[int, ...],
    overwrites: tuple[tuple[int, int], ...],
    *,
    managed: frozenset[int] = frozenset(),
):
    p = permissions()
    roles = tuple(
        p.SourceRole(
            str(111 + i),
            grant,
            i,
            i in managed,
            frozenset({"permissions", "position", "managed"}),
        )
        for i, grant in enumerate(grants)
    )
    channel = p.SourceChannel(
        "222",
        0,
        tuple(p.SourceOverwrite(str(111 + i), *pair) for i, pair in enumerate(overwrites)),
        0,
        frozenset({"type", "permission_overwrites"}),
    )
    return p.PermissionSourceEvidence(
        "111", "discord-to-stoat-v1", roles, (channel,), frozenset({"222"}), "bound"
    )


def source_oracle(source, members: frozenset[str], bit: int) -> bool:
    """Apply full raw bitfields in Discord's documented sequence, not the predicate."""
    value = source.roles[0].permissions
    for role in source.roles[1:]:
        if role.id in members:
            value |= role.permissions
    if value & 8:
        return True
    for overwrite in source.channels[0].overwrites:
        if overwrite.role_id == source.guild_id:
            value = (value & ~overwrite.deny) | overwrite.allow
    deny = allow = 0
    for overwrite in source.channels[0].overwrites:
        if overwrite.role_id in members:
            deny |= overwrite.deny
            allow |= overwrite.allow
    value = (value & ~deny) | allow
    return bool(value & (1 << bit))


def test_explicit_allow_wins_over_another_role_deny() -> None:
    p = source_functions()
    predicate = p.BitPredicate(P=2, N=1, G=0, c=False)
    assert p.evaluate_source_bit(predicate, 3) is True
    assert p.evaluate_source_bit(predicate, 1) is False


@pytest.mark.parametrize("members", [-1, True, 1.5, None])
def test_source_evaluation_rejects_invalid_membership(members: object) -> None:
    p = source_functions()
    with pytest.raises(ValueError):
        p.evaluate_source_bit(p.BitPredicate(0, 0, 0, False), members)


def test_source_formation_matches_full_bitfield_oracle_exhaustively() -> None:
    p = source_functions()
    # Guild grants include administrator and a deliberately wide unmapped bit.
    grants = (0, 1 << 10, 8, (1 << 10) | (1 << 100))
    cells = ((0, 0), (1 << 10, 0), (0, 1 << 10), (1 << 10, 1 << 10))
    for raw in product(grants, repeat=3):
        for overwrites in product(cells, repeat=3):
            source = source_evidence(raw, overwrites)
            for ids in (("112", "113"), ("113", "112")):
                pairs, problems = p.build_source_predicates(source, "222", ids, (10, 100))
                assert problems == ()
                assert tuple(bit for bit, _ in pairs) == (10, 100)
                for membership in range(4):
                    assigned = frozenset(
                        role_id for i, role_id in enumerate(ids) if membership & (1 << i)
                    )
                    for bit, predicate in pairs:
                        assert p.evaluate_source_bit(predicate, membership) == source_oracle(
                            source, assigned, bit
                        ), (raw, overwrites, ids, membership, bit)


def test_source_formation_uses_requested_universe_not_capture_or_discord_rank_order() -> None:
    p = source_functions()
    source = source_evidence((0, 0, 1 << 100), ((0, 0), (0, 1 << 100), (1 << 100, 0)))
    source = replace(source, roles=tuple(reversed(source.roles)))
    pairs, problems = p.build_source_predicates(source, "222", ("113", "112"), (100,))
    assert problems == ()
    assert pairs == ((100, p.BitPredicate(P=1, N=2, G=1, c=False)),)


def test_source_formation_retains_membership_bits_above_machine_width() -> None:
    p = source_functions()
    source = source_evidence((0,) * 102, ((0, 0),) * 101 + ((1 << 100, 0),))
    ids = tuple(role.id for role in source.roles[1:])
    pairs, problems = p.build_source_predicates(source, "222", ids, (100,))
    assert problems == ()
    assert pairs == ((100, p.BitPredicate(P=1 << 100, N=0, G=0, c=False)),)
    assert p.evaluate_source_bit(pairs[0][1], 1 << 100)
    assert not p.evaluate_source_bit(pairs[0][1], (1 << 100) - 1)


@pytest.mark.parametrize("channel_type", [0, 2, 4, 5, 13, 15, 16])
def test_source_formation_accepts_known_guild_overwrite_scopes(channel_type: int) -> None:
    p = source_functions()
    source = source_evidence((0,), ())
    source = replace(source, channels=(replace(source.channels[0], type=channel_type),))
    assert p.build_source_predicates(source, "222", (), (10,)) == (
        ((10, p.BitPredicate(0, 0, 0, False)),),
        (),
    )


@pytest.mark.parametrize("channel_type", [1, 3, 10, 11, 12, 14, 99])
def test_source_formation_refuses_non_guild_thread_and_unknown_scopes(channel_type: int) -> None:
    p = source_functions()
    source = source_evidence((0,), ())
    source = replace(source, channels=(replace(source.channels[0], type=channel_type),))
    pairs, problems = p.build_source_predicates(source, "222", (), (10,))
    assert pairs == () and problems
    assert {problem.reason for problem in problems} == {"unsupported"}


def test_source_formation_refuses_member_specific_effects_without_member_facts() -> None:
    p = source_functions()
    source = source_evidence((0,), ())
    source = replace(source, channels=(replace(source.channels[0], member_overwrite_count=1),))
    pairs, problems = p.build_source_predicates(source, "222", (), (10,))
    assert pairs == () and problems[0].reason == "unsupported"
    assert problems[0].entity_ids == ("222",)


def test_source_managed_role_refusal_matches_actual_effect_exhaustively() -> None:
    p = source_functions()
    grants = (0, 1 << 10, 8)
    cells = ((0, 0), (1 << 10, 0), (0, 1 << 10), (1 << 10, 1 << 10))
    for raw in product(grants, repeat=3):
        for overwrites in product(cells, repeat=3):
            source = source_evidence(raw, overwrites, managed=frozenset({2}))
            expected = {
                members: source_oracle(source, members, 10)
                for members in (
                    frozenset(),
                    frozenset({"112"}),
                    frozenset({"113"}),
                    frozenset({"112", "113"}),
                )
            }
            material = any(
                expected[members] != expected[members | {"113"}]
                for members in (frozenset(), frozenset({"112"}))
            )
            pairs, problems = p.build_source_predicates(source, "222", ("112",), (10,))
            if material:
                assert pairs == () and problems[0].reason == "unsupported"
                assert problems[0].bit == 10 and "113" in problems[0].entity_ids
            else:
                assert problems == ()
                for membership in range(2):
                    assigned = frozenset({"112"}) if membership else frozenset()
                    assert p.evaluate_source_bit(pairs[0][1], membership) == expected[assigned]


@pytest.mark.parametrize(
    "change",
    [
        "missing-channel",
        "uncovered",
        "duplicate-channel",
        "missing-everyone",
        "duplicate-role",
        "unknown-grants",
        "unknown-managed",
        "unknown-position",
        "missing-role-presence",
        "unknown-overwrites",
        "unknown-members",
        "unknown-type",
        "missing-channel-presence",
        "duplicate-overwrite",
        "unknown-overwrite-role",
    ],
)
def test_source_formation_refuses_incomplete_or_ambiguous_facts(change: str) -> None:
    p = source_functions()
    source = source_evidence((0, 0), ((0, 0),))
    channel = source.channels[0]
    if change == "missing-channel":
        source = replace(source, channels=())
    elif change == "uncovered":
        source = replace(source, covered_channel_ids=frozenset())
    elif change == "duplicate-channel":
        source = replace(source, channels=(channel, channel))
    elif change == "missing-everyone":
        source = replace(source, roles=source.roles[1:])
    elif change == "duplicate-role":
        source = replace(source, roles=source.roles + (source.roles[1],))
    elif change in {"unknown-grants", "unknown-managed", "unknown-position"}:
        field = {
            "unknown-grants": "permissions",
            "unknown-managed": "managed",
            "unknown-position": "position",
        }[change]
        source = replace(source, roles=(source.roles[0], replace(source.roles[1], **{field: None})))
    elif change == "missing-role-presence":
        source = replace(
            source, roles=(source.roles[0], replace(source.roles[1], capture_fields=frozenset()))
        )
    elif change == "unknown-overwrite-role":
        source = replace(
            source, channels=(replace(channel, overwrites=(p.SourceOverwrite("999", 0, 0),)),)
        )
    elif change == "duplicate-overwrite":
        source = replace(source, channels=(replace(channel, overwrites=channel.overwrites * 2),))
    else:
        field, value = {
            "unknown-overwrites": ("overwrites", None),
            "unknown-members": ("member_overwrite_count", None),
            "unknown-type": ("type", None),
            "missing-channel-presence": ("capture_fields", frozenset()),
        }[change]
        source = replace(source, channels=(replace(channel, **{field: value}),))
    pairs, problems = p.build_source_predicates(source, "222", ("112",), (10,))
    assert pairs == () and problems
    assert all(problem.reason == "unverified" for problem in problems)


@pytest.mark.parametrize("ids", [(), ("112", "112"), ("111", "112"), ("113",), ("",), ["112"]])
def test_source_formation_refuses_inexact_role_universe(ids: object) -> None:
    p = source_functions()
    pairs, problems = p.build_source_predicates(source_evidence((0, 0), ()), "222", ids, (10,))
    assert pairs == () and problems[0].reason == "unverified"


@pytest.mark.parametrize(
    "channel_id,bits",
    [
        ("", (10,)),
        (" ", (10,)),
        (None, (10,)),
        ("222", (True,)),
        ("222", (-1,)),
        ("222", (10, 10)),
        ("222", [10]),
    ],
)
def test_source_formation_refuses_invalid_selection_without_problem_constructor_failure(
    channel_id, bits
) -> None:
    p = source_functions()
    pairs, problems = p.build_source_predicates(source_evidence((0,), ()), channel_id, (), bits)
    assert pairs == () and problems[0].reason == "unverified"


def test_source_formation_does_not_return_partial_bits_or_mutate_evidence() -> None:
    p = source_functions()
    source = source_evidence((0, 1 << 11), (), managed=frozenset({1}))
    original = replace(source)
    pairs, problems = p.build_source_predicates(source, "222", (), (10, 11))
    assert pairs == () and problems[0].bit == 11
    assert source == original
    assert p.PermissionRuntime().ready == {}


def test_source_formation_ignores_managed_effects_outside_requested_bits() -> None:
    p = source_functions()
    source = source_evidence((0, 1 << 100), (), managed=frozenset({1}))
    assert p.build_source_predicates(source, "222", (), (10,)) == (
        ((10, p.BitPredicate(0, 0, 0, False)),),
        (),
    )


def test_source_formation_requires_ordered_not_just_immutable_role_universe() -> None:
    p = source_functions()
    pairs, problems = p.build_source_predicates(
        source_evidence((0, 0), ()), "222", frozenset({"112"}), (10,)
    )
    assert pairs == () and problems[0].reason == "unverified"


def test_source_formation_handles_joint_managed_effects_without_assignments() -> None:
    p = source_functions()
    bit = 1 << 10
    cells = ((0, 0), (bit, 0), (0, bit), (bit, bit))
    memberships = [
        frozenset(str(112 + i) for i in range(3) if mask & (1 << i)) for mask in range(8)
    ]
    for everyone in (0, bit):
        for grants in product((0, bit), repeat=3):
            for overwrites in product(cells, repeat=3):
                source = source_evidence(
                    (everyone, *grants), ((0, 0), *overwrites), managed=frozenset({2, 3})
                )
                expected = {members: source_oracle(source, members, 10) for members in memberships}
                material = any(
                    value != expected[members & {"112"}] for members, value in expected.items()
                )
                pairs, problems = p.build_source_predicates(source, "222", ("112",), (10,))
                if material:
                    assert pairs == () and problems[0].reason == "unsupported"
                else:
                    assert problems == ()
                    assert p.evaluate_source_bit(pairs[0][1], 0) == expected[frozenset()]
                    assert p.evaluate_source_bit(pairs[0][1], 1) == expected[frozenset({"112"})]
