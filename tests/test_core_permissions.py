"""Contracts for permission facts, proposed values and operation-local readiness."""

from dataclasses import FrozenInstanceError
from importlib import import_module
from importlib.util import find_spec
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
