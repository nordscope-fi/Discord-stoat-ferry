from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MODULE = REPO / "scripts/agent-compat/critique-budget.mjs"
NODE = shutil.which("node")
DESIGN_PATH = "docs/plans/designs/example.md"


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    design = root / DESIGN_PATH
    design.parent.mkdir(parents=True)
    design.write_text("# Design\n\nFirst version.\n")
    return root


def _run_budget(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    assert NODE is not None
    return subprocess.run(
        [NODE, str(MODULE), *args, "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def _claim(root: Path, design_path: str = DESIGN_PATH) -> subprocess.CompletedProcess[str]:
    return _run_budget(root, "claim", "--design", design_path)


def _output(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert result.stdout, result.stderr
    value = json.loads(result.stdout)
    assert isinstance(value, dict)
    return value


def _record_path(root: Path) -> Path:
    key = hashlib.sha256(DESIGN_PATH.encode()).hexdigest()
    return root / "docs/plans/.review/critique-budget" / f"{key}.json"


def _complete(
    root: Path,
    claim: dict[str, object],
    outcome: str,
    *,
    unresolved: tuple[str, ...] = (),
    evidence: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    args = [
        "complete",
        "--design",
        DESIGN_PATH,
        "--cycle",
        str(claim["cycle_id"]),
        "--round",
        str(claim["round"]),
        "--outcome",
        outcome,
    ]
    if unresolved:
        args.extend(["--unresolved", ",".join(unresolved)])
    if evidence is not None:
        args.extend(["--evidence", str(evidence.relative_to(root))])
    return _run_budget(root, *args)


def _decide(
    root: Path,
    claim: dict[str, object],
    decision: str,
) -> subprocess.CompletedProcess[str]:
    return _run_budget(
        root,
        "decide",
        "--design",
        DESIGN_PATH,
        "--cycle",
        str(claim["cycle_id"]),
        "--design-sha",
        str(claim["design_sha256"]),
        "--decision",
        decision,
    )


def _round_three(
    root: Path,
    unresolved: tuple[str, ...] = ("finding-1",),
) -> dict[str, object]:
    first = _output(_claim(root))
    first_complete = _complete(root, first, "iterate", unresolved=unresolved)
    assert first_complete.returncode == 0, first_complete.stderr
    second = _output(_claim(root))
    second_complete = _complete(root, second, "iterate", unresolved=unresolved)
    assert second_complete.returncode == 0, second_complete.stderr
    third = _claim(root)
    assert third.returncode == 0, third.stderr
    return _output(third)


def _write_evidence(
    root: Path,
    claim: dict[str, object],
    entries: list[dict[str, object]],
    *,
    name: str = "evidence.json",
) -> Path:
    path = root / name
    path.write_text(
        json.dumps(
            {
                "cycle_id": claim["cycle_id"],
                "round": 3,
                "design_sha256": claim["design_sha256"],
                "entries": entries,
            }
        )
    )
    return path


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_claim_records_round_before_reviewer_dispatch(tmp_path: Path) -> None:
    root = _root(tmp_path)

    result = _claim(root)

    assert result.returncode == 0, result.stderr
    response = _output(result)
    record = json.loads(_record_path(root).read_text())
    attempt = record["cycles"][-1]["attempts"][-1]
    assert response["round"] == 1
    assert response["mode"] == "critique"
    assert attempt["round"] == 1
    assert attempt["status"] == "started"
    assert attempt["design_sha256"] == response["design_sha256"]


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_claim_preserves_attempts_across_processes(tmp_path: Path) -> None:
    root = _root(tmp_path)
    first = _claim(root)
    assert first.returncode == 0, first.stderr

    second = _claim(root)

    assert second.returncode == 0, second.stderr
    assert _output(second)["round"] == 2
    assert len(json.loads(_record_path(root).read_text())["cycles"][-1]["attempts"]) == 2


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_started_attempt_counts_when_completion_never_arrives(tmp_path: Path) -> None:
    root = _root(tmp_path)

    rounds = []
    for _ in range(3):
        result = _claim(root)
        assert result.returncode == 0, result.stderr
        rounds.append(_output(result)["round"])

    assert rounds == [1, 2, 3]


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_design_edit_does_not_reset_the_cycle(tmp_path: Path) -> None:
    root = _root(tmp_path)
    first = _claim(root)
    assert first.returncode == 0, first.stderr
    first_output = _output(first)
    (root / DESIGN_PATH).write_text("# Design\n\nSecond version.\n")

    second = _claim(root)

    assert second.returncode == 0, second.stderr
    second_output = _output(second)
    assert second_output["cycle_id"] == first_output["cycle_id"]
    assert second_output["round"] == 2
    assert second_output["design_sha256"] != first_output["design_sha256"]


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_fourth_claim_is_rejected_before_reviewer_dispatch(tmp_path: Path) -> None:
    root = _root(tmp_path)
    for _ in range(3):
        result = _claim(root)
        assert result.returncode == 0, result.stderr

    fourth = _claim(root)

    assert fourth.returncode != 0
    response = _output(fourth)
    assert response["action"] == "owner-decision-required"
    assert response["attempts"] == 3
    assert response["decisions"] == ["accept", "return-to-design", "restart"]
    assert len(json.loads(_record_path(root).read_text())["cycles"][-1]["attempts"]) == 3


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_claim_uses_private_state_permissions(tmp_path: Path) -> None:
    root = _root(tmp_path)

    result = _claim(root)

    assert result.returncode == 0, result.stderr
    record = _record_path(root)
    assert stat.S_IMODE(record.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(record.stat().st_mode) == 0o600


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_live_lock_returns_busy_without_consuming_a_round(tmp_path: Path) -> None:
    root = _root(tmp_path)
    record = _record_path(root)
    record.parent.mkdir(parents=True, mode=0o700)
    lock = record.with_suffix(".lock")
    lock.write_text(json.dumps({"pid": os.getpid(), "token": "live-owner"}))
    lock.chmod(0o600)

    result = _claim(root)

    assert result.returncode != 0
    assert _output(result)["action"] == "busy"
    assert not record.exists()
    assert lock.exists()


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_abandoned_lock_is_recovered_once(tmp_path: Path) -> None:
    root = _root(tmp_path)
    record = _record_path(root)
    record.parent.mkdir(parents=True, mode=0o700)
    lock = record.with_suffix(".lock")
    lock.write_text(json.dumps({"pid": 99_999_999, "token": "abandoned-owner"}))
    lock.chmod(0o600)

    result = _claim(root)

    assert result.returncode == 0, result.stderr
    assert _output(result)["round"] == 1
    assert not lock.exists()


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_claim_rejects_absolute_and_outside_design_paths(tmp_path: Path) -> None:
    root = _root(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n")
    linked = root / "docs/plans/designs/linked.md"
    linked.symlink_to(outside)

    absolute = _claim(root, str(root / DESIGN_PATH))
    escaped = _claim(root, "docs/plans/designs/linked.md")

    assert absolute.returncode != 0
    assert _output(absolute)["action"] == "invalid-design"
    assert escaped.returncode != 0
    assert _output(escaped)["action"] == "invalid-design"


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_claim_rejects_a_linked_state_directory(tmp_path: Path) -> None:
    root = _root(tmp_path)
    outside = tmp_path / "outside-state"
    outside.mkdir()
    (root / "docs/plans/.review").symlink_to(outside, target_is_directory=True)

    result = _claim(root)

    assert result.returncode != 0
    assert _output(result)["action"] == "repair-state"
    assert not (outside / "critique-budget").exists()


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_damaged_state_blocks_a_new_attempt(tmp_path: Path) -> None:
    root = _root(tmp_path)
    record = _record_path(root)
    record.parent.mkdir(parents=True, mode=0o700)
    record.write_text("not json\n")
    record.chmod(0o600)

    result = _claim(root)

    assert result.returncode != 0
    assert _output(result)["action"] == "repair-state"
    assert record.read_text() == "not json\n"


@pytest.mark.skipif(NODE is None, reason="node is required")
@pytest.mark.parametrize(
    ("outcome", "status", "cycle_state", "next_step"),
    [
        ("pass", "completed", "closed", "test-scenarios"),
        ("iterate", "completed", "active", "critique"),
        ("rethink", "completed", "closed", "brainstorm"),
        ("failed", "failed", "active", "critique"),
        ("timed_out", "timed_out", "active", "critique"),
    ],
)
def test_complete_records_each_round_outcome(
    tmp_path: Path,
    outcome: str,
    status: str,
    cycle_state: str,
    next_step: str,
) -> None:
    root = _root(tmp_path)
    claim = _output(_claim(root))

    result = _complete(
        root,
        claim,
        outcome,
        unresolved=("finding-1",) if outcome == "iterate" else (),
    )

    assert result.returncode == 0, result.stderr
    assert _output(result)["next"] == next_step
    cycle = json.loads(_record_path(root).read_text())["cycles"][-1]
    assert cycle["state"] == cycle_state
    assert cycle["attempts"][-1]["status"] == status


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_round_three_requires_every_unresolved_finding_once(tmp_path: Path) -> None:
    root = _root(tmp_path)
    claim = _round_three(root, ("finding-1", "finding-2"))
    source = root / "docs/reference.md"
    source.write_text("current evidence\n")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    evidence = _write_evidence(
        root,
        claim,
        [
            {
                "finding_id": "finding-1",
                "source_kind": "repo",
                "locator": "docs/reference.md",
                "source_sha256": digest,
                "result": "confirmed",
            },
            {
                "finding_id": "finding-2",
                "source_kind": "repo",
                "locator": "docs/reference.md",
                "source_sha256": digest,
                "result": "contradicted",
            },
        ],
    )

    result = _complete(root, claim, "evidence", evidence=evidence)

    assert result.returncode == 0, result.stderr
    response = _output(result)
    assert response["action"] == "owner-decision-required"
    assert response["settled_ids"] == ["finding-1", "finding-2"]
    assert response["unresolved_ids"] == []


@pytest.mark.skipif(NODE is None, reason="node is required")
@pytest.mark.parametrize("entry_mode", ["missing", "duplicate"])
def test_round_three_rejects_inexact_finding_coverage(
    tmp_path: Path,
    entry_mode: str,
) -> None:
    root = _root(tmp_path)
    claim = _round_three(root, ("finding-1", "finding-2"))
    source = root / "docs/reference.md"
    source.write_text("current evidence\n")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    ids = ["finding-1"] if entry_mode == "missing" else ["finding-1", "finding-1"]
    evidence = _write_evidence(
        root,
        claim,
        [
            {
                "finding_id": finding_id,
                "source_kind": "repo",
                "locator": "docs/reference.md",
                "source_sha256": digest,
                "result": "confirmed",
            }
            for finding_id in ids
        ],
    )

    result = _complete(root, claim, "evidence", evidence=evidence)

    assert result.returncode != 0
    assert _output(result)["action"] == "owner-decision-required"
    cycle = json.loads(_record_path(root).read_text())["cycles"][-1]
    assert cycle["state"] == "owner_review"
    assert cycle["attempts"][-1]["status"] == "failed"


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_round_three_accepts_all_current_source_kinds(tmp_path: Path) -> None:
    root = _root(tmp_path)
    claim = _round_three(root, ("repo", "docs", "standard"))
    source = root / "docs/reference.md"
    source.write_text("current evidence\n")
    evidence = _write_evidence(
        root,
        claim,
        [
            {
                "finding_id": "repo",
                "source_kind": "repo",
                "locator": "docs/reference.md",
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "result": "confirmed",
            },
            {
                "finding_id": "docs",
                "source_kind": "official-docs",
                "locator": "https://example.invalid/current-docs",
                "retrieved_on": datetime.now(UTC).date().isoformat(),
                "result": "contradicted",
            },
            {
                "finding_id": "standard",
                "source_kind": "immutable",
                "locator": "https://example.invalid/spec",
                "version": "RFC-0001",
                "result": "confirmed",
            },
        ],
    )

    result = _complete(root, claim, "evidence", evidence=evidence)

    assert result.returncode == 0, result.stderr
    assert _output(result)["settled_ids"] == ["docs", "repo", "standard"]


@pytest.mark.skipif(NODE is None, reason="node is required")
@pytest.mark.parametrize("failure", ["stale-repo", "missing-date", "stale-date", "training"])
def test_round_three_rejects_stale_incomplete_or_training_evidence(
    tmp_path: Path,
    failure: str,
) -> None:
    root = _root(tmp_path)
    claim = _round_three(root)
    source = root / "docs/reference.md"
    source.write_text("current evidence\n")
    entries: dict[str, object] = {
        "finding_id": "finding-1",
        "source_kind": "repo",
        "locator": "docs/reference.md",
        "source_sha256": "0" * 64,
        "result": "confirmed",
    }
    if failure == "missing-date":
        entries = {
            "finding_id": "finding-1",
            "source_kind": "official-docs",
            "locator": "https://example.invalid/current-docs",
            "result": "confirmed",
        }
    elif failure == "stale-date":
        entries = {
            "finding_id": "finding-1",
            "source_kind": "official-docs",
            "locator": "https://example.invalid/current-docs",
            "retrieved_on": "2000-01-01",
            "result": "confirmed",
        }
    elif failure == "training":
        entries = {
            "finding_id": "finding-1",
            "source_kind": "training-memory",
            "locator": "model memory",
            "result": "confirmed",
        }
    evidence = _write_evidence(root, claim, [entries])

    result = _complete(root, claim, "evidence", evidence=evidence)

    assert result.returncode != 0
    response = _output(result)
    assert response["action"] == "owner-decision-required"
    assert response["attempts"] == 3


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_not_found_evidence_remains_unresolved_for_owner(tmp_path: Path) -> None:
    root = _root(tmp_path)
    claim = _round_three(root)
    source = root / "docs/reference.md"
    source.write_text("no answer here\n")
    evidence = _write_evidence(
        root,
        claim,
        [
            {
                "finding_id": "finding-1",
                "source_kind": "repo",
                "locator": "docs/reference.md",
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "result": "not_found",
            }
        ],
    )

    result = _complete(root, claim, "evidence", evidence=evidence)

    assert result.returncode == 0, result.stderr
    response = _output(result)
    assert response["action"] == "owner-decision-required"
    assert response["unresolved_ids"] == ["finding-1"]
    assert response["settled_ids"] == []


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_round_three_stops_and_explicit_restart_opens_round_one(tmp_path: Path) -> None:
    root = _root(tmp_path)
    claim = _round_three(root)
    source = root / "docs/reference.md"
    source.write_text("answer\n")
    evidence = _write_evidence(
        root,
        claim,
        [
            {
                "finding_id": "finding-1",
                "source_kind": "repo",
                "locator": "docs/reference.md",
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "result": "confirmed",
            }
        ],
    )
    completed = _complete(root, claim, "evidence", evidence=evidence)
    assert completed.returncode == 0, completed.stderr
    fourth = _claim(root)
    assert fourth.returncode != 0

    restarted = _decide(root, claim, "restart")
    assert restarted.returncode == 0, restarted.stderr
    next_claim = _claim(root)

    assert next_claim.returncode == 0, next_claim.stderr
    next_output = _output(next_claim)
    assert next_output["round"] == 1
    assert next_output["cycle_id"] != claim["cycle_id"]
    cycles = json.loads(_record_path(root).read_text())["cycles"]
    assert cycles[0]["owner_decision"]["decision"] == "restart"
    assert cycles[1]["opened_by"] == "owner_restart"


@pytest.mark.skipif(NODE is None, reason="node is required")
@pytest.mark.parametrize(
    ("decision", "next_step"),
    [("accept", "test-scenarios"), ("return-to-design", "brainstorm")],
)
def test_owner_closes_cycle_with_bound_decision(
    tmp_path: Path,
    decision: str,
    next_step: str,
) -> None:
    root = _root(tmp_path)
    claim = _round_three(root)
    failed = _complete(root, claim, "failed")
    assert failed.returncode == 0, failed.stderr

    result = _decide(root, claim, decision)

    assert result.returncode == 0, result.stderr
    assert _output(result)["next"] == next_step
    cycle = json.loads(_record_path(root).read_text())["cycles"][-1]
    assert cycle["state"] == "closed"
    assert cycle["owner_decision"]["design_sha256"] == claim["design_sha256"]


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_owner_decision_rejects_a_changed_design(tmp_path: Path) -> None:
    root = _root(tmp_path)
    claim = _round_three(root)
    failed = _complete(root, claim, "failed")
    assert failed.returncode == 0, failed.stderr
    (root / DESIGN_PATH).write_text("# Design\n\nChanged after evidence.\n")

    result = _decide(root, claim, "accept")

    assert result.returncode != 0
    assert _output(result)["action"] == "stale-owner-decision"
    cycle = json.loads(_record_path(root).read_text())["cycles"][-1]
    assert cycle["state"] == "owner_review"
    assert cycle["owner_decision"] is None


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_rethink_requires_owner_restart_before_another_critique(tmp_path: Path) -> None:
    root = _root(tmp_path)
    claim = _output(_claim(root))
    rethink = _complete(root, claim, "rethink")
    assert rethink.returncode == 0, rethink.stderr
    (root / DESIGN_PATH).write_text("# Design\n\nReworked after RETHINK.\n")
    blocked = _claim(root)
    assert blocked.returncode != 0
    blocked_output = _output(blocked)
    assert "design_sha256" in blocked_output

    restart = _run_budget(
        root,
        "decide",
        "--design",
        DESIGN_PATH,
        "--cycle",
        str(claim["cycle_id"]),
        "--design-sha",
        str(blocked_output["design_sha256"]),
        "--decision",
        "restart",
    )
    assert restart.returncode == 0, restart.stderr
    next_claim = _claim(root)

    assert next_claim.returncode == 0, next_claim.stderr
    assert _output(next_claim)["round"] == 1


@pytest.mark.skipif(NODE is None, reason="node is required")
def test_completion_rejects_wrong_cycle_round_and_changed_design(tmp_path: Path) -> None:
    root = _root(tmp_path)
    claim = _output(_claim(root))
    wrong_cycle = {**claim, "cycle_id": "wrong-cycle"}
    wrong_round = {**claim, "round": 2}

    cycle_result = _complete(root, wrong_cycle, "iterate")
    round_result = _complete(root, wrong_round, "iterate")
    (root / DESIGN_PATH).write_text("# Design\n\nChanged during review.\n")
    stale_result = _complete(root, claim, "iterate")

    assert cycle_result.returncode != 0
    assert _output(cycle_result)["action"] == "stale-completion"
    assert round_result.returncode != 0
    assert _output(round_result)["action"] == "stale-completion"
    assert stale_result.returncode != 0
    assert _output(stale_result)["action"] == "stale-completion"
