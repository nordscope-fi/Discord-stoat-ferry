"""Contracts for permission facts, proposed values and operation-local readiness."""

from dataclasses import FrozenInstanceError, replace
from functools import partial
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


def synthesis_functions() -> ModuleType:
    p = source_functions()
    assert callable(getattr(p, "constant_result", None)), "Constant query is missing"
    assert callable(getattr(p, "synthesize_bit", None)), "Exact synthesis is missing"
    return p


def table_query(table: tuple[bool, ...], zeros: int, ones: int) -> bool | None:
    values = {
        value
        for members, value in enumerate(table)
        if not members & zeros and members & ones == ones
    }
    assert values
    return next(iter(values)) if len(values) == 1 else None


def target_truth(operations: tuple[bool | None, ...], grants: int, base: bool) -> tuple[bool, ...]:
    result = []
    for members in range(1 << len(operations)):
        allowed = base or bool(grants & members)
        for index, operation in enumerate(operations):
            if members & (1 << index) and operation is not None:
                allowed = operation
        result.append(allowed)
    return tuple(result)


def test_neutral_role_remains_a_free_membership() -> None:
    p = synthesis_functions()
    predicate = p.BitPredicate(P=2, N=1, G=0, c=False)
    assert p.constant_result(predicate, 3, 0, 1) is None
    assert p.constant_result(predicate, 3, 0, 2) is True


def test_symbolic_constants_match_all_small_source_cofactors() -> None:
    p = synthesis_functions()
    for size in range(4):
        all_roles = (1 << size) - 1
        for allow, deny, grant, base in product(
            range(all_roles + 1), range(all_roles + 1), range(all_roles + 1), (False, True)
        ):
            predicate = p.BitPredicate(allow, deny, grant, base)
            table = tuple(p.evaluate_source_bit(predicate, members) for members in range(1 << size))
            for states in product((0, 1, 2), repeat=size):
                zeros = sum(1 << i for i, state in enumerate(states) if state == 1)
                ones = sum(1 << i for i, state in enumerate(states) if state == 2)
                assert p.constant_result(predicate, all_roles, zeros, ones) is table_query(
                    table, zeros, ones
                ), (predicate, zeros, ones)


def test_synthesis_matches_independent_encodings_for_every_three_role_truth_function() -> None:
    p = synthesis_functions()
    for size in range(4):
        for grants, base in product(range(1 << size), (False, True)):
            possible = {
                target_truth(ops, grants, base) for ops in product((None, False, True), repeat=size)
            }
            for table in product((False, True), repeat=1 << size):
                result = p.synthesize_bit(size, partial(table_query, table), grants, base)
                assert (result is not None) == (table in possible), (size, grants, base, table)
                if result is not None:
                    assert isinstance(result, tuple)
                    assert all(op is None or type(op) is bool for op in result)
                    assert target_truth(result, grants, base) == table


def test_synthesis_matches_four_role_source_predicates_against_independent_encodings() -> None:
    p = synthesis_functions()
    for grants, base in product(range(16), (False, True)):
        possible = {
            target_truth(ops, grants, base) for ops in product((None, False, True), repeat=4)
        }
        for allow, deny, grant in product(range(16), repeat=3):
            predicate = p.BitPredicate(allow, deny, grant, base)
            table = tuple(p.evaluate_source_bit(predicate, members) for members in range(16))
            result = p.synthesize_bit(4, partial(p.constant_result, predicate, 15), grants, base)
            assert (result is not None) == (table in possible), (predicate, grants)
            if result is not None:
                assert target_truth(result, grants, base) == table


@pytest.mark.parametrize("size", [0, 1, 2, 16, 65, 101])
def test_synthesis_query_count_is_linear_for_large_role_universes(size: int) -> None:
    p = synthesis_functions()
    all_roles = (1 << size) - 1
    predicate = p.BitPredicate(0, 0, all_roles, False)
    calls = []

    def query(zeros: int, ones: int) -> bool | None:
        calls.append((zeros, ones))
        assert not zeros & ones and not (zeros | ones) & ~all_roles
        return p.constant_result(predicate, all_roles, zeros, ones)

    assert p.synthesize_bit(size, query, all_roles, False) == (True,) * size
    assert len(calls) <= 2 * size + 2


def test_synthesis_keeps_administrator_priority_and_rejects_conflicting_rank() -> None:
    p = synthesis_functions()
    for administrator, expected in ((2, (False, True)), (1, None)):
        predicate = p.BitPredicate(administrator, 0, 0, False)
        assert p.synthesize_bit(2, partial(p.constant_result, predicate, 3), 3, False) == expected


def test_synthesis_retains_wide_role_masks_and_sparse_constant_constraints() -> None:
    p = synthesis_functions()
    high = 1 << 100
    predicate = p.BitPredicate(high, 1, 0, False)
    assert p.constant_result(predicate, high | 1, 0, 1) is None
    assert p.constant_result(predicate, high | 1, 0, high) is True
    assert p.synthesize_bit(
        101, lambda z, o: p.constant_result(predicate, (high << 1) - 1, z, o), 0, False
    ) == (False,) * 100 + (True,)


@pytest.mark.parametrize(
    "all_roles,zeros,ones",
    [
        (True, 0, 0),
        (-1, 0, 0),
        (3, True, 0),
        (3, -1, 0),
        (3, 0, True),
        (3, 0, -1),
        (3, 1, 1),
        (3, 4, 0),
        (3, 0, 4),
        (0, 0, 0),
    ],
)
def test_constant_queries_reject_invalid_or_inconsistent_universes(all_roles, zeros, ones) -> None:
    p = synthesis_functions()
    with pytest.raises(ValueError):
        p.constant_result(p.BitPredicate(1, 0, 0, False), all_roles, zeros, ones)


