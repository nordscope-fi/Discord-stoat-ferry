from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="Git mode fixtures run in the Linux CI jobs"
)

REPO = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


def run_fixture(tmp_path: Path, case: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    assert NODE is not None
    return subprocess.run(
        [
            NODE,
            "tests/fixtures/chunk_snapshot_runner.mjs",
            case,
            str(tmp_path),
            *arguments,
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )


def test_capture_preserves_bytes_modes_new_files_and_deletions(tmp_path: Path) -> None:
    result = run_fixture(tmp_path, "capture-content")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["round_trip"] is True
    assert report["modes"] == ["100644", "100755"]
    assert report["new_file"] == {"before": False, "after": True}
    assert report["deleted_file"] == {"before": True, "after": False}
    assert report["binary_sha256"] == report["saved_binary_sha256"]
    assert report["starting_tree"] == report["expected_starting_tree"]
    assert report["ending_tree"] == report["expected_ending_tree"]


def test_capture_rejects_unreadable_content_without_success_record(tmp_path: Path) -> None:
    result = run_fixture(tmp_path, "capture-unreadable")
    if result.returncode == 0 and json.loads(result.stdout).get("permission_guard_unsupported"):
        pytest.skip("the current privileged host can read mode-000 files")
    assert result.returncode == 1
    assert "chunk 1" in result.stderr
    assert "unreadable.bin" in result.stderr
    assert not (tmp_path / "repo/docs/plans/.review/chunk-snapshots/tasks/1002/patch.json").exists()


def test_capture_rejects_dangling_candidate_link_before_read(tmp_path: Path) -> None:
    result = run_fixture(tmp_path, "capture-dangling-link")
    assert result.returncode == 1
    assert "rejected before read: dangling.txt" in result.stderr
    assert not (tmp_path / "repo/docs/plans/.review/chunk-snapshots/tasks/1002/patch.json").exists()


def test_capture_rejects_escaping_evidence_root_before_write(tmp_path: Path) -> None:
    result = run_fixture(tmp_path, "reject-escaping-evidence-root")
    assert result.returncode == 1
    assert "evidence root must be a clean relative path" in result.stderr
    assert not (tmp_path / "outside-evidence").exists()


def test_capture_rejects_dangling_evidence_directory_before_write(
    tmp_path: Path,
) -> None:
    result = run_fixture(tmp_path, "reject-dangling-evidence-directory")
    assert result.returncode == 1
    assert "evidence directory is unsafe" in result.stderr
    assert not (tmp_path / "missing-evidence-target").exists()


def test_baseline_replays_prior_task_record(tmp_path: Path) -> None:
    result = run_fixture(tmp_path, "prior-task-baseline")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["prior_task_digests"] == [report["expected_prior_digest"]]
    assert report["baseline_sha256"] == report["current_sha256"]


@pytest.mark.parametrize(
    ("variant", "message"),
    [
        ("missing", "task-patch file does not exist"),
        ("damaged", "task-patch record is invalid"),
        ("current-mismatch", "baseline path does not match prior tasks"),
    ],
)
def test_baseline_rejects_invalid_prior_task_record(
    tmp_path: Path, variant: str, message: str
) -> None:
    result = run_fixture(tmp_path, "prior-task-baseline", variant)
    assert result.returncode == 1
    assert message in result.stderr


def test_baseline_rejects_reordered_prior_task_records(tmp_path: Path) -> None:
    result = run_fixture(tmp_path, "prior-task-reordered")
    assert result.returncode == 1
    assert "prior task records are out of order" in result.stderr


def assert_fixture_ok(tmp_path: Path, case: str, *arguments: str) -> dict[str, object]:
    result = run_fixture(tmp_path, case, *arguments)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_two_chunks_reach_shipping_before_task_commits(tmp_path: Path) -> None:
    report = assert_fixture_ok(tmp_path, "two-chunks")
    assert report["task_trees"] == 4
    assert report["prerequisite_commits"] == 0
    assert report["shipping_ok"] is True


def test_shipping_rejects_incomplete_snapshot_prefix(tmp_path: Path) -> None:
    result = run_fixture(tmp_path, "shipping-incomplete")
    assert result.returncode == 1
    assert "complete approved task sequence" in result.stderr


def test_task_issues_close_only_after_atomic_commits(tmp_path: Path) -> None:
    report = assert_fixture_ok(tmp_path, "task-closure")
    assert report["before_commits"] == []
    assert report["after_commits"] == [1002, 1003, 1004, 1005]


@pytest.mark.parametrize("change", ["content", "mode", "deletion", "unreadable"])
def test_changed_or_unreadable_candidate_blocks(tmp_path: Path, change: str) -> None:
    result = run_fixture(tmp_path, "changed-candidate", change)
    assert result.returncode == 1
    assert "chunk" in result.stderr.lower()


@pytest.mark.parametrize(
    "damage",
    [
        "missing-snapshot",
        "missing-patch",
        "damaged-blob",
        "malformed-json",
        "invalid-schema",
        "wrong-digest",
    ],
)
def test_missing_or_damaged_evidence_blocks(tmp_path: Path, damage: str) -> None:
    result = run_fixture(tmp_path, "damaged", damage)
    assert result.returncode == 1
    assert "chunk" in result.stderr.lower()


@pytest.mark.parametrize("identity", ["plan", "parent", "task", "start"])
def test_changed_plan_issue_or_starting_commit_blocks(tmp_path: Path, identity: str) -> None:
    result = run_fixture(tmp_path, "changed-identity", identity)
    assert result.returncode == 1
    assert "identity" in result.stderr.lower()


def test_later_snapshot_parent_follows_approved_plan_order(tmp_path: Path) -> None:
    result = run_fixture(tmp_path, "changed-later-parent")
    assert result.returncode == 1
    assert "chunk" in result.stderr.lower() and "order" in result.stderr.lower()


@pytest.mark.parametrize("fault", ["reordered", "duplicated", "omitted", "changed"])
def test_reordered_duplicated_or_omitted_task_patches_block(tmp_path: Path, fault: str) -> None:
    result = run_fixture(tmp_path, "task-order", fault)
    assert result.returncode == 1
    assert "task" in result.stderr.lower()


def test_later_chunk_can_change_an_earlier_file(tmp_path: Path) -> None:
    report = assert_fixture_ok(tmp_path, "later-chunk")
    assert report["final_content"] == "C"
    assert report["unrelated_start_rejected"] is True


def test_temporary_index_reconstruction_preserves_active_state(tmp_path: Path) -> None:
    report = assert_fixture_ok(tmp_path, "temporary-index")
    assert report["reconstructed_tree"] == report["expected_tree"]
    assert report["active_index_before"] == report["active_index_after"]
    assert report["worktree_before"] == report["worktree_after"]
    assert report["failure_preserved_state"] is True


@pytest.mark.parametrize(
    "fault",
    [
        "absolute",
        "traversal",
        "outside-link",
        "mcp-config",
        "environment",
        "unrelated",
        "generated-evidence",
    ],
)
def test_unsafe_or_unrelated_paths_block_before_read(tmp_path: Path, fault: str) -> None:
    result = run_fixture(tmp_path, "unsafe", fault)
    assert result.returncode == 1
    assert "rejected before read" in result.stderr.lower()


def test_approved_ignored_instructions_keep_separate_identity(tmp_path: Path) -> None:
    report = assert_fixture_ok(tmp_path, "instructions")
    assert report["separate_identity"] is True
    assert report["substitutes_rejected"] is True
    assert report["backup_required"] is True


def test_provider_artifacts_and_posted_markers_belong_to_snapshot(
    tmp_path: Path,
) -> None:
    report = assert_fixture_ok(tmp_path, "provider-binding")
    assert report["valid_binding"] is True
    assert report["cross_snapshot_rejected"] is True
    assert report["changed_bytes_rejected"] is True
    assert report["missing_readback_rejected"] is True


@pytest.mark.parametrize("status", ["valid", "failed", "timed_out", "interrupted"])
def test_provider_unavailability_remains_advisory(tmp_path: Path, status: str) -> None:
    report = assert_fixture_ok(tmp_path, "provider-status", status)
    assert report["status"] == status
    assert report["advisory"] is (status != "valid")
    assert report["extra_provider"] is False


def test_authorized_critical_findings_still_block(tmp_path: Path) -> None:
    report = assert_fixture_ok(tmp_path, "critical-findings")
    assert report["authorized_confirmed_blocks"] is True
    assert report["current_rule_confirmed_blocks"] is True
    assert report["refuted_blocks"] is False
    assert report["unauthorized_blocks"] is False
    assert report["fresh_fix_passes"] is True
    assert report["fabricated_clear_rejected"] is True
    assert report["edited_result_rejected"] is True
    assert report["unsafe_command_rejected"] is True
    assert report["failed_checks_rejected"] is True
    assert report["invalid_scoped_review_rejected"] is True


def test_single_chunk_full_uses_final_reviews_without_chunk_calls(
    tmp_path: Path,
) -> None:
    report = assert_fixture_ok(tmp_path, "route-matrix")
    assert report == {
        "single_full_final_reviews": True,
        "single_full_chunk_marker": False,
        "multipart_chunk_marker": True,
        "bounded_chunk_marker": False,
        "direct_chunk_marker": False,
    }


def test_single_chunk_shipping_requires_no_chunk_marker(tmp_path: Path) -> None:
    report = assert_fixture_ok(tmp_path, "single-chunk-shipping")
    assert report == {"accepted": True, "marker_count": 0}


def test_validation_rejects_changed_head_before_and_after_commits(tmp_path: Path) -> None:
    report = assert_fixture_ok(tmp_path, "head-mismatch")
    assert report == {"shipping_rejected": True, "committed_rejected": True}


def test_instruction_backup_evidence_is_revalidated(tmp_path: Path) -> None:
    report = assert_fixture_ok(tmp_path, "backup-revalidation")
    assert report["valid"] is True
    assert report["rejected"] == [
        "commit",
        "parent",
        "paths",
        "bytes",
        "mode",
        "snapshot-log",
    ]


@pytest.mark.parametrize("change", ["content", "mode", "deletion", "unreadable", "extra"])
def test_validation_rejects_post_snapshot_candidate_changes(tmp_path: Path, change: str) -> None:
    result = run_fixture(tmp_path, "post-snapshot-candidate", change)
    assert result.returncode == 1
    assert "candidate" in result.stderr.lower() or "unrelated" in result.stderr.lower()


def test_two_chunk_chain_survives_process_interruption(tmp_path: Path) -> None:
    report = assert_fixture_ok(tmp_path, "interruption")
    assert report["reloaded"] is True
    assert report["final_tree_matches"] is True


def test_fix_requires_fresh_snapshot_checks_and_scoped_review(tmp_path: Path) -> None:
    report = assert_fixture_ok(tmp_path, "fresh-fix")
    assert report["old_evidence_rejected"] is True
    assert report["fresh_checks_required"] is True
    assert report["old_evidence_retained"] is True
    assert report["revised_tree_matches"] is True


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "extra",
        "omitted",
        "reordered",
        "changed",
        "wrong-parent",
        "extra-parent",
    ],
)
def test_shipping_commits_match_entire_reviewed_result(tmp_path: Path, fault: str) -> None:
    report = assert_fixture_ok(tmp_path, "shipping", fault)
    assert report["accepted"] is (fault == "none")