@pytest.mark.parametrize(
    "size,grants,base",
    [
        (True, 0, False),
        (-1, 0, False),
        (2, True, False),
        (2, -1, False),
        (2, 4, False),
        (2, 0, 1),
        (2, 0, None),
    ],
)
def test_synthesis_rejects_invalid_fixed_layout_inputs(size, grants, base) -> None:
    p = synthesis_functions()
    with pytest.raises(ValueError):
        p.synthesize_bit(size, lambda z, o: False, grants, base)


@pytest.mark.parametrize("value", [0, 1, -1, "true", (), []])
def test_synthesis_rejects_non_boolean_query_results(value: object) -> None:
    p = synthesis_functions()
    with pytest.raises(ValueError):
        p.synthesize_bit(1, lambda z, o: value, 0, False)


def planned_contracts() -> ModuleType:
    p = permissions()
    assert callable(getattr(p, "PlannedRole", None)), "Explicit planned-role facts are missing"
    return p


def test_planned_role_and_server_facts_remain_separate_from_readback() -> None:
    p = planned_contracts()
    role = p.PlannedRole("112", p.PermissionCell(1 << 100, 0), 0)
    layout = p.TargetLayout(
        planned_roles=(("112", None),), planned_role_facts=(role,), planned_server_default=0
    )
    assert layout.roles == () and layout.server_default is None and layout.server_id == ""
    assert layout.planned_role_facts == (role,)
    assert layout.planned_server_default == 0
    assert p.TargetLayout().planned_server_default is None
    assert p.PermissionReadback().layout.planned_role_facts == ()
    with pytest.raises(ValueError):
        p.PermissionReadback(layout)
    with pytest.raises(FrozenInstanceError):
        role.rank = 1
    with pytest.raises(FrozenInstanceError):
        layout.planned_server_default = 1


@pytest.mark.parametrize(
    "source_id,cell,rank",
    [("", None, 0), ("112", None, 0), ("112", [], 0), ("112", "cell", 0), ("112", 0, -1)],
)
def test_planned_role_rejects_missing_or_mutable_facts(source_id, cell, rank) -> None:
    p = planned_contracts()
    with pytest.raises(ValueError):
        p.PlannedRole(source_id, cell, rank)


def test_planned_layout_rejects_mutable_collections_and_invalid_defaults() -> None:
    p = planned_contracts()
    for facts in ([], ([],)):
        with pytest.raises(ValueError):
            p.TargetLayout(planned_role_facts=facts)
    for value in (True, -1, 0.5):
        with pytest.raises(ValueError):
            p.TargetLayout(planned_server_default=value)


def compiler_functions() -> ModuleType:
    p = planned_contracts()
    for name in (
        "validate_calculation_inputs",
        "compile_supported_bits",
        "compile_permission_plan",
    ):
        assert callable(getattr(p, name, None)), f"Compiler boundary {name} is missing"
    return p


def compiler_layout(source, *, new_server: bool = False):
    p = planned_contracts()
    from discord_ferry.discord.permissions import translate_permissions

    ordinary = [r for r in source.roles if r.id != source.guild_id and not r.managed]
    return p.TargetLayout(
        service="https://target.test",
        server_id="" if new_server else "server",
        roles=()
        if new_server
        else tuple(
            p.TargetRole(
                f"r-{role.id}", p.PermissionCell(translate_permissions(role.permissions), 0), i
            )
            for i, role in enumerate(ordinary)
        ),
        server_default=None if new_server else translate_permissions(source.roles[0].permissions),
        channels=()
        if new_server
        else tuple(
            p.TargetChannel(f"c-{c.id}", p.PermissionCell(0, 0), ()) for c in source.channels
        ),
        planned_roles=tuple((r.id, None if new_server else f"r-{r.id}") for r in ordinary),
        planned_channels=tuple(
            (c.id, None if new_server else f"c-{c.id}") for c in source.channels
        ),
        fingerprint="target-context",
        planned_role_facts=tuple(
            p.PlannedRole(r.id, p.PermissionCell(translate_permissions(r.permissions), 0), i)
            for i, r in enumerate(ordinary)
        )
        if new_server
        else (),
        planned_server_default=translate_permissions(source.roles[0].permissions)
        if new_server
        else None,
    )


def compiled_target_value(channel, layout, assigned: frozenset[str]) -> int:
    """Independent full-bitfield target order, ending with its visibility gate."""
    value = layout.server_default if layout.server_id else layout.planned_server_default
    value = (value | channel.default.allow) & ~channel.default.deny
    observed = {r.id: r for r in layout.roles}
    future = {r.source_id: r for r in layout.planned_role_facts}
    mappings = dict(layout.planned_roles)
    ordered = sorted(
        mappings,
        key=lambda sid: (
            (observed[mappings[sid]] if mappings[sid] is not None else future[sid]).rank
        ),
        reverse=True,
    )
    for sid in ordered:
        if sid in assigned:
            role = observed[mappings[sid]] if mappings[sid] is not None else future[sid]
            value = (value | role.permissions.allow) & ~role.permissions.deny
    cells = dict(channel.roles)
    for sid in ordered:
        if sid in assigned:
            value = (value | cells[sid].allow) & ~cells[sid].deny
    return value if value & (1 << 20) else 0


def test_incomplete_layout_cannot_produce_writable_cells() -> None:
    p = compiler_functions()
    source = p.PermissionSourceEvidence("111", "discord-to-stoat-v1", schema_version=0)
    result = p.compile_permission_plan(source, p.TargetLayout())
    assert result.problems and result.channels == ()
    assert all(problem.reason == "unverified" for problem in result.problems)


@pytest.mark.parametrize("new_server", [False, True])
def test_compiler_private_and_read_only_channels_match_independent_access(new_server: bool) -> None:
    p = compiler_functions()
    view, send = 1 << 10, 1 << 11
    for deny in (view, send):
        source = source_evidence((view | send, view | send), ((0, deny),))
        layout = compiler_layout(source, new_server=new_server)
        result = p.compile_permission_plan(source, layout)
        assert result.problems == () and len(result.channels) == 1
        for assigned in (frozenset(), frozenset({"112"})):
            target = compiled_target_value(result.channels[0], layout, assigned)
            assert bool(target & (1 << 20)) == source_oracle(source, assigned, 10)
            assert bool(target & (1 << 22)) == (
                source_oracle(source, assigned, 10) and source_oracle(source, assigned, 11)
            )


def test_compiler_accounts_for_send_dependencies_and_preserves_unrelated_cells() -> None:
    p = compiler_functions()
    source = source_evidence(((1 << 10) | (1 << 14) | (1 << 15) | (1 << 17),), ())
    layout = compiler_layout(source)
    unrelated = (1 << 100) | (1 << 12)
    layout = replace(
        layout, channels=(p.TargetChannel("c-222", p.PermissionCell(unrelated, 1 << 99), ()),)
    )
    result = p.compile_permission_plan(source, layout)
    assert result.problems == ()
    channel = result.channels[0]
    value = compiled_target_value(channel, layout, frozenset())
    assert value & unrelated == unrelated
    assert channel.default.deny & (1 << 99)
    assert not value & ((1 << 26) | (1 << 27) | (1 << 37) | (1 << 38))


def test_compiler_refuses_all_channels_when_one_is_unsupported() -> None:
    p = compiler_functions()
    source = source_evidence((1 << 10,), ())
    source = replace(
        source,
        channels=source.channels
        + (replace(source.channels[0], id="333", member_overwrite_count=1),),
        covered_channel_ids=frozenset({"222", "333"}),
    )
    result = p.compile_permission_plan(source, compiler_layout(source))
    assert result.channels == () and result.problems[0].reason == "unsupported"


def test_compiler_requires_explicit_planned_role_facts_and_new_server_default() -> None:
    p = compiler_functions()
    source = source_evidence((1 << 10, 0), ())
    layout = compiler_layout(source, new_server=True)
    for incomplete in (
        replace(layout, planned_role_facts=()),
        replace(layout, planned_server_default=None),
    ):
        result = p.compile_permission_plan(source, incomplete)
        assert result.channels == () and result.problems[0].reason == "unverified"


@pytest.mark.parametrize(
    "change",
    [
        "service",
        "source-version",
        "source-binding",
        "target-binding",
        "server-default",
        "role-map",
        "duplicate-map",
        "duplicate-role",
        "rank",
        "rank-tie",
        "role-permissions",
        "channel-default",
        "channel-roles",
        "duplicate-channel-role",
        "contradictory-plan",
    ],
)
def test_compiler_refuses_unknown_or_ambiguous_context(change: str) -> None:
    p = compiler_functions()
    source = source_evidence(((1 << 10) | (1 << 11), 0, 0), ())
    layout = compiler_layout(source)
    if change == "source-version":
        source = replace(source, schema_version=0)
    elif change == "source-binding":
        source = replace(source, fingerprint="")
    elif change in {"service", "target-binding", "server-default"}:
        field, value = {
            "service": ("service", ""),
            "target-binding": ("fingerprint", ""),
            "server-default": ("server_default", None),
        }[change]
        layout = replace(layout, **{field: value})
    elif change == "role-map":
        layout = replace(layout, planned_roles=layout.planned_roles[:1])
    elif change == "duplicate-map":
        layout = replace(layout, planned_roles=layout.planned_roles + layout.planned_roles[:1])
    elif change == "duplicate-role":
        layout = replace(layout, roles=layout.roles + layout.roles[:1])
    elif change in {"rank", "rank-tie", "role-permissions"}:
        field, value = {
            "rank": ("rank", None),
            "rank-tie": ("rank", 1),
            "role-permissions": ("permissions", None),
        }[change]
        layout = replace(
            layout, roles=(replace(layout.roles[0], **{field: value}), layout.roles[1])
        )
    elif change == "contradictory-plan":
        layout = replace(layout, planned_server_default=0)
    else:
        field, value = {
            "channel-default": ("default", None),
            "channel-roles": ("roles", None),
            "duplicate-channel-role": (
                "roles",
                (("r-112", p.PermissionCell(0, 0)), ("r-112", p.PermissionCell(0, 0))),
            ),
        }[change]
        layout = replace(layout, channels=(replace(layout.channels[0], **{field: value}),))
    result = p.compile_permission_plan(source, layout)
    assert result.channels == () and result.problems[0].reason == "unverified"


@pytest.mark.parametrize("recreate_role", [False, True])
def test_compiler_preserves_deleted_role_cells_with_complete_bound_inventory(
    recreate_role: bool,
) -> None:
    p = compiler_functions()
    source = source_evidence((1 << 10, 0), ())
    layout = compiler_layout(source)
    if recreate_role:
        proposed = compiler_layout(source, new_server=True)
        layout = replace(
            layout,
            roles=(),
            planned_roles=proposed.planned_roles,
            planned_role_facts=proposed.planned_role_facts,
        )
    control = p.compile_permission_plan(source, layout)
    assert control.problems == ()
    stale = p.PermissionCell((1 << 22) | (1 << 100), 1 << 20)
    original = replace(
        layout,
        channels=(replace(layout.channels[0], roles=(("deleted-role", stale),)),),
    )
    result = p.compile_permission_plan(source, original)
    assert result.problems == ()
    assert result.channels[0].default == control.channels[0].default
    assert result.channels[0].roles == control.channels[0].roles
    assert result.channels[0].affected_mask == control.channels[0].affected_mask
    assert result.channels[0].preserved_mask & (1 << 100)
    assert result.channels[0].target_layout == original
    assert result.calculation_fingerprint != control.calculation_fingerprint
    assert result.target_layout == original
    assert original.channels[0].roles == (("deleted-role", stale),)

    # Independent backend lookup: membership alone does not activate an absent role.
    owned_cell = dict(result.channels[0].roles)["112"]
    for assigned in (frozenset(), frozenset({"112"})):
        baseline = compiled_target_value(result.channels[0], layout, assigned)
        for stale_member in (False, True):
            members = {"r-112"} if assigned else set()
            if stale_member:
                members.add("deleted-role")
            actual = {"r-112": owned_cell}
            value = layout.server_default
            default = result.channels[0].default
            value = (value | default.allow) & ~default.deny
            for rid, cell in (("r-112", owned_cell), ("deleted-role", stale)):
                if rid in members and rid in actual:
                    value = (value | cell.allow) & ~cell.deny
            value = value if value & (1 << 20) else 0
            assert value == baseline
            assert bool(value & (1 << 20)) == source_oracle(source, assigned, 10)

    for incomplete in (
        replace(original, fingerprint=""),
        replace(original, service=""),
        replace(original, server_default=None),
        replace(original, planned_role_facts=()) if recreate_role else replace(original, roles=()),
    ):
        refused = p.compile_permission_plan(source, incomplete)
        assert refused.channels == () and refused.problems[0].reason == "unverified"


def test_compiler_preserves_inert_native_cells_and_refuses_material_native_effects() -> None:
    p = compiler_functions()
    source = source_evidence((1 << 10,), ())
    layout = compiler_layout(source)
    native = p.TargetRole("native", p.PermissionCell(1 << 100, 0), 0)
    layout = replace(
        layout,
        roles=(native,),
        channels=(replace(layout.channels[0], roles=(("native", p.PermissionCell(1 << 100, 0)),)),),
    )
    result = p.compile_permission_plan(source, layout)
    assert result.problems == () and result.channels[0].roles == ()
    assert result.target_layout.channels == layout.channels
    for permissions in (p.PermissionCell(1 << 20, 0), p.PermissionCell(0, 1 << 20)):
        bad = replace(layout, roles=(replace(native, permissions=permissions),))
        refused = p.compile_permission_plan(source, bad)
        assert refused.channels == () and refused.problems[0].reason == "unsupported"
        active_cell = replace(
            layout,
            channels=(replace(layout.channels[0], roles=(("native", permissions),)),),
        )
        refused = p.compile_permission_plan(source, active_cell)
        assert refused.channels == () and refused.problems[0].reason == "unsupported"


def test_compiler_does_not_assume_grant_only_fallback_for_server_denies() -> None:
    p = compiler_functions()
    source = source_evidence((1 << 10, 0), ())
    layout = compiler_layout(source)
    layout = replace(
        layout, roles=(replace(layout.roles[0], permissions=p.PermissionCell(0, 1 << 20)),)
    )
    result = p.compile_permission_plan(source, layout)
    assert result.channels == () and result.problems[0].reason == "unsupported"


def test_compiler_detects_incompatible_rank_without_changing_roles() -> None:
    p = compiler_functions()
    source = source_evidence((0, 1 << 10, 8), ((0, 1 << 10),))
    layout = compiler_layout(source)
    # Admin is initially below the ordinary role: no exact overwrite order.
    original = replace(layout)
    refused = p.compile_permission_plan(source, layout)
    assert refused.channels == () and refused.problems[0].reason == "unsupported"
    assert layout == original
    good = replace(
        layout, roles=(replace(layout.roles[0], rank=1), replace(layout.roles[1], rank=0))
    )
    compiled = p.compile_permission_plan(source, good)
    assert compiled.problems == ()
    for assigned in (
        frozenset(),
        frozenset({"112"}),
        frozenset({"113"}),
        frozenset({"112", "113"}),
    ):
        value = compiled_target_value(compiled.channels[0], good, assigned)
        assert bool(value & (1 << 20)) == source_oracle(source, assigned, 10)


def test_compiler_is_deterministic_immutable_and_creates_no_readiness() -> None:
    p = compiler_functions()
    source = source_evidence((1 << 10, 0), ())
    layout = compiler_layout(source, new_server=True)
    first = p.compile_permission_plan(source, layout)
    assert first.problems == () and first == p.compile_supported_bits(source, layout)
    assert first == p.compile_permission_plan(source, layout)
    assert first.source_fingerprint == source.fingerprint and first.calculation_fingerprint
    assert p.PermissionRuntime(plan=first).ready == {}
    with pytest.raises(FrozenInstanceError):
        first.calculation_fingerprint = "ready"


def test_compiler_can_choose_a_data_default_when_everyone_cannot_view_channel() -> None:
    p = compiler_functions()
    view, send = 1 << 10, 1 << 11
    source = source_evidence((send, 0, send), ((0, 0), (view, send), (send, 0)))
    layout = compiler_layout(source)
    compiled = p.compile_permission_plan(source, layout)
    assert compiled.problems == ()
    for assigned in (
        frozenset(),
        frozenset({"112"}),
        frozenset({"113"}),
        frozenset({"112", "113"}),
    ):
        value = compiled_target_value(compiled.channels[0], layout, assigned)
        visible = source_oracle(source, assigned, 10)
        assert bool(value & (1 << 20)) == visible
        assert bool(value & (1 << 22)) == (visible and source_oracle(source, assigned, 11))


def test_compiler_exhaustive_two_role_visibility_and_send_match_all_target_encodings() -> None:
    p = compiler_functions()
    view, send = 1 << 10, 1 << 11
    tables = {
        target_truth(ops, 3, base)
        for ops in product((None, False, True), repeat=2)
        for base in (False, True)
    }
    possible = {
        tuple((v, v and s) for v, s in zip(vt, st, strict=True)) for vt in tables for st in tables
    }
    cells = tuple(
        (va | sa, vd | sd)
        for (va, vd), (sa, sd) in product(
            ((0, 0), (view, 0), (0, view)), ((0, 0), (send, 0), (0, send))
        )
    )
    assigned = (frozenset(), frozenset({"113"}), frozenset({"112"}), frozenset({"112", "113"}))
    for overwrites in product(cells, repeat=3):
        source = source_evidence((view | send,) * 3, overwrites)
        layout = compiler_layout(source)
        wanted = tuple(
            (
                source_oracle(source, members, 10),
                source_oracle(source, members, 10) and source_oracle(source, members, 11),
            )
            for members in assigned
        )
        compiled = p.compile_permission_plan(source, layout)
        assert (not compiled.problems) == (wanted in possible), (
            overwrites,
            wanted,
            compiled.problems,
        )
        if not compiled.problems:
            for members, expected in zip(assigned, wanted, strict=True):
                value = compiled_target_value(compiled.channels[0], layout, members)
                assert (bool(value & (1 << 20)), bool(value & (1 << 22))) == expected


def test_compiler_uses_the_entire_existing_mapping_inventory() -> None:
    p = compiler_functions()
    from discord_ferry.discord.permissions import DISCORD_TO_STOAT, translate_permissions

    affected = sum(
        1 << bit
        for bit in {
            target
            for value in DISCORD_TO_STOAT.values()
            for target in (value if isinstance(value, list) else [value])
        }
    )
    for bit in DISCORD_TO_STOAT:
        grants = (1 << 10) | (1 << 11) | (1 << bit)
        source = source_evidence((grants,), ())
        layout = compiler_layout(source)
        compiled = p.compile_permission_plan(source, layout)
        assert compiled.problems == (), bit
        assert compiled.channels[0].affected_mask == affected
        assert compiled_target_value(
            compiled.channels[0], layout, frozenset()
        ) & affected == translate_permissions(grants)


def test_compiler_checks_managed_effects_after_visibility_and_implicit_dependencies() -> None:
    p = compiler_functions()
    view, send = 1 << 10, 1 << 11
    for everyone, managed_grant, supported in (
        (0, send, True),
        (view, send, False),
        (view | send, send, True),
        (view, 1 << 100, True),
    ):
        source = source_evidence((everyone, managed_grant), (), managed=frozenset({1}))
        compiled = p.compile_permission_plan(source, compiler_layout(source))
        assert (not compiled.problems) is supported
        if not supported:
            assert compiled.channels == () and compiled.problems[0].reason == "unsupported"


@pytest.mark.parametrize("channel_type", [2, 4, 13])
def test_compiler_refuses_unmodelled_category_and_live_voice_scopes(channel_type: int) -> None:
    p = compiler_functions()
    source = source_evidence((1 << 10,), ())
    source = replace(source, channels=(replace(source.channels[0], type=channel_type),))
    compiled = p.compile_permission_plan(source, compiler_layout(source))
    assert compiled.channels == () and compiled.problems[0].reason == "unsupported"


def test_compiler_preserves_owned_unmapped_cells_and_observed_inputs() -> None:
    p = compiler_functions()
    source = source_evidence((1 << 10, 0), ())
    layout = compiler_layout(source)
    preserved = p.PermissionCell(1 << 100, 1 << 99)
    layout = replace(layout, channels=(replace(layout.channels[0], roles=(("r-112", preserved),)),))
    before = replace(layout)
    compiled = p.compile_permission_plan(source, layout)
    assert compiled.problems == () and layout == before
    cell = dict(compiled.channels[0].roles)["112"]
    assert cell.allow & preserved.allow == preserved.allow
    assert cell.deny & preserved.deny == preserved.deny
    assert (
        compiled.channels[0].preserved_mask & (preserved.allow | preserved.deny)
        == preserved.allow | preserved.deny
    )


def test_visibility_and_dependency_queries_match_independent_small_truth_tables() -> None:
    p = compiler_functions()
    predicates = [
        p.BitPredicate(allow, deny, grant, base)
        for allow, deny, grant, base in product(range(4), range(4), range(4), (False, True))
    ]
    constraints = []
    for states in product((0, 1, 2), repeat=2):
        zeros = sum(1 << i for i, state in enumerate(states) if state == 1)
        ones = sum(1 << i for i, state in enumerate(states) if state == 2)
        constraints.append((zeros, ones))
    for i, view in enumerate(predicates):
        for j, data in enumerate(predicates):
            send = predicates[(i + 3 * j) % len(predicates)]
            for zeros, ones in constraints:
                values = {
                    p.evaluate_source_bit(data, members) and p.evaluate_source_bit(send, members)
                    for members in range(4)
                    if not members & zeros
                    and members & ones == ones
                    and p.evaluate_source_bit(view, members)
                }
                expected = next(iter(values)) if len(values) == 1 else None if values else False
                assert (
                    p._visible_bit_query(view, (data, send), 3, 0, False, zeros, ones) is expected
                )


@pytest.mark.parametrize("rank", [-1, True, None, 0.5])
def test_planned_role_rejects_invalid_rank_with_valid_permission_facts(rank: object) -> None:
    p = planned_contracts()
    with pytest.raises(ValueError):
        p.PlannedRole("112", p.PermissionCell(0, 0), rank)


def runtime_case(*, grants=((1 << 10) | (1 << 11), (1 << 10) | (1 << 11))):
    p = compiler_functions()
    source = source_evidence(grants, ((0, 1 << 11),))
    layout = compiler_layout(source)
    plan = p.compile_permission_plan(source, layout)
    assert not plan.problems
    channel = plan.channels[0]
    bindings = dict(layout.planned_roles)
    actual = p.PermissionReadback(
        replace(
            layout,
            planned_roles=(),
            planned_channels=(),
            fingerprint="observed-context",
            channels=(
                p.TargetChannel(
                    "c-222",
                    channel.default,
                    tuple((bindings[sid], cell) for sid, cell in channel.roles),
                ),
            ),
        )
    )
    runtime = p.PermissionRuntime(plan=plan)
    for method in (
        "bind",
        "bind_server",
        "bind_role",
        "bind_channel",
        "channel_plan",
        "verify",
        "require",
        "invalidate_channel",
        "invalidate_layout",
        "stop",
        "note_write",
        "note_write_confirmed",
        "note_write_rejected",
    ):
        assert callable(getattr(runtime, method, None)), f"Runtime boundary {method} is missing"
    return p, runtime, plan, actual


def test_permission_plan_is_not_runtime_authority() -> None:
    from discord_ferry.errors import PermissionSafetyError

    _, runtime, _, _ = runtime_case()
    assert runtime.ready == {}
    assert not runtime.outcome.verified
    with pytest.raises(PermissionSafetyError) as caught:
        runtime.require("c-222")
    assert caught.value.outcome.reason == "unverified"
    assert runtime.stopped and not runtime.ready


def test_actual_verified_readback_is_required_for_authority() -> None:
    p, runtime, _, actual = runtime_case()
    assert runtime.verify("c-222", actual) is actual
    assert runtime.require("c-222") is actual
    assert runtime.outcome.verified
    other = p.PermissionRuntime(plan=runtime.plan)
    assert not other.ready and not other.outcome.verified


def test_injected_ready_record_is_not_a_runtime_proof() -> None:
    from discord_ferry.errors import PermissionSafetyError

    _, runtime, _, actual = runtime_case()
    runtime.ready["c-222"] = actual
    with pytest.raises(PermissionSafetyError):
        runtime.require("c-222")


def test_equivalent_carried_cells_do_not_need_canonical_rewrites() -> None:
    p, runtime, plan, actual = runtime_case(grants=(0, 0))
    neutral = p.TargetChannel("c-222", p.PermissionCell(0, 0), ())
    assert neutral.default != plan.channels[0].default
    actual = replace(actual, layout=replace(actual.layout, channels=(neutral,)))
    runtime.verify("c-222", actual)
    assert runtime.require("c-222") is actual


@pytest.mark.parametrize("conditional_visibility", [False, True])
def test_runtime_ignores_mapped_bit_differences_only_for_hidden_members(
    conditional_visibility: bool,
) -> None:
    p = compiler_functions()
    grants = (0, 1 << 10, 0) if conditional_visibility else (0, 0)
    source = source_evidence(grants, ())
    layout = compiler_layout(source)
    plan = p.compile_permission_plan(source, layout)
    assert not plan.problems
    proposed = plan.channels[0]
    cells = dict(proposed.roles)
    hidden_role = "113" if conditional_visibility else "112"
    cells[hidden_role] = p.PermissionCell(
        cells[hidden_role].allow | (1 << 22), cells[hidden_role].deny & ~(1 << 22)
    )
    if conditional_visibility:
        cells["112"] = p.PermissionCell(
            cells["112"].allow & ~(1 << 22), cells["112"].deny | (1 << 22)
        )
    carried = replace(proposed, roles=tuple(cells.items()))
    ids = tuple(cells)
    for assignment in range(1 << len(ids)):
        members = frozenset(sid for index, sid in enumerate(ids) if assignment & (1 << index))
        value = compiled_target_value(carried, layout, members)
        visible = source_oracle(source, members, 10)
        assert bool(value & (1 << 20)) == visible
        assert bool(value & (1 << 22)) == (visible and source_oracle(source, members, 11))
    actual = p.PermissionReadback(
        replace(
            layout,
            planned_roles=(),
            planned_channels=(),
            channels=(
                p.TargetChannel(
                    "c-222",
                    carried.default,
                    tuple((f"r-{sid}", cell) for sid, cell in carried.roles),
                ),
            ),
        )
    )
    runtime = p.PermissionRuntime(plan)
    runtime.verify("c-222", actual)
    assert runtime.require("c-222") is actual


@pytest.mark.parametrize(
    "change", ["default", "role_cell", "grant", "rank", "inventory", "unrelated"]
)
def test_runtime_refuses_changed_permission_facts(change: str) -> None:
    from discord_ferry.errors import PermissionSafetyError

    p, runtime, _, actual = runtime_case()
    layout = actual.layout
    channel = layout.channels[0]
    if change == "default":
        layout = replace(layout, server_default=layout.server_default ^ (1 << 22))
    elif change == "role_cell":
        rid, cell = channel.roles[0]
        layout = replace(
            layout,
            channels=(
                replace(
                    channel,
                    roles=(
                        (rid, p.PermissionCell(cell.allow | (1 << 22), cell.deny & ~(1 << 22))),
                    ),
                ),
            ),
        )
    elif change in {"grant", "rank"}:
        role = layout.roles[0]
        changed = (
            replace(role, rank=role.rank + 1)
            if change == "rank"
            else replace(role, permissions=p.PermissionCell(role.permissions.allow ^ (1 << 20), 0))
        )
        layout = replace(layout, roles=(changed,))
    elif change == "inventory":
        layout = replace(
            layout, roles=(*layout.roles, p.TargetRole("native", p.PermissionCell(0, 0), 8))
        )
    else:
        layout = replace(
            layout,
            channels=(
                replace(
                    channel,
                    default=p.PermissionCell(
                        channel.default.allow | (1 << 12), channel.default.deny
                    ),
                ),
            ),
        )
    with pytest.raises(PermissionSafetyError) as caught:
        runtime.verify("c-222", replace(actual, layout=layout))
    assert caught.value.outcome.reason == "readback_mismatch"
    assert runtime.stopped and not runtime.ready


@pytest.mark.parametrize("change", ["service", "server", "channel", "missing", "problem"])
def test_runtime_never_uses_expected_values_for_missing_or_misbound_reads(change: str) -> None:
    from discord_ferry.errors import PermissionSafetyError

    p, runtime, _, actual = runtime_case()
    if change == "service":
        actual = replace(actual, layout=replace(actual.layout, service="https://other.test"))
    elif change == "server":
        actual = replace(actual, layout=replace(actual.layout, server_id="other"))
    elif change == "channel":
        actual = replace(
            actual,
            layout=replace(
                actual.layout, channels=(replace(actual.layout.channels[0], id="other"),)
            ),
        )
    elif change == "missing":
        actual = replace(actual, layout=replace(actual.layout, server_default=None))
    else:
        actual = replace(
            actual,
            problems=(p.PermissionProblem("unverified", (), None, "Refresh actual evidence"),),
        )
    with pytest.raises(PermissionSafetyError) as caught:
        runtime.verify("c-222", actual)
    assert caught.value.outcome.reason == "unverified"
    assert not runtime.ready


def test_runtime_invalidated_proof_needs_a_new_readback() -> None:
    _, runtime, _, actual = runtime_case()
    runtime.verify("c-222", actual)
    runtime.invalidate_channel("c-222")
    assert not runtime.ready and not runtime.outcome.verified
    runtime.verify("c-222", actual)
    assert runtime.require("c-222") is actual
    runtime.invalidate_layout()
    assert not runtime.ready and not runtime.outcome.verified


def test_runtime_plan_change_invalidates_existing_authority() -> None:
    from discord_ferry.errors import PermissionSafetyError

    _, runtime, plan, actual = runtime_case()
    runtime.verify("c-222", actual)
    runtime.plan = replace(plan, source_fingerprint="changed-source")
    with pytest.raises(PermissionSafetyError):
        runtime.require("c-222")
    assert not runtime.ready


def test_runtime_stop_is_terminal_and_preserves_primary_error() -> None:
    from discord_ferry.errors import PermissionSafetyError

    _, runtime, _, actual = runtime_case()
    runtime.verify("c-222", actual)
    runtime.note_write()
    first = runtime.stop("write_uncertain", stage="application")
    assert first.outcome.effects == "changes_possible"
    assert runtime.stop("unverified", stage="recheck") is first
    with pytest.raises(PermissionSafetyError) as caught:
        runtime.verify("c-222", actual)
    assert caught.value is first
    assert runtime.stopped and not runtime.ready


def test_runtime_effects_accounting_does_not_confirm_pending_writes() -> None:
    _, runtime, _, _ = runtime_case()
    runtime.note_write()
    runtime.note_write()
    assert runtime.effects == "changes_possible"
    runtime.note_write_confirmed()
    assert runtime.effects == "changes_possible"
    runtime.note_write_confirmed()
    assert runtime.effects == "changes_confirmed"
    runtime.note_write()
    assert runtime.effects == "changes_possible"
    runtime.note_write_rejected()
    assert runtime.effects == "changes_confirmed"


def test_observed_encoding_matches_the_existing_symbolic_query() -> None:
    p = synthesis_functions()
    matches = getattr(p, "_encoding_matches", None)
    assert callable(matches), "Readback needs the existing symbolic encoding verification"
    for size in range(3):
        for grants, base in product(range(1 << size), (False, True)):
            for ops in product((None, False, True), repeat=size):
                expected = target_truth(ops, grants, base)
                for table in product((False, True), repeat=1 << size):

                    def agrees(zeros, ones, wanted, *, table=table):
                        return table_query(table, zeros, ones) is wanted

                    assert matches(ops, agrees, grants, base) == (table == expected)


def test_observed_encoding_matches_independent_visibility_restricted_tables() -> None:
    p = synthesis_functions()
    for grants, base in product(range(4), (False, True)):
        for operations in product((None, False, True), repeat=2):
            actual = target_truth(operations, grants, base)
            for table in product((False, True), repeat=4):
                for visible in range(16):

                    def agrees(zeros, ones, wanted, *, table=table, visible=visible):
                        return all(
                            value is wanted
                            for assignment, value in enumerate(table)
                            if visible & (1 << assignment)
                            and not assignment & zeros
                            and assignment & ones == ones
                        )

                    expected = all(
                        not visible & (1 << assignment) or result == table[assignment]
                        for assignment, result in enumerate(actual)
                    )
                    assert p._encoding_matches(operations, agrees, grants, base) == expected


def test_runtime_binds_created_identities_without_fabricating_observations() -> None:
    from discord_ferry.errors import PermissionSafetyError

    p = compiler_functions()
    source = source_evidence((0, (1 << 10) | (1 << 11)), ())
    layout = compiler_layout(source, new_server=True)
    plan = p.compile_permission_plan(source, layout)
    assert not plan.problems
    runtime = p.PermissionRuntime(plan=plan)
    assert callable(getattr(runtime, "bind_server", None)), "Created bindings need a runtime owner"
    runtime.bind_server("created-server")
    runtime.bind_role("112", "created-role")
    runtime.bind_channel("222", "created-channel")
    channel = runtime.channel_plan("created-channel")
    assert {rid for rid, _ in channel.roles} == {"created-role"}
    assert plan.target_layout.roles == () and plan.target_layout.server_id == ""
    assert channel.target_layout is plan.target_layout
    assert not runtime.ready
    planned = layout.planned_role_facts[0]
    actual = p.PermissionReadback(
        p.TargetLayout(
            service=layout.service,
            server_id="created-server",
            roles=(p.TargetRole("created-role", planned.permissions, planned.rank),),
            server_default=layout.planned_server_default,
            channels=(p.TargetChannel("created-channel", channel.default, channel.roles),),
            fingerprint="fresh-created-context",
        )
    )
    runtime.verify("created-channel", actual)
    assert runtime.require("created-channel") is actual
    wrong = replace(
        actual, layout=replace(actual.layout, roles=(replace(actual.layout.roles[0], rank=7),))
    )
    with pytest.raises(PermissionSafetyError):
        runtime.verify("created-channel", wrong)
    assert not runtime.ready


def test_runtime_does_not_report_all_channels_verified_after_one_read() -> None:
    p = compiler_functions()
    source = source_evidence((0,), ())
    source = replace(
        source,
        channels=(*source.channels, replace(source.channels[0], id="223")),
        covered_channel_ids=frozenset({"222", "223"}),
    )
    layout = compiler_layout(source)
    plan = p.compile_permission_plan(source, layout)
    runtime = p.PermissionRuntime(plan=plan)
    assert callable(getattr(runtime, "verify", None)), "Channel readiness needs observed proof"
    observations = []
    for proposal in plan.channels:
        cid = "c-" + proposal.source_key
        actual = p.PermissionReadback(
            replace(
                layout,
                planned_roles=(),
                planned_channels=(),
                fingerprint=cid,
                channels=(p.TargetChannel(cid, proposal.default, ()),),
            )
        )
        runtime.verify(cid, actual)
        observations.append(actual)
        assert runtime.require(cid) is actual
        if len(observations) == 1:
            assert not runtime.outcome.verified
    assert runtime.outcome.verified
    runtime.invalidate_channel("c-222")
    assert not runtime.outcome.verified
    assert runtime.require("c-223") is observations[1]


@pytest.mark.parametrize("binding", ["server", "role", "channel"])
def test_runtime_rebinding_cannot_reuse_an_existing_proof(binding: str) -> None:
    from discord_ferry.errors import PermissionSafetyError

    _, runtime, _, actual = runtime_case()
    runtime.verify("c-222", actual)
    with pytest.raises(PermissionSafetyError):
        if binding == "server":
            runtime.bind_server("different-server")
        elif binding == "role":
            runtime.bind_role("112", "different-role")
        else:
            runtime.bind_channel("222", "different-channel")
    assert runtime.stopped and not runtime.ready


@pytest.mark.parametrize("cell", [(0, 0), (1 << 20, 1 << 12)])
def test_runtime_preserves_inactive_cells_in_observed_context(cell) -> None:
    from discord_ferry.errors import PermissionSafetyError

    p, _, _, old_actual = runtime_case()
    source = source_evidence(((1 << 10) | (1 << 11), (1 << 10) | (1 << 11)), ((0, 1 << 11),))
    layout = compiler_layout(source)
    inactive = ("deleted-role", p.PermissionCell(*cell))
    layout = replace(layout, channels=(replace(layout.channels[0], roles=(inactive,)),))
    plan = p.compile_permission_plan(source, layout)
    assert not plan.problems
    runtime = p.PermissionRuntime(plan=plan)
    channel = old_actual.layout.channels[0]
    actual = replace(
        old_actual,
        layout=replace(
            old_actual.layout, channels=(replace(channel, roles=(*channel.roles, inactive)),)
        ),
    )
    runtime.verify("c-222", actual)
    assert runtime.require("c-222") is actual
    with pytest.raises(PermissionSafetyError):
        runtime.verify("c-222", old_actual)
    assert not runtime.ready


def test_readback_encoding_query_remains_linear_for_large_role_sets() -> None:
    p = synthesis_functions()
    matches = getattr(p, "_encoding_matches", None)
    assert callable(matches)
    size, calls = 100, []
    grants = (1 << size) - 1
    predicate = p.BitPredicate(0, 0, grants, False)

    def query(zeros, ones, wanted):
        calls.append((zeros, ones))
        return p.constant_result(predicate, grants, zeros, ones) is wanted

    assert matches((None,) * size, query, grants, False)
    assert len(calls) <= size + 1


@pytest.mark.parametrize(
    "change", ["missing-cells", "duplicate-cells", "native-grant", "role-deny"]
)
def test_runtime_does_not_trust_unsupported_or_incomplete_plan_assumptions(change) -> None:
    from discord_ferry.errors import PermissionSafetyError

    p, _, plan, observed = runtime_case()
    layout = plan.target_layout
    if change == "missing-cells":
        layout = replace(layout, channels=(replace(layout.channels[0], roles=None),))
    elif change == "duplicate-cells":
        pair = ("r-112", p.PermissionCell(0, 0))
        layout = replace(layout, channels=(replace(layout.channels[0], roles=(pair, pair)),))
    elif change == "native-grant":
        native = p.TargetRole("native-role", p.PermissionCell(1 << 20, 0), 500)
        layout = replace(layout, roles=(*layout.roles, native))
        observed = replace(observed, layout=replace(observed.layout, roles=layout.roles))
    else:
        role = layout.roles[0]
        role = replace(role, permissions=p.PermissionCell(role.permissions.allow, 1 << 20))
        layout = replace(layout, roles=(role,))
        observed = replace(observed, layout=replace(observed.layout, roles=layout.roles))
    plan = replace(
        plan,
        target_layout=layout,
        channels=tuple(replace(channel, target_layout=layout) for channel in plan.channels),
    )
    runtime = p.PermissionRuntime(plan=plan)
    with pytest.raises(PermissionSafetyError):
        runtime.verify("c-222", observed)
    assert runtime.stopped and not runtime.ready
